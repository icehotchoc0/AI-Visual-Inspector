"""
Train PatchCore on every category in settings.MVTEC_CATEGORIES.

Usage (from project root, GPU machine, deps installed - see SETUP_GUIDE.md):
    python training/train_patchcore.py
    python training/train_patchcore.py --categories bottle,screw
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `training.common` resolves when run as `python training/train_patchcore.py`

from training.common import train_and_evaluate

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--categories", type=str, default=None, help="comma-separated, defaults to .env setting")
    args = parser.parse_args()

    categories = args.categories.split(",") if args.categories else None
    train_and_evaluate(model_backend="patchcore", model_cls_name="Patchcore", categories=categories)
