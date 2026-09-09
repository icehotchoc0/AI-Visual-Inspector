"""
FastAPI app for AI-Visual-Inspector.

    POST /inspect   - upload an image, get back a pass/fail verdict,
                       heatmap overlay, and (if defective) a natural-
                       language report.
    GET  /health    - liveness + whether the VLM/Ollama is reachable.
    GET  /metrics    - per-category benchmark numbers, read from
                       training/results/metrics.json once training has
                       been run (see training/evaluate.py).

Run from the project root:
    uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

from backend.config import settings
from backend.detector import ModelNotTrainedError, get_detector, make_heatmap_overlay
from backend.schemas import CategoryMetrics, InspectionResult, MetricsResponse
from backend.vlm_explainer import explain_defect

app = FastAPI(
    title="AI-Visual-Inspector",
    description="Local-first industrial visual quality inspection: anomaly detection + VLM explanation.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

METRICS_PATH = Path("training/results/metrics.json")


@app.get("/health")
def health() -> dict:
    ollama_reachable = False
    if settings.vlm_enabled:
        try:
            import ollama

            ollama.Client(host=settings.ollama_host).list()
            ollama_reachable = True
        except Exception:
            ollama_reachable = False

    return {
        "status": "ok",
        "ad_backend": settings.ad_backend,
        "categories": settings.categories,
        "vlm_enabled": settings.vlm_enabled,
        "ollama_reachable": ollama_reachable,
    }


@app.get("/metrics", response_model=MetricsResponse)
def metrics() -> MetricsResponse:
    if not METRICS_PATH.exists():
        # Honest empty state - no fabricated numbers before training has run.
        entries = [
            CategoryMetrics(category=c, model_backend=settings.ad_backend, trained=False)
            for c in settings.categories
        ]
        return MetricsResponse(categories=entries)

    raw = json.loads(METRICS_PATH.read_text())
    entries = [CategoryMetrics(**row) for row in raw]
    return MetricsResponse(categories=entries)


@app.post("/inspect", response_model=InspectionResult)
async def inspect(
    file: UploadFile = File(...),
    category: str = "bottle",
    backend: str | None = None,
) -> InspectionResult:
    backend = backend or settings.ad_backend
    if category not in settings.categories:
        raise HTTPException(400, f"Unknown category '{category}'. Known: {settings.categories}")

    contents = await file.read()
    try:
        image = Image.open(io.BytesIO(contents)).convert("RGB")
    except Exception as exc:
        raise HTTPException(400, f"Could not read image: {exc}") from exc

    try:
        detector = get_detector(category, backend)
    except ModelNotTrainedError as exc:
        raise HTTPException(409, str(exc)) from exc

    detection = detector.predict(image)

    overlay_b64 = None
    description = cause = severity = None
    explanation_source = "none"
    explanation_latency_ms = None

    if detection.heatmap is not None:
        overlay_image = make_heatmap_overlay(image, detection.heatmap)
        overlay_b64 = _image_to_base64(overlay_image)
    else:
        overlay_image = image

    # Only ask the VLM to explain when there is actually something to
    # explain - saves a model call and avoids inventing a story for a
    # part the detector already found to be fine.
    if detection.is_defective:
        explanation = explain_defect(overlay_image, category)
        description = explanation.defect_description
        cause = explanation.likely_cause
        severity = explanation.severity
        explanation_source = explanation.source
        explanation_latency_ms = explanation.latency_ms

    return InspectionResult(
        category=category,
        model_backend=backend,
        is_defective=detection.is_defective,
        anomaly_score=detection.anomaly_score,
        heatmap_png_base64=overlay_b64,
        defect_description=description,
        likely_cause=cause,
        severity=severity,
        explanation_source=explanation_source,
        inference_latency_ms=detection.latency_ms,
        explanation_latency_ms=explanation_latency_ms,
    )


def _image_to_base64(image: Image.Image) -> str:
    import base64

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")
