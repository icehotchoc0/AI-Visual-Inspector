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

**Confirmed already, nothing to do:** this project targets anomalib 2.x.
`pip install -r requirements.txt` pulled anomalib 2.6.1 the first time
this was set up, and the code already uses the v2 names
(`anomalib.data.MVTecAD`, not the old v1 `MVTec`). If your install
resolves to a different 2.x version, the below still applies; if it
somehow installs a 1.x version instead, swap `MVTecAD` back to `MVTec`
in `training/common.py`.

**`ImportError` on `from anomalib.deploy import TorchInferencer`:**
Current anomalib docs call this class "legacy" (kept for backward
compatibility, not the primary documented path) in favour of:
```python
from anomalib.data import PredictDataset
dataset = PredictDataset(path="path/to/single/image/folder", image_size=(256, 256))
predictions = engine.predict(model=model, dataset=dataset, ckpt_path=str(ckpt))
```
If `TorchInferencer` has been removed by the time you run this, replace
the body of `AnomalyDetector._load`/`predict` in `backend/detector.py`
with the `PredictDataset` + `engine.predict()` approach above - you'll
need to write the uploaded image to a temp folder first since
`PredictDataset` reads from disk, not from an in-memory array.

**`ImportError` on `from anomalib.models import Patchcore` or
`EfficientAd`:** these two class names are confirmed correct for 2.6.1.
If a newer anomalib release changes them, `python -c "import
anomalib.models; print(dir(anomalib.models))"` will show the current
names.

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
