# 마이스터즈 AI 사진 검수 (AI-Visual-Inspector)

마이스터즈의 현장 AS·설치·시공 작업 사진을 제출 즉시 자동 검증하는 **독립 마이크로서비스**입니다.
기사가 제출한 작업 사진 묶음(작업 1건 = 사진 N장)을 받아 네 가지를 검사하고
`PASS / REVIEW / FAIL` 판정과 한국어 근거를 돌려줍니다.

| 검사 축 | 무엇을 잡는가 | 어떻게 |
|---|---|---|
| **사진 품질** | 흐림, 어두움/과노출, 저해상도, 저대비 | OpenCV 수치 검사 (결정적) |
| **부정 제출** | 같은 작업 내 중복, 이전 작업 사진 재사용, 다른 현장(GPS)·다른 시간(EXIF) 사진, 편집 흔적 | 퍼셉추얼 해시 + EXIF (결정적, DB 교차 검사) |
| **체크리스트** | 필수 컷 누락, 전/후 컷 불일치, 신고 컷과 실제 장면 불일치 | 카테고리 YAML + VLM 컷 분류 |
| **시공 하자** | 전선 노출, 누수, 보온재 누락, 유리 파손, 마감 불량 등 카테고리별 루브릭 | VLM (Claude 기본 → Ollama 폴백) |

```
기사 앱 / 서비스 백엔드
        │  POST /v1/inspections  (작업 메타 + 사진 N장)
        ▼
┌─ 결정적 검사 ──────────────────────────┐
│ 품질(블러·노출)  →  EXIF·해시(부정)   │   ← 규칙, 항상 실행
└──────────────┬─────────────────────────┘
               ▼ (품질 통과 사진만)
┌─ VLM 분석 (사진별 병렬) ────────────────┐
│ 컷 분류 + 작업부위 가시성 + 하자 목록   │   ← Claude API, 실패 시 Ollama
│ 전/후 쌍 비교(동일 장소·작업 흔적)       │      JSON 스키마 강제, 확신도 포함
└──────────────┬─────────────────────────┘
               ▼
┌─ 규칙 엔진 (backend/verdict.py) ───────┐
│ 확신도 임계값 → 심각도 → PASS/REVIEW/FAIL │   ← VLM은 판정하지 않음
└──────────────┬─────────────────────────┘
               ▼
   InspectionResult (JSON) + SQLite 저장 (이력·해시 인덱스)
```

## 설계 원칙

- **VLM은 기술만, 판정은 규칙이.** 원본 프로젝트의 "모델의 자기 보고는 주장이지 사실이 아니다" 원칙을 유지합니다.
  VLM 출력은 JSON 스키마로 강제되고 각 하자에 확신도가 붙습니다. `verdict.py`의 명시적 규칙이 확신도·심각도를
  판정으로 바꾸며, 임계값은 카테고리 YAML에서 조정합니다.
- **결정적으로 잡을 수 있는 것은 모델에 맡기지 않습니다.** 중복·재사용·GPS·시각·블러는 해시와 수치로 잡습니다.
  VLM이 전부 죽어도 이 검사는 돌아가고, 그 경우 판정은 자동으로 `REVIEW`가 됩니다.
- **카테고리는 설정입니다.** 가전(에어컨·정수기·식기세척기·주방후드·인덕션 설치, 세탁기·냉장고 수리), 주거 시공(배관, 전기, 도어, 환풍기, 수전), 업무·교육 시설(전자칠판) 13종이
  `config/categories/<그룹>/<id>.yaml`에 한 파일씩 정의되어 있습니다. 파일을 추가하면 API·프롬프트·체크리스트·대시보드가 함께 바뀌고, `enabled: false`로 보류할 수 있습니다.
- **이상 탐지(Anomalib)는 선택 플러그인으로 격하.** 현장 사진은 구도가 매번 달라 "정상 이미지만으로 학습"하는 방식이
  주 판정이 될 수 없습니다. 실외기 거치대처럼 고정 구도 컷에만 카테고리별로 켜고, 결과는 REVIEW 트리거(보조 근거)로만 씁니다.

## 빠른 시작

```bash
python -m venv venv && source venv/bin/activate      # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                  # ANTHROPIC_API_KEY, INSPECTOR_API_KEYS 설정
uvicorn backend.app:app --host 0.0.0.0 --port 8000    # API: http://localhost:8000/docs
streamlit run frontend/dashboard.py                   # 운영자 대시보드(마이스터즈 테마): http://localhost:8501
pytest tests/                                         # 99 tests, GPU·네트워크 불필요
```

