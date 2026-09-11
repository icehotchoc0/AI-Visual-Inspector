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

**Important - do this BEFORE `pip install -r requirements.txt`, not after.**
A plain `pip install torch` (which is what happens if torch is pulled in
only as anomalib's dependency, e.g. via `requirements.txt` directly) gets
you the **CPU-only** build - PyPI's default torch wheel does not bundle
CUDA. This bit us on the first real run here: training ran on CPU
(`GPU available: False` in the logs) even with an RTX 3080 present.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install --upgrade pip

# Install the CUDA build of torch FIRST (check nvidia-smi's "CUDA Version"
# field if unsure - cu128 works with any reasonably current driver on a
# 30-series card or newer):
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

# Verify BEFORE installing anomalib:
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"

# Only once that prints True and your GPU's name, install the rest:
pip install -r requirements.txt
```

If `torch.cuda.is_available()` still prints `False` after the cu128
install, your driver is likely older - try `cu118` instead (most broadly
compatible): `pip install torch torchvision --index-url
https://download.pytorch.org/whl/cu118`. Either way, `pip install -r
requirements.txt` afterward will see torch already satisfied and won't
downgrade it back to CPU - just don't run `pip install torch` bare again
in this venv.

If you already ran `pip install -r requirements.txt` first (as happened
here) and training already completed on CPU, the trained checkpoints are
still valid - CPU vs GPU only affects training/inference *speed*, not
correctness. Fix the install for the *remaining* training runs
(EfficientAd in particular needs GPU speed far more than PatchCore did).

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
training script (Anomalib's `MVTecAD` datamodule handles this - no manual
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

**Ollama connection refused from `vlm_explainer.py`, or `/health` shows
`"ollama_reachable": false`:**
Confirm Ollama is running (`ollama list` should not error) and that
`INSPECTOR_OLLAMA_HOST` in `.env` matches wherever it's listening
(default `http://localhost:11434`). Note this is deliberately
`INSPECTOR_OLLAMA_HOST`, not `OLLAMA_HOST` - see the dedicated
troubleshooting entry below for why.

**`ValueError: train_batch_size for EfficientAd should be 1.`:**
Confirmed by a real run on 2026-09-09. EfficientAd's architecture computes its normalization statistics per-sample during training, so anomalib hard-requires `train_batch_size=1` for this backend specifically (PatchCore has no such restriction and uses the default 32). `training/common.py` already sets this automatically based on `model_backend` - if you see this error, you're on an older copy of the file; pull the latest.

**`DLL load failed while importing cv2: The paging file is too small`, `pyarrow>=13.0.0 is required`, or `MemoryError` in a `DataLoader worker process` (all appearing together, mid-training):**
Confirmed by a real run on 2026-09-09. This is Windows-specific resource exhaustion, not a bug in the data or the GPU setup: anomalib's default `num_workers=8` spawns 8 separate worker *processes* for data loading, and on Windows each one has to re-import the entire torch/anomalib/opencv/pandas stack from scratch (no `fork()` like on Linux), which is heavy enough to exhaust the system paging file. Training itself (the GPU forward/backward pass) had already succeeded for epoch 0 before this hit - it only shows up when the *next* set of dataloader workers spin up (e.g. for validation). `training/common.py` already sets `num_workers=2` for the `efficient_ad` backend specifically (PatchCore keeps the default 8, which worked fine). If you still hit this, drop `num_workers` to `0` in `training/common.py` (no multiprocessing at all - slower but immune to this).

**`/health` shows `"ollama_reachable": false` with
`"ollama_host": "0.0.0.0"` even though `ollama list` works fine:**
Confirmed on Ali's machine on 2026-09-10 - a real, subtle bug, not a
one-off. `OLLAMA_HOST` is Ollama's own well-known env var for
configuring what address the Ollama *server* binds to (people commonly
set it to `0.0.0.0` system-wide to allow LAN access to Ollama). This
project's `backend/config.py` used to read the *client's* target URL
from that exact same env var name - so a system-wide `OLLAMA_HOST=0.0.0.0`
silently overrode `.env`'s `http://localhost:11434` (pydantic-settings
gives real environment variables priority over the `.env` file), and the
backend then tried to connect *to* `0.0.0.0` as a client target, which
isn't valid. `backend/config.py` now reads `INSPECTOR_OLLAMA_HOST`
instead (see `.env.example`) specifically to avoid ever colliding with
Ollama's own env var again, regardless of how a given machine has
Ollama's server configured. If you're on an older `.env`, add
`INSPECTOR_OLLAMA_HOST=http://localhost:11434` to it (the old
`OLLAMA_HOST=...` line is now unused by this app and can stay or go -
it won't be read either way). The `/health` endpoint also now returns
an `ollama_error` field with the real exception text instead of
silently swallowing it - check that first if this happens again.

**`run_inspector.bat` / `.ps1` opens a window that says `'streamlit' is
not recognized` or `'uvicorn' is not recognized`:**
Confirmed on Ali's machine on 2026-09-10 - a real bug in the launcher
scripts that shipped here, not a setup mistake. The scripts opened
*new* terminal windows via `start`/`Start-Process`, and those new
windows don't inherit this venv's activation (they start with the base
system PATH). Both scripts now call the venv's own
`venv\Scripts\uvicorn.exe` / `venv\Scripts\streamlit.exe` directly
by path instead of relying on activation carrying over. If you're on an
older copy of these files, pull the latest, or just run the two
commands manually in two terminals where you've activated the venv
yourself (as shown in step 6 above).

**`ModelNotTrainedError` from the API:**
Expected before step 4 has been run for that category/backend - the
checkpoint just doesn't exist yet.

**Running everything inside `docker-compose.yml`:**
Only the backend runs in Docker - training needs a real GPU and stays on
the host. Ollama also stays on the host; the container reaches it via
`host.docker.internal` (already configured in `docker-compose.yml`).
