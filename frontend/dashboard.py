"""
마이스터즈 AI 사진 검수 - 운영자 대시보드 (Streamlit)

백엔드(FastAPI)를 HTTP로 호출하는 얇은 클라이언트입니다. 작업 하나(사진 N장)를
올려 판정을 확인하고, 저장된 검사 이력을 조회합니다. 투자자 시연 흐름(작업 종류
선택 → 사진 업로드·컷 지정 → 검사 실행 → 판정과 근거 → 재제출)을 그대로 따릅니다.

실행 (백엔드가 먼저 떠 있어야 함):
    streamlit run frontend/dashboard.py
환경변수:
    INSPECTOR_API_URL  (기본 http://localhost:8000)
    INSPECTOR_API_KEY  (백엔드에 INSPECTOR_API_KEYS 가 설정된 경우)

표기 원칙 (DESIGN.md): 이모지 대신 색 표식, 숫자로 말하기, 한국어 우선.
"""
import json
import os

import requests
import streamlit as st

API_URL = os.environ.get("INSPECTOR_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("INSPECTOR_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}

BRAND = "마이스터즈 AI 사진 검수"
# 판정 → (라벨, 표시 함수). PASS/REVIEW/FAIL 은 API 코드값, 화면에는 한국어만.
VERDICT_LABEL = {"PASS": "합격", "REVIEW": "검토 필요", "FAIL": "불합격"}
VERDICT_BOX = {"PASS": st.success, "REVIEW": st.warning, "FAIL": st.error}
# 사진 상태 → (라벨, 색 표식). Streamlit 마크다운 색상 문법 사용.
STATUS_LABEL = {"OK": "정상", "FLAGGED": "지적", "REJECTED": "반려"}
code_by_label_review = {"합격": "PASS", "불합격": "FAIL", "검토 필요": "REVIEW"}
STATUS_MARK = {"OK": ":green[●]", "FLAGGED": ":orange[●]", "REJECTED": ":red[●]"}
SEV_LABEL = {"high": "심각", "medium": "보통", "low": "경미"}
SOURCE_LABEL = {
    "quality_check": "품질 검사", "fraud_check": "부정 검사", "checklist": "체크리스트",
    "vlm": "AI 분석", "anomaly_detector": "이상 탐지", "system": "시스템",
}

st.set_page_config(page_title=BRAND, page_icon=":material/photo_camera:", layout="wide")
st.title(BRAND)
st.caption("현장 작업 사진을 제출 즉시 검사합니다: 사진 품질 · 부정 제출 · 체크리스트 · 시공 하자")