최소 호출 예:

```bash
curl -X POST http://localhost:8000/v1/inspections/upload \
  -H "X-API-Key: $INSPECTOR_API_KEY" \
  -F 'job={"job_id":"2026-10-0001","category":"aircon_install","technician_id":"T-17"}' \
  -F 'photos=[{"photo_id":"before","declared_shot_type":"indoor_unit_before","phase":"before"},
              {"photo_id":"after","declared_shot_type":"indoor_unit_after","phase":"after"},
              {"photo_id":"outdoor","declared_shot_type":"outdoor_unit_after"},
              {"photo_id":"pipe","declared_shot_type":"pipe_finish"}]' \
  -F files=@before.jpg -F files=@after.jpg -F files=@outdoor.jpg -F files=@pipe.jpg
```

응답 요약(전체 스키마는 [docs/API.md](docs/API.md)):

```json
{
  "verdict": "FAIL",
  "summary": "불합격 - 심각한 시공/수리 하자 1건",
  "verdict_reasons": [
    "심각한 시공/수리 하자 1건: 전선 노출: 실내기 우측 하단 결선부 피복이 벗겨져 심선이 보임 @ 실내기 우측 하단 [after] (심각, 확신도 0.91)",
    "품질 문제로 반려된 사진 1장 (재촬영 권장): pipe"
  ],
  "checklist": [{"shot_id": "pipe_finish", "name": "배관 마감", "required": true, "satisfied": false, "photo_ids": []}],
  "photos": [{"photo_id": "after", "status": "FLAGGED", "resolved_shot_type": "indoor_unit_after", "findings": [...]}]
}
```

