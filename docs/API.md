# API 레퍼런스

Base URL: `http://<host>:8000` · 인증: `X-API-Key: <key>` (`INSPECTOR_API_KEYS` 설정 시) · 대화형 문서: `/docs`

## POST /v1/inspections

작업 1건을 JSON 으로 검사합니다. 사진은 base64.

```json
{
  "job": {
    "job_id": "2026-10-0001",
    "category": "plumbing",
    "technician_id": "T-17",
    "customer_ref": "SO-88213",
    "scheduled_start": "2026-10-29T09:00:00+09:00",
    "scheduled_end":   "2026-10-29T12:00:00+09:00",
    "site_lat": 37.5665, "site_lon": 126.9780,
    "notes": "주방 싱크대 하부 급수관 교체"
  },
  "photos": [
    {"photo_id": "p1", "image_base64": "...", "declared_shot_type": "site_before", "phase": "before"},
    {"photo_id": "p2", "image_base64": "...", "declared_shot_type": "pipe_connection", "phase": "after"},
    {"photo_id": "p3", "image_base64": "...", "declared_shot_type": "leak_test"},
    {"photo_id": "p4", "image_base64": "...", "declared_shot_type": "site_after"}
  ],
  "options": {"run_vlm": true, "run_fraud_history": true, "return_thumbnails": false}
}
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `job.job_id` | ✔ | 서비스 쪽 작업 식별자. 같은 job_id 재검사는 교차 재사용 검사에서 제외됨 |
| `job.category` | ✔ | `/v1/categories` 의 id |
| `job.scheduled_*` | | 있으면 EXIF 촬영 시각이 (±3시간 여유) 범위 밖일 때 `captured_outside_schedule` |
| `job.site_lat/lon` | | 있으면 EXIF GPS 가 1 km 이상 떨어지면 `gps_far_from_site` |
| `job.notes` | | 기사 메모. VLM 컨텍스트로 전달됨 |
| `photos[].declared_shot_type` | | 기사 앱이 지정한 컷 id. VLM 분류와 다르면 `shot_type_mismatch` |
| `photos[].phase` | | `before` / `after` |
| `options.run_vlm` | | `false` 면 결정적 검사만 (무료·즉시). 판정은 PASS 가능하나 하자 검사 생략이 근거에 기록됨 |
| `options.run_fraud_history` | | `false` 면 이전 작업 해시와 비교하지 않음 |

제한: 사진 최대 30장(`MAX_PHOTOS_PER_JOB`), 장당 15 MB.

## POST /v1/inspections/upload

multipart/form-data. 서비스 백엔드가 파일을 그대로 중계할 때 편합니다.

| 폼 필드 | 설명 |
|---|---|
| `job` | `JobContext` JSON 문자열 |
| `photos` | `PhotoInput` 메타 배열 JSON (image_base64 제외). `photo_id` 는 파일명(확장자 제외)과 일치 |
| `options` | `InspectionOptions` JSON (선택) |
| `files` | 이미지 파일들 (여러 개) |

## 응답: InspectionResult

```json
{
  "inspection_id": "insp_3f9c2a1b7d8e4f10",
  "job_id": "2026-10-0001",
  "category": "plumbing",
  "category_name": "배관 시공",
  "verdict": "REVIEW",
  "summary": "검토 필요 - 보통 수준 하자 1건",
  "verdict_reasons": ["보통 수준 하자 1건: 배관 지지 불량: ... (보통, 확신도 0.78)", "..."],
  "photos": [
    {
      "photo_id": "p2",
      "status": "FLAGGED",
      "declared_shot_type": "pipe_connection",
      "resolved_shot_type": "pipe_connection",
      "phase": "after",
      "quality": {"width": 3024, "height": 4032, "blur_score": 412.5, "brightness": 131.0, "contrast": 55.2, "passed": true},
      "meta": {"exif_datetime": "2026-10-29T10:12:44", "gps_lat": 37.56, "gps_lon": 126.97, "camera_model": "SM-S928N", "software": null,
               "perceptual_hash": "9c6e3b1d8f0a2c47", "sha256": "..."},
      "analysis": {"shot_type": "pipe_connection", "shot_confidence": 0.93, "work_area_visible": true,
                   "scene_description": "싱크대 하부 새 급수 배관 이음부가 보입니다.", "provider": "claude", "model": "claude-opus-5-5", "latency_ms": 6210.4},
      "findings": [
        {"code": "pipe_unsupported", "kind": "defect", "source": "vlm", "severity": "medium", "confidence": 0.78,
         "message": "배관 지지 불량: 수평 구간 약 60cm 가 클램프 없이 늘어져 있음", "photo_id": "p2",
         "location_hint": "이음부 좌측 수평 구간", "evidence": "배관이 아래로 휘어 있고 고정 클램프가 보이지 않음", "data": {"provider": "claude"}}
      ],
      "anomaly_score": null,
      "thumbnail_png_base64": null
    }
  ],
  "checklist": [
    {"shot_id": "site_before", "name": "시공 전 현장", "required": true, "satisfied": true, "photo_ids": ["p1"]},
    {"shot_id": "material_label", "name": "자재 확인", "required": false, "satisfied": false, "photo_ids": []}
  ],
  "job_findings": [
    {"code": "before_after_summary", "kind": "info", "source": "vlm", "severity": "low", "confidence": 0.9,
     "message": "기존 부식된 배관이 새 배관으로 교체되고 마감되었습니다.", "related_photo_ids": ["p1", "p4"]}
  ],
  "stats": {"num_photos": 4, "num_rejected": 0, "num_flagged": 1, "vlm_calls": 5, "vlm_errors": 0,
            "vlm_input_tokens": 9120, "vlm_output_tokens": 1430, "latency_ms": 18420.3},
  "vlm_provider_used": "claude",
  "created_at": "2026-10-29T01:12:50.123456Z",
  "pipeline_version": "2.0.0"
}
```

### verdict

| 값 | 의미 | 주요 트리거 |
|---|---|---|
| `FAIL` | 자동 불합격 | 확신도 ≥0.8 부정 제출(중복·재사용·GPS), 필수 컷 누락(정책 `fail`), 확신도 ≥ `min_defect_confidence` 인 **심각** 하자, 전/후 사진 장소 불일치(≥0.8) |
| `REVIEW` | 사람 검토 | 보통 하자, 확신도 0.4~0.6 하자 의심, EXIF 시각/편집 흔적, 전후 쌍 불완전, 컷 신고 불일치, 이상 탐지 히트, **VLM 전체 장애** |
| `PASS` | 자동 합격 | 위 어느 것도 없음. 경미 하자·반려 사진은 근거에만 기록 |

### photos[].status

`REJECTED` 품질 불량(재촬영) · `FLAGGED` 보통 이상 지적 있음 · `OK`

### Finding.kind / source / 대표 code

| kind | source | code 예 |
|---|---|---|
| quality | quality_check | `blurry`, `too_dark`, `overexposed`, `low_resolution`, `low_contrast` |
| fraud | fraud_check | `duplicate_in_job`, `near_duplicate_in_job`, `reused_from_other_job`, `similar_to_other_job`, `gps_spread`, `gps_far_from_site`, `captured_outside_schedule`, `capture_time_spread`, `edited_with_software` |
| checklist | checklist | `missing_required_shot`, `before_after_incomplete`, `unclassified_photo` |
| consistency | vlm | `shot_type_mismatch`, `work_area_not_visible`, `before_after_different_location`, `work_not_visible_after` |
| defect | vlm | 카테고리 `defect_rubric[].code` (예 `exposed_wiring`, `leak_visible`) 또는 `other:<제목>` |
| anomaly | anomaly_detector | `anomaly_detected` |
| info | fraud_check / vlm / system | `no_exif`, `before_after_summary`, `before_after_concern`, `unknown_declared_shot` |

## GET /v1/inspections?limit=&job_id=&category=&verdict=

`[{inspection_id, job_id, category, verdict, created_at, num_photos}]` 최신순.

## GET /v1/inspections/{inspection_id}

저장된 `InspectionResult`. 없으면 404.

## GET /v1/categories

`[{id, name, group, description, shots:[{id,name,description,required,phase}], defect_codes:[{code,name,description,default_severity}], before_after_pairs}]`

## GET /health (인증 없음)

```json
{"status":"ok","version":"2.0.0","auth_enabled":true,"categories":["aircon_install","..."],
 "vlm_enabled":true,
 "vlm_providers":{"claude":{"model":"claude-opus-5-5","reachable":true,"error":null},
                  "ollama":{"model":"qwen2.5vl:7b","reachable":false,"error":"ConnectError: ..."}},
 "store":{"inspections":1203,"photo_hashes":4711,"by_verdict":{"PASS":900,"REVIEW":220,"FAIL":83}}}
```

## 오류

| HTTP | 상황 |
|---|---|
| 400 | 알 수 없는 카테고리, 디코딩 불가 이미지, 중복 photo_id, 사진 수/크기 초과 (`detail` 에 사유) |
| 401 | X-API-Key 누락/불일치 |
| 404 | 검사 ID 없음 |
| 422 | 요청 스키마 위반 |

VLM 장애는 HTTP 오류가 아닙니다. 검사는 완료되고 `verdict=REVIEW`, `stats.vlm_errors>0`, 근거에 "VLM 공급자에 모두 연결할 수 없어 …" 가 기록됩니다.
