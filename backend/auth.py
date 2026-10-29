"""
API-key authentication for service-to-service calls.

Callers send `X-API-Key: <key>`. Accepted keys come from INSPECTOR_API_KEYS
(comma-separated). When the list is empty the check is disabled - that is
only acceptable for local development, so the app logs a loud warning at
startup (see backend/app.py).
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

from backend.config import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _constant_time_in(candidate: str, accepted: set[str]) -> bool:
    ok = False
    for key in accepted:
        ok |= hmac.compare_digest(candidate.encode(), key.encode())
    return ok


async def require_api_key(request: Request, api_key: str | None = Depends(_api_key_header)) -> str | None:
    accepted = settings.api_key_set
    if not accepted:
        return None  # auth disabled (dev)
    if not api_key or not _constant_time_in(api_key, accepted):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return api_key
