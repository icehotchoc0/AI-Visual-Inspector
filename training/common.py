"""
Shared training/eval logic for both backbones (PatchCore, EfficientAd).

Verified against anomalib==2.6.1 (the version actually installed via
requirements.txt on 2026-09-09) using the current official docs:

    from anomalib.data import MVTecAD          # renamed from `MVTec` in v2
    from anomalib.models import Patchcore, EfficientAd
    from anomalib.engine import Engine

Not run end-to-end here (no GPU in this environment) - the imports and
class names are confirmed correct for 2.6.1, but the exact keys returned
by `engine.test()` (assumed below: "image_AUROC", "pixel_AUROC") were not
independently confirmed and are the most likely thing to need a tweak.
See SETUP_GUIDE.md -> Troubleshooting.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `backend.config` import

from backend.config import settings  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"
METRICS_PATH = RESULTS_DIR / "metrics.json"


def _load_existing_metrics() -> list[dict]:
    if METRICS_PATH.exists():
        return json.loads(METRICS_PATH.read_text())
    return []


def _save_metrics(rows: list[dict]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(rows, indent=2))


def _upsert_metric_row(rows: list[dict], new_row: dict) -> list[dict]:
    key = (new_row["category"], new_row["model_backend"])
    rows = [r for r in rows if (r["category"], r["model_backend"]) != key]
    rows.append(new_row)
    return rows


def train_and_evaluate(model_backend: str, model_cls_name: str, categories: list[str] | None = None) -> None:
    """model_backend: 'patchcore' | 'efficient_ad' (used for our own file layout)
    model_cls_name:  the anomalib class name to import, e.g. 'Patchcore' or 'EfficientAd'
    """
    from anomalib.data import MVTecAD
    from anomalib.engine import Engine
    import anomalib.models as anomalib_models

    model_cls = getattr(anomalib_models, model_cls_name)
    categories = categories or settings.categories
    metrics_rows = _load_existing_metrics()

    for category in categories:
        print(f"\n=== Training {model_backend} on category='{category}' ===")
        # EfficientAd hard-requires train_batch_size == 1 (its normalization
        # statistics are computed per-sample); PatchCore is fine with the
        # anomalib default of 32.
        train_batch_size = 1 if model_backend == "efficient_ad" else 32
        # On Windows, each DataLoader worker is a brand-new process that has
        # to re-import the whole torch/anomalib/opencv/pandas stack from
        # scratch (no fork()), which is memory-hungry. anomalib's default of
        # 8 workers was exhausting the system paging file mid-training on
        # Ali's machine (DLL load failed / MemoryError in worker processes).
        # 2 workers is still parallel but far lighter; drop to 0 if this
        # still happens.
        num_workers = 2 if model_backend == "efficient_ad" else 8
        datamodule = MVTecAD(
            root=str(settings.mvtec_root_path),
            category=category,
            train_batch_size=train_batch_size,
            num_workers=num_workers,
        )
        model = model_cls()
        # anomalib doesn't set a default max_epochs for EfficientAd (it's
        # left at Lightning's own default), which becomes 1000 epochs -
        # confirmed on Ali's machine: ~29s/epoch would be ~8 hours *per
        # category*. Community reports (anomalib discussion #2262) get
        # image_AUROC ~0.95 after just 10 epochs; 50 gives real safety
        # margin while still finishing all 4 categories in ~2 hours on an
        # RTX 3080. PatchCore has no training loop in this sense (single
        # feature-extraction + coreset pass), so it's left untouched.
        engine_kwargs = {"max_epochs": 50} if model_backend == "efficient_ad" else {}
        engine = Engine(**engine_kwargs)

        engine.fit(model=model, datamodule=datamodule)
        test_results = engine.test(model=model, datamodule=datamodule)
        result = test_results[0] if isinstance(test_results, list) else test_results

        image_auroc = result.get("image_AUROC") or result.get("AUROC")
        pixel_auroc = result.get("pixel_AUROC")

        # Copy the trained checkpoint into our own predictable layout so
        # backend/detector.py can find it without knowing anomalib's
        # internal results/ folder structure.
        target_dir = settings.checkpoint_dir_path / category / model_backend
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "model.ckpt"

        best_ckpt = getattr(getattr(engine, "trainer", None), "checkpoint_callback", None)
        source_ckpt = getattr(best_ckpt, "best_model_path", None) if best_ckpt else None
        if source_ckpt and Path(source_ckpt).exists():
            shutil.copy(source_ckpt, target_path)
            print(f"Checkpoint saved to {target_path}")
        else:
            print(
                f"WARNING: could not locate the checkpoint anomalib saved for "
                f"'{category}'. Check the `results/` folder anomalib created "
                f"in the current directory and copy it to {target_path} manually."
            )

        metrics_rows = _upsert_metric_row(
            metrics_rows,
            {
                "category": category,
                "model_backend": model_backend,
                "image_auroc": float(image_auroc) if image_auroc is not None else None,
                "pixel_auroc": float(pixel_auroc) if pixel_auroc is not None else None,
                "num_test_images": None,
                "trained": True,
            },
        )
        _save_metrics(metrics_rows)
        print(f"Metrics so far saved to {METRICS_PATH}")
