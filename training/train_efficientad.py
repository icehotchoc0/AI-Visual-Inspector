"""
Train EfficientAd on every category in settings.MVTEC_CATEGORIES.
Much faster to train/infer than PatchCore - good for the real-time demo.

Usage:
    python training/train_efficientad.py
"""
import argparse

from training.common import train_and_evaluate

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--categories", type=str, default=None)
    args = parser.parse_args()

    categories = args.categories.split(",") if args.categories else None
    train_and_evaluate(model_backend="efficient_ad", model_cls_name="EfficientAd", categories=categories)
