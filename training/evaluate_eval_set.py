"""
Measure the pipeline against a labelled eval set (docs/DATA_SPEC.md §1).

    python training/evaluate_eval_set.py --category aircon_install [--no-vlm] [--limit 20]

Runs each job through the real pipeline (real VLM providers unless --no-vlm),
and reports:
  * verdict accuracy + confusion (expected vs predicted PASS/REVIEW/FAIL)
  * shot-type classification accuracy (VLM vs label), excluding rejected photos
  * defect-code precision/recall (photo level, confidence-gated like the verdict)
  * quality-issue recall, fraud-code recall
Writes a JSON report next to jobs.jsonl so runs can be compared over time
(prompt or threshold changes). This spends real API tokens when the VLM is on.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.categories import load_registry  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.pipeline import InspectionPipeline  # noqa: E402
from backend.schemas import InspectionOptions, InspectionRequest, JobContext, PhotoInput  # noqa: E402
from backend.vlm import build_router  # noqa: E402


def load_jobs(path: Path, limit: int | None) -> list[dict]:
    jobs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return jobs[:limit] if limit else jobs


def evaluate(pipeline: InspectionPipeline, eval_dir: Path, jobs: list[dict], min_conf: float, run_vlm: bool) -> dict:
    verdict_conf: Counter = Counter()
    shot_total = shot_correct = 0
    defect_tp: Counter = Counter(); defect_fp: Counter = Counter(); defect_fn: Counter = Counter()
    quality_total = quality_hit = 0
    fraud_total = fraud_hit = 0
    per_job = []

    for job in jobs:
        photos_dir = eval_dir / "photos" / job["job_id"]
        images: dict[str, bytes] = {}
        inputs = []
        for p in job["photos"]:
            candidates = list(photos_dir.glob(f"{p['photo_id']}.*"))
            if not candidates:
                print(f"[eval] missing file for {job['job_id']}/{p['photo_id']}", file=sys.stderr)
                continue
            images[p["photo_id"]] = candidates[0].read_bytes()
            inputs.append(PhotoInput(photo_id=p["photo_id"], declared_shot_type=p.get("declared_shot_type") or p.get("shot_type") if p.get("shot_type") != "other" else None, phase=p.get("phase")))
        if not inputs:
            continue
        req = InspectionRequest(job=JobContext(job_id=job["job_id"], category=job["category"], notes=job.get("notes")), photos=inputs,
                                options=InspectionOptions(run_vlm=run_vlm, run_fraud_history=False))
        t0 = time.perf_counter()
        res = pipeline.run(req, images)
        verdict_conf[(job.get("expected_verdict", "?"), res.verdict)] += 1

        by_id = {p.photo_id: p for p in res.photos}
        for p in job["photos"]:
            r = by_id.get(p["photo_id"])
            if r is None:
                continue
            if p.get("quality_issue"):
                quality_total += 1
                quality_hit += int(any(f.code == p["quality_issue"] for f in r.findings))
            if r.status == "REJECTED":
                continue
            if r.analysis.provider not in (None, "none"):
                shot_total += 1
                predicted = r.analysis.shot_type if (r.analysis.shot_type and r.analysis.shot_confidence >= 0.5) else "other"
                shot_correct += int(predicted == (p.get("shot_type") or "other"))
            expected = {d["code"] for d in p.get("defects", [])}
            predicted_codes = {f.code for f in r.findings if f.kind == "defect" and f.confidence >= min_conf}
            for c in expected & predicted_codes:
                defect_tp[c] += 1
            for c in predicted_codes - expected:
                defect_fp[c] += 1
            for c in expected - predicted_codes:
                defect_fn[c] += 1
        for code in job.get("fraud", []):
            fraud_total += 1
            fraud_hit += int(any(f.code == code for f in res.job_findings) or any(f.code == code for p in res.photos for f in p.findings))
        per_job.append({"job_id": job["job_id"], "expected": job.get("expected_verdict"), "predicted": res.verdict, "summary": res.summary, "latency_ms": round((time.perf_counter() - t0) * 1000)})
        print(f"[eval] {job['job_id']}: expected={job.get('expected_verdict')} predicted={res.verdict} ({res.summary})")

    total = sum(verdict_conf.values())
    correct = sum(v for (e, p), v in verdict_conf.items() if e == p)
    codes = set(defect_tp) | set(defect_fp) | set(defect_fn)
    defects = {}
    for c in sorted(codes):
        tp, fp, fn = defect_tp[c], defect_fp[c], defect_fn[c]
        defects[c] = {"tp": tp, "fp": fp, "fn": fn,
                      "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                      "recall": round(tp / (tp + fn), 3) if tp + fn else None}
    return {
        "jobs": total,
        "verdict_accuracy": round(correct / total, 3) if total else None,
        "verdict_confusion": {f"{e}->{p}": v for (e, p), v in sorted(verdict_conf.items())},
        "shot_accuracy": round(shot_correct / shot_total, 3) if shot_total else None,
        "shot_evaluated": shot_total,
        "defects": defects,
        "quality_recall": round(quality_hit / quality_total, 3) if quality_total else None,
        "fraud_recall": round(fraud_hit / fraud_total, 3) if fraud_total else None,
        "per_job": per_job,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--category", required=True)
    ap.add_argument("--dataset-root", default=str(settings.dataset_root_path))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-vlm", action="store_true", help="deterministic checks only (free, fast)")
    args = ap.parse_args()

    eval_dir = Path(args.dataset_root) / "eval" / args.category
    jobs_path = eval_dir / "jobs.jsonl"
    if not jobs_path.exists():
        print(f"[eval] no eval set at {jobs_path} - see docs/DATA_SPEC.md", file=sys.stderr)
        return 2
    registry = load_registry(settings.categories_path)
    vlm = None if args.no_vlm else build_router(settings)
    pipeline = InspectionPipeline(registry, settings, vlm=vlm, store=None)
    jobs = load_jobs(jobs_path, args.limit)
    report = evaluate(pipeline, eval_dir, jobs, registry.get(args.category).verdict.min_defect_confidence, run_vlm=not args.no_vlm)
    report["category"] = args.category
    report["vlm_providers"] = vlm.names if vlm else []
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = eval_dir / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "per_job"}, ensure_ascii=False, indent=2))
    print(f"[eval] report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
