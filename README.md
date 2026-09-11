# AI-Visual-Inspector

A local-first industrial visual quality-control system: a deterministic
anomaly-detection model decides pass/fail and localizes the defect, and a
local vision-language model then explains that decision in plain
language - like a human inspector writing a short note, not a black box
just outputting a number.

No cloud APIs. Detection and explanation both run on local hardware
(the anomaly model on GPU via PyTorch/Anomalib, the explanation model on
Ollama) - the same local-only, privacy-preserving pattern as
[AI-Portfolio-Agent](../AI-Portfolio-Agent) and [AI-Dev-Crew](../AI-Dev-Crew).

## Status

Both backbones are trained on all 4 categories (bottle, cable,
metal_nut, screw) - real results below. PatchCore ran on CPU on
2026-09-09 (~67 minutes total). EfficientAd ran on an RTX 3080 on
2026-09-10, capped at 50 epochs/category (~15-20 min/category, under
1.5 hours total). The full pipeline (FastAPI backend + Streamlit
dashboard + Anomalib inference + Qwen2.5-VL explanation) has been run
end-to-end on real MVTec AD test images - see the demo below.

## Demo

![AI-Visual-Inspector demo: a defective bottle sample is picked from
the MVTec AD test set, the anomaly detector flags it FAIL with a
heatmap over the damaged region, and the local VLM writes a short
inspection report (defect type, likely cause,
severity)](docs/demo/ai-visual-inspector-demo.gif)

A `broken_large` bottle sample from the MVTec AD test set is
classified FAIL (anomaly score 0.939), the heatmap localizes the
damage, and Qwen2.5-VL (running locally via Ollama) writes the
inspection report - defect type, likely cause, and severity - entirely
on local hardware.

## How it works

```
Image of a part
        │
        ▼
Anomaly detector (Anomalib: PatchCore or EfficientAd)
   trained ONLY on images of good parts
        │
        ├─► anomaly score  ──► is_defective? (THE decision - deterministic)
        └─► anomaly heatmap ──► where on the image
        │
        ▼  (only if is_defective == True)
Heatmap overlaid on the original image
        │
        ▼
Local VLM (Qwen2.5-VL via Ollama) describes the highlighted region:
   defect type, likely cause, severity
   - descriptive only, cannot override the verdict above
        │
        ▼
FastAPI /inspect response  ──►  Streamlit dashboard
```

## Why this design

- **The VLM never decides pass/fail.** The anomaly score from the
  deterministic model is the only thing that sets `is_defective`. The VLM
  is called *after* that decision, only to describe it, and if its own
  text contradicts the decision (e.g. it says "looks fine" about a part
  already flagged as defective) the response is discarded rather than
  trusted - same "don't take an agent's self-report as fact, verify
  independently" rule as AI-Dev-Crew's test-verification step.
- **Unsupervised, not classification.** The detector is trained only on
  images of *good* parts and flags anything that deviates - this matches
  how real factories actually work (defects are rare; you rarely have
  thousands of labeled defective examples to train a classifier on).
- **Two backbones, compared honestly.** PatchCore (memory-bank based,
  higher accuracy, slower) and EfficientAd (distillation-based, much
  faster, slightly lower accuracy) are both trained and benchmarked
  per-category rather than picking one and asserting it's "the best" -
  see the results table below once populated.
- **The VLM is only called when there's something to explain.** A part
  the detector already found to be fine never gets sent to the VLM -
  saves a model call and removes any chance of the VLM inventing a
  defect that isn't there.

## Results

Real numbers from `training/evaluate.py`, produced by actual training
runs (PyTorch/Anomalib 2.6.1): PatchCore on CPU on 2026-09-09,
EfficientAd on an RTX 3080 (GPU) on 2026-09-10:

