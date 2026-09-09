"""
Print a Markdown table from training/results/metrics.json - paste straight
into README.md's results section. Does not retrain anything.

Usage:
    python training/evaluate.py
"""
import json
from pathlib import Path

METRICS_PATH = Path(__file__).resolve().parent / "results" / "metrics.json"


def main() -> None:
    if not METRICS_PATH.exists():
        print("No metrics yet - run training/train_patchcore.py and/or train_efficientad.py first.")
        return

    rows = json.loads(METRICS_PATH.read_text())
    rows.sort(key=lambda r: (r["category"], r["model_backend"]))

    print("| Category | Backend | Image AUROC | Pixel AUROC |")
    print("|---|---|---|---|")
    for r in rows:
        img = f"{r['image_auroc']:.3f}" if r.get("image_auroc") is not None else "-"
        pix = f"{r['pixel_auroc']:.3f}" if r.get("pixel_auroc") is not None else "-"
        print(f"| {r['category']} | {r['model_backend']} | {img} | {pix} |")


if __name__ == "__main__":
    main()
