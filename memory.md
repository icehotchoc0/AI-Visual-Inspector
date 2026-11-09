# memory.md - 세션 작업 메모리 (컨텍스트 압축 대비)

> 목적: 대화가 압축되거나 새 세션이 시작돼도 작업 상태·결정·주의점을 즉시 복원하기 위한 파일.
> 규칙: 큰 작업 단위가 끝날 때마다 "현재 상태"와 "다음 할 일"을 갱신하고 커밋한다. 사실만 적고 추측은 적지 않는다.

## 1. 프로젝트 한 줄
마이스터즈(Meisters, 한국 B2C 홈케어·B2B 설치/AS) 현장 작업 사진을 제출 즉시 검사하는 마이크로서비스
"마이스터즈 AI 사진 검수"(기술명 AI-Visual-Inspector). 규칙 검사(품질·부정·체크리스트) + AI 비전(Claude 기본, Ollama 폴백) →
규칙 엔진이 PASS/REVIEW/FAIL 판정. 사용자: 브라이언(meisters.dev@gmail.com, GitHub icehotchoc0), 한국어 소통, 맥 bash.

## 2. 저장소·브랜치·실행
- GitHub: icehotchoc0/AI-Visual-Inspector, 작업 브랜치 `feature/field-service-inspection` (main 미머지, PR 없음. PR은 요청 시에만)
- 클라우드 세션 클론: `/home/user/ai-visual-inspector` (푸시 권한 있음). 커밋 작성자 meisters-dev, 공동 작성자 줄 필수.
- 로컬(사용자 맥): `~/vI/AI-Visual-Inspector`, Docker로 실행. 루트에서 `docker compose up -d --build` (compose.yaml/compose.prod.yaml).
  대시보드 호스트 포트는 8502(8501은 다른 프로젝트 visionguard가 사용) → `.env` `INSPECTOR_DASHBOARD_PORT=8502`.
- 테스트: `pytest tests/` (106개, 오프라인). PostgreSQL 검증: `INSPECTOR_TEST_DATABASE_URL=...` 로 동일 스위트.
- 문서: README.md, SETUP_GUIDE.md, docs/{API,ARCHITECTURE,DATA_SPEC,OPERATIONS,PRIVACY,TRAINING_STRATEGY}.md
- 덱(스크래치, 세션 종료 시 소실 가능): 설계 문서 덱·VC 시연 덱 생성 스크립트는
  `/tmp/claude-0/-home-user-ai-visual-inspector/9274b4e4-636c-521b-bb4f-81f30fd5aa75/scratchpad/deck/{build.js,build_vc.js}` (pptxgenjs).

## 3. 핵심 설계 결정 (바꾸려면 사용자 확인)
- AI는 관찰만(JSON 스키마 강제, 확신도), 판정은 backend/verdict.py 규칙만. AI 장애 → REVIEW.
- 카테고리는 설정: `config/categories/<그룹>/<id>.yaml` (한 파일 = 작업 종류), `_defaults.yaml` 임계값, `_groups.yaml` 그룹.
  활성 13종: 가전 7(aircon_install, washer_install, fridge_install, water_purifier, dishwasher_install, range_hood_install, induction_install),
  주거 시공 5(plumbing, electrical, door_install, ventilation_fan, faucet_replace), 업무·교육 시설 1(eboard_install). 창호(window_install)는 `enabled: false` 보류.
  세탁기·냉장고는 "설치"(수리 아님). 전기는 유지.
- 사진 품질: 밝기 보정 선명도(최강 모서리) 기준, 2단계(경고 medium=분석 계속 / 반려 high). 기본 sharpness_warn 30, reject 12.
  현장 사진이 전부 흐림으로 나오던 문제의 수정. 보정 스크립트 scripts/quality_calibrate.py.
- VLM: Claude claude-opus-5-5, effort medium, output_config.format JSON 스키마, fallbacks="default"(beta server-side-fallback-2026-07-01),
  시스템 블록 프롬프트 캐싱. **스키마에 숫자 minimum/maximum 금지(400 오류)** → 코드에서 클램프. Ollama qwen2.5vl:7b 폴백.
- 저장: SQLAlchemy(SQLite 기본, PostgreSQL 지원; bit_count((x # y)::bit(64))). 사진은 기본 미보관, INSPECTOR_PHOTO_STORE_DIR 설정 시 REVIEW/FAIL만 90일.
- 비동기: ?mode=async, DB 큐 + 워커, HMAC 웹훅, Idempotency-Key. 검수: /v1/reviews/queue, POST /v1/inspections/{id}/review.
- 브랜드: 로고 frontend/assets/meisters_logo.png (검정 #191A1A, 레드 #C4191F, 옐로 #F3CB0E). 이모지 금지, 한국어 라벨(합격/검토 필요/불합격).

## 4. 현재 상태 (2026-11-09)
- 사용자 로컬 Docker에서 전체 흐름 동작 확인(Claude 연결, 자동 컷 분류 포함). 실사진 기준값 보정은 아직.
- 진행 중: 시연용 UI/UX 개선 — frontend/dashboard.py 전면 재작성(브랜드 색, 단계형 흐름, 판정 히어로·KPI·칩·사진 카드, 검수 큐·이력·통계 탭), 로고 삽입 완료.
  스크린샷 검증 중(스크래치 ui2/). 확인된 것: 홈·업로드 화면 양호. 미확인: 결과 영역 하단, 검수 큐 화면.
- 커밋 전 상태: dashboard.py, .streamlit/config.toml, frontend/assets/* 변경분 미커밋.

## 5. 다음 할 일
1. UI 스크린샷(결과·검수 큐) 확인 후 미세 조정 → 커밋·푸시 → 사용자에게 `git pull && docker compose up -d --build` 안내.
2. 사용자 쪽: 실사진으로 선명도 보정, 검수 큐에 판정 쌓기(50건) → export_eval_set → evaluate_eval_set 로 정확도 측정.
3. 법무 검토(docs/PRIVACY.md), VC 덱 숫자 입력(3·15번 슬라이드)은 사용자 몫.

## 6. 환경·도구 주의점 (삽질 방지)
- 이 샌드박스: Docker 데몬은 `dockerd --host=unix://<scratch>/docker.sock --data-root=<scratch>/dockerroot` 로 띄울 수 있음(백그라운드 실행). 빌드는 `--network host` 필요.
  프록시가 apt 미러·Docker Hub 일부를 막음(429/403) → 베이스 이미지에 CA 넣은 검증용 사본으로 빌드. 저장소 Dockerfile은 apt 단계 없음.
- `pkill -f "<내 명령 문자열>"`은 자기 셸까지 죽임 → `pkill -x` 사용.
- Streamlit 대시보드 스크린샷: Playwright(Node, NODE_PATH=deck/node_modules) 사용. fullPage 캡처가 1000px에 멈춤 → viewport 높이를 크게 잡을 것.
- compose 변수 치환 `.env`는 compose 파일 폴더 기준 → compose 파일은 저장소 루트에 둠.
- YAML에서 `id: off` 는 불리언 → 테스트 id로 쓰지 말 것.
- Anthropic 구조화 출력: 숫자 min/max 불가, 프리필 불가, 강제 tool_choice 불가.
