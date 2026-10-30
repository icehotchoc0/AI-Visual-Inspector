"""
Pydantic models shared by the API, the pipeline, and the checks.

Vocabulary
----------
Finding   : one machine-readable observation about a photo or a job
            (quality issue, suspected fraud, missing shot, visual defect,
            anomaly score). Findings carry a `source` so a reviewer can see
            whether a deterministic check or the VLM produced them.
Verdict   : PASS | REVIEW | FAIL. Always computed by backend/verdict.py from
            findings via explicit rules - never written by the VLM.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "REVIEW", "FAIL"]
PhotoStatus = Literal["OK", "FLAGGED", "REJECTED"]
Severity = Literal["low", "medium", "high"]
FindingSource = Literal["quality_check", "fraud_check", "checklist", "vlm", "anomaly_detector", "system"]
FindingKind = Literal["quality", "fraud", "checklist", "defect", "anomaly", "consistency", "info"]
Phase = Literal["before", "after", "any"]


class Finding(BaseModel):
    code: str = Field(description="machine-readable code, e.g. blurry, duplicate_cross_job, exposed_wiring")
    kind: FindingKind
    source: FindingSource
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    message: str = Field(description="사람이 읽는 한국어 설명")
    photo_id: Optional[str] = Field(default=None, description="photo-level finding; None for job-level")
    related_photo_ids: list[str] = Field(default_factory=list)
    location_hint: Optional[str] = Field(default=None, description="e.g. '좌측 상단', '배관 이음부'")
    evidence: Optional[str] = Field(default=None, description="what the check/VLM actually observed")
    data: dict = Field(default_factory=dict, description="numeric details (blur score, hamming distance, ...)")


# ---------------------------------------------------------------- request --
class PhotoInput(BaseModel):
    photo_id: str = Field(min_length=1, max_length=128)
    image_base64: Optional[str] = Field(default=None, description="PNG/JPEG bytes, base64 (JSON API)")
    declared_shot_type: Optional[str] = Field(default=None, description="shot id from the category, as declared by the technician app")
    phase: Optional[Phase] = None
    captured_at: Optional[datetime] = Field(default=None, description="client-reported capture time (EXIF is also read)")


class JobContext(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)
    category: str
    technician_id: Optional[str] = None
    customer_ref: Optional[str] = Field(default=None, description="opaque reference to the service order")
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    site_lat: Optional[float] = None
    site_lon: Optional[float] = None
    notes: Optional[str] = Field(default=None, max_length=2000, description="기사 메모 / 작업 내용 (VLM 컨텍스트)")


class InspectionRequest(BaseModel):
    job: JobContext
    photos: list[PhotoInput] = Field(min_length=1)
    options: "InspectionOptions" = Field(default_factory=lambda: InspectionOptions())


class InspectionOptions(BaseModel):
    run_vlm: bool = True
    run_fraud_history: bool = Field(default=True, description="compare hashes against previously stored inspections")
    return_thumbnails: bool = False
    language: Literal["ko"] = "ko"
    callback_url: Optional[str] = Field(default=None, max_length=2000, pattern=r"^https?://", description="async 모드에서 완료 시 결과를 POST 할 URL (HMAC 서명)")


class JobStatus(BaseModel):
    job_key: str
    status: Literal["queued", "running", "done", "failed"]
    inspection_id: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional["InspectionResult"] = None


# --------------------------------------------------------------- response --
class PhotoQuality(BaseModel):
    width: int
    height: int
    blur_score: float
    brightness: float
    contrast: float
    passed: bool


class PhotoMeta(BaseModel):
    exif_datetime: Optional[datetime] = None
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    camera_model: Optional[str] = None
    software: Optional[str] = None
    perceptual_hash: str
    sha256: str


class PhotoAnalysis(BaseModel):
    """What the VLM said about one photo (descriptive layer)."""
    shot_type: Optional[str] = None
    shot_confidence: float = 0.0
    work_area_visible: Optional[bool] = None
    scene_description: Optional[str] = None
    provider: Optional[str] = Field(default=None, description="claude | ollama | none")
    model: Optional[str] = None
    latency_ms: Optional[float] = None


class PhotoResult(BaseModel):
    photo_id: str
    status: PhotoStatus
    declared_shot_type: Optional[str] = None
    resolved_shot_type: Optional[str] = Field(default=None, description="declared if VLM agrees or VLM unavailable, else VLM's")
    phase: Optional[Phase] = None
    quality: PhotoQuality
    meta: PhotoMeta
    analysis: PhotoAnalysis
    findings: list[Finding] = Field(default_factory=list)
    anomaly_score: Optional[float] = None
    thumbnail_png_base64: Optional[str] = None
    stored: bool = Field(default=False, description="original bytes retained for review (GET /v1/inspections/{id}/photos/{photo_id})")


class ChecklistItem(BaseModel):
    shot_id: str
    name: str
    required: bool
    satisfied: bool
    photo_ids: list[str] = Field(default_factory=list)


class InspectionResult(BaseModel):
    inspection_id: str
    job_id: str
    category: str
    category_name: str
    verdict: Verdict
    verdict_reasons: list[str] = Field(description="한국어 판정 근거 (순서 = 중요도)")
    summary: str = Field(description="운영자/기사용 한 줄 요약 (한국어)")
    photos: list[PhotoResult]
    checklist: list[ChecklistItem]
    job_findings: list[Finding] = Field(default_factory=list, description="job-level findings (missing shots, cross-photo fraud, before/after mismatch)")
    stats: dict = Field(default_factory=dict)
    vlm_provider_used: Optional[str] = None
    created_at: datetime
    pipeline_version: str


class InspectionListItem(BaseModel):
    inspection_id: str
    job_id: str
    category: str
    verdict: Verdict
    created_at: datetime
    num_photos: int
    review_verdict: Optional[Verdict] = None
    technician_id: Optional[str] = None


class ReviewRequest(BaseModel):
    verdict: Verdict = Field(description="검수자 최종 판정")
    reviewer: str = Field(min_length=1, max_length=128)
    notes: Optional[str] = Field(default=None, max_length=4000)


class ReviewInfo(BaseModel):
    inspection_id: str
    auto_verdict: Verdict
    review_verdict: Optional[Verdict] = None
    review_notes: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    photos_stored: bool = False
    technician_id: Optional[str] = None


class TechnicianStat(BaseModel):
    technician_id: str
    total: int
    PASS: int = 0
    REVIEW: int = 0
    FAIL: int = 0
    pass_rate: float
    fail_rate: float


class GroupInfo(BaseModel):
    id: str
    name: str
    order: int
    category_ids: list[str] = Field(default_factory=list)


class CategoryInfo(BaseModel):
    id: str
    name: str
    group: str
    group_name: str
    description: str
    shots: list[dict]
    defect_codes: list[dict]
    before_after_pairs: list[list[str]]


InspectionRequest.model_rebuild()
JobStatus.model_rebuild()
