"""
Checklist coverage: did the technician submit every required shot, and do
before/after pairs both exist?

Shot identity is resolved per photo by backend/pipeline.py (declared shot
type cross-checked against the VLM's classification). This module only
reasons about the resolved shot types, so it is pure and fully testable.
"""
from __future__ import annotations

from collections import defaultdict

from backend.categories import Category
from backend.schemas import ChecklistItem, Finding


def evaluate_checklist(
    category: Category,
    resolved: dict[str, str | None],
    rejected_photo_ids: set[str] | None = None,
) -> tuple[list[ChecklistItem], list[Finding]]:
    """
    resolved: photo_id -> shot id (or None when unknown / "other").
    rejected_photo_ids: photos that failed quality and therefore cannot count
    as evidence for a shot.
    """
    rejected_photo_ids = rejected_photo_ids or set()
    by_shot: dict[str, list[str]] = defaultdict(list)
    for photo_id, shot_id in resolved.items():
        if shot_id and photo_id not in rejected_photo_ids:
            by_shot[shot_id].append(photo_id)

    items: list[ChecklistItem] = []
    findings: list[Finding] = []
    for shot in category.shots:
        photo_ids = by_shot.get(shot.id, [])
        satisfied = bool(photo_ids)
        items.append(ChecklistItem(shot_id=shot.id, name=shot.name, required=shot.required, satisfied=satisfied, photo_ids=photo_ids))
        if shot.required and not satisfied:
            only_rejected = [pid for pid, sid in resolved.items() if sid == shot.id and pid in rejected_photo_ids]
            if only_rejected:
                msg = f"필수 컷 '{shot.name}'에 해당하는 사진이 품질 문제로 반려되어 증빙이 없습니다. 다시 촬영해 주세요."
            else:
                msg = f"필수 컷 '{shot.name}'이(가) 누락되었습니다."
            findings.append(
                Finding(
                    code="missing_required_shot",
                    kind="checklist",
                    source="checklist",
                    severity="high",
                    message=msg,
                    related_photo_ids=only_rejected,
                    data={"shot_id": shot.id},
                )
            )

    for before_id, after_id in category.before_after_pairs:
        has_before, has_after = bool(by_shot.get(before_id)), bool(by_shot.get(after_id))
        if has_before != has_after:
            missing = before_id if not has_before else after_id
            missing_name = (category.shot(missing).name if category.shot(missing) else missing)
            findings.append(
                Finding(
                    code="before_after_incomplete",
                    kind="checklist",
                    source="checklist",
                    severity="medium",
                    message=f"전/후 비교 컷 중 '{missing_name}'이(가) 없어 작업 전후 변화를 확인할 수 없습니다.",
                    data={"pair": [before_id, after_id], "missing": missing},
                )
            )

    unknown = [pid for pid, sid in resolved.items() if sid is None and pid not in rejected_photo_ids]
    for pid in unknown:
        findings.append(
            Finding(
                code="unclassified_photo",
                kind="checklist",
                source="checklist",
                severity="low",
                confidence=0.8,
                photo_id=pid,
                message=f"사진 '{pid}'이(가) 이 작업의 어떤 컷에도 해당하지 않습니다 (작업 부위 미촬영 또는 무관한 사진).",
            )
        )
    return items, findings
