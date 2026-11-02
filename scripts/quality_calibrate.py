"""
Pick photo-quality thresholds from YOUR photos.

    python scripts/quality_calibrate.py /path/to/photos [--warn 30 --reject 12 --dark-warn 35 --dark-reject 12]

Walks the folder (jpg/png/heic), prints the sharpness/brightness distribution
and how many photos the given thresholds would warn on or reject. Put a mix of
clearly good photos and the ones technicians actually send; set `reject` just
below the sharpness of the worst photo a reviewer can still use, and `warn`
around the 10th percentile of good photos.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.checks.quality import _to_gray, laplacian_variance, sharpness_score  # noqa: E402

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--warn", type=float, default=30.0)
    ap.add_argument("--reject", type=float, default=12.0)
    ap.add_argument("--dark-warn", type=float, default=35.0)
    ap.add_argument("--dark-reject", type=float, default=12.0)
    ap.add_argument("--show", type=int, default=10, help="print the N least sharp files")
    args = ap.parse_args()

    rows = []
    for p in sorted(Path(args.folder).rglob("*")):
        if p.suffix.lower() not in EXT:
            continue
        try:
            img = ImageOps.exif_transpose(Image.open(p)) or Image.open(p)
            g = _to_gray(img)
            rows.append((str(p.relative_to(args.folder)), sharpness_score(g), float(g.mean()), laplacian_variance(g), img.size))
        except Exception as exc:  # noqa: BLE001
            print(f"skip {p}: {exc}", file=sys.stderr)
    if not rows:
        print("no images found")
        return 1
    sharp = np.array([r[1] for r in rows]); bright = np.array([r[2] for r in rows])
    pct = lambda a, q: float(np.percentile(a, q))  # noqa: E731
    print(f"photos: {len(rows)}")
    print(f"sharpness  p5={pct(sharp,5):.1f}  p10={pct(sharp,10):.1f}  p25={pct(sharp,25):.1f}  median={pct(sharp,50):.1f}  p75={pct(sharp,75):.1f}  max={sharp.max():.1f}")
    print(f"brightness p5={pct(bright,5):.1f}  p10={pct(bright,10):.1f}  median={pct(bright,50):.1f}  p95={pct(bright,95):.1f}")
    rej = int(((sharp < args.reject) | (bright < args.dark_reject)).sum())
    warn = int((((sharp < args.warn) & (sharp >= args.reject)) | ((bright < args.dark_warn) & (bright >= args.dark_reject))).sum())
    print(f"with warn={args.warn} reject={args.reject} dark_warn={args.dark_warn} dark_reject={args.dark_reject}: "
          f"reject {rej} ({rej/len(rows)*100:.0f}%), warn {warn} ({warn/len(rows)*100:.0f}%), ok {len(rows)-rej-warn}")
    print(f"\nleast sharp {args.show}:")
    for name, s, b, lap, size in sorted(rows, key=lambda r: r[1])[: args.show]:
        tag = "REJECT" if s < args.reject or b < args.dark_reject else ("warn" if s < args.warn or b < args.dark_warn else "ok")
        print(f"  {tag:6s} sharp={s:6.1f} bright={b:6.1f} lap={lap:7.1f} {size[0]}x{size[1]}  {name}")
    print("\nsuggestion: sharpness_warn ≈ p10 of good photos, sharpness_reject ≈ just below the worst usable photo. Edit config/categories/_defaults.yaml -> quality.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
