"""
Pydantic response/request models for the /inspect and /metrics endpoints.
"""
from typing import Optional
from pydantic import BaseModel, Field


class InspectionResult(BaseModel):
    category: str
    model_backend: str = Field(description="patchcore | efficient_ad")

    # These two fields ALWAYS come from the deterministic anomaly detector.
    # The VLM is never allowed to change them - see vlm_explainer.py.
    is_defective: bool
    anomaly_score: float

    heatmap_png_base64: Optional[str] = Field(
        default=None, description="Base64 PNG of the original image with the anomaly heatmap overlaid"
    )

    # Natural-language layer - descriptive only, never authoritative.
    defect_description: Optional[str] = None
    likely_cause: Optional[str] = None
    severity: Optional[str] = Field(default=None, description="low | medium | high")
    explanation_source: str = Field(default="none", description="vlm | none | vlm_discarded_inconsistent")

    inference_latency_ms: float
    explanation_latency_ms: Optional[float] = None


class CategoryMetrics(BaseModel):
    category: str
    model_backend: str
    image_auroc: Optional[float] = None
    pixel_auroc: Optional[float] = None
    num_test_images: Optional[int] = None
    trained: bool = False


class MetricsResponse(BaseModel):
    categories: list[CategoryMetrics]
    note: str = (
        "Metrics are populated by training/evaluate.py after training runs on a GPU machine. "
        "Until that has been run locally, entries show trained=false."
    )
