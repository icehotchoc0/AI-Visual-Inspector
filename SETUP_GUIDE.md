# Setup Guide - AI-Visual-Inspector

Written for Windows + an NVIDIA GPU (RTX 3080), same environment as
AI-Portfolio-Agent and AI-Dev-Crew. Run all commands from PowerShell,
from inside this project folder, unless noted otherwise.

## 0. Prerequisites

- Python 3.10 or 3.11
- An NVIDIA GPU + CUDA-capable PyTorch install (Anomalib training uses it)
- [Ollama](https://ollama.com) already installed (you have this from
  AI-Portfolio-Agent)
- ~15-20 GB free disk space (MVTec AD categories + model checkpoints)

## 1. Create a virtual environment and install dependencies

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

If `pip install anomalib[core]` fails on your torch/CUDA combination,
install torch first for your exact CUDA version from
https://pytorch.org/get-started/locally/, then re-run
`pip install anomalib[core]`.

## 2. Pull the local VLM model

```powershell
ollama pull qwen2.5vl:7b
ollama list   # confirm it shows up
```

If `qwen2.5vl:7b` isn't available under that exact tag on your Ollama
version, run `ollama search qwen2.5-vl` (or check https://ollama.com/library)
for the current tag name and update `VLM_MODEL` in `.env` to match.

## 3. Configure environment

```powershell
copy .env.example .env
```

Defaults are sane for a first run (4 categories: bottle, cable, metal_nut,
screw; PatchCore as the default served backend). Edit `.env` if you want
fewer/more categories - fewer categories means less training time.

## 4. Download the dataset & train

The MVTec AD dataset downloads **automatically** the first time you run a
training script (Anomalib's `MVTec` datamodule handles this - no manual
download step). Expect the first run per category to take a while
(dataset download + several training epochs).

```powershell
# Trains PatchCore on every category listed in .env, one at a time.
python training/train_patchcore.py

# Trains EfficientAd on every category (much faster per-category than PatchCore).
python training/train_efficientad.py

# Optional: just one category while testing the pipeline end to end first.
python training/train_patchcore.py --categories bottle
```

Each run appends to `training/results/metrics.json` and copies the
trained checkpoint to `backend/data/checkpoints/<category>/<backend>/model.ckpt`.

Recommended order: train **one category with EfficientAd first**
(fastest) to confirm the whole pipeline works, before committing GPU time
to all four categories on both backbones.

## 5. Check the results table

```powershell
python training/evaluate.py
```

Paste the printed Markdown table into `README.md`'s "Results" section,
replacing the placeholder table.

## 6. Run the app

```powershell
# One-click (opens two windows: backend + dashboard):
.\run_inspector.bat

# Or manually, in two separate terminals:
uvicorn backend.app:app --host 0.0.0.0 --port 8000
streamlit run frontend/dashboard.py
```

Open http://localhost:8501, pick a category, use the "sample test image"
tab (pulls from the downloaded MVTec test set - includes both good and
genuinely defective parts), and click **Run Inspection**.

## 7. Record the demo

Once you can see a real defective sample get flagged with a heatmap and a
written explanation, screen-record that flow (same tool you used for
AI-Dev-Crew's demo gif) and save it to `docs/demo/`. Update the README's
"Demo" and "Status" sections once that's in place.

## Troubleshooting

**`ImportError` on `from anomalib.deploy import TorchInferencer` or
`from anomalib.models import Patchcore/EfficientAd`:**
Anomalib has reorganized its public API across 1.x minor versions a few
times. Run `python -c "import anomalib; print(anomalib.__version__)"`
and check that version's docs/changelog on
https://github.com/openvinotoolkit/anomalib for the current import path,
then fix the import line in `backend/detector.py` (for inference) or
`training/common.py` (for training) - both are written so the fix is a
one-line change in one place, not a rewrite.

**`engine.test()` result keys don't match `image_AUROC`/`pixel_AUROC`:**
Print `result` in `training/common.py` before those two lines to see the
exact keys your installed version returns, and adjust the two `.get(...)`
calls.

**Ollama connection refused from `vlm_explainer.py`:**
Confirm Ollama is running (`ollama list` should not error) and that
`OLLAMA_HOST` in `.env` matches wherever it's listening (default
`http://localhost:11434`).

**`ModelNotTrainedError` from the API:**
Expected before step 4 has been run for that category/backend - the
checkpoint just doesn't exist yet.

**Running everything inside `docker-compose.yml`:**
Only the backend runs in Docker - training needs a real GPU and stays on
the host. Ollama also stays on the host; the container reaches it via
`host.docker.internal` (already configured in `docker-compose.yml`).
