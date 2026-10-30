# 운영 가이드 (OPERATIONS)

상용 운영에 필요한 설정, 배포 형태, 관측, 장애 대응을 정리합니다. 설치·실행 기본은 `SETUP_GUIDE.md`, API 계약은 `docs/API.md`.

## 1. 배포 형태

| 규모 | 구성 | 설정 |
|---|---|---|
| 파일럿 (서버 1대) | uvicorn 1프로세스 + 내장 큐 워커 + SQLite | 기본값 그대로. `INSPECTOR_QUEUE_WORKERS=2` |
| 상용 (복수 인스턴스) | API N개 + 워커 프로세스 M개 + PostgreSQL 14+ | `INSPECTOR_DATABASE_URL=postgresql+psycopg://...`, API 쪽 `INSPECTOR_QUEUE_WORKERS=0`, 워커 쪽만 `>0` |
| 민감 데이터·망분리 | 위 구성 + Ollama 서버 | `INSPECTOR_VLM_PROVIDERS=ollama` 또는 카테고리별 `privacy.local_only: true` |

- SQLite는 **인스턴스 1개**에서만 안전합니다. 두 번째 인스턴스를 띄우기 전에 PostgreSQL로 옮기세요 (`pip install psycopg[binary]`). 스키마는 기동 시 자동 생성됩니다.
- 워커 프로세스는 API와 같은 이미지로 `INSPECTOR_QUEUE_WORKERS=4 python -c "import backend.app"` 대신 `uvicorn`을 띄우되 외부 포트를 노출하지 않는 방식이 가장 단순합니다. 큐 클레임은 DB 트랜잭션이라 프로세스 수와 무관하게 중복 실행이 없습니다.
- 리버스 프록시(nginx/ALB)에서 HTTPS 종료, 요청 본문 크기(사진 30장 × 15 MB 고려), `/metrics` 접근 제한을 설정합니다.

## 2. 필수 환경변수 점검표

| 변수 | 상용 값 | 비고 |
|---|---|---|
| `INSPECTOR_API_KEYS` | 호출자별 키, 쉼표 구분 | 비어 있으면 인증 꺼짐 + 기동 경고 |
| `ANTHROPIC_API_KEY` | 발급 키 | Claude 공급자 |
| `INSPECTOR_VLM_PROVIDERS` | `claude,ollama` | 장애 시 로컬 폴백 |
| `INSPECTOR_DATABASE_URL` | PostgreSQL URL | 복수 인스턴스 필수 |
| `INSPECTOR_PHOTO_STORE_DIR` / `_POLICY` / `_RETENTION_DAYS` | 공유 볼륨 경로 / `review_only` / 90 | 검수 큐에 사진을 보여주려면 필요 |
| `INSPECTOR_WEBHOOK_SECRET` | 긴 난수 | 콜백 서명. 수신 측에서 `X-Inspector-Signature` 검증 |
| `INSPECTOR_CORS_ORIGINS` | 운영자 화면 도메인만 | `*` 금지 |
| `INSPECTOR_RATE_LIMIT_PER_MINUTE` | 호출자당 분당 요청 수 | 게이트웨이에 한도가 있으면 0 |
| `INSPECTOR_VLM_MAX_CONCURRENCY` | 4~8 | Anthropic 속도 제한에 맞춤 |
| `INSPECTOR_VLM_MAX_TOKENS_PER_JOB` | 예: 60000 | 사진 30장 작업의 비용 상한 |
| `INSPECTOR_LOG_JSON` | `true` | 로그 수집기 연동 |

## 3. 관측

- **로그**: 한 줄 JSON. `request_id`(응답 `X-Request-ID`와 동일), `event`(http_request / inspection / queued / review), `inspection_id`, `verdict`, `latency_ms`. 사진·메모 내용은 기록하지 않습니다.
- **메트릭** (`/metrics`, Prometheus):
  - `inspector_inspections_total{category,verdict,mode}` 판정 분포. REVIEW 비율 급증 = 모델 장애 또는 임계값 문제.
  - `inspector_inspection_seconds` 처리 지연 히스토그램.
  - `inspector_vlm_calls_total{provider,task,outcome}` outcome=error/circuit_open 증가 시 공급자 장애.
  - `inspector_vlm_tokens_total{provider,kind}` kind=cache_read_input_tokens 가 input_tokens 에 비해 낮으면 프롬프트 캐시 미적중.
  - `inspector_queue_jobs_total{status}` callback_failed 증가 시 수신 측 점검.
  - `inspector_rate_limited_total`.
- **권장 알림**: 5분간 `verdict="REVIEW"` 비율 > 50%, `outcome="error"` 비율 > 20%, 큐 `queue_active`(`/health`) > 100, p90 지연 > 60초.

## 4. 장애 대응

| 증상 | 확인 | 조치 |
|---|---|---|
| 모든 판정이 REVIEW, 근거에 "VLM 공급자에 모두 연결할 수 없어" | `/health` → `vlm_providers.*.reachable`, 로그 `VLM provider ... failed` | 키·네트워크 확인. 회로 차단기가 열렸으면 60초 후 자동 재시도 |
| 429 급증 | `inspector_rate_limited_total` | 호출자 키 분리 또는 한도 상향 |
| 큐가 쌓임 | `/health` → `store.queue_active` | 워커 프로세스 증설. 워커가 죽은 경우 30분 뒤 자동 재큐잉 |
| 콜백 미수신 | `inspector_queue_jobs_total{status="callback_failed"}` | 결과는 `GET /v1/jobs/{key}` 로 재조회 가능. 수신 측 2xx 응답 확인 |
| 비용 급증 | `inspector_vlm_tokens_total` | `INSPECTOR_VLM_MAX_TOKENS_PER_JOB` 설정, `INSPECTOR_CLAUDE_EFFORT=low`, 2단계 운영(`run_vlm=false` 선검사) |

## 5. 정기 작업

- 사진 보존 정리: `python scripts/purge_photos.py` 를 매일 cron. 기동 시에도 1회 실행됩니다.
- 평가 세트 갱신: 검수 완료 건이 쌓이면 `python training/export_eval_set.py --category <id>` 후 `python training/evaluate_eval_set.py --category <id>` 로 정확도 재측정. 프롬프트(`PROMPT_VERSION`)나 임계값을 바꾸면 반드시 재측정.
- 부하 점검: 배포 전 `python scripts/loadtest.py --url ... --key ... --no-vlm --jobs 100 --concurrency 10`.

## 6. 변경 관리

- 프롬프트를 바꾸면 `backend/pipeline.py`의 `PROMPT_VERSION`을 올립니다. 결과 `stats.prompt_version` 으로 어느 버전이 판정했는지 추적됩니다.
- 판정 규칙(`backend/verdict.py`) 변경은 `tests/test_verdict.py` 회귀 테스트와 평가 세트 재측정이 통과해야 배포합니다.
- 카테고리 파일 변경은 CI의 설정 검증 단계가 폴더·id 일치를 확인합니다.
