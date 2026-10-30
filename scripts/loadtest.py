"""
Simple load test against a running service (no extra dependencies beyond httpx).

    python scripts/loadtest.py --url http://localhost:8000 --key demo-key --concurrency 8 --jobs 40 [--mode async] [--no-vlm]

Generates synthetic 4-photo plumbing jobs, fires them with N concurrent
workers and prints latency percentiles, status codes and verdict counts.
With --no-vlm it exercises the deterministic path only (no model cost);
without it every job costs real VLM tokens - keep --jobs small.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from PIL import Image, ImageDraw

SHOTS = ["site_before", "pipe_connection", "leak_test", "site_after"]


def synthetic_photo(seed: int) -> str:
    rng = random.Random(seed)
    img = Image.new("RGB", (800, 600), (rng.randint(40, 200), rng.randint(40, 200), 120))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rng.randint(0, 700), rng.randint(0, 500)
        d.rectangle([x0, y0, x0 + rng.randint(40, 250), y0 + rng.randint(40, 250)], fill=tuple(rng.randint(0, 255) for _ in range(3)), outline=(0, 0, 0), width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


RUN_SEED = int(time.time()) % 100000  # new photos per run, otherwise the cross-job reuse check fails every repeat run


def one_job(client: httpx.Client, base: str, i: int, mode: str, run_vlm: bool) -> dict:
    body = {"job": {"job_id": f"load-{RUN_SEED}-{i}", "category": "plumbing", "technician_id": f"T-{i % 5}"},
            "photos": [{"photo_id": s, "image_base64": synthetic_photo(RUN_SEED * 1000 + i * 10 + k), "declared_shot_type": s} for k, s in enumerate(SHOTS)],
            "options": {"run_vlm": run_vlm, "run_fraud_history": True}}
    t0 = time.perf_counter()
    r = client.post(f"{base}/v1/inspections", params={"mode": mode}, json=body)
    out = {"status": r.status_code, "latency": time.perf_counter() - t0, "verdict": None}
    if r.status_code == 200:
        out["verdict"] = r.json().get("verdict")
    elif r.status_code == 202:
        key = r.json()["job_key"]
        for _ in range(600):
            j = client.get(f"{base}/v1/jobs/{key}", params={"include_result": "false"}).json()
            if j["status"] in ("done", "failed"):
                out["verdict"] = j["status"]
                break
            time.sleep(0.5)
        out["latency"] = time.perf_counter() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--key", default="")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--jobs", type=int, default=20)
    ap.add_argument("--mode", choices=["sync", "async"], default="sync")
    ap.add_argument("--no-vlm", action="store_true")
    args = ap.parse_args()
    headers = {"X-API-Key": args.key} if args.key else {}
    results = []
    t0 = time.perf_counter()
    with httpx.Client(headers=headers, timeout=600) as client, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one_job, client, args.url, i, args.mode, not args.no_vlm) for i in range(args.jobs)]
        for f in as_completed(futures):
            results.append(f.result())
            print(".", end="", flush=True)
    total = time.perf_counter() - t0
    lat = sorted(r["latency"] for r in results)
    pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]  # noqa: E731
    statuses = {}
    verdicts = {}
    for r in results:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    print("\n" + json.dumps({
        "jobs": len(results), "concurrency": args.concurrency, "mode": args.mode, "vlm": not args.no_vlm,
        "wall_s": round(total, 1), "throughput_jobs_per_min": round(len(results) / total * 60, 1),
        "latency_s": {"p50": round(pct(0.5), 2), "p90": round(pct(0.9), 2), "p99": round(pct(0.99), 2), "max": round(lat[-1], 2), "mean": round(statistics.mean(lat), 2)},
        "status_codes": statuses, "verdicts": verdicts,
    }, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] in (200, 202) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
