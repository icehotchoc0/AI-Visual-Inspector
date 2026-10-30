"""
Prompt + JSON-schema builders for the two VLM tasks.

Both prompts are built from the category config so that adding a category
(or a defect code) in YAML changes what the model is asked without any
code change. Output is Korean; field names are English for the API.
"""
from __future__ import annotations

from backend.categories import Category

SYSTEM_PROMPT = """당신은 현장 서비스(가전 설치·수리, 주거 시설 시공) 작업 사진을 검토하는 품질 검사 보조원입니다.

역할과 한계:
- 당신은 사진에 보이는 것을 정확하게 기술하고 분류합니다. 합격/불합격 판정은 별도의 규칙 엔진이 내리며, 당신의 역할이 아닙니다.
- 보이지 않는 것을 추측하지 마십시오. 확실하지 않으면 confidence 를 낮게 주십시오.
- 하자(defect)는 사진에서 실제로 식별 가능한 근거(evidence)가 있을 때만 보고하십시오. 근거 없는 추정은 보고하지 마십시오.
- 사진이 작업 부위를 보여주지 않거나 무관한 장면이면 그대로 기술하십시오.
- 모든 자연어 필드는 한국어로, 간결하게 작성하십시오.
- 반드시 요구된 JSON 스키마에 맞는 JSON 객체만 출력하십시오."""


def _shot_catalogue(category: Category) -> str:
    lines = []
    for s in category.shots:
        req = "필수" if s.required else "선택"
        phase = {"before": "작업 전", "after": "작업 후", "any": "단계 무관"}[s.phase]
        lines.append(f"- {s.id}: {s.name} ({req}, {phase}) - {s.description}")
    lines.append("- other: 위 어느 컷에도 해당하지 않음 (무관한 장면, 작업 부위가 보이지 않음 등)")
    return "\n".join(lines)


def _defect_catalogue(category: Category) -> str:
    lines = [f"- {d.code}: {d.name} - {d.description} (기본 심각도: {d.default_severity})" for d in category.defect_rubric]
    lines.append("- other: 목록에 없지만 명백히 보이는 시공/수리 하자 (title 에 무엇인지 기재)")
    return "\n".join(lines)


def category_context(category: Category) -> str:
    """Static per-category block: identical for every photo of a category, so
    it is sent as a cacheable system block (prompt caching) and only the
    short per-photo part below changes between requests."""
    return f"""작업 종류: {category.name} ({category.group})
작업 설명: {category.description.strip()}

[이 작업에서 가능한 사진 컷]
{_shot_catalogue(category)}

[이 작업에서 확인해야 할 시각적 하자]
{_defect_catalogue(category)}

사진 1장을 분석할 때의 지시:
1. 이 사진이 위 컷 목록 중 어디에 해당하는지 shot_type 으로 분류하고 confidence 를 주십시오. 기사가 신고한 컷은 참고만 하고, 사진 내용이 다르면 사진 내용을 따르십시오.
2. 작업 대상(설비/부위)이 사진에 실제로 보이는지 work_area_visible 로 표시하십시오.
3. 장면을 한국어 1~2문장으로 scene_description 에 기술하십시오.
4. 사진에서 식별 가능한 하자를 defects 배열에 보고하십시오. 각 하자는 위 하자 코드 중 하나(없으면 other)를 쓰고, 어디에(location) 무엇이 보이는지(evidence) 구체적으로 적으십시오. 하자가 없으면 빈 배열을 반환하십시오.
5. 사진 품질이나 구도 때문에 판단이 제한되면 visibility_note 에 적으십시오."""


def analyze_photo_prompt(
    category: Category,
    declared_shot_type: str | None,
    phase: str | None,
    technician_notes: str | None,
) -> str:
    """Per-photo (volatile) part. Goes in the user message after the image."""
    declared = (
        f"기사가 신고한 컷: {declared_shot_type} ({category.shot(declared_shot_type).name if category.shot(declared_shot_type) else '알 수 없는 컷'})"
        if declared_shot_type
        else "기사가 신고한 컷: 없음"
    )
    phase_txt = f"기사가 신고한 단계: {phase}" if phase else "기사가 신고한 단계: 없음"
    notes = f"\n기사 메모: {technician_notes.strip()}" if technician_notes else ""
    return f"""{declared}
{phase_txt}{notes}

위 사진을 시스템 지시에 따라 분석하고 JSON 으로 답하십시오."""


def analyze_photo_schema(category: Category) -> dict:
    shot_enum = category.shot_ids + ["other"]
    defect_enum = [d.code for d in category.defect_rubric] + ["other"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["shot_type", "shot_confidence", "work_area_visible", "scene_description", "defects", "visibility_note"],
        "properties": {
            "shot_type": {"type": "string", "enum": shot_enum},
            "shot_confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "work_area_visible": {"type": "boolean"},
            "scene_description": {"type": "string"},
            "visibility_note": {"type": "string", "description": "빈 문자열 허용"},
            "defects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "title", "description", "severity", "confidence", "location", "evidence"],
                    "properties": {
                        "code": {"type": "string", "enum": defect_enum},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "location": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
        },
    }


def compare_before_after_prompt(category: Category, before_shot: str, after_shot: str) -> str:
    b = category.shot(before_shot)
    a = category.shot(after_shot)
    return f"""작업 종류: {category.name}
작업 설명: {category.description.strip()}

첫 번째 사진은 '{b.name if b else before_shot}' (작업 전), 두 번째 사진은 '{a.name if a else after_shot}' (작업 후)로 제출되었습니다.

지시:
1. 두 사진이 같은 장소/같은 설비를 찍은 것인지 same_location 으로 판단하고 confidence 를 주십시오. 벽지·타일·주변 가구·창문·배관 경로 등 고정된 배경 요소를 근거로 삼으십시오.
2. 작업 후 사진에서 해당 작업이 실제로 수행된 흔적(새 설비, 교체 부품, 마감 등)이 보이는지 work_completed_visible 로 표시하십시오.
3. 전후 변화를 changes_description 에 한국어 1~3문장으로 기술하십시오.
4. 전후 비교에서 드러나는 우려 사항(예: 작업 전 없던 손상이 생김, 배경이 달라 다른 현장으로 보임)을 concerns 배열에 적으십시오. 없으면 빈 배열."""


COMPARE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["same_location", "confidence", "work_completed_visible", "changes_description", "concerns"],
    "properties": {
        "same_location": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "work_completed_visible": {"type": "boolean"},
        "changes_description": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
}