## API 한눈에

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/inspections` | JSON (사진 base64) 로 작업 검사 |
| POST | `/v1/inspections/upload` | multipart (`job`, `photos`, `options` JSON + `files[]`) |
| GET | `/v1/inspections` | 이력 조회 (`job_id`, `category`, `verdict`, `limit`) |
| GET | `/v1/inspections/{id}` | 저장된 결과 |
| GET | `/v1/categories` | 카테고리·필수 컷·하자 코드 카탈로그 |
| GET | `/health` | 상태 + VLM 공급자 도달 여부 (인증 없음) |

인증은 `X-API-Key` 헤더(`INSPECTOR_API_KEYS`). 비어 있으면 개발 모드로 비활성화되고 기동 시 경고를 남깁니다.

## VLM 구성

`INSPECTOR_VLM_PROVIDERS=claude,ollama` (기본). 순서대로 시도하고, 전송 오류·안전 분류기 거절·스키마 위반 시 다음 공급자로 넘어갑니다.

- **Claude** (`claude-opus-5-5`, `anthropic` SDK): `output_config.format`으로 JSON 스키마 출력을 강제하고,
  `fallbacks="default"`(서버측 거절 폴백 베타)를 켜서 안전 분류기 거절 시 Anthropic 권장 모델로 자동 재시도합니다.
  사고 깊이는 `INSPECTOR_CLAUDE_EFFORT`(기본 `medium`)로 조절합니다.
- **Ollama** (`qwen2.5vl:7b`): 완전 로컬 폴백. 같은 JSON 스키마를 `format` 파라미터로 강제합니다.
- 응답에는 사진별로 어떤 공급자·모델이 분석했는지(`analysis.provider/model`)와 토큰 사용량(`stats`)이 남습니다.

## 상용 운영 기능

- **비동기 처리**: `?mode=async` 로 202 반환, DB 큐 + 워커, 완료 시 HMAC 서명 웹훅, `Idempotency-Key` 로 중복 실행 방지.
- **검수 워크플로**: 검수 큐 API·대시보드 탭, 검수자 최종 판정 저장, 검수 완료 건을 평가 세트로 내보내기(`training/export_eval_set.py`), 기사별 판정 통계.
- **저장소**: SQLite(1대) 또는 PostgreSQL(복수 인스턴스), 선택적 사진 보관(REVIEW/FAIL 건, 보존일 자동 파기).
- **VLM 제어**: 전역 동시 호출 제한, 작업당 토큰 상한, 공급자 회로 차단기, 프롬프트 캐싱, 로컬 전용(개인정보) 정책.
- **관측·보안**: 요청 ID, JSON 로그, Prometheus `/metrics`, API 키별 속도 제한, CORS 화이트리스트, HEIC 지원.
- 자세한 내용: [docs/OPERATIONS.md](docs/OPERATIONS.md), 개인정보·법무 체크리스트: [docs/PRIVACY.md](docs/PRIVACY.md).

## 저장소 구조

```
backend/
  app.py              FastAPI 라우트 (/v1/*), 인증, 의존성 조립
  pipeline.py         작업 1건 오케스트레이션 (품질→해시→VLM→체크리스트→전후비교→판정→저장)
  verdict.py          규칙 기반 PASS/REVIEW/FAIL (유일한 판정 지점)
  categories.py       config/categories/ 폴더 로더 (그룹·enabled·order) + 검증
  schemas.py          API/도메인 모델 (Finding, PhotoResult, InspectionResult ...)
  store.py            SQLAlchemy (SQLite/PostgreSQL): 검사 이력, 검수 판정, 사진 해시, 비동기 큐
  photostore.py       선택적 사진 보관·보존일 파기
  queue.py            비동기 워커, 서명 웹훅
  observability.py    요청 ID, JSON 로그, Prometheus 메트릭
  ratelimit.py        API 키별 토큰 버킷
  auth.py             X-API-Key
  checks/quality.py   블러·노출·해상도
  checks/fraud.py     dHash 중복/재사용, EXIF 시각·GPS·편집 소프트웨어
  checks/checklist.py 필수 컷·전후 쌍 커버리지
  vlm/base.py         공급자 인터페이스 (이미지+프롬프트+스키마 → dict)
  vlm/prompts.py      한국어 프롬프트·JSON 스키마 빌더 (카테고리 YAML 기반)
  vlm/claude_provider.py / ollama_provider.py / router.py
  detector.py         선택 플러그인: Anomalib (고정 구도 컷)
config/categories/        카테고리 13종: <그룹>/<id>.yaml 한 파일에 한 작업 종류
  _defaults.yaml           공통 임계값, _groups.yaml 그룹 이름·순서 (창호는 enabled: false 로 보류)
frontend/dashboard.py   운영자 대시보드 (업로드·컷 지정·결과·이력)
training/
  train_folder.py        우리 데이터로 이상 탐지 플러그인 학습 (docs/DATA_SPEC.md 규격)
  evaluate_eval_set.py   라벨된 평가 세트로 판정·컷·하자 정확도 측정
  train_patchcore.py / train_efficientad.py / evaluate.py   (레거시 MVTec 벤치마크)
tests/                  99 tests - FakeVLM 으로 파이프라인·API·큐·검수 전체를 오프라인 검증
scripts/                purge_photos.py (보존 정리), loadtest.py (부하 테스트)
.github/workflows/ci.yml  푸시마다 설정 검증·테스트·기동 확인
docs/API.md · ARCHITECTURE.md · DATA_SPEC.md
docker/                 backend + dashboard compose
```

## 데이터

학습·평가 데이터 폴더 규격은 [docs/DATA_SPEC.md](docs/DATA_SPEC.md)에 정의했습니다.
운영 초기에는 평가 세트(라벨된 작업 50건/카테고리)를 먼저 만들고 `training/evaluate_eval_set.py`로
프롬프트·임계값을 튜닝하는 것을 권장합니다. 이상 탐지 학습 세트는 고정 구도 컷이 생길 때 추가합니다.

## 알려진 한계

- VLM 하자 검출의 실제 정밀도/재현율은 **우리 사진으로 측정하기 전까지 알 수 없습니다.** 그래서 기본 정책은
  보수적입니다: 확신도 0.6 미만은 참고만, 심각(high)만 FAIL, 보통(medium)은 REVIEW.
- 교차 작업 재사용 검사는 SQLite 에서는 선형 스캔(사진 10만 장까지 충분), PostgreSQL 에서는 `bit_count` SQL 로 동작합니다.
- EXIF 는 메신저 전송 시 흔히 제거됩니다. 기사 앱이 원본을 올리도록 해야 시각·GPS 검증이 의미 있습니다.
- 전/후 비교는 쌍당 첫 사진끼리만 비교합니다(비용 제어). 필요하면 `pipeline.py::_compare` 호출을 확장하세요.
- 개인정보(국외 이전·위치정보) 검토는 기술적 선택지만 준비되어 있습니다. 결론은 법무 검토가 필요합니다 (`docs/PRIVACY.md`).

## 원본 프로젝트

이 저장소는 MVTec AD 벤치마크용 PatchCore/EfficientAd + Qwen2.5-VL 설명 데모에서 출발했습니다.
그 코드(`training/train_*.py`, `backend/detector.py`)와 측정 결과는 유지되며 선택 플러그인으로 연결됩니다.
