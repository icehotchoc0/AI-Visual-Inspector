"""
Train the optional anomaly-detection plugin on OUR OWN normal photos.

    python training/train_folder.py --model-key aircon_outdoor_unit --backend patchcore

Reads the folder layout described in docs/DATA_SPEC.md:

    <INSPECTOR_DATASET_ROOT>/<model_key>/
        train/good/*.jpg          normal (defect-free) photos of ONE fixed view
        test/good/*.jpg           optional - held-out normal photos
        test/<defect_name>/*.jpg  optional - defective photos for evaluation

and writes backend/data/checkpoints/<model_key>/<backend>/model.ckpt, which
backend/detector.py loads when a category's `anomaly.model_key` matches.

Anomaly detection only makes sense for a *fixed view* (same product, same
framing - e.g. the outdoor-unit bracket shot). Free-form field photos vary
too much; use the VLM rubric for those. Requires requirements-anomaly.txt
and, realistically, a GPU.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings  # noqa: E402

MIN_TRAIN_IMAGES = 30


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-key", required=True, help="dataset folder name == categories.yaml anomaly.model_key")
    parser.add_argument("--backend", default=settings.ad_backend, choices=["patchcore", "efficient_ad"])
    parser.add_argument("--dataset-root", default=str(settings.dataset_root_path))
    parser.add_argument("--image-size", type=int, default=256)
    parser.add_argument("--max-epochs", type=int, default=None, help="EfficientAd only (PatchCore is single-pass)")
    args = parser.parse_args()

    root = Path(args.dataset_root) / args.model_key
    train_good = root / "train" / "good"
    images = sorted(p for p in train_good.glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    if len(images) < MIN_TRAIN_IMAGES:
        print(f"[train_folder] need at least {MIN_TRAIN_IMAGES} normal images in {train_good}, found {len(images)}", file=sys.stderr)
        return 2

    # ANOMALIB-API: imports kept local so --help works without torch installed.
    from anomalib.data import Folder
    from anomalib.engine import Engine
    from anomalib.models import EfficientAd, Patchcore
    from lightning.pytorch.callbacks import ModelCheckpoint

    has_test = (root / "test").exists()
    abnormal_dirs = [d for d in (root / "test").iterdir() if d.is_dir() and d.name != "good"] if has_test else []
    datamodule = Folder(
        name=args.model_key,
        root=str(root),
        normal_dir="train/good",
        normal_test_dir="test/good" if (root / "test" / "good").exists() else None,
        abnormal_dir=[str(d.relative_to(root)) for d in abnormal_dirs] or None,
        train_batch_size=1 if args.backend == "efficient_ad" else 16,
        eval_batch_size=16,
    )

    model = Patchcore() if args.backend == "patchcore" else EfficientAd()
    out_dir = settings.checkpoint_dir_path / args.model_key / args.backend
    out_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_cb = ModelCheckpoint(dirpath=str(out_dir), filename="model", save_last=False, save_top_k=1, monitor=None)
    engine_kwargs = {"callbacks": [checkpoint_cb], "default_root_dir": str(out_dir / "lightning")}
    if args.max_epochs:
        engine_kwargs["max_epochs"] = args.max_epochs
    engine = Engine(**engine_kwargs)

    print(f"[train_folder] training {args.backend} on {len(images)} normal images from {train_good}")
    engine.fit(model=model, datamodule=datamodule)

    metrics: dict = {"model_key": args.model_key, "backend": args.backend, "num_train_images": len(images), "evaluated": False}
    if abnormal_dirs or (root / "test" / "good").exists():
        results = engine.test(model=model, datamodule=datamodule)
        if results:
            row = results[0]
            metrics.update({"image_auroc": row.get("image_AUROC"), "pixel_auroc": row.get("pixel_AUROC"), "evaluated": True})
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")

    ckpt = out_dir / "model.ckpt"
    print(f"[train_folder] checkpoint: {ckpt} (exists={ckpt.exists()})")
    print(f"[train_folder] metrics: {json.dumps(metrics, ensure_ascii=False)}")
    print("[train_folder] enable it in config/categories.yaml:\n"
          f"  anomaly: {{enabled: true, model_key: {args.model_key}, shot_types: [<shot id>]}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
