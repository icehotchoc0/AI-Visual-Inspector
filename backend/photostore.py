"""
Optional photo retention for the review workflow.

By default the service keeps no photo bytes (only hashes and findings).
Reviewers cannot look at a REVIEW case without the photos, so operators
can enable retention:

    INSPECTOR_PHOTO_STORE_DIR=/data/photos
    INSPECTOR_PHOTO_STORE_POLICY=review_only   # review_only | all | off
    INSPECTOR_PHOTO_RETENTION_DAYS=90

review_only keeps photos for REVIEW and FAIL verdicts (the ones a human
looks at or a technician may dispute); PASS photos are discarded. Expired
folders are purged at startup and by scripts/purge_photos.py (cron).
Files are served only through the authenticated API, never from a public
directory.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe(name: str) -> str:
    return _SAFE.sub("_", name)[:128] or "x"


class PhotoStore:
    def __init__(self, root: str | Path, retention_days: int = 90, policy: str = "review_only"):
        self.root = Path(root)
        self.retention_days = retention_days
        self.policy = policy
        self.root.mkdir(parents=True, exist_ok=True)

    def should_store(self, verdict: str) -> bool:
        if self.policy == "all":
            return True
        if self.policy == "review_only":
            return verdict in ("REVIEW", "FAIL")
        return False

    def _dir(self, inspection_id: str, created: datetime | None = None) -> Path:
        created = created or datetime.now(timezone.utc)
        return self.root / created.strftime("%Y-%m") / _safe(inspection_id)

    def save(self, inspection_id: str, photo_id: str, data: bytes, ext: str = "jpg", created: datetime | None = None) -> Path:
        d = self._dir(inspection_id, created)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_safe(photo_id)}.{_safe(ext).lower()}"
        path.write_bytes(data)
        return path

    def find(self, inspection_id: str, photo_id: str) -> Path | None:
        for month_dir in self.root.iterdir() if self.root.exists() else []:
            d = month_dir / _safe(inspection_id)
            if d.is_dir():
                for p in d.glob(f"{_safe(photo_id)}.*"):
                    return p
        return None

    def delete(self, inspection_id: str) -> bool:
        removed = False
        for month_dir in self.root.iterdir() if self.root.exists() else []:
            d = month_dir / _safe(inspection_id)
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed = True
        return removed

    def purge_expired(self, now: float | None = None) -> int:
        """Delete inspection folders whose newest file is older than the retention window."""
        now = now or time.time()
        cutoff = now - self.retention_days * 86400
        removed = 0
        if not self.root.exists():
            return 0
        for month_dir in list(self.root.iterdir()):
            if not month_dir.is_dir():
                continue
            for d in list(month_dir.iterdir()):
                if not d.is_dir():
                    continue
                newest = max((p.stat().st_mtime for p in d.iterdir()), default=0)
                if newest < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            if not any(month_dir.iterdir()):
                month_dir.rmdir()
        if removed:
            log.info("purged %d expired photo folders (retention %d days)", removed, self.retention_days)
        return removed
