"""
마이스터즈 AI 사진 검수 (AI-Visual-Inspector) - field-service photo verification microservice.

    POST /v1/inspections              JSON body (photos as base64)   -> InspectionResult
    POST /v1/inspections/upload       multipart: job=<json>, files[] -> InspectionResult
    GET  /v1/inspections              list recent inspections
    GET  /v1/inspections/{id}         fetch a stored result
    GET  /v1/categories               category catalogue (shots, defect codes)
    GET  /health                      liveness + provider reachability

Run from the project root:
    uvicorn backend.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from backend.auth import require_api_key
from backend.observability import RequestIdMiddleware, configure_logging, metrics_payload, record_inspection
from backend.ratelimit import rate_limited
from backend.categories import CategoryRegistry, load_registry
from backend.config import settings
from backend.pipeline import PIPELINE_VERSION, InspectionPipeline, PipelineError
from backend.schemas import CategoryInfo, GroupInfo, InspectionListItem, InspectionRequest, InspectionResult, JobContext, PhotoInput, InspectionOptions
from backend.photostore import PhotoStore
from backend.store import Store
from backend.vlm import build_router

log = logging.getLogger("inspector")


# ------------------------------------------------------------- wiring --
@lru_cache(maxsize=1)
def get_registry() -> CategoryRegistry:
    return load_registry(settings.categories_path)


@lru_cache(maxsize=1)
def get_store() -> Store:
    return Store(settings.effective_database_url)


@lru_cache(maxsize=1)
def get_photo_store() -> PhotoStore | None:
    if not settings.photo_store_dir or settings.photo_store_policy == "off":
        return None
    return PhotoStore(settings.photo_store_dir, settings.photo_retention_days, settings.photo_store_policy)


@lru_cache(maxsize=1)
def get_vlm():
    return build_router(settings)


@lru_cache(maxsize=1)
def get_pipeline() -> InspectionPipeline:
    return InspectionPipeline(get_registry(), settings, vlm=get_vlm(), store=get_store(), photo_store=get_photo_store())


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_json, settings.log_level)
    if not settings.api_key_set:
        log.warning("INSPECTOR_API_KEYS is empty - API authentication is DISABLED. Set it before exposing this service.")
    reg = get_registry()
    vlm = get_vlm()
    log.info("categories: %s | vlm providers: %s | db: %s | cors: %s | rate limit: %s/min", reg.ids, vlm.names if vlm else "disabled", settings.db_path, settings.cors_origin_list or "off", settings.rate_limit_per_minute)
    if "*" in settings.cors_origin_list:
        log.warning("INSPECTOR_CORS_ORIGINS=* allows any browser origin - do not use in production.")
    ps = get_photo_store()
    if ps is not None:
        log.info("photo retention on: dir=%s policy=%s days=%d", ps.root, ps.policy, ps.retention_days)
        ps.purge_expired()
    yield


app = FastAPI(
    title="마이스터즈 AI 사진 검수",
    description="현장 AS·설치·시공 작업 사진을 제출 즉시 검사합니다. 사진 품질·부정 제출·체크리스트는 규칙으로, 시공 하자는 AI 비전 모델(Claude 기본, Ollama 폴백)로 분석하고 규칙 엔진이 PASS(합격)/REVIEW(검토 필요)/FAIL(불합격)을 판정합니다.",
    version=PIPELINE_VERSION,
    lifespan=lifespan,
)
if settings.cors_origin_list:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET", "POST"], allow_headers=["*"], expose_headers=["X-Request-ID"])
app.add_middleware(RequestIdMiddleware)


# ------------------------------------------------------------- routes --
@app.get("/health")
def health() -> dict:
    vlm = get_vlm()
    providers = {}
    if vlm is not None:
        for p in vlm.providers:
            providers[p.name] = _probe_provider(p)
    try:
        store_stats = get_store().stats()
    except Exception as exc:  # noqa: BLE001
        store_stats = {"error": str(exc)}
    return {
        "status": "ok",
        "version": PIPELINE_VERSION,
        "auth_enabled": bool(settings.api_key_set),
        "categories": get_registry().ids,
        "vlm_enabled": settings.vlm_enabled,
        "vlm_providers": providers,
        "store": store_stats,
    }


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    if not settings.metrics_enabled:
        raise HTTPException(404, "metrics disabled")
    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)


def _probe_provider(provider) -> dict:
    """Cheap reachability probe - never calls the model."""
    info: dict = {"model": getattr(provider, "model", None), "reachable": None, "error": None}
    try:
        if provider.name == "ollama":
            import ollama

            ollama.Client(host=provider.host, timeout=3).list()
            info["reachable"] = True
        elif provider.name == "claude":
            import os

            info["reachable"] = bool(getattr(provider, "_client", None)) and bool(
                os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or settings.anthropic_api_key
            )
            if not info["reachable"]:
                info["error"] = "no ANTHROPIC_API_KEY / INSPECTOR_ANTHROPIC_API_KEY configured"
    except Exception as exc:  # noqa: BLE001
        info["reachable"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


@app.get("/v1/categories/groups", response_model=list[GroupInfo], dependencies=[Depends(rate_limited)])
def list_category_groups() -> list[GroupInfo]:
    reg = get_registry()
    return [GroupInfo(id=g.id, name=g.name, order=g.order, category_ids=[c.id for c in reg.all(g.id)]) for g in reg.groups]


@app.get("/v1/categories", response_model=list[CategoryInfo], dependencies=[Depends(rate_limited)])
def list_categories(group: Optional[str] = Query(default=None, description="그룹 id 로 필터 (appliance | housing | commercial ...)")) -> list[CategoryInfo]:
    reg = get_registry()
    out = []
    for c in reg.all(group):
        out.append(
            CategoryInfo(
                id=c.id, name=c.name, group=c.group, group_name=reg.group_name(c.group), description=c.description.strip(),
                shots=[s.model_dump() for s in c.shots],
                defect_codes=[d.model_dump() for d in c.defect_rubric],
                before_after_pairs=[list(p) for p in c.before_after_pairs],
            )
        )
    return out


@app.post("/v1/inspections", response_model=InspectionResult, dependencies=[Depends(rate_limited)])
async def create_inspection(request: InspectionRequest) -> InspectionResult:
    return await _run(request, None)


@app.post("/v1/inspections/upload", response_model=InspectionResult, dependencies=[Depends(rate_limited)])
async def create_inspection_upload(
    job: str = Form(..., description="JobContext JSON"),
    photos: Optional[str] = Form(default=None, description="JSON array of PhotoInput metadata (photo_id must equal the uploaded filename stem or filename)"),
    options: Optional[str] = Form(default=None, description="InspectionOptions JSON"),
    files: list[UploadFile] = File(...),
) -> InspectionResult:
    try:
        job_ctx = JobContext.model_validate_json(job)
        opts = InspectionOptions.model_validate_json(options) if options else InspectionOptions()
        photo_meta = {p["photo_id"]: p for p in json.loads(photos)} if photos else {}
    except (ValidationError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(422, f"invalid form field: {exc}") from exc

    images: dict[str, bytes] = {}
    photo_inputs: list[PhotoInput] = []
    for f in files:
        name = f.filename or f"photo_{len(images)+1}"
        stem = name.rsplit(".", 1)[0]
        pid = stem if stem in photo_meta else (name if name in photo_meta else stem)
        meta = dict(photo_meta.get(pid, {}))
        meta["photo_id"] = pid
        try:
            photo_inputs.append(PhotoInput(**meta))
        except ValidationError as exc:
            raise HTTPException(422, f"invalid photo metadata for '{pid}': {exc}") from exc
        images[pid] = await f.read()

    try:
        request = InspectionRequest(job=job_ctx, photos=photo_inputs, options=opts)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await _run(request, images)


async def _run(request: InspectionRequest, images: dict[str, bytes] | None) -> InspectionResult:
    pipeline = get_pipeline()
    try:
        result = await run_in_threadpool(pipeline.run, request, images)
    except PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    record_inspection(result, mode="sync")
    log.info("inspection done", extra={"event": "inspection", "inspection_id": result.inspection_id, "job_id": result.job_id, "category": result.category, "verdict": result.verdict, "latency_ms": result.stats.get("latency_ms")})
    return result


@app.get("/v1/inspections", response_model=list[InspectionListItem], dependencies=[Depends(rate_limited)])
def list_inspections(
    limit: int = Query(default=50, ge=1, le=500),
    job_id: Optional[str] = None,
    category: Optional[str] = None,
    verdict: Optional[str] = Query(default=None, pattern="^(PASS|REVIEW|FAIL)$"),
) -> list[InspectionListItem]:
    return get_store().list(limit=limit, job_id=job_id, category=category, verdict=verdict)


@app.get("/v1/inspections/{inspection_id}", response_model=InspectionResult, dependencies=[Depends(rate_limited)])
def get_inspection(inspection_id: str) -> InspectionResult:
    result = get_store().get(inspection_id)
    if result is None:
        raise HTTPException(404, f"inspection '{inspection_id}' not found")
    return result


@app.get("/v1/inspections/{inspection_id}/photos/{photo_id}", dependencies=[Depends(rate_limited)])
def get_inspection_photo(inspection_id: str, photo_id: str) -> Response:
    ps = get_photo_store()
    if ps is None:
        raise HTTPException(404, "photo retention is disabled (INSPECTOR_PHOTO_STORE_DIR)")
    path = ps.find(inspection_id, photo_id)
    if path is None:
        raise HTTPException(404, "photo not retained for this inspection")
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "heic": "image/heic", "webp": "image/webp"}.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
    return Response(content=path.read_bytes(), media_type=media, headers={"Cache-Control": "private, max-age=300"})
