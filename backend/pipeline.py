"""
Inspection pipeline - orchestrates one job (N photos) end to end.

    decode -> quality -> EXIF/hash -> [VLM per photo] -> fraud (job) ->
    checklist -> [VLM before/after] -> [anomaly plugin] -> verdict -> store

Deterministic steps always run. VLM steps run only on photos that passed
quality, and only when a provider is reachable; their output is turned into
Finding objects with confidences and handed to backend/verdict.py, which
alone decides PASS/REVIEW/FAIL.
"""
from __future__ import annotations

import base64
import io
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from PIL import Image, ImageOps

from backend.categories import Category, CategoryRegistry
from backend.checks.checklist import evaluate_checklist
from backend.checks.fraud import build_meta, run_fraud_checks
from backend.checks.quality import assess_quality
from backend.schemas import (
    Finding,
    InspectionRequest,
    InspectionResult,
    PhotoAnalysis,
    PhotoInput,
    PhotoResult,
)
from backend.verdict import decide, photo_status
from backend.vlm.base import VLMRequest, VLMUnavailable, prepare_image
from backend.vlm.prompts import COMPARE_SCHEMA, SYSTEM_PROMPT, analyze_photo_prompt, analyze_photo_schema, compare_before_after_prompt

log = logging.getLogger(__name__)

PIPELINE_VERSION = "2.0.0"


class PipelineError(ValueError):
    """Client-caused problem (bad image, unknown category) - maps to HTTP 4xx."""


