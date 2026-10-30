"""
Per-caller token-bucket rate limit (in-process).

Keyed by API key when auth is on, by client IP otherwise. Good for a single
instance; when running several replicas put the limit at the gateway (or
back this with Redis) - the dependency interface stays the same.
"""
from __future__ import annotations

import threading
import time

from fastapi import Depends, HTTPException, Request

from backend.auth import require_api_key
from backend.config import settings
from backend.observability import METRICS_AVAILABLE, RATE_LIMITED


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int | None = None):
        self.rate = rate_per_minute / 60.0
        self.capacity = burst or max(1, rate_per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> tuple[bool, float]:
        """Returns (allowed, seconds_until_next_token)."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1.0 - tokens) / self.rate if self.rate else 60.0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_bucket: TokenBucket | None = None


def get_bucket() -> TokenBucket | None:
    global _bucket
    if settings.rate_limit_per_minute <= 0:
        return None
    if _bucket is None or _bucket.capacity != max(1, settings.rate_limit_per_minute):
        _bucket = TokenBucket(settings.rate_limit_per_minute)
    return _bucket


async def rate_limited(request: Request, api_key: str | None = Depends(require_api_key)) -> None:
    bucket = get_bucket()
    if bucket is None:
        return
    key = api_key or (request.client.host if request.client else "anonymous")
    allowed, wait = bucket.try_acquire(key)
    if not allowed:
        if METRICS_AVAILABLE:
            RATE_LIMITED.inc()
        raise HTTPException(status_code=429, detail="rate limit exceeded", headers={"Retry-After": str(max(1, int(wait + 0.999)))})
