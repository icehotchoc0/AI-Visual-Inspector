"""
Async inspection queue: DB-backed jobs + in-process worker threads.

    POST /v1/inspections?mode=async  ->  202 {job_key, status: queued}
    GET  /v1/jobs/{job_key}          ->  queued | running | done (+result) | failed (+error)

Jobs live in the queue_jobs table (see backend/store.py), so they survive
a restart: a worker crash leaves a job "running" and the next startup
requeues anything stale. Workers are plain threads polling the table;
for several API replicas or very high volume, run the same worker loop
in separate processes against PostgreSQL - claim_next() is atomic - or
replace QueueWorker with a Celery/SQS consumer that calls the same
`process_job()`.

Callbacks: when the request carries options.callback_url the result is
POSTed there as JSON with an HMAC-SHA256 signature
(X-Inspector-Signature: sha256=<hex> over the raw body, key =
INSPECTOR_WEBHOOK_SECRET). Three attempts with backoff; failures are
logged and the job still counts as done (the result stays retrievable).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Callable

from backend.observability import METRICS_AVAILABLE, QUEUE_JOBS, record_inspection, request_id_var
from backend.pipeline import PipelineError
from backend.schemas import InspectionRequest

log = logging.getLogger(__name__)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(sign(secret, body), header)


def default_sender(timeout_s: float = 10.0) -> Callable[[str, bytes, dict], int]:
    import httpx

    def _send(url: str, body: bytes, headers: dict) -> int:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            return client.post(url, content=body, headers=headers).status_code

    return _send


def deliver_callback(url: str, payload: dict, secret: str, sender: Callable[[str, bytes, dict], int], attempts: int = 3) -> bool:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Inspector-Signature": sign(secret, body) if secret else "", "X-Inspector-Event": "inspection.completed"}
    if not secret:
        headers.pop("X-Inspector-Signature")
    delay = 1.0
    for i in range(attempts):
        try:
            status = sender(url, body, headers)
            if 200 <= status < 300:
                return True
            log.warning("callback %s returned %s (attempt %d/%d)", url, status, i + 1, attempts)
        except Exception as exc:  # noqa: BLE001
            log.warning("callback %s failed: %s (attempt %d/%d)", url, exc, i + 1, attempts)
        time.sleep(delay)
        delay *= 2
    return False


def process_job(store, pipeline, job: dict, *, secret: str = "", sender: Callable | None = None) -> dict:
    """Run one claimed job to completion. Returns {"inspection_id" | "error"}."""
    token = request_id_var.set(job["job_key"])
    try:
        try:
            request = InspectionRequest.model_validate_json(job["request_json"])
            result = pipeline.run(request)
        except PipelineError as exc:
            store.finish_job(job["job_key"], None, f"invalid request: {exc}")
            if METRICS_AVAILABLE:
                QUEUE_JOBS.labels("failed").inc()
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("queue job %s crashed", job["job_key"])
            store.finish_job(job["job_key"], None, f"{type(exc).__name__}: {exc}")
            if METRICS_AVAILABLE:
                QUEUE_JOBS.labels("failed").inc()
            return {"error": str(exc)}
        store.finish_job(job["job_key"], result.inspection_id, None)
        record_inspection(result, mode="async")
        if METRICS_AVAILABLE:
            QUEUE_JOBS.labels("done").inc()
        log.info("async inspection done", extra={"event": "inspection", "inspection_id": result.inspection_id, "job_id": result.job_id, "category": result.category, "verdict": result.verdict})
        if job.get("callback_url"):
            payload = {"job_key": job["job_key"], "status": "done", "inspection_id": result.inspection_id, "job_id": result.job_id, "verdict": result.verdict, "summary": result.summary, "result": json.loads(result.model_dump_json())}
            ok = deliver_callback(job["callback_url"], payload, secret, sender or default_sender())
            if METRICS_AVAILABLE:
                QUEUE_JOBS.labels("callback_ok" if ok else "callback_failed").inc()
        return {"inspection_id": result.inspection_id}
    finally:
        request_id_var.reset(token)


class QueueWorker:
    def __init__(self, store, pipeline, *, workers: int = 2, poll_interval_s: float = 0.5, secret: str = "", sender: Callable | None = None):
        self.store, self.pipeline = store, pipeline
        self.workers, self.poll_interval_s, self.secret, self.sender = workers, poll_interval_s, secret, sender
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def process_one(self) -> bool:
        job = self.store.claim_next()
        if job is None:
            return False
        process_job(self.store, self.pipeline, job, secret=self.secret, sender=self.sender)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.process_one():
                    self._stop.wait(self.poll_interval_s)
            except Exception:  # noqa: BLE001
                log.exception("queue worker loop error")
                self._stop.wait(self.poll_interval_s)

    def start(self) -> None:
        requeued = self.store.requeue_stale_running()
        if requeued:
            log.warning("requeued %d stale running jobs", requeued)
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"inspector-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("queue workers started: %d", self.workers)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
