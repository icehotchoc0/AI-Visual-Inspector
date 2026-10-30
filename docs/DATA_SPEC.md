# 데이터 규격 (DATA_SPEC)

이 서비스가 쓰는 데이터는 세 종류입니다. 학습 데이터는 이 세션에서 접근할 수 없었으므로
**폴더 규격과 임포트 규칙**만 정의합니다. 규격대로 넣으면 스크립트가 그대로 돌아갑니다.

| 종류 | 용도 | 위치 |
|---|---|---|
| 평가 세트 (eval set) | VLM 하자 검출·컷 분류 정확도 측정, 프롬프트/임계값 튜닝 | `backend/data/datasets/eval/<category>/` |
| 이상 탐지 학습 세트 | 고정 구도 컷용 Anomalib 플러그인 학습 (선택) | `backend/data/datasets/<model_key>/` |
| 운영 DB | 검사 결과·사진 해시 (부정 제출 교차 검사) | `backend/data/inspector.db` |

`backend/data/` 전체는 `.gitignore` 대상입니다. 원본 사진은 저장소에 커밋하지 않습니다.

---

## 1. 평가 세트 (VLM 검사 정확도 측정용)

현장 사진은 구도·조명이 매번 달라 "정상 이미지만으로 학습"하는 방식이 맞지 않습니다.
그래서 주 검사 경로는 VLM 루브릭이고, 이 세트는 **그 결과를 측정**하기 위한 정답 데이터입니다.

```
backend/data/datasets/eval/<category_id>/
├── jobs.jsonl                 # 작업 단위 정답 (한 줄 = 한 작업)
└── photos/
    └── <job_id>/
        ├── <photo_id>.jpg
        └── ...
```

`jobs.jsonl` 한 줄의 형식:

```json
{
  "job_id": "2026-10-001",
  "category": "aircon_install",
  "expected_verdict": "FAIL",
  "photos": [
    {"photo_id": "p1", "shot_type": "indoor_unit_before", "phase": "before",
     "defects": []},
    {"photo_id": "p2", "shot_type": "pipe_finish", "phase": "after",
     "defects": [{"code": "pipe_insulation_missing", "severity": "medium"}]},
    {"photo_id": "p3", "shot_type": "other", "phase": null, "defects": [],
     "quality_issue": "blurry"}
  ],
  "fraud": ["duplicate_in_job"],
  "notes": "검수자 메모"
}
```

규칙
- `shot_type` 은 `config/categories/<그룹>/<카테고리>.yaml` 의 `shots[].id` 또는 `other`.
- `defects[].code` 는 `defect_rubric[].code` 또는 `other`. 하자가 없으면 빈 배열.
- `quality_issue` 는 `blurry | too_dark | overexposed | low_resolution` 중 하나(없으면 생략).
- `fraud` 는 작업 단위 부정 라벨: `duplicate_in_job | reused_from_other_job | gps_spread | ...`.
- `expected_verdict` 는 운영 기준으로 사람이 내린 최종 판정(PASS/REVIEW/FAIL).
- 사진 파일명은 `photo_id` 와 동일하게. EXIF 는 **지우지 말고** 원본 그대로 보존(시간·GPS 검증 평가용).

권장 규모: 카테고리당 작업 50건 이상, 그중 FAIL 20% 이상. 하자 코드별 최소 10장.

---

## 2. 이상 탐지 학습 세트 (선택, 고정 구도 컷 전용)

실외기 거치대, 분전반 내부처럼 **같은 제품·같은 구도**로 반복 촬영되는 컷에만 적용합니다.
Anomalib `Folder` 데이터모듈 규격을 그대로 따릅니다.

```
backend/data/datasets/<model_key>/
├── train/
│   └── good/            # 정상(하자 없는) 사진만. 최소 30장, 권장 100장 이상
├── test/                # 선택 - 평가용
│   ├── good/
│   ├── bracket_unsecured/
│   └── pipe_insulation_missing/
└── README.md            # 촬영 조건(거리·각도·조명) 메모
```

규칙
- `model_key` 는 카테고리 파일의 `anomaly.model_key` 와 동일한 문자열 (예: `aircon_outdoor_unit`).
- `test/<폴더명>` 은 하자 코드와 같은 이름을 권장(리포트 가독성).
- JPG/PNG, 최소 640px. 촬영 거리·각도가 흔들리면 성능이 급락하므로 기사 앱의 촬영 가이드(프레임 오버레이)와 함께 운용.

학습·등록:

```bash
pip install -r requirements-anomaly.txt
python training/train_folder.py --model-key aircon_outdoor_unit --backend patchcore
```

```yaml
# config/categories/<그룹>/<카테고리>.yaml - 해당 파일에 추가
anomaly:
  enabled: true
  model_key: aircon_outdoor_unit
  shot_types: [outdoor_unit_after]
```

판정 규칙상 이상 탐지 결과는 **REVIEW 트리거**(보조 근거)이며 단독으로 FAIL 을 만들지 않습니다.

---

## 3. 운영 DB 에서 평가 세트 만들기

운영 중 사람이 검토(REVIEW)한 결과가 가장 좋은 정답 데이터입니다. `inspections` 테이블의 `result_json`
과 운영자의 최종 판정을 묶어 위 `jobs.jsonl` 형식으로 내보내는 것을 권장합니다
(기사 앱/운영 백오피스에서 "최종 판정" 필드를 함께 저장해 두세요).

---

## 4. 레거시: MVTec AD 벤치마크

`training/train_patchcore.py`, `training/train_efficientad.py`, `training/evaluate.py` 는 원본 프로젝트의
MVTec AD 벤치마크용으로 그대로 유지됩니다 (`MVTEC_ROOT`, `MVTEC_CATEGORIES`). 운영 파이프라인과는 무관합니다.
