"""
Request IDs, structured (JSON) logging and Prometheus metrics.

* Every request gets an X-Request-ID (taken from the caller if present,
  generated otherwise). It is echoed in the response and attached to every
  log line produced while handling the request.
* Logs are one JSON object per line when INSPECTOR_LOG_JSON=true, so a log
  shipper can index them. Photo bytes, notes and other customer content are
  never logged - only ids, codes and numbers.
* Metrics are exposed at /metrics (Prometheus text format). Restrict that
  path at the reverse proxy; it is unauthenticated on purpose so a scraper
  does not need an API key.
"""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# ------------------------------------------------------------------ logging --
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key in ("inspection_id", "job_id", "category", "verdict", "provider", "latency_ms", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(json_logs: bool, level: str = "INFO") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
        handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # uvicorn's access log duplicates our request log; keep its error log.
    logging.getLogger("uvicorn.access").disabled = True


# ------------------------------------------------------------------ metrics --
try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    HTTP_REQUESTS = Counter("inspector_http_requests_total", "HTTP requests", ["method", "path", "status"])
    HTTP_LATENCY = Histogram("inspector_http_request_seconds", "HTTP request latency", ["path"], buckets=(0.05, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120))
    INSPECTIONS = Counter("inspector_inspections_total", "Completed inspections", ["category", "verdict", "mode"])
    INSPECTION_LATENCY = Histogram("inspector_inspection_seconds", "Inspection pipeline latency", ["category"], buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 120, 300))
    PHOTOS = Counter("inspector_photos_total", "Photos processed", ["status"])
    FINDINGS = Counter("inspector_findings_total", "Findings emitted", ["kind", "code"])
    VLM_CALLS = Counter("inspector_vlm_calls_total", "VLM provider calls", ["provider", "task", "outcome"])
    VLM_LATENCY = Histogram("inspector_vlm_call_seconds", "VLM call latency", ["provider"], buckets=(0.5, 1, 2, 4, 8, 15, 30, 60, 120))
    VLM_TOKENS = Counter("inspector_vlm_tokens_total", "VLM tokens", ["provider", "kind"])
    RATE_LIMITED = Counter("inspector_rate_limited_total", "Requests rejected by the rate limiter", [])
    QUEUE_JOBS = Counter("inspector_queue_jobs_total", "Async queue jobs", ["status"])
    METRICS_AVAILABLE = True
except ImportError:  # pragma: no cover
    METRICS_AVAILABLE = False


def metrics_payload() -> tuple[bytes, str]:
    if not METRICS_AVAILABLE:
        return b"# prometheus_client not installed\n", "text/plain"
    return generate_latest(), CONTENT_TYPE_LATEST


def record_inspection(result, mode: str = "sync") -> None:
    if not METRICS_AVAILABLE:
        return
    INSPECTIONS.labels(result.category, result.verdict, mode).inc()
    INSPECTION_LATENCY.labels(result.category).observe(result.stats.get("latency_ms", 0) / 1000.0)
    for p in result.photos:
        PHOTOS.labels(p.status).inc()
        for f in p.findings:
            FINDINGS.labels(f.kind, f.code.split(":")[0]).inc()
    for f in result.job_findings:
        FINDINGS.labels(f.kind, f.code.split(":")[0]).inc()


def record_vlm_call(provider: str, task: str, outcome: str, latency_s: float | None = None, usage: dict | None = None) -> None:
    if not METRICS_AVAILABLE:
        return
    VLM_CALLS.labels(provider, task, outcome).inc()
    if latency_s is not None:
        VLM_LATENCY.labels(provider).observe(latency_s)
    for kind in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = (usage or {}).get(kind)
        if v:
            VLM_TOKENS.labels(provider, kind).inc(v)


# --------------------------------------------------------------- middleware --
class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            elapsed = time.perf_counter() - start
            path = request.scope.get("route").path if request.scope.get("route") else request.url.path
            if METRICS_AVAILABLE:
                HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
                HTTP_LATENCY.labels(path).observe(elapsed)
            logging.getLogger("inspector.http").info(
                "%s %s -> %s in %.0f ms", request.method, request.url.path, status, elapsed * 1000,
                extra={"event": "http_request", "latency_ms": round(elapsed * 1000, 1)},
            )
            request_id_var.reset(token)