class InspectionPipeline:
    def __init__(
        self,
        registry: CategoryRegistry,
        settings,
        vlm=None,
        store=None,
        *,
        anomaly_enabled: bool = True,
        max_workers: int = 4,
    ):
        self.registry = registry
        self.settings = settings
        self.vlm = vlm
        self.store = store
        self.anomaly_enabled = anomaly_enabled
        self.max_workers = max_workers

    # ------------------------------------------------------------------ API --
    def run(self, request: InspectionRequest, images: dict[str, bytes] | None = None) -> InspectionResult:
        t0 = time.perf_counter()
        images = dict(images or {})
        if request.job.category not in self.registry:
            raise PipelineError(f"Unknown category '{request.job.category}'. Known: {self.registry.ids}")
        category = self.registry.get(request.job.category)
        if len(request.photos) > self.settings.max_photos_per_job:
            raise PipelineError(f"Too many photos: {len(request.photos)} > {self.settings.max_photos_per_job}")

        # ---- decode ----
        decoded: dict[str, tuple[Image.Image, bytes]] = {}
        seen_ids: set[str] = set()
        for p in request.photos:
            if p.photo_id in seen_ids:
                raise PipelineError(f"Duplicate photo_id '{p.photo_id}' in request")
            seen_ids.add(p.photo_id)
            raw = images.get(p.photo_id)
            if raw is None and p.image_base64:
                try:
                    raw = base64.b64decode(p.image_base64, validate=False)
                except Exception as exc:  # noqa: BLE001
                    raise PipelineError(f"photo '{p.photo_id}': invalid base64") from exc
            if not raw:
                raise PipelineError(f"photo '{p.photo_id}': no image data (provide image_base64 or a multipart file)")
            if len(raw) > self.settings.max_image_bytes:
                raise PipelineError(f"photo '{p.photo_id}': image too large ({len(raw)} bytes)")
            try:
                img = Image.open(io.BytesIO(raw))
                img.load()
                img = ImageOps.exif_transpose(img) or img
            except Exception as exc:  # noqa: BLE001
                raise PipelineError(f"photo '{p.photo_id}': cannot decode image ({type(exc).__name__})") from exc
            decoded[p.photo_id] = (img, raw)

        # ---- per-photo deterministic ----
        photo_inputs = {p.photo_id: p for p in request.photos}
        qualities, metas, findings_by_photo = {}, {}, {}
        for pid, (img, raw) in decoded.items():
            q, qf = assess_quality(img, pid, category.quality)
            qualities[pid] = q
            metas[pid] = build_meta(img, raw)
            findings_by_photo[pid] = list(qf)
        rejected = {pid for pid, q in qualities.items() if not q.passed}

        # ---- VLM per photo (parallel) ----
        vlm_requested = bool(request.options.run_vlm)
        vlm_available = True
        analyses: dict[str, PhotoAnalysis] = {pid: PhotoAnalysis(provider="none") for pid in decoded}
        vlm_calls, vlm_errors, tokens_in, tokens_out = 0, 0, 0, 0
        provider_used: str | None = None
        candidates = [pid for pid in decoded if pid not in rejected]
        if vlm_requested and self.vlm is not None and candidates:
            def _one(pid: str):
                return pid, self._analyze_photo(category, photo_inputs[pid], decoded[pid][0], request.job.notes)

            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(candidates))) as pool:
                for pid, outcome in pool.map(_one, candidates):
                    vlm_calls += 1
                    if outcome is None:
                        vlm_errors += 1
                        continue
                    analysis, extra_findings, usage = outcome
                    analyses[pid] = analysis
                    findings_by_photo[pid].extend(extra_findings)
                    provider_used = provider_used or analysis.provider
                    tokens_in += usage.get("input_tokens") or 0
                    tokens_out += usage.get("output_tokens") or 0
            if vlm_calls and vlm_errors == vlm_calls:
                vlm_available = False
        elif vlm_requested and self.vlm is None:
            vlm_available = False

        # ---- resolve shot types ----
        resolved: dict[str, str | None] = {}
        for pid in decoded:
            declared = photo_inputs[pid].declared_shot_type
            declared = declared if (declared and category.shot(declared)) else None
            if declared is None and photo_inputs[pid].declared_shot_type:
                findings_by_photo[pid].append(
                    Finding(code="unknown_declared_shot", kind="info", source="system", severity="low", photo_id=pid,
                            message=f"신고된 컷 '{photo_inputs[pid].declared_shot_type}'은(는) 카테고리 '{category.id}'에 정의되지 않은 컷입니다.")
                )
            a = analyses[pid]
            vlm_shot = a.shot_type if (a.shot_type and a.shot_type != "other" and a.shot_confidence >= 0.5) else None
            if pid in rejected:
                resolved[pid] = declared
            elif vlm_shot is None and a.provider not in (None, "none") and a.shot_type == "other" and a.shot_confidence >= 0.7:
                # VLM is confident the photo matches none of the shots: it is "other" even if declared.
                resolved[pid] = None
                if declared:
                    findings_by_photo[pid].append(
                        Finding(code="shot_type_mismatch", kind="consistency", source="vlm", severity="medium", confidence=a.shot_confidence, photo_id=pid,
                                message=f"사진 '{pid}'은(는) '{category.shot(declared).name}' 컷으로 신고되었지만 해당 장면이 보이지 않습니다.",
                                evidence=a.scene_description)
                    )
            elif vlm_shot and declared and vlm_shot != declared and a.shot_confidence >= 0.7:
                resolved[pid] = vlm_shot
                findings_by_photo[pid].append(
                    Finding(code="shot_type_mismatch", kind="consistency", source="vlm", severity="medium", confidence=a.shot_confidence, photo_id=pid,
                            message=f"사진 '{pid}'은(는) '{category.shot(declared).name}' 컷으로 신고되었지만 '{category.shot(vlm_shot).name}' 컷으로 보입니다.",
                            evidence=a.scene_description, data={"declared": declared, "vlm": vlm_shot})
                )
            else:
                resolved[pid] = declared or vlm_shot

        # ---- anomaly plugin (fixed-view shots only) ----
        anomaly_scores: dict[str, float] = {}
        if self.anomaly_enabled and category.anomaly.enabled:
            for pid in candidates:
                if resolved.get(pid) in category.anomaly.shot_types:
                    score, f = self._anomaly(category, pid, decoded[pid][0])
                    if score is not None:
                        anomaly_scores[pid] = score
                    if f:
                        findings_by_photo[pid].append(f)

        # ---- job-level deterministic ----
        history = self.store if (request.options.run_fraud_history and self.store is not None) else None
        job_findings: list[Finding] = []
        fraud_findings = run_fraud_checks(metas, request.job, category.fraud, history=history)
        for f in fraud_findings:
            (findings_by_photo[f.photo_id] if f.photo_id in findings_by_photo else job_findings).append(f)

        checklist, cl_findings = evaluate_checklist(category, resolved, rejected)
        for f in cl_findings:
            (findings_by_photo[f.photo_id] if f.photo_id in findings_by_photo else job_findings).append(f)

        # ---- before/after comparison (VLM) ----
        if vlm_requested and self.vlm is not None and vlm_available:
            by_shot: dict[str, list[str]] = {}
            for pid, sid in resolved.items():
                if sid and pid not in rejected:
                    by_shot.setdefault(sid, []).append(pid)
            for before_id, after_id in category.before_after_pairs:
                if by_shot.get(before_id) and by_shot.get(after_id):
                    b_pid, a_pid = by_shot[before_id][0], by_shot[after_id][0]
                    cmp_findings, usage = self._compare(category, before_id, after_id, b_pid, a_pid, decoded[b_pid][0], decoded[a_pid][0])
                    vlm_calls += 1
                    tokens_in += usage.get("input_tokens") or 0
                    tokens_out += usage.get("output_tokens") or 0
                    job_findings.extend(cmp_findings)

        # ---- assemble photo results ----
        photos: list[PhotoResult] = []
        for p in request.photos:
            pid = p.photo_id
            pf = findings_by_photo[pid]
            status = photo_status(qualities[pid].passed, pf, category.verdict.min_defect_confidence)
            thumb = None
            if request.options.return_thumbnails:
                thumb = _thumbnail_b64(decoded[pid][0])
            photos.append(
                PhotoResult(
                    photo_id=pid, status=status, declared_shot_type=p.declared_shot_type, resolved_shot_type=resolved.get(pid),
                    phase=p.phase, quality=qualities[pid], meta=metas[pid], analysis=analyses[pid], findings=pf,
                    anomaly_score=anomaly_scores.get(pid), thumbnail_png_base64=thumb,
                )
            )

        decision = decide(category, photos, job_findings, vlm_requested=vlm_requested, vlm_available=vlm_available)

        result = InspectionResult(
            inspection_id=f"insp_{uuid.uuid4().hex[:16]}",
            job_id=request.job.job_id,
            category=category.id,
            category_name=category.name,
            verdict=decision.verdict,
            verdict_reasons=decision.reasons,
            summary=decision.summary,
            photos=photos,
            checklist=checklist,
            job_findings=job_findings,
            stats={
                "num_photos": len(photos),
                "num_rejected": len(rejected),
                "num_flagged": sum(1 for p in photos if p.status == "FLAGGED"),
                "vlm_calls": vlm_calls,
                "vlm_errors": vlm_errors,
                "vlm_input_tokens": tokens_in,
                "vlm_output_tokens": tokens_out,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            },
            vlm_provider_used=provider_used,
            created_at=datetime.now(timezone.utc),
            pipeline_version=PIPELINE_VERSION,
        )
        if self.store is not None:
            try:
                self.store.save(result)
            except Exception:  # noqa: BLE001 - persistence failure must not lose the verdict
                log.exception("failed to persist inspection %s", result.inspection_id)
        return result

    # ------------------------------------------------------------- helpers --
    def _analyze_photo(self, category: Category, photo: PhotoInput, image: Image.Image, notes: str | None):
        req = VLMRequest(
            system=SYSTEM_PROMPT,
            prompt=analyze_photo_prompt(category, photo.declared_shot_type, photo.phase, notes),
            images=[prepare_image(image, self.settings.vlm_image_max_side)],
            schema=analyze_photo_schema(category),
            task="analyze_photo",
        )
        try:
            resp = self.vlm.complete_json(req)
        except VLMUnavailable as exc:
            log.warning("VLM unavailable for photo %s: %s", photo.photo_id, exc.errors)
            return None
        d = resp.data
        analysis = PhotoAnalysis(
            shot_type=d.get("shot_type"),
            shot_confidence=float(d.get("shot_confidence", 0.0)),
            work_area_visible=d.get("work_area_visible"),
            scene_description=d.get("scene_description"),
            provider=resp.provider,
            model=resp.model,
            latency_ms=round(resp.latency_ms, 1),
        )
        findings: list[Finding] = []
        for item in d.get("defects", []) or []:
            code = item.get("code", "other")
            spec = category.defect(code)
            severity = item.get("severity") or (spec.default_severity if spec else "medium")
            title = item.get("title") or (spec.name if spec else "기타 하자")
            findings.append(
                Finding(
                    code=code if spec else f"other:{title[:40]}",
                    kind="defect",
                    source="vlm",
                    severity=severity,
                    confidence=max(0.0, min(1.0, float(item.get("confidence", 0.5)))),
                    photo_id=photo.photo_id,
                    message=f"{title}: {item.get('description', '').strip()}".rstrip(": "),
                    location_hint=item.get("location") or None,
                    evidence=item.get("evidence") or None,
                    data={"provider": resp.provider},
                )
            )
        if d.get("work_area_visible") is False and analysis.shot_type != "other":
            findings.append(
                Finding(code="work_area_not_visible", kind="consistency", source="vlm", severity="low", confidence=analysis.shot_confidence,
                        photo_id=photo.photo_id, message=f"사진 '{photo.photo_id}'에 작업 대상 부위가 명확히 보이지 않습니다.", evidence=d.get("visibility_note") or None)
            )
        return analysis, findings, resp.usage

    def _compare(self, category: Category, before_id: str, after_id: str, b_pid: str, a_pid: str, b_img: Image.Image, a_img: Image.Image):
        b = prepare_image(b_img, self.settings.vlm_image_max_side)
        a = prepare_image(a_img, self.settings.vlm_image_max_side)
        b.label, a.label = "[사진 1: 작업 전]", "[사진 2: 작업 후]"
        req = VLMRequest(system=SYSTEM_PROMPT, prompt=compare_before_after_prompt(category, before_id, after_id), images=[b, a], schema=COMPARE_SCHEMA, task="compare_before_after")
        try:
            resp = self.vlm.complete_json(req)
        except VLMUnavailable as exc:
            log.warning("VLM unavailable for before/after compare: %s", exc.errors)
            return [], {}
        d = resp.data
        findings: list[Finding] = []
        conf = float(d.get("confidence", 0.0))
        related = [b_pid, a_pid]
        if d.get("same_location") is False:
            findings.append(
                Finding(code="before_after_different_location", kind="consistency", source="vlm", severity="high", confidence=conf, related_photo_ids=related,
                        message=f"작업 전 사진 '{b_pid}'과 작업 후 사진 '{a_pid}'의 배경이 일치하지 않습니다.", evidence=d.get("changes_description") or None)
            )
        elif d.get("work_completed_visible") is False:
            findings.append(
                Finding(code="work_not_visible_after", kind="consistency", source="vlm", severity="medium", confidence=conf, related_photo_ids=related,
                        message=f"작업 후 사진 '{a_pid}'에서 작업이 수행된 흔적이 확인되지 않습니다.", evidence=d.get("changes_description") or None)
            )
        for concern in d.get("concerns", []) or []:
            if concern and concern.strip():
                findings.append(
                    Finding(code="before_after_concern", kind="info", source="vlm", severity="low", confidence=conf, related_photo_ids=related, message=concern.strip())
                )
        if d.get("changes_description"):
            findings.append(
                Finding(code="before_after_summary", kind="info", source="vlm", severity="low", confidence=conf, related_photo_ids=related, message=d["changes_description"].strip())
            )
        return findings, resp.usage

    def _anomaly(self, category: Category, pid: str, image: Image.Image):
        try:
            from backend.detector import ModelNotTrainedError, get_detector

            det = get_detector(category.anomaly.model_key, self.settings.ad_backend)
            res = det.predict(image)
        except Exception as exc:  # noqa: BLE001 - plugin is best-effort
            log.info("anomaly plugin skipped for %s/%s: %s", category.id, pid, exc)
            return None, None
        f = None
        if res.is_defective:
            f = Finding(code="anomaly_detected", kind="anomaly", source="anomaly_detector", severity="medium",
                        confidence=max(0.0, min(1.0, res.anomaly_score)), photo_id=pid,
                        message=f"사진 '{pid}'이(가) 정상 사진 분포에서 벗어납니다 (score={res.anomaly_score:.3f}).",
                        data={"anomaly_score": res.anomaly_score, "backend": det.backend, "model_key": det.category})
        return res.anomaly_score, f


def _thumbnail_b64(image: Image.Image, size: int = 256) -> str:
    img = image.convert("RGB").copy()
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
