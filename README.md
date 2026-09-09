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

The full pipeline (training scripts, inference API, dashboard) is built
and unit-tested where that doesn't require a GPU (see
[`tests/`](./tests)). The anomaly-detection models have **not yet been
trained** - that needs a CUDA GPU and takes real wall-clock time per
category, which didn't happen inside the environment this scaffold was
written in. The demo GIF, the results table below, and the "does it
actually work end to end" claim will be added after that training run -
see [`SETUP_GUIDE.md`](./SETUP_GUIDE.md) for exactly how to produce it.
This status line gets deleted once that's done - it stays honest in the
meantime, same as AI-Dev-Crew's README does about its own test results.

## Demo

*(To be added: a GIF of the Streamlit dashboard classifying a real
defective part from the MVTec AD test set, with the heatmap and the
VLM's written report, once training has run. See `docs/demo/`.)*

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

*(Populated by `training/evaluate.py` after training - see
[`SETUP_GUIDE.md`](./SETUP_GUIDE.md). Not filled in yet; showing the
table shape.)*

| Category | Backend | Image AUROC | Pixel AUROC |
|---|---|---|---|
| bottle | patchcore | - | - |
| cable | patchcore | - | - |
| metal_nut | patchcore | - | - |
| screw | patchcore | - | - |
| bottle | efficient_ad | - | - |
| cable | efficient_ad | - | - |
| metal_nut | efficient_ad | - | - |
| screw | efficient_ad | - | - |

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

- Training has not been run yet in this environment (needs a local GPU) -
  see "Status" above.
- The VLM's `likely_cause` and `severity` fields are natural-language
  suggestions grounded in the detected region, not verified diagnoses -
  they're there to make the report readable, not to replace the anomaly
  score as the source of truth.
- `backend/detector.py` and `training/common.py` are written against the
  documented anomalib>=1.1 API; exact class/attribute names have moved
  between anomalib releases before, so a version mismatch is possible and
  is called out explicitly in code comments and in
  `SETUP_GUIDE.md -> Troubleshooting`.

## Companion projects

Part of the same portfolio as [AI-Portfolio-Agent](../AI-Portfolio-Agent)
(RAG + tool-calling single agent) and [AI-Dev-Crew](../AI-Dev-Crew)
(multi-agent orchestration) - together showing three different local-AI
skills: single-agent RAG, multi-agent orchestration, and classic
CV/deep-learning model training + deployment.
