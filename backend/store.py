"""
Persistence: SQLite by default (zero-ops), one file under backend/data.

Two tables:
  inspections  - full result JSON per run (audit trail, GET /v1/inspections/{id})
  photo_hashes - perceptual hashes of every accepted photo, used by the
                 fraud check to catch photos re-used across jobs.

Hamming search is a linear scan in Python, which is fine up to ~10^5
photos. Beyond that, move to Postgres and index the hash with a bit-string
BK-tree or pg_similarity - the HashHistory interface stays the same.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from backend.checks.fraud import HashHistory, HistoricalMatch, hamming
from backend.schemas import InspectionListItem, InspectionResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    category      TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    num_photos    INTEGER NOT NULL,
    result_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspections_job ON inspections(job_id);
CREATE INDEX IF NOT EXISTS idx_inspections_created ON inspections(created_at);

CREATE TABLE IF NOT EXISTS photo_hashes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    photo_id      TEXT NOT NULL,
    phash         TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hashes_sha ON photo_hashes(sha256);
CREATE INDEX IF NOT EXISTS idx_hashes_job ON photo_hashes(job_id);
"""


class SQLiteStore(HashHistory):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # -- HashHistory -----------------------------------------------------------
    def find_similar(self, phash: str, max_distance: int, exclude_job_id: str) -> list[HistoricalMatch]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT photo_id, job_id, inspection_id, phash FROM photo_hashes WHERE job_id != ?",
                (exclude_job_id,),
            ).fetchall()
        matches = []
        for row in rows:
            d = hamming(phash, row["phash"])
            if d <= max_distance:
                matches.append(HistoricalMatch(photo_id=row["photo_id"], job_id=row["job_id"], inspection_id=row["inspection_id"], distance=d))
        return matches

    # -- inspections -------------------------------------------------------------
    def save(self, result: InspectionResult) -> None:
        created = result.created_at.astimezone(timezone.utc).isoformat() if result.created_at.tzinfo else result.created_at.isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?,?)",
                (result.inspection_id, result.job_id, result.category, result.verdict, created, len(result.photos), result.model_dump_json()),
            )
            # Only photos that passed quality are useful reference hashes; a
            # black frame would match every other black frame.
            self._conn.executemany(
                "INSERT INTO photo_hashes (inspection_id, job_id, photo_id, phash, sha256, created_at) VALUES (?,?,?,?,?,?)",
                [
                    (result.inspection_id, result.job_id, p.photo_id, p.meta.perceptual_hash, p.meta.sha256, created)
                    for p in result.photos
                    if p.status != "REJECTED"
                ],
            )

    def get(self, inspection_id: str) -> InspectionResult | None:
        with self._lock:
            row = self._conn.execute("SELECT result_json FROM inspections WHERE inspection_id = ?", (inspection_id,)).fetchone()
        return InspectionResult.model_validate_json(row["result_json"]) if row else None

    def list(self, limit: int = 50, job_id: str | None = None, category: str | None = None, verdict: str | None = None) -> list[InspectionListItem]:
        clauses, params = [], []
        if job_id:
            clauses.append("job_id = ?"); params.append(job_id)
        if category:
            clauses.append("category = ?"); params.append(category)
        if verdict:
            clauses.append("verdict = ?"); params.append(verdict)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT inspection_id, job_id, category, verdict, created_at, num_photos FROM inspections {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [
            InspectionListItem(
                inspection_id=r["inspection_id"], job_id=r["job_id"], category=r["category"], verdict=r["verdict"],
                created_at=datetime.fromisoformat(r["created_at"]), num_photos=r["num_photos"],
            )
            for r in rows
        ]

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM inspections").fetchone()[0]
            hashes = self._conn.execute("SELECT COUNT(*) FROM photo_hashes").fetchone()[0]
            by_verdict = {r[0]: r[1] for r in self._conn.execute("SELECT verdict, COUNT(*) FROM inspections GROUP BY verdict")}
        return {"inspections": total, "photo_hashes": hashes, "by_verdict": by_verdict}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
