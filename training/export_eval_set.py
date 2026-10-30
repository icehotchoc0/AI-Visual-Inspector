"""
Turn reviewed inspections into an eval set (docs/DATA_SPEC.md §1).

    python training/export_eval_set.py --category aircon_install [--out backend/data/datasets/eval]

For every inspection with a reviewer verdict it writes one line to
<out>/<category>/jobs.jsonl and copies retained photos (when the photo
store kept them) to <out>/<category>/photos/<job_id>/<photo_id>.<ext>.
Jobs without retained photos are listed but skipped with a reason, since
the eval runner needs the images.
Labels: expected_verdict = reviewer verdict; shot_type = resolved shot;
defects = VLM findings the reviewer did not contradict are NOT assumed
correct - defect codes are exported only from review notes written as
`#code` tags (e.g. "#exposed_wiring 확인"), so the label stays human-made.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402
from backend.photostore import PhotoStore  # noqa: E402
from backend.store import Store  # noqa: E402

_TAG = re.compile(r"#([a-z][a-z0-9_]+)")


def export(store: Store, photo_store: PhotoStore | None, category: str, out: Path, limit: int = 1000) -> dict:
    target = out / category
    (target / "photos").mkdir(parents=True, exist_ok=True)
    lines, skipped = [], []
    for item in store.list_reviewed(limit=limit, category=category):
        res = item["result"]
        tags = set(_TAG.findall(item["review_notes"] or ""))
        photos = []
        copied = 0
        for p in res.photos:
            entry = {"photo_id": p.photo_id, "shot_type": p.resolved_shot_type or "other", "phase": p.phase,
                     "defects": [{"code": c, "severity": "medium"} for c in tags if any(f.code == c for f in p.findings)]}
            if p.status == "REJECTED":
                q = [f.code for f in p.findings if f.kind == "quality" and f.severity == "high"]
                if q:
                    entry["quality_issue"] = q[0]
            photos.append(entry)
            src = photo_store.find(res.inspection_id, p.photo_id) if (photo_store and item["photos_stored"]) else None
            if src:
                dst = target / "photos" / res.job_id / src.name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
        if copied == 0:
            skipped.append({"inspection_id": res.inspection_id, "reason": "no retained photos"})
            continue
        fraud = sorted({f.code for p in res.photos for f in p.findings if f.kind == "fraud"} | {f.code for f in res.job_findings if f.kind == "fraud"})
        lines.append({"job_id": res.job_id, "category": category, "expected_verdict": item["review_verdict"], "auto_verdict": res.verdict,
                      "photos": photos, "fraud": fraud, "notes": item["review_notes"], "inspection_id": res.inspection_id})
    (target / "jobs.jsonl").write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + ("\n" if lines else ""), encoding="utf-8")
    return {"exported": len(lines), "skipped": skipped, "path": str(target / "jobs.jsonl")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", required=True)
    ap.add_argument("--out", default=str(settings.dataset_root_path / "eval"))
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()
    store = Store(settings.effective_database_url)
    ps = PhotoStore(settings.photo_store_dir, settings.photo_retention_days, settings.photo_store_policy) if settings.photo_store_dir else None
    report = export(store, ps, args.category, Path(args.out), args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if ps is None:
        print("[export] photo retention is off - enable INSPECTOR_PHOTO_STORE_DIR to export images", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
