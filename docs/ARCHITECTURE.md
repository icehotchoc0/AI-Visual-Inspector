# 아키텍처

## 1. 한 작업의 처리 흐름

```
InspectionRequest (job, photos[], options)
  │
  ├─ decode: base64/multipart → PIL, EXIF 회전 보정, 크기/개수 제한                  pipeline.py
  │
  ├─ 사진별 결정적 검사 (항상)
  │     quality.assess_quality   → PhotoQuality, findings(blurry/too_dark/...)      checks/quality.py
  │     fraud.build_meta         → PhotoMeta(dHash, sha256, EXIF 시각·GPS·소프트웨어)  checks/fraud.py
  │     rejected = 품질 'high' 지적이 있는 사진
  │
  ├─ 사진별 VLM 분석 (품질 통과 사진만, ThreadPool 병렬)                               vlm/*
  │     prompts.analyze_photo_prompt(category, declared, phase, notes) + JSON 스키마
  │     router: claude → ollama (오류·거절·스키마 위반 시 폴백), 스키마 재검증
  │     → PhotoAnalysis(shot_type, confidence, work_area_visible, scene) + defect findings
  │
  ├─ 컷 해석 (resolved_shot_type)
  │     VLM 확신 ≥0.7 이고 신고와 다르면 VLM 컷 채택 + shot_type_mismatch(consistency)
  │     VLM 불가면 신고 컷 그대로 (판정은 REVIEW 로 떨어짐)
  │
  ├─ [선택] 이상 탐지 플러그인: category.anomaly.shot_types 에 해당하는 컷만               detector.py
  │
  ├─ 작업 단위 결정적 검사
  │     fraud.run_fraud_checks: 작업 내 중복, 이전 작업 재사용(store), GPS 분산/현장 거리,
  │                             EXIF 시각 vs 예정 시간, 촬영 시각 분산, 편집 소프트웨어
  │     checklist.evaluate_checklist: 필수 컷 커버리지(반려 사진 제외), 전후 쌍, 미분류 사진
  │
  ├─ [VLM] 전/후 쌍 비교: same_location / work_completed_visible / concerns
  │
  ├─ verdict.decide → PASS/REVIEW/FAIL + 한국어 근거 (유일한 판정 지점)                  verdict.py
  │
  └─ store.save → inspections + photo_hashes (다음 작업의 재사용 검사 자료)              store.py
```

## 2. 책임 분리

| 층 | 모듈 | 결정적? | 실패 시 |
|---|---|---|---|
| 검사 입력/출력 계약 | `schemas.py`, `categories.py` | - | 422/400 |
| 품질·부정·체크리스트 | `checks/*` | ✔ | 없음 (순수 함수) |
| VLM 공급자 | `vlm/claude_provider.py`, `vlm/ollama_provider.py` | ✘ | `VLMError` → 라우터가 다음 공급자 |
| 라우팅/스키마 검증 | `vlm/router.py` | ✔ | `VLMUnavailable` → 파이프라인이 `vlm_available=False` 로 기록 |
| 판정 | `verdict.py` | ✔ | 없음 |
| 저장 | `store.py` | ✔ | 로그만 (판정은 반환) |

VLM 이 하는 일은 세 가지뿐입니다: 컷 분류, 장면 기술, 루브릭 기반 하자 나열(확신도 포함). 이 출력은
JSON 스키마(공급자 측 강제 + 라우터 재검증)를 통과해야 하고, 그 뒤 `verdict.py`의 규칙만이 판정을 만듭니다.
프롬프트에도 "판정은 당신의 역할이 아니다"를 명시했지만, 코드 구조상 VLM 이 판정 필드에 손댈 경로 자체가 없습니다.

## 3. 카테고리 설정이 흐르는 곳

`config/categories/<그룹>/<id>.yaml` 의 한 카테고리는 다음에 동시에 쓰입니다 (`_defaults.yaml` 임계값과 필드 단위 머지, `enabled: false` 면 미로드).

- `shots[]` → VLM 컷 분류 enum, 체크리스트 필수 컷, `/v1/categories`, 대시보드 선택지
- `defect_rubric[]` → VLM 하자 코드 enum + 설명 프롬프트, 기본 심각도, 응답 `finding.code`
- `before_after_pairs` → 전후 쌍 존재 검사 + VLM 비교 호출
- `quality` / `fraud` / `verdict` → 임계값 (defaults 와 필드 단위 머지)
- `anomaly` → 플러그인 on/off, 체크포인트 키, 적용 컷

## 4. 확장 포인트

- **새 카테고리/하자 코드**: 그룹 폴더에 YAML 파일 하나 추가. 새 그룹은 `_groups.yaml` 에 이름·순서를 등록. 테스트 `test_categories.py` 가 참조 무결성과 폴더·파일명 일치를 검사합니다.
- **새 VLM 공급자**: `VLMProvider` 프로토콜(`complete_json`) 구현 후 `router.build_router` 에 이름을 추가.
- **다른 저장소(Postgres 등)**: `HashHistory.find_similar` + `save/get/list` 를 구현. 파이프라인은 인터페이스만 봅니다.
- **비동기 처리**: 현재는 요청 안에서 동기 처리(사진당 수 초). 대량 배치는 큐(예: Celery/SQS)에 `pipeline.run` 을 감싸고
  결과를 `store` 에서 폴링하는 구조로 확장할 수 있습니다. `inspection_id` 는 이미 전역 고유합니다.
- **판정 정책**: `verdict.py` 의 규칙은 코드지만 임계값은 YAML 입니다. 규칙 변경은 `test_verdict.py` 로 회귀 검증.

## 5. 비용·지연 (Claude 기준, 사진 4장 작업)

- VLM 호출 5회(사진 4 + 전후 비교 1), 사진당 입력 약 1.5~2.5K 토큰(1568px JPEG), 출력 200~400 토큰.
- 병렬 4 워커 기준 작업 1건 10~20초. `INSPECTOR_CLAUDE_EFFORT=low` 로 지연·비용을 줄일 수 있으며,
  정확도 영향은 `training/evaluate_eval_set.py` 로 측정하세요.
- `options.run_vlm=false` 는 결정적 검사만 수행(무료, <1초)하므로 1차 게이트(품질·중복)로 쓰고
  통과한 작업만 VLM 검사로 보내는 2단계 운영도 가능합니다.

## 6. 보안 메모

- 사진은 디스크에 저장하지 않습니다(해시·메타·분석 결과만 DB). 썸네일은 `return_thumbnails=true` 일 때 응답에만 포함.
- Claude 로 보내는 이미지는 EXIF 가 제거된 리사이즈 JPEG 입니다(위치 정보 미전송).
- `INSPECTOR_API_KEYS` 비어 있으면 인증이 꺼집니다. 운영 배포 전 반드시 설정하고 `/health` 의 `auth_enabled` 로 확인.
