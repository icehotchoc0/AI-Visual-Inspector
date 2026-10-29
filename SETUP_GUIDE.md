# Setup Guide - 마이스터즈 AI 사진 검수 (AI-Visual-Inspector)

## 0. 요구 사항

- Python 3.10+ (3.11 권장)
- Anthropic API 키 (Claude 공급자) - 없으면 `INSPECTOR_VLM_PROVIDERS=ollama` 로 로컬 전용 운영 가능
- (선택) [Ollama](https://ollama.com) + `ollama pull qwen2.5vl:7b` - 로컬 폴백
- (선택) NVIDIA GPU - 이상 탐지 플러그인 학습용(`requirements-anomaly.txt`). 운영 API 자체는 GPU 불필요

## 1. 설치

```bash
python -m venv venv
source venv/bin/activate          # Windows PowerShell: .\venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

`.env` 에서 최소 설정:

```ini
ANTHROPIC_API_KEY=sk-ant-...
INSPECTOR_API_KEYS=change-me-1          # 서비스 백엔드가 X-API-Key 로 보낼 값
INSPECTOR_VLM_PROVIDERS=claude,ollama   # Ollama 없으면 claude 만
```

## 2. 실행

```bash
uvicorn backend.app:app --host 0.0.0.0 --port 8000
```

```bash
INSPECTOR_API_KEY=change-me-1 streamlit run frontend/dashboard.py
```

- API 문서: http://localhost:8000/docs
- 상태: http://localhost:8000/health - `vlm_providers.claude.reachable` 가 `true` 인지 확인
- 대시보드: http://localhost:8501

Windows 는 `run_inspector.ps1` / `run_inspector.bat` 로 두 프로세스를 한 번에 띄울 수 있습니다.

## 3. 테스트

```bash
pytest tests/
```

GPU·네트워크·API 키 없이 돌아갑니다(VLM 은 FakeVLM, Claude SDK 는 모킹). 64개 테스트가 품질/부정/체크리스트/판정 규칙,
라우터 폴백, 파이프라인 시나리오(PASS/FAIL/REVIEW), API(JSON·multipart·인증)를 덮습니다.

## 4. Docker

```bash
cd docker
docker compose up --build            # backend :8000, dashboard :8501
docker compose build --build-arg ANOMALY=1 backend   # 이상 탐지 플러그인 포함 이미지
```

`../.env` 를 그대로 읽습니다. 호스트의 Ollama 는 `host.docker.internal` 로 연결됩니다.

## 5. 카테고리 추가/수정

`config/categories.yaml` 을 편집하면 재시작 후 바로 반영됩니다. 항목 설명은 파일 상단 주석 참고.
변경 뒤 `pytest tests/test_categories.py` 로 참조 무결성(전후 쌍·이상 탐지 컷 id)을 확인하세요.

## 6. 평가 세트로 정확도 측정 (권장)

`docs/DATA_SPEC.md` §1 규격으로 라벨된 작업을 모은 뒤:

```bash
python training/evaluate_eval_set.py --category aircon_install --no-vlm   # 결정적 검사만, 무료
python training/evaluate_eval_set.py --category aircon_install            # 실제 Claude/Ollama 호출 (토큰 과금)
```

판정 정확도·컷 분류 정확도·하자 코드별 precision/recall 이 출력되고 `report_*.json` 으로 저장됩니다.
프롬프트(`backend/vlm/prompts.py`)나 임계값(YAML `verdict.min_defect_confidence`)을 바꾼 뒤 비교하세요.

## 7. 이상 탐지 플러그인 (선택)

고정 구도 컷(예: 실외기 거치대)의 정상 사진이 30장 이상 모이면:

```bash
pip install -r requirements-anomaly.txt   # CUDA torch 는 먼저 설치 (아래 참고)
python training/train_folder.py --model-key aircon_outdoor_unit --backend patchcore
```

그리고 `categories.yaml` 해당 카테고리에 `anomaly: {enabled: true, model_key: aircon_outdoor_unit, shot_types: [outdoor_unit_after]}`.

CUDA 빌드 torch 는 PyPI 기본 휠이 아니므로 `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128`
을 **먼저** 실행한 뒤 `requirements-anomaly.txt` 를 설치하세요(`torch.cuda.is_available()` 로 확인).

## 8. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| `/health` 에서 `claude.reachable=false`, "no ANTHROPIC_API_KEY" | `.env` 또는 환경변수에 키 없음 |
| 판정이 전부 `REVIEW`, 근거에 "VLM 공급자에 모두 연결할 수 없어" | 두 공급자 모두 실패. 백엔드 로그의 `VLM provider ... failed` 줄 확인 |
| Ollama 가 `0.0.0.0` 에 연결 시도 | 시스템 `OLLAMA_HOST` 가 서버 바인드용으로 설정된 경우. 이 프로젝트는 `INSPECTOR_OLLAMA_HOST` 만 읽으므로 그 값을 확인 |
| 사진이 전부 `REJECTED` | 블러 임계값(`quality.blur_threshold`)이 촬영 기기와 안 맞음. 응답의 `quality.blur_score` 분포를 보고 YAML 조정 |
| 중복 오탐 (`duplicate_in_job`) | 같은 설비를 거의 같은 구도로 찍은 경우. `fraud.duplicate_hamming_max` 를 4 로 낮추거나 컷 가이드로 구도 분리 |
| 401 | `X-API-Key` 헤더 누락 또는 `INSPECTOR_API_KEYS` 불일치 |

## 9. 레거시: MVTec AD 벤치마크

원본 데모의 `training/train_patchcore.py`, `train_efficientad.py`, `evaluate.py` 는 그대로 동작합니다
(`MVTEC_ROOT`, `MVTEC_CATEGORIES`). 운영 서비스와는 독립입니다.
