"""
Rule-based verdict. THE decision layer.

Input : findings from the deterministic checks and the VLM (descriptive,
        with confidences) plus the category's verdict policy.
Output: PASS | REVIEW | FAIL with ordered, human-readable Korean reasons.

The VLM never sets the verdict. Its defect findings only matter once they
clear the category's confidence threshold, and even then a rule - not the
model - maps severity to the outcome. Every rule is explicit below so an
operator can read why a job failed and tune the policy in YAML.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.categories import Category
from backend.schemas import Finding, PhotoResult, Verdict

# Fraud codes that, when confident, are grounds for FAIL on their own.
_FAIL_FRAUD_CODES = {"duplicate_in_job", "reused_from_other_job", "gps_spread", "gps_far_from_site"}
# Fraud/consistency codes that call for a human look.
_REVIEW_FRAUD_CODES = {"similar_to_other_job", "edited_with_software", "captured_outside_schedule", "capture_time_spread", "near_duplicate_in_job"}


@dataclass
class Decision:
    verdict: Verdict
    reasons: list[str]
    summary: str
    fail_reasons: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _sev_label(sev: str) -> str:
    return {"high": "심각", "medium": "보통", "low": "경미"}[sev]


def decide(
    category: Category,
    photos: list[PhotoResult],
    job_findings: list[Finding],
    *,
    vlm_requested: bool,
    vlm_available: bool,
) -> Decision:
    policy = category.verdict
    fail: list[str] = []
    review: list[str] = []
    notes: list[str] = []

    all_findings: list[Finding] = list(job_findings)
    for p in photos:
        all_findings.extend(p.findings)

    # ---- 1. Fraud / integrity ------------------------------------------------
    for f in all_findings:
        if f.kind != "fraud":
            continue
        if f.code in _FAIL_FRAUD_CODES and f.severity == "high" and f.confidence >= 0.8:
            fail.append(f"부정 제출 의심: {f.message}")
        elif f.code in _REVIEW_FRAUD_CODES or f.severity in ("medium", "high"):
            review.append(f"확인 필요: {f.message}")

    # ---- 2. Checklist --------------------------------------------------------
    missing = [f for f in all_findings if f.code == "missing_required_shot"]
    if missing:
        names = ", ".join(f"'{category.shot(f.data.get('shot_id')).name}'" if category.shot(f.data.get("shot_id", "")) else f.data.get("shot_id", "?") for f in missing)
        msg = f"필수 컷 {len(missing)}개 누락: {names}"
        (fail if policy.missing_required_shot == "fail" else review).append(msg)
    for f in all_findings:
        if f.code == "before_after_incomplete":
            review.append(f.message)

    # ---- 3. Before/after consistency (VLM-derived, rule-gated) ---------------
    for f in all_findings:
        if f.kind != "consistency":
            continue
        if f.code == "before_after_different_location" and f.confidence >= 0.8:
            fail.append(f"전/후 사진이 서로 다른 장소로 판단됨: {f.message}")
        elif f.code in ("before_after_different_location", "work_not_visible_after", "shot_type_mismatch"):
            review.append(f.message)

    # ---- 4. Visual defects (VLM, confidence-gated) ---------------------------
    confident_defects = [f for f in all_findings if f.kind == "defect" and f.confidence >= policy.min_defect_confidence]
    weak_defects = [f for f in all_findings if f.kind == "defect" and 0.4 <= f.confidence < policy.min_defect_confidence and f.severity in ("medium", "high")]
    high = [f for f in confident_defects if f.severity == "high"]
    medium = [f for f in confident_defects if f.severity == "medium"]
    low = [f for f in confident_defects if f.severity == "low"]
    if high:
        fail.append("심각한 시공/수리 하자 " + f"{len(high)}건: " + "; ".join(_defect_line(f) for f in high))
    if medium:
        review.append("보통 수준 하자 " + f"{len(medium)}건: " + "; ".join(_defect_line(f) for f in medium))
    if weak_defects:
        review.append("확신도가 낮은 하자 의심 " + f"{len(weak_defects)}건 (육안 확인 권장): " + "; ".join(_defect_line(f) for f in weak_defects))
    if low:
        notes.append("경미한 지적 " + f"{len(low)}건: " + "; ".join(_defect_line(f) for f in low))

    # ---- 5. Anomaly detector (supporting evidence only) ----------------------
    for f in all_findings:
        if f.kind == "anomaly" and f.severity in ("medium", "high"):
            review.append(f"이상 탐지 모델이 비정상 패턴을 감지했습니다: {f.message}")

    # ---- 6. Photo quality ----------------------------------------------------
    rejected = [p for p in photos if p.status == "REJECTED"]
    if rejected:
        notes.append(f"품질 문제로 반려된 사진 {len(rejected)}장 (재촬영 권장): " + ", ".join(p.photo_id for p in rejected))

    # ---- 7. VLM availability -------------------------------------------------
    if vlm_requested and not vlm_available:
        review.append("VLM 공급자(Claude/Ollama)에 모두 연결할 수 없어 시각적 하자 검사가 수행되지 않았습니다. 수동 검토가 필요합니다.")
    elif not vlm_requested:
        notes.append("요청에 따라 VLM 하자 검사를 생략했습니다.")

    # ---- Resolve -------------------------------------------------------------
    if fail:
        verdict: Verdict = "FAIL"
    elif review:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    reasons = fail + review + notes
    summary = _summary(verdict, fail, review, notes, category)
    return Decision(verdict=verdict, reasons=reasons, summary=summary, fail_reasons=fail, review_reasons=review, notes=notes)


def _defect_line(f: Finding) -> str:
    where = f" @ {f.location_hint}" if f.location_hint else ""
    who = f" [{f.photo_id}]" if f.photo_id else ""
    return f"{f.message}{where}{who} ({_sev_label(f.severity)}, 확신도 {f.confidence:.2f})"


def _summary(verdict: Verdict, fail: list[str], review: list[str], notes: list[str], category: Category) -> str:
    if verdict == "FAIL":
        head = fail[0]
        more = f" 외 {len(fail) - 1}건" if len(fail) > 1 else ""
        return f"불합격 - {head.split(':')[0]}{more}"
    if verdict == "REVIEW":
        head = review[0]
        more = f" 외 {len(review) - 1}건" if len(review) > 1 else ""
        return f"검토 필요 - {head.split(':')[0]}{more}"
    tail = f" ({notes[0].split(':')[0]})" if notes else ""
    return f"합격 - {category.name} 필수 컷 확인 완료, 심각한 하자·부정 제출 징후 없음{tail}"


def photo_status(quality_passed: bool, findings: list[Finding], min_conf: float) -> str:
    if not quality_passed:
        return "REJECTED"
    for f in findings:
        if f.kind in ("defect", "fraud", "consistency", "anomaly") and f.severity in ("medium", "high") and f.confidence >= min(min_conf, 0.6):
            return "FLAGGED"
    return "OK"
