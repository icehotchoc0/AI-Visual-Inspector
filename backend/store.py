"""
Persistence on SQLAlchemy Core: SQLite by default (zero-ops, one file),
PostgreSQL for multi-instance production (INSPECTOR_DATABASE_URL).

Tables
  inspections   full result JSON per run + reviewer decision columns
  photo_hashes  perceptual hashes of accepted photos (cross-job reuse check)
  queue_jobs    async inspection queue (status, idempotency, callback)

Hash search: on PostgreSQL the hamming distance is computed in SQL with
bit_count(phash_int # :h) (PG14+); on SQLite it is a Python scan, fine up
to ~10^5 rows. Both go through the same HashHistory interface.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine

from backend.checks.fraud import HashHistory, HistoricalMatch, hamming
from backend.schemas import InspectionListItem, InspectionResult

metadata = MetaData()

inspections = Table(
    "inspections", metadata,
    Column("inspection_id", String(40), primary_key=True),
    Column("job_id", String(128), nullable=False, index=True),
    Column("category", String(64), nullable=False, index=True),
    Column("technician_id", String(128), nullable=True, index=True),
    Column("verdict", String(10), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("num_photos", Integer, nullable=False),
    Column("pipeline_version", String(20), nullable=True),
    Column("prompt_version", String(20), nullable=True),
    Column("result_json", Text, nullable=False),
    Column("photos_stored", Boolean, nullable=False, default=False),
    Column("review_verdict", String(10), nullable=True, index=True),
    Column("review_notes", Text, nullable=True),
    Column("reviewed_by", String(128), nullable=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=True),
)

photo_hashes = Table(
    "photo_hashes", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("inspection_id", String(40), nullable=False, index=True),
    Column("job_id", String(128), nullable=False, index=True),
    Column("photo_id", String(128), nullable=False),
    Column("phash", String(16), nullable=False),
    Column("phash_int", BigInteger, nullable=False, index=True),
    Column("sha256", String(64), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

queue_jobs = Table(
    "queue_jobs", metadata,
    Column("job_key", String(40), primary_key=True),
    Column("idempotency_key", String(200), nullable=True, unique=True),
    Column("status", String(12), nullable=False, index=True),  # queued | running | done | failed
    Column("inspection_id", String(40), nullable=True),
    Column("request_json", Text, nullable=False),
    Column("callback_url", String(2000), nullable=True),
    Column("error", Text, nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False, index=True),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
)


def _phash_to_int(hex16: str) -> int:
    """64-bit unsigned hex -> signed 64-bit int (BIGINT-safe)."""
    v = int(hex16, 16)
    return v - (1 << 64) if v >= (1 << 63) else v


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Store(HashHistory):
    def __init__(self, database_url: str):
        self.url = database_url
        kwargs: dict = {"future": True, "pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            path = database_url.replace("sqlite:///", "", 1)
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine: Engine = create_engine(database_url, **kwargs)
        self._lock = threading.RLock() if database_url.startswith("sqlite") else None
        metadata.create_all(self.engine)
        if database_url.startswith("sqlite"):
            with self.engine.begin() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))

    @classmethod
    def from_sqlite_path(cls, path: str | Path) -> "Store":
        return cls(f"sqlite:///{path}")

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def _begin(self):
        return self.engine.begin()

    # ---------------------------------------------------------- hash history --
    def find_similar(self, phash: str, max_distance: int, exclude_job_id: str) -> list[HistoricalMatch]:
        h_int = _phash_to_int(phash)
        with self.engine.connect() as conn:
            if self.is_postgres:
                rows = conn.execute(
                    text("SELECT photo_id, job_id, inspection_id, bit_count(phash_int # :h) AS d FROM photo_hashes WHERE job_id != :j AND bit_count(phash_int # :h) <= :d"),
                    {"h": h_int, "j": exclude_job_id, "d": max_distance},
                ).fetchall()
                return [HistoricalMatch(photo_id=r[0], job_id=r[1], inspection_id=r[2], distance=int(r[3])) for r in rows]
            rows = conn.execute(select(photo_hashes.c.photo_id, photo_hashes.c.job_id, photo_hashes.c.inspection_id, photo_hashes.c.phash).where(photo_hashes.c.job_id != exclude_job_id)).fetchall()
        out = []
        for photo_id, job_id, inspection_id, ph in rows:
            d = hamming(phash, ph)
            if d <= max_distance:
                out.append(HistoricalMatch(photo_id=photo_id, job_id=job_id, inspection_id=inspection_id, distance=d))
        return out

    # ------------------------------------------------------------ inspections --
    def save(self, result: InspectionResult, technician_id: str | None = None, photos_stored: bool = False) -> None:
        created = _aware(result.created_at)
        row = {
            "inspection_id": result.inspection_id, "job_id": result.job_id, "category": result.category,
            "technician_id": technician_id, "verdict": result.verdict, "created_at": created,
            "num_photos": len(result.photos), "pipeline_version": result.pipeline_version,
            "prompt_version": result.stats.get("prompt_version"), "result_json": result.model_dump_json(),
            "photos_stored": photos_stored,
        }
        hashes = [
            {"inspection_id": result.inspection_id, "job_id": result.job_id, "photo_id": p.photo_id, "phash": p.meta.perceptual_hash,
             "phash_int": _phash_to_int(p.meta.perceptual_hash), "sha256": p.meta.sha256, "created_at": created}
            for p in result.photos if p.status != "REJECTED"  # a black frame would match every other black frame
        ]
        with self._begin() as conn:
            conn.execute(inspections.delete().where(inspections.c.inspection_id == result.inspection_id))
            conn.execute(inspections.insert().values(**row))
            conn.execute(photo_hashes.delete().where(photo_hashes.c.inspection_id == result.inspection_id))
            if hashes:
                conn.execute(photo_hashes.insert(), hashes)

    def get(self, inspection_id: str) -> InspectionResult | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(inspections.c.result_json).where(inspections.c.inspection_id == inspection_id)).fetchone()
        return InspectionResult.model_validate_json(row[0]) if row else None

    def get_review(self, inspection_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(inspections.c.verdict, inspections.c.review_verdict, inspections.c.review_notes, inspections.c.reviewed_by, inspections.c.reviewed_at, inspections.c.photos_stored, inspections.c.technician_id)
                .where(inspections.c.inspection_id == inspection_id)
            ).fetchone()
        if not row:
            return None
        return {"verdict": row[0], "review_verdict": row[1], "review_notes": row[2], "reviewed_by": row[3], "reviewed_at": _aware(row[4]), "photos_stored": bool(row[5]), "technician_id": row[6]}

    def set_review(self, inspection_id: str, verdict: str, reviewer: str, notes: str | None) -> bool:
        with self._begin() as conn:
            res = conn.execute(
                update(inspections).where(inspections.c.inspection_id == inspection_id)
                .values(review_verdict=verdict, review_notes=notes, reviewed_by=reviewer, reviewed_at=_now())
            )
        return res.rowcount > 0

    def mark_photos_stored(self, inspection_id: str, stored: bool = True) -> None:
        with self._begin() as conn:
            conn.execute(update(inspections).where(inspections.c.inspection_id == inspection_id).values(photos_stored=stored))

    def list(self, limit: int = 50, job_id: str | None = None, category: str | None = None, verdict: str | None = None,
             technician_id: str | None = None, review_state: str | None = None) -> list[InspectionListItem]:
        q = select(inspections.c.inspection_id, inspections.c.job_id, inspections.c.category, inspections.c.verdict, inspections.c.created_at,
                   inspections.c.num_photos, inspections.c.review_verdict, inspections.c.technician_id)
        if job_id:
            q = q.where(inspections.c.job_id == job_id)
        if category:
            q = q.where(inspections.c.category == category)
        if verdict:
            q = q.where(inspections.c.verdict == verdict)
        if technician_id:
            q = q.where(inspections.c.technician_id == technician_id)
        if review_state == "pending":
            q = q.where(inspections.c.review_verdict.is_(None))
        elif review_state == "reviewed":
            q = q.where(inspections.c.review_verdict.is_not(None))
        q = q.order_by(inspections.c.created_at.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(q).fetchall()
        return [
            InspectionListItem(inspection_id=r[0], job_id=r[1], category=r[2], verdict=r[3], created_at=_aware(r[4]), num_photos=r[5], review_verdict=r[6], technician_id=r[7])
            for r in rows
        ]

    def list_reviewed(self, limit: int = 1000, category: str | None = None) -> list[dict]:
        q = select(inspections.c.inspection_id, inspections.c.result_json, inspections.c.review_verdict, inspections.c.review_notes, inspections.c.photos_stored).where(inspections.c.review_verdict.is_not(None))
        if category:
            q = q.where(inspections.c.category == category)
        q = q.order_by(inspections.c.reviewed_at.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(q).fetchall()
        return [{"inspection_id": r[0], "result": InspectionResult.model_validate_json(r[1]), "review_verdict": r[2], "review_notes": r[3], "photos_stored": bool(r[4])} for r in rows]

    def technician_stats(self, days: int = 30, limit: int = 200) -> list[dict]:
        since = _now() - timedelta(days=days)
        q = (
            select(inspections.c.technician_id, inspections.c.verdict, func.count())
            .where(inspections.c.technician_id.is_not(None), inspections.c.created_at >= since)
            .group_by(inspections.c.technician_id, inspections.c.verdict)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).fetchall()
        agg: dict[str, dict] = {}
        for tech, verdict, n in rows:
            a = agg.setdefault(tech, {"technician_id": tech, "total": 0, "PASS": 0, "REVIEW": 0, "FAIL": 0})
            a[verdict] = a.get(verdict, 0) + n
            a["total"] += n
        out = sorted(agg.values(), key=lambda a: (-a["FAIL"], -a["total"]))[:limit]
        for a in out:
            a["fail_rate"] = round(a["FAIL"] / a["total"], 3) if a["total"] else 0.0
            a["pass_rate"] = round(a["PASS"] / a["total"], 3) if a["total"] else 0.0
        return out

    def stats(self) -> dict:
        with self.engine.connect() as conn:
            total = conn.execute(select(func.count()).select_from(inspections)).scalar_one()
            hashes = conn.execute(select(func.count()).select_from(photo_hashes)).scalar_one()
            by_verdict = {r[0]: r[1] for r in conn.execute(select(inspections.c.verdict, func.count()).group_by(inspections.c.verdict))}
            pending = conn.execute(select(func.count()).select_from(inspections).where(inspections.c.verdict == "REVIEW", inspections.c.review_verdict.is_(None))).scalar_one()
            queued = conn.execute(select(func.count()).select_from(queue_jobs).where(queue_jobs.c.status.in_(["queued", "running"]))).scalar_one()
        return {"inspections": total, "photo_hashes": hashes, "by_verdict": by_verdict, "review_pending": pending, "queue_active": queued, "backend": self.engine.dialect.name}

    # ------------------------------------------------------------------ queue --
    def enqueue(self, request_json: str, callback_url: str | None, idempotency_key: str | None) -> tuple[str, bool]:
        """Returns (job_key, created). With an idempotency key an existing job is returned instead of a new one."""
        if idempotency_key:
            with self.engine.connect() as conn:
                row = conn.execute(select(queue_jobs.c.job_key).where(queue_jobs.c.idempotency_key == idempotency_key)).fetchone()
            if row:
                return row[0], False
        key = f"q_{uuid.uuid4().hex[:16]}"
        with self._begin() as conn:
            conn.execute(queue_jobs.insert().values(job_key=key, idempotency_key=idempotency_key, status="queued", request_json=request_json, callback_url=callback_url, attempts=0, created_at=_now()))
        return key, True

    def claim_next(self) -> dict | None:
        """Atomically move the oldest queued job to running and return it."""
        with self._begin() as conn:
            row = conn.execute(select(queue_jobs.c.job_key).where(queue_jobs.c.status == "queued").order_by(queue_jobs.c.created_at).limit(1)).fetchone()
            if not row:
                return None
            res = conn.execute(update(queue_jobs).where(queue_jobs.c.job_key == row[0], queue_jobs.c.status == "queued").values(status="running", started_at=_now(), attempts=queue_jobs.c.attempts + 1))
            if res.rowcount != 1:
                return None
            job = conn.execute(select(queue_jobs).where(queue_jobs.c.job_key == row[0])).mappings().fetchone()
        return dict(job)

    def finish_job(self, job_key: str, inspection_id: str | None, error: str | None) -> None:
        with self._begin() as conn:
            conn.execute(update(queue_jobs).where(queue_jobs.c.job_key == job_key).values(status="failed" if error else "done", inspection_id=inspection_id, error=error, finished_at=_now()))

    def get_job(self, job_key: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(queue_jobs).where(queue_jobs.c.job_key == job_key)).mappings().fetchone()
        return dict(row) if row else None

    def requeue_stale_running(self, older_than_minutes: int = 30) -> int:
        """Jobs left 'running' by a crashed worker go back to the queue."""
        cutoff = _now() - timedelta(minutes=older_than_minutes)
        with self._begin() as conn:
            res = conn.execute(update(queue_jobs).where(queue_jobs.c.status == "running", queue_jobs.c.started_at < cutoff).values(status="queued"))
        return res.rowcount

    def close(self) -> None:
        self.engine.dispose()


class SQLiteStore(Store):
    """Backwards-compatible constructor taking a file path."""

    def __init__(self, path: str | Path):
        super().__init__(f"sqlite:///{path}")