| Category | Backend | Image AUROC | Pixel AUROC |
|---|---|---|---|
| bottle | patchcore | 1.000 | 0.986 |
| cable | patchcore | 0.986 | 0.985 |
| metal_nut | patchcore | 0.998 | 0.987 |
| screw | patchcore | 0.964 | 0.989 |
| bottle | efficient_ad | 1.000 | 0.979 |
| cable | efficient_ad | 0.917 | 0.973 |
| metal_nut | efficient_ad | 0.991 | 0.957 |
| screw | efficient_ad | 0.973 | 0.983 |

## Tech stack

- **Anomaly detection:** [Anomalib](https://github.com/openvinotoolkit/anomalib) (PatchCore, EfficientAd) on PyTorch
- **Dataset:** [MVTec AD](https://www.mvtec.com/company/research/datasets/mvtec-ad) - the standard industrial anomaly-detection benchmark
- **VLM:** [Qwen2.5-VL](https://huggingface.co/Qwen) served locally via [Ollama](https://ollama.com)
- **Backend:** FastAPI
- **Dashboard:** Streamlit
- **Packaging:** Docker (backend), plain `pip install` for training (needs local GPU)

## Project structure

```
AI-Visual-Inspector/
├── backend/
│   ├── app.py             # FastAPI: /inspect, /health, /metrics
│   ├── detector.py         # Anomalib wrapper - the deterministic verdict
│   ├── vlm_explainer.py     # Qwen2.5-VL via Ollama - descriptive layer + guardrail
│   ├── config.py             # env-based settings
│   ├── schemas.py             # pydantic response models
│   └── data/                   # checkpoints/, mvtec_ad/ (gitignored, regenerable)
├── training/
│   ├── common.py            # shared train+eval loop
│   ├── train_patchcore.py    # python training/train_patchcore.py
│   ├── train_efficientad.py   # python training/train_efficientad.py
│   └── evaluate.py             # prints the results table above from saved metrics
├── frontend/
│   └── dashboard.py          # Streamlit demo UI
├── tests/                      # guardrail + schema unit tests (no GPU needed)
├── docker/                       # Dockerfile.backend, docker-compose.yml
├── docs/demo/                     # demo gif/mp4 (added after training)
├── run_inspector.ps1 / .bat         # one-click: start backend + dashboard
└── SETUP_GUIDE.md
```

## Setup

See [SETUP_GUIDE.md](./SETUP_GUIDE.md) for exact step-by-step commands:
installing dependencies, pulling the VLM model, downloading MVTec AD
(automatic, via Anomalib), training both backbones, and running the app.

## Limitations

- EfficientAd's cable result (image AUROC 0.917) is noticeably behind
  PatchCore's (0.986) - plausible given it was capped at 50 epochs for
  time and cable is MVTec AD's hardest category for most models; more
  epochs would likely close some of the gap.
- The VLM's `likely_cause` and `severity` fields are natural-language
  suggestions grounded in the detected region, not verified diagnoses -
  they're there to make the report readable, not to replace the anomaly
  score as the source of truth.
- `backend/detector.py` and `training/common.py` target anomalib 2.6.1.
  Both sides are now confirmed correct by real runs, not just
  documentation: `training/common.py` (MVTecAD, Patchcore, Engine, the
  `image_AUROC`/`pixel_AUROC` result keys) by the training runs above,
  and `backend/detector.py` by running the FastAPI backend against the
  saved checkpoints end-to-end. `TorchInferencer` turned out not to
  support the raw Lightning `.ckpt` files training produces, so
  `detector.py` uses `Engine.predict()` + `PredictDataset` instead - see
  `SETUP_GUIDE.md -> Troubleshooting` for the details.

## Companion projects

Part of the same portfolio as [AI-Portfolio-Agent](../AI-Portfolio-Agent)
(RAG + tool-calling single agent) and [AI-Dev-Crew](../AI-Dev-Crew)
(multi-agent orchestration) - together showing three different local-AI
skills: single-agent RAG, multi-agent orchestration, and classic
CV/deep-learning model training + deployment.
