"""Delete stored photos past INSPECTOR_PHOTO_RETENTION_DAYS. Run daily from cron:
    0 4 * * * cd /app && python scripts/purge_photos.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402
from backend.photostore import PhotoStore  # noqa: E402

if not settings.photo_store_dir:
    print("photo store disabled (INSPECTOR_PHOTO_STORE_DIR empty)")
    raise SystemExit(0)
n = PhotoStore(settings.photo_store_dir, settings.photo_retention_days, settings.photo_store_policy).purge_expired()
print(f"purged {n} inspection folders")