@st.cache_data(ttl=60)
def fetch_categories():
    r = requests.get(f"{API_URL}/v1/categories", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def mark(ok: bool | None) -> str:
    if ok is None:
        return ":gray[●]"
    return ":green[●]" if ok else ":red[●]"


# ------------------------------------------------------------ 사이드바 ----
with st.sidebar:
    st.header("서비스 상태")
    try:
        health = requests.get(f"{API_URL}/health", timeout=3).json()
        st.success(f"검사 서비스 연결됨 (v{health.get('version')})")
        for name, info in (health.get("vlm_providers") or {}).items():
            label = {"claude": "Claude (기본)", "ollama": "로컬 모델 (폴백)"}.get(name, name)
            st.markdown(f"{mark(info.get('reachable'))} AI 분석 - {label} · `{info.get('model')}`")
            if info.get("error"):
                st.caption(info["error"])
        store = health.get("store", {})
        st.caption(
            f"인증 {'사용' if health.get('auth_enabled') else '미사용(개발 모드)'} · "
            f"저장된 검사 {store.get('inspections', '?')}건 · 사진 지문 {store.get('photo_hashes', '?')}개"
        )
    except requests.RequestException:
        st.error(f"검사 서비스에 연결할 수 없습니다: {API_URL}")
        st.stop()

tab_run, tab_review, tab_history, tab_stats = st.tabs(["검사 실행", "검수 큐", "검사 이력", "기사별 통계"])

# ------------------------------------------------------------ 검사 실행 ----
with tab_run:
    try:
        categories = fetch_categories()
    except requests.RequestException as exc:
        st.error(f"작업 종류 목록을 불러오지 못했습니다: {exc}")
        st.stop()

    cat_by_id = {c["id"]: c for c in categories}
    # 그룹 → 작업 종류 2단계 선택. 그룹 순서는 API(_groups.yaml order)를 따름.
    group_names: dict[str, str] = {}
    for c in categories:
        group_names.setdefault(c["group"], c.get("group_name") or c["group"])
    col_a, col_b = st.columns([1, 2])
    with col_a:
        group_id = st.selectbox("작업 그룹", list(group_names), format_func=lambda g: f"{group_names[g]} ({sum(1 for c in categories if c['group'] == g)})")
        in_group = [c["id"] for c in categories if c["group"] == group_id]
        cat_id = st.selectbox("작업 종류", in_group, format_func=lambda i: cat_by_id[i]["name"])
        job_id = st.text_input("작업 번호", value="demo-001")
        technician = st.text_input("기사 ID", value="")
        notes = st.text_area("기사 메모 (선택)", height=80)
        run_vlm = st.checkbox("AI 하자 분석 실행", value=True)
        history_check = st.checkbox("이전 작업 사진 재사용 검사", value=True)
    with col_b:
        cat = cat_by_id[cat_id]
        st.markdown(f"**{cat['name']}** - {cat['description']}")
        req = [s for s in cat["shots"] if s["required"]]
        st.markdown(f"**필수 컷 {len(req)}개:** " + ", ".join(s["name"] for s in req))
        st.markdown(f"**검사 하자 항목 {len(cat['defect_codes'])}개:** " + ", ".join(d["name"] for d in cat["defect_codes"]))

    st.subheader("1. 사진 업로드와 컷 지정")
    uploads = st.file_uploader("작업 사진 (여러 장)", type=["png", "jpg", "jpeg", "heic", "heif"], accept_multiple_files=True)
    shot_options = ["(미지정)"] + [s["id"] for s in cat["shots"]]
    shot_names = {s["id"]: s["name"] for s in cat["shots"]}
    phase_options = {"(미지정)": None, "작업 전": "before", "작업 후": "after"}
    photo_meta = []
    if uploads:
        cols = st.columns(min(4, len(uploads)))
        for i, up in enumerate(uploads):
            with cols[i % len(cols)]:
                st.image(up.getvalue(), caption=up.name, use_container_width=True)
                pid = up.name.rsplit(".", 1)[0]
                shot = st.selectbox("컷", shot_options, key=f"shot_{i}", format_func=lambda s: s if s == "(미지정)" else shot_names[s])
                phase = st.selectbox("단계", list(phase_options), key=f"phase_{i}")
                meta = {"photo_id": pid}
                if shot != "(미지정)":
                    meta["declared_shot_type"] = shot
                if phase_options[phase]:
                    meta["phase"] = phase_options[phase]
                photo_meta.append(meta)

    st.subheader("2. 검사 실행")
    if st.button("검사 실행", type="primary", disabled=not uploads):
        job = {"job_id": job_id, "category": cat_id}
        if technician:
            job["technician_id"] = technician
        if notes:
            job["notes"] = notes
        files = [("files", (up.name, up.getvalue(), up.type or "image/jpeg")) for up in uploads]
        data = {
            "job": json.dumps(job, ensure_ascii=False),
            "photos": json.dumps(photo_meta, ensure_ascii=False),
            "options": json.dumps({"run_vlm": run_vlm, "run_fraud_history": history_check, "return_thumbnails": False}),
        }
        with st.spinner("검사 중입니다. 규칙 검사는 즉시, AI 분석은 사진당 수 초가 걸립니다."):
            try:
                r = requests.post(f"{API_URL}/v1/inspections/upload", data=data, files=files, headers=HEADERS, timeout=600)
                r.raise_for_status()
                st.session_state["last_result"] = r.json()
                st.session_state["last_images"] = {up.name.rsplit(".", 1)[0]: up.getvalue() for up in uploads}
            except requests.HTTPError as exc:
                st.error(f"검사 실패 ({exc.response.status_code}): {exc.response.text}")
            except requests.RequestException as exc:
                st.error(f"검사 실패: {exc}")

    result = st.session_state.get("last_result")
    if result:
        v = result["verdict"]
        label = VERDICT_LABEL[v]
        summary = result["summary"]
        if summary.startswith(label):  # 요약문이 이미 "불합격 - ..." 로 시작하면 라벨을 중복 표시하지 않음
            summary = summary[len(label):].lstrip(" -")
        VERDICT_BOX[v](f"**{label}** - {summary}")

        st.markdown("**판정 근거**")
        for reason in result["verdict_reasons"]:
            st.write(f"- {reason}")

        st.markdown("**체크리스트**")
        cl_cols = st.columns(min(4, max(1, len(result["checklist"]))))
        for i, item in enumerate(result["checklist"]):
            with cl_cols[i % len(cl_cols)]:
                m = mark(item["satisfied"]) if item["required"] else (":green[●]" if item["satisfied"] else ":gray[●]")
                st.markdown(f"{m} {item['name']}" + (" *(필수)*" if item["required"] else ""))
                if item["photo_ids"]:
                    st.caption(", ".join(item["photo_ids"]))

        st.markdown("**사진별 결과**")
        imgs = st.session_state.get("last_images", {})
        for p in result["photos"]:
            shot_label = shot_names.get(p.get("resolved_shot_type"), p.get("resolved_shot_type")) or "미분류"
            with st.expander(f"{STATUS_LABEL[p['status']]} · {p['photo_id']} · 컷: {shot_label}", expanded=p["status"] != "OK"):
                c1, c2 = st.columns([1, 2])
                with c1:
                    st.markdown(f"{STATUS_MARK[p['status']]} {STATUS_LABEL[p['status']]}")
                    if p["photo_id"] in imgs:
                        st.image(imgs[p["photo_id"]], use_container_width=True)
                    q = p["quality"]
                    st.caption(f"{q['width']}×{q['height']} · 선명도 {q['blur_score']:.0f} · 밝기 {q['brightness']:.0f}")
                    a = p["analysis"]
                    if a.get("provider") and a["provider"] != "none":
                        st.caption(f"AI 분석: {a['provider']} ({a.get('model')}) · {a.get('latency_ms', 0):.0f} ms")
                with c2:
                    a = p["analysis"]
                    if a.get("scene_description"):
                        st.write(a["scene_description"])
                    declared = p.get("declared_shot_type")
                    if declared and declared != p.get("resolved_shot_type"):
                        st.caption(f"신고 컷: {shot_names.get(declared, declared)} → 판독 컷: {shot_label}")
                    if not p["findings"]:
                        st.write("지적 사항 없음")
                    for f in p["findings"]:
                        sev = SEV_LABEL.get(f["severity"], f["severity"])
                        where = f" (위치: {f['location_hint']})" if f.get("location_hint") else ""
                        src = SOURCE_LABEL.get(f["source"], f["source"])
                        st.markdown(f"- **[{sev}]** {f['message']}{where}")
                        st.caption(f"{src} · 확신도 {f['confidence']:.2f}" + (f" · 근거: {f['evidence']}" if f.get("evidence") else ""))

        if result["job_findings"]:
            st.markdown("**작업 단위 지적 사항**")
            for f in result["job_findings"]:
                st.markdown(f"- **[{SEV_LABEL.get(f['severity'], f['severity'])}]** {f['message']}")
                st.caption(f"{SOURCE_LABEL.get(f['source'], f['source'])} · 확신도 {f['confidence']:.2f}")

        s = result["stats"]
        st.caption(
            f"검사 번호 {result['inspection_id']} · 처리 {s['latency_ms']/1000:.1f}초 · AI 호출 {s['vlm_calls']}회"
            f"(실패 {s['vlm_errors']}) · 토큰 입력 {s['vlm_input_tokens']} / 출력 {s['vlm_output_tokens']}"
            f" · AI 공급자 {result.get('vlm_provider_used') or '-'}"
        )
        st.download_button("결과 JSON 다운로드", json.dumps(result, ensure_ascii=False, indent=2), file_name=f"{result['inspection_id']}.json", mime="application/json")

# ------------------------------------------------------------ 검수 큐 ----
with tab_review:
    st.caption("자동 판정이 '검토 필요'인 작업을 사람이 확인하고 최종 판정을 남깁니다. 저장된 판정은 평가 세트의 정답으로 쓰입니다.")
    reviewer = st.text_input("검수자 이름", value=st.session_state.get("reviewer", ""), key="reviewer_name")
    st.session_state["reviewer"] = reviewer
    try:
        queue = requests.get(f"{API_URL}/v1/reviews/queue", params={"limit": 100}, headers=HEADERS, timeout=10).json()
    except requests.RequestException as exc:
        st.error(f"검수 큐를 불러오지 못했습니다: {exc}")
        queue = []
    if not queue:
        st.success("검수 대기 중인 작업이 없습니다.")
    else:
        st.write(f"대기 {len(queue)}건")
        pick = st.selectbox("검수할 작업", [q["inspection_id"] for q in queue], format_func=lambda i: next(f"{q['job_id']} · {q['category']} · {q['created_at'][:16]}" for q in queue if q["inspection_id"] == i))
        detail = requests.get(f"{API_URL}/v1/inspections/{pick}", headers=HEADERS, timeout=10).json()
        st.warning(f"**검토 필요** - {detail['summary']}")
        for reason in detail["verdict_reasons"]:
            st.write(f"- {reason}")
        cols = st.columns(min(4, max(1, len(detail["photos"]))))
        for i, p in enumerate(detail["photos"]):
            with cols[i % len(cols)]:
                st.markdown(f"{STATUS_MARK[p['status']]} **{p['photo_id']}** · {p.get('resolved_shot_type') or '미분류'}")
                if p.get("stored"):
                    st.image(f"{API_URL}/v1/inspections/{pick}/photos/{p['photo_id']}" if not API_KEY else requests.get(f"{API_URL}/v1/inspections/{pick}/photos/{p['photo_id']}", headers=HEADERS, timeout=10).content, use_container_width=True)
                else:
                    st.caption("사진 미보관 (INSPECTOR_PHOTO_STORE_DIR 설정 시 표시)")
                for f in p["findings"]:
                    st.caption(f"[{SEV_LABEL.get(f['severity'], f['severity'])}] {f['message']}")
        c1, c2, c3 = st.columns([1, 1, 2])
        final = c1.radio("최종 판정", ["합격", "불합격", "검토 필요"], horizontal=False)
        notes = c3.text_area("검수 메모 (하자 코드는 #코드 형태로 적으면 평가 세트 라벨이 됩니다. 예: #exposed_wiring 확인)", height=100)
        if c2.button("판정 저장", type="primary", disabled=not reviewer):
            body = {"verdict": code_by_label_review[final], "reviewer": reviewer, "notes": notes or None}
            r = requests.post(f"{API_URL}/v1/inspections/{pick}/review", json=body, headers=HEADERS, timeout=10)
            if r.ok:
                st.success("저장했습니다. 다음 작업을 선택하세요.")
                st.rerun()
            else:
                st.error(f"저장 실패 ({r.status_code}): {r.text}")

# ------------------------------------------------------------ 검사 이력 ----
with tab_history:
    f1, f2, f3 = st.columns(3)
    with f1:
        h_job = st.text_input("작업 번호 필터", value="")
    with f2:
        h_verdict = st.selectbox("판정 필터", ["(전체)", "합격", "검토 필요", "불합격"])
    with f3:
        h_limit = st.number_input("최대 건수", min_value=1, max_value=500, value=50)
    params = {"limit": int(h_limit)}
    if h_job:
        params["job_id"] = h_job
    code_by_label = {v: k for k, v in VERDICT_LABEL.items()}
    if h_verdict != "(전체)":
        params["verdict"] = code_by_label[h_verdict]
    try:
        rows = requests.get(f"{API_URL}/v1/inspections", params=params, headers=HEADERS, timeout=10).json()
    except requests.RequestException as exc:
        st.error(f"이력을 불러오지 못했습니다: {exc}")
        rows = []
    if rows:
        st.dataframe(
            [{"검사 번호": r["inspection_id"], "작업 번호": r["job_id"], "기사": r.get("technician_id") or "", "작업 종류": r["category"], "자동 판정": VERDICT_LABEL.get(r["verdict"], r["verdict"]), "검수 판정": VERDICT_LABEL.get(r.get("review_verdict"), "") if r.get("review_verdict") else "", "사진 수": r["num_photos"], "검사 시각": r["created_at"][:19]} for r in rows],
            use_container_width=True,
        )
        pick = st.selectbox("상세 보기", [r["inspection_id"] for r in rows])
        if pick and st.button("불러오기"):
            r = requests.get(f"{API_URL}/v1/inspections/{pick}", headers=HEADERS, timeout=10)
            if r.ok:
                st.session_state["last_result"] = r.json()
                st.session_state["last_images"] = {}
                st.info("'검사 실행' 탭에서 결과를 확인하세요.")
    else:
        st.info("저장된 검사가 없습니다.")

# ------------------------------------------------------------ 기사별 통계 ----
with tab_stats:
    days = st.slider("기간 (일)", min_value=7, max_value=180, value=30)
    try:
        stats = requests.get(f"{API_URL}/v1/stats/technicians", params={"days": days}, headers=HEADERS, timeout=10).json()
    except requests.RequestException as exc:
        st.error(f"통계를 불러오지 못했습니다: {exc}")
        stats = []
    if stats:
        st.dataframe(
            [{"기사": s_["technician_id"], "검사 수": s_["total"], "합격": s_["PASS"], "검토": s_["REVIEW"], "불합격": s_["FAIL"], "합격률": f"{s_['pass_rate']*100:.0f}%", "불합격률": f"{s_['fail_rate']*100:.0f}%"} for s_ in stats],
            use_container_width=True,
        )
        st.caption("불합격률이 높은 순. 기사 앱이 technician_id 를 보내는 작업만 집계됩니다.")
    else:
        st.info("집계할 데이터가 없습니다.")
