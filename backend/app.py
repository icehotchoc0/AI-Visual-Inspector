"""
AI-Visual-Inspector - field-service photo verification microservice.

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

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from backend.auth import require_api_key
from backend.categories import CategoryRegistry, load_registry
from backend.config import settings
from backend.pipeline import PIPELINE_VERSION, InspectionPipeline, PipelineError
from backend.schemas import CategoryInfo, InspectionListItem, InspectionRequest, InspectionResult, JobContext, PhotoInput, InspectionOptions
from backend.store import SQLiteStore
from backend.vlm import build_router

log = logging.getLogger("inspector")


# ------------------------------------------------------------- wiring --
@lru_cache(maxsize=1)
def get_registry() -> CategoryRegistry:
    return load_registry(settings.categories_path)


@lru_cache(maxsize=1)
def get_store() -> SQLiteStore:
    return SQLiteStore(settings.db_path)


@lru_cache(maxsize=1)
def get_vlm():
    return build_router(settings)


@lru_cache(maxsize=1)
def get_pipeline() -> InspectionPipeline:
    return InspectionPipeline(get_registry(), settings, vlm=get_vlm(), store=get_store())


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.api_key_set:
        log.warning("INSPECTOR_API_KEYS is empty - API authentication is DISABLED. Set it before exposing this service.")
    reg = get_registry()
    vlm = get_vlm()
    log.info("categories: %s | vlm providers: %s | db: %s", reg.ids, vlm.names if vlm else "disabled", settings.db_path)
    yield


app = FastAPI(
    title="AI-Visual-Inspector",
    description="현장 AS/설치 작업 사진 검증: 사진 품질·부정 제출·체크리스트는 규칙으로, 시각적 하자는 VLM(Claude → Ollama 폴백)으로 분석하고 규칙 엔진이 PASS/REVIEW/FAIL을 판정합니다.",
    version=PIPELINE_VERSION,
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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


@app.get("/v1/categories", response_model=list[CategoryInfo], dependencies=[Depends(require_api_key)])
def list_categories() -> list[CategoryInfo]:
    out = []
    for c in get_registry().all():
        out.append(
            CategoryInfo(
                id=c.id, name=c.name, group=c.group, description=c.description.strip(),
                shots=[s.model_dump() for s in c.shots],
                defect_codes=[d.model_dump() for d in c.defect_rubric],
                before_after_pairs=[list(p) for p in c.before_after_pairs],
            )
        )
    return out


@app.post("/v1/inspections", response_model=InspectionResult, dependencies=[Depends(require_api_key)])
async def create_inspection(request: InspectionRequest) -> InspectionResult:
    return await _run(request, None)


@app.post("/v1/inspections/upload", response_model=InspectionResult, dependencies=[Depends(require_api_key)])
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
        return await run_in_threadpool(pipeline.run, request, images)
    except PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/v1/inspections", response_model=list[InspectionListItem], dependencies=[Depends(require_api_key)])
def list_inspections(
    limit: int = Query(default=50, ge=1, le=500),
    job_id: Optional[str] = None,
    category: Optional[str] = None,
    verdict: Optional[str] = Query(default=None, pattern="^(PASS|REVIEW|FAIL)$"),
) -> list[InspectionListItem]:
    return get_store().list(limit=limit, job_id=job_id, category=category, verdict=verdict)


@app.get("/v1/inspections/{inspection_id}", response_model=InspectionResult, dependencies=[Depends(require_api_key)])
def get_inspection(inspection_id: str) -> InspectionResult:
    result = get_store().get(inspection_id)
    if result is None:
        raise HTTPException(404, f"inspection '{inspection_id}' not found")
    return result
