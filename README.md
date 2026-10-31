# 마이스터즈 AI 사진 검수 (AI-Visual-Inspector)

[![CI](https://github.com/icehotchoc0/AI-Visual-Inspector/actions/workflows/ci.yml/badge.svg?branch=feature%2Ffield-service-inspection)](https://github.com/icehotchoc0/AI-Visual-Inspector/actions/workflows/ci.yml)

마이스터즈의 현장 AS·설치·시공 작업 사진을 **제출 즉시 자동 검증**하는 독립 마이크로서비스입니다.
기사 앱이나 운영 백엔드가 작업 1건(사진 N장)을 보내면 네 가지를 검사하고, 규칙 엔진이
**합격(PASS) / 검토 필요(REVIEW) / 불합격(FAIL)** 판정과 한국어 근거를 돌려줍니다.

| 검사 축 | 무엇을 잡는가 | 어떻게 |
|---|---|---|
| **사진 품질** | 흐림, 어두움·과노출, 저해상도, 저대비 | 수치 검사 (규칙) |
| **부정 제출** | 같은 작업 내 중복, 이전 작업 사진 재사용, 다른 현장(GPS)·다른 시간(EXIF), 편집 흔적 | 사진 지문(해시) + EXIF, DB 교차 검사 (규칙) |
| **체크리스트** | 필수 컷 누락, 작업 전/후 컷 불일치, 신고 컷과 실제 장면 불일치 | 카테고리 설정 + AI 컷 분류 |
| **시공 하자** | 전선 노출, 누수, 보온재 누락, 마감 불량 등 작업 종류별 항목 | AI 비전 모델 (Claude 기본, Ollama 폴백) |

```
기사 앱 / 운영 백엔드
        │  POST /v1/inspections  (작업 메타 + 사진 N장, 동기 또는 비동기)
        ▼
┌─ 규칙 검사 (항상 실행, 1초 미만) ──────────────────────────┐
│ 품질(선명도·노출)  →  사진 지문·EXIF(중복·재사용·위치·시각)  │
└──────────────┬───────────────────────────────────────────┘
               ▼ (품질 통과 사진만)
┌─ AI 비전 분석 (사진별 병렬) ────────────────────────────────┐
│ 컷 분류 + 작업 부위 가시성 + 하자 목록 (JSON 스키마, 확신도)  │  ← Claude, 실패·거절·스키마 위반 시 Ollama
│ 작업 전/후 쌍 비교 (같은 장소인지, 작업 흔적이 보이는지)       │
└──────────────┬───────────────────────────────────────────┘
               ▼
┌─ 규칙 엔진 (backend/verdict.py) ───────────────────────────┐
│ 확신도 기준 → 심각도 → PASS / REVIEW / FAIL + 한국어 근거     │  ← AI는 판정 필드를 건드릴 수 없음
└──────────────┬───────────────────────────────────────────┘
               ▼
   결과 JSON + DB 저장(이력·해시 인덱스) → 검수 큐 → 평가 세트
```

## 설계 원칙

- **AI는 관찰만 기록하고, 판정은 규칙이 내립니다.** AI 출력은 JSON 스키마로 강제되고 하자마다 확신도가 붙습니다. `backend/verdict.py`의 명시적 규칙만이 확신도·심각도를 판정으로 바꾸며, 기준값은 카테고리 설정에서 조정합니다.
- **규칙으로 잡을 수 있는 것은 모델에 맡기지 않습니다.** 중복·재사용·GPS·촬영 시각·흐림은 해시와 수치로 잡습니다. AI 공급자가 모두 중단돼도 이 검사는 동작하고, 그 경우 판정은 자동으로 `REVIEW`가 됩니다.
- **작업 종류는 코드가 아니라 설정입니다.** `config/categories/<그룹>/<id>.yaml` 한 파일이 작업 종류 하나입니다. 파일을 추가하면 API·프롬프트·체크리스트·대시보드가 함께 바뀌고, `enabled: false`로 보류할 수 있습니다.
- **이상 탐지(Anomalib)는 선택 플러그인입니다.** 현장 사진은 구도가 매번 달라 "정상 이미지만으로 학습"하는 방식이 주 판정이 될 수 없습니다. 고정 구도 컷에만 켜고 결과는 REVIEW를 유발하는 보조 근거로만 씁니다.

## 작업 종류 (13종 활성)

| 그룹 | 작업 종류 |
|---|---|
| 가전 (7) | 에어컨 설치, 세탁기 수리, 냉장고 수리, 정수기 설치·관리, 식기세척기 설치, 주방후드 설치·교체, 인덕션 설치 |
| 주거 시공 (5) | 배관 시공, 전기 시공, 도어 설치, 환풍기 설치·교체, 수전 교체 |
| 업무·교육 시설 (1) | 전자칠판 설치 |
| 보류 (`enabled: false`) | 창호 시공 |

각 파일은 필수·선택 촬영 컷(`shots`), 전/후 비교 쌍, 하자 항목(`defect_rubric`)을 정의합니다. 기사 앱은 `/v1/categories`로 이 목록을 받아 촬영 단계 화면을 만들고, 사진마다 컷 id를 붙여 보냅니다. 공통 기준값은 `_defaults.yaml`, 그룹 이름·순서는 `_groups.yaml`.

## 빠른 시작

```bash
python -m venv venv && source venv/bin/activate      # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                  # ANTHROPIC_API_KEY, INSPECTOR_API_KEYS 채우기
uvicorn backend.app:app --host 0.0.0.0 --port 8000    # API 문서: http://localhost:8000/docs
INSPECTOR_API_KEY=<키> streamlit run frontend/dashboard.py   # 운영자 대시보드: http://localhost:8501
pytest tests/                                         # 99 tests, GPU·네트워크·API 키 불필요
```

`/health`에서 `vlm_providers.claude.reachable`이 `true`면 준비 완료입니다. 자세한 설치·문제 해결은 [SETUP_GUIDE.md](SETUP_GUIDE.md).

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

응답 요약 (전체 스키마는 [docs/API.md](docs/API.md)):

```json
{
  "verdict": "FAIL",
  "summary": "불합격 - 심각한 시공/수리 하자 1건",
  "verdict_reasons": [
    "심각한 시공/수리 하자 1건: 전선 노출: 실내기 우측 하단 결선부 피복이 벗겨져 심선이 보임 @ 실내기 우측 하단 [after] (심각, 확신도 0.91)",
    "품질 문제로 반려된 사진 1장 (재촬영 권장): pipe"
  ],
  "checklist": [{"shot_id": "pipe_finish", "name": "배관 마감", "required": true, "satisfied": false, "photo_ids": []}],
  "photos": [{"photo_id": "after", "status": "FLAGGED", "resolved_shot_type": "indoor_unit_after", "findings": ["..."]}],
  "stats": {"vlm_calls": 5, "vlm_input_tokens": 9120, "latency_ms": 18420.3, "prompt_version": "2026-10-30.1"}
}
```

## API 한눈에

| 메서드 | 경로 | 설명 |
|---|---|---|
| POST | `/v1/inspections` | JSON(사진 base64)으로 작업 검사. `?mode=async`면 202 + 작업 키 |
| POST | `/v1/inspections/upload` | multipart (`job`, `photos`, `options` JSON + `files[]`). `?mode=async` 동일 |
| GET | `/v1/jobs/{job_key}` | 비동기 작업 상태와 결과 |
| GET | `/v1/inspections` | 이력 (`job_id`, `category`, `verdict`, `technician_id`, `review_state`) |
| GET | `/v1/inspections/{id}` | 저장된 결과 |
| GET | `/v1/inspections/{id}/photos/{photo_id}` | 보관된 원본 사진 (사진 보관 설정 시) |
| GET/POST | `/v1/inspections/{id}/review` | 검수자 최종 판정 조회·저장 |
| GET | `/v1/reviews/queue` | 검수 대기 큐 (자동 판정 REVIEW, 미검수) |
| GET | `/v1/stats/technicians` | 기사별 판정 통계 |
| GET | `/v1/categories`, `/v1/categories/groups` | 작업 종류 카탈로그 (`?group=` 필터) |
| GET | `/health`, `/metrics` | 상태·공급자 도달 여부, Prometheus 메트릭 (인증 없음) |

- 인증: `X-API-Key` (`INSPECTOR_API_KEYS`, 쉼표로 여러 개). 비어 있으면 개발 모드로 꺼지고 기동 시 경고.
- 비동기: 완료 시 `options.callback_url`로 결과를 POST (`X-Inspector-Signature` HMAC 서명). `Idempotency-Key` 헤더로 재시도 시 중복 실행 방지.
- 오류: 400 잘못된 입력, 401 인증, 404 없음, 422 스키마, 429 속도 제한. AI 장애는 오류가 아니라 `REVIEW` 판정.

## AI 비전 모델 구성

`INSPECTOR_VLM_PROVIDERS=claude,ollama` (기본). 순서대로 시도하고 전송 오류·안전 분류기 거절·스키마 위반 시 다음 공급자로 넘어갑니다.

- **Claude** (`claude-opus-5-5`, Anthropic SDK): JSON 스키마 출력 강제(`output_config.format`), 서버측 거절 폴백(`fallbacks="default"`), 카테고리 설명 블록 프롬프트 캐싱. 추론 깊이는 `INSPECTOR_CLAUDE_EFFORT`(기본 `medium`).
- **Ollama** (`qwen2.5vl:7b`): 완전 로컬 폴백. 같은 JSON 스키마를 `format`으로 강제. `INSPECTOR_VLM_LOCAL_ONLY=true` 또는 카테고리 `privacy.local_only`, 요청 `options.local_only`로 로컬만 쓰도록 강제할 수 있습니다.
- 운영 제어: 전역 동시 호출 제한, 작업당 토큰 상한(초과분은 REVIEW), 공급자별 회로 차단기. 응답 `stats`에 호출 수·토큰·캐시 적중·프롬프트 버전이 남습니다.

## 상용 운영 기능

| 영역 | 내용 |
|---|---|
| 비동기 처리 | DB 큐 + 워커 스레드/프로세스, 서명 웹훅, 멱등성, 죽은 워커 작업 자동 재큐잉 |
| 검수 워크플로 | 검수 큐 API·대시보드 탭, 검수자 판정 저장, `#코드` 메모 태그 → 하자 라벨, 평가 세트 내보내기 |
| 저장소 | SQLite(서버 1대) / PostgreSQL 14+(복수 인스턴스, 해시 검색을 SQL로) |
| 사진 보관 | 기본 미보관. 켜면 REVIEW/FAIL 건만 보존(기본 90일) 후 자동 파기, 인증된 API로만 조회 |
| 관측 | 요청 ID, 한 줄 JSON 로그(사진·메모 미기록), Prometheus 메트릭(판정 분포·지연·AI 호출·토큰·큐) |
| 보안 | API 키별 속도 제한(429), CORS 화이트리스트(기본 차단), 외부 전송 이미지 EXIF 제거·축소 |
| 입력 | JPEG/PNG/HEIC(아이폰 원본), 작업당 사진 30장, 장당 15 MB |

운영 가이드 [docs/OPERATIONS.md](docs/OPERATIONS.md) · 개인정보·법무 체크리스트 [docs/PRIVACY.md](docs/PRIVACY.md) · 모델 학습 전략 [docs/TRAINING_STRATEGY.md](docs/TRAINING_STRATEGY.md)

### 배포

```bash
cd docker && docker compose up --build                       # 파일럿: SQLite, API+대시보드
cd docker && docker compose -f docker-compose.prod.yml up -d  # 상용: PostgreSQL + API + 워커 + 대시보드 + 사진 정리
```

### 주요 환경변수

| 변수 | 기본 | 설명 |
|---|---|---|
| `INSPECTOR_API_KEYS` | (없음) | 호출자 인증 키. 상용 필수 |
| `ANTHROPIC_API_KEY` | (없음) | Claude 호출 키 |
| `INSPECTOR_VLM_PROVIDERS` | `claude,ollama` | 공급자 순서 |
| `INSPECTOR_DATABASE_URL` | SQLite 파일 | PostgreSQL URL (복수 인스턴스) |
| `INSPECTOR_PHOTO_STORE_DIR` | (없음) | 검수용 사진 보관 경로 |
| `INSPECTOR_QUEUE_WORKERS` | 2 | 이 프로세스의 큐 워커 수 (API 전용 레플리카는 0) |
| `INSPECTOR_WEBHOOK_SECRET` | (없음) | 콜백 서명 키 |
| `INSPECTOR_VLM_MAX_TOKENS_PER_JOB` | 0 | 작업당 AI 토큰 상한 |
| `INSPECTOR_VLM_LOCAL_ONLY` | false | 사진을 클라우드로 보내지 않음 |

전체 목록과 설명은 [.env.example](.env.example).

## 저장소 구조

```
backend/
  app.py              FastAPI 라우트, 인증·속도 제한, 큐 워커 기동
  pipeline.py         작업 1건 오케스트레이션 (품질→해시→AI→체크리스트→전후 비교→판정→저장)
  verdict.py          규칙 기반 PASS/REVIEW/FAIL (유일한 판정 지점)
  categories.py       config/categories/ 로더 (그룹·enabled·order) + 검증
  schemas.py          API·도메인 모델
  store.py            SQLAlchemy: 검사 이력, 검수 판정, 사진 해시, 비동기 큐
  photostore.py       사진 보관·보존일 파기
  queue.py            비동기 워커, 서명 웹훅
  observability.py    요청 ID, JSON 로그, Prometheus 메트릭
  ratelimit.py        API 키별 토큰 버킷
  auth.py             X-API-Key
  checks/             quality.py 품질 · fraud.py 지문·EXIF · checklist.py 필수 컷
  vlm/                base.py 인터페이스 · prompts.py 한국어 프롬프트·스키마 · claude_provider.py · ollama_provider.py · router.py (폴백·차단기·동시성)
  detector.py         선택 플러그인: Anomalib (고정 구도 컷)
config/categories/    _defaults.yaml, _groups.yaml, <그룹>/<id>.yaml (13종 + 보류 1종)
frontend/dashboard.py 운영자 대시보드: 검사 실행 · 검수 큐 · 검사 이력 · 기사별 통계 (마이스터즈 테마)
training/
  export_eval_set.py  검수 완료 건 → 평가 세트 (docs/DATA_SPEC.md)
  evaluate_eval_set.py 평가 세트로 판정·컷·하자 정확도 측정
  train_folder.py     고정 구도 컷용 이상 탐지 학습
  train_patchcore.py / train_efficientad.py / evaluate.py   레거시 MVTec 벤치마크
scripts/              purge_photos.py (보존 정리), loadtest.py (부하 테스트)
tests/                99 tests: 규칙·판정·라우터·파이프라인·API·큐·검수·저장소 (FakeVLM, SQLite 또는 PostgreSQL)
docs/                 API.md · ARCHITECTURE.md · DATA_SPEC.md · OPERATIONS.md · PRIVACY.md · TRAINING_STRATEGY.md
docker/               Dockerfile.backend · Dockerfile.dashboard · docker-compose.yml · docker-compose.prod.yml
.github/workflows/    ci.yml: 설정 검증, 테스트(SQLite·PostgreSQL), 기동 확인
```

## 데이터와 정확도

- 하자 검출의 실제 정밀도·재현율은 **우리 사진으로 측정하기 전까지 알 수 없습니다.** 그래서 기본 정책은 보수적입니다: 확신도 0.6 미만은 참고만, 심각(high)만 FAIL, 보통(medium)은 REVIEW.
- 측정 순서: 검수 큐에서 판정을 남긴다 → `training/export_eval_set.py`로 평가 세트 생성 → `training/evaluate_eval_set.py`로 리포트 → 카테고리 파일의 하자 설명·기준값 조정 → 재측정. 폴더 규격은 [docs/DATA_SPEC.md](docs/DATA_SPEC.md).
- 학습은 필수가 아닙니다. 언제 어떤 로컬 모델 학습이 필요한지는 [docs/TRAINING_STRATEGY.md](docs/TRAINING_STRATEGY.md).

## 알려진 한계

- EXIF는 메신저 전송 시 흔히 제거됩니다. 기사 앱이 원본을 올려야 시각·GPS 검증이 의미 있습니다.
- 전/후 비교는 쌍마다 첫 사진 한 장씩만 비교합니다(비용 제어).
- 개인정보(국외 이전·위치정보) 검토는 기술적 선택지만 준비되어 있습니다. 결론은 법무 검토가 필요합니다.
- PostgreSQL 경로는 PostgreSQL 16으로 테스트 전체를 통과했지만, 운영 규모의 해시 테이블(수백만 행) 성능은 아직 측정하지 않았습니다.

## 원본 프로젝트

MVTec AD 벤치마크용 PatchCore/EfficientAd + Qwen2.5-VL 설명 데모에서 출발했습니다. 그 코드(`training/train_*.py`, `backend/detector.py`)와 측정 결과는 유지되며 선택 플러그인으로 연결됩니다.
