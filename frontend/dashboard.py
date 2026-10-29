"""
운영자용 Streamlit 대시보드 - AI-Visual-Inspector (현장 AS/설치 사진 검증)

백엔드(FastAPI)를 HTTP로 호출하는 얇은 클라이언트입니다. 작업 하나(사진 N장)를
올려 판정을 확인하고, 저장된 검사 이력을 조회합니다.

실행 (백엔드가 먼저 떠 있어야 함):
    streamlit run frontend/dashboard.py
환경변수:
    INSPECTOR_API_URL  (기본 http://localhost:8000)
    INSPECTOR_API_KEY  (백엔드에 INSPECTOR_API_KEYS 가 설정된 경우)
"""
import base64
import io
import json
import os

import requests
import streamlit as st
from PIL import Image

API_URL = os.environ.get("INSPECTOR_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("INSPECTOR_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}

VERDICT_STYLE = {"PASS": ("✅ 합격", st.success), "REVIEW": ("🟡 검토 필요", st.warning), "FAIL": ("❌ 불합격", st.error)}
STATUS_ICON = {"OK": "🟢", "FLAGGED": "🟡", "REJECTED": "🔴"}
SEV_LABEL = {"high": "심각", "medium": "보통", "low": "경미"}

st.set_page_config(page_title="AI-Visual-Inspector", page_icon="🔍", layout="wide")
st.title("🔍 AI-Visual-Inspector - 현장 작업 사진 검증")


@st.cache_data(ttl=60)
def fetch_categories():
    r = requests.get(f"{API_URL}/v1/categories", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


with st.sidebar:
    st.header("백엔드 상태")
    try:
        health = requests.get(f"{API_URL}/health", timeout=3).json()
        st.success(f"온라인 (v{health.get('version')})")
        for name, info in (health.get("vlm_providers") or {}).items():
            icon = "🟢" if info.get("reachable") else ("⚪" if info.get("reachable") is None else "🔴")
            st.write(f"{icon} VLM `{name}` - {info.get('model')}")
            if info.get("error"):
                st.caption(info["error"])
        st.caption(f"인증: {'활성' if health.get('auth_enabled') else '비활성(개발 모드)'} · 저장된 검사 {health.get('store', {}).get('inspections', '?')}건")
    except requests.RequestException:
        st.error(f"백엔드에 연결할 수 없습니다: {API_URL}")
        st.stop()

tab_run, tab_history = st.tabs(["검사 실행", "검사 이력"])

# ------------------------------------------------------------------ run ----
with tab_run:
    try:
        categories = fetch_categories()
    except requests.RequestException as exc:
        st.error(f"카테고리를 불러오지 못했습니다: {exc}")
        st.stop()

    cat_by_id = {c["id"]: c for c in categories}
    groups = {"appliance": "가전", "housing": "주거 시공"}
    col_a, col_b = st.columns([1, 2])
    with col_a:
        cat_id = st.selectbox("작업 종류", list(cat_by_id), format_func=lambda i: f"[{groups.get(cat_by_id[i]['group'], cat_by_id[i]['group'])}] {cat_by_id[i]['name']}")
        job_id = st.text_input("작업 번호 (job_id)", value="demo-001")
        technician = st.text_input("기사 ID", value="")
        notes = st.text_area("기사 메모 (선택)", height=80)
        run_vlm = st.checkbox("VLM 하자 분석 실행", value=True)
        history_check = st.checkbox("이전 작업 사진 재사용 검사", value=True)
    with col_b:
        cat = cat_by_id[cat_id]
        st.markdown(f"**{cat['name']}** - {cat['description']}")
        req = [s for s in cat["shots"] if s["required"]]
        st.markdown("**필수 컷:** " + ", ".join(f"`{s['id']}` {s['name']}" for s in req))
        st.markdown("**검사 하자 코드:** " + ", ".join(d["name"] for d in cat["defect_codes"]))

    st.subheader("1. 사진 업로드 및 컷 지정")
    uploads = st.file_uploader("작업 사진 (여러 장)", type=["png", "jpg", "jpeg"], accept_multiple_files=True)
    shot_options = ["(미지정)"] + [s["id"] for s in cat["shots"]]
    shot_names = {s["id"]: s["name"] for s in cat["shots"]}
    photo_meta = []
    if uploads:
        cols = st.columns(min(4, len(uploads)))
        for i, up in enumerate(uploads):
            with cols[i % len(cols)]:
                st.image(up.getvalue(), caption=up.name, use_container_width=True)
                pid = up.name.rsplit(".", 1)[0]
                shot = st.selectbox("컷", shot_options, key=f"shot_{i}", format_func=lambda s: s if s == "(미지정)" else f"{s} · {shot_names[s]}")
                phase = st.selectbox("단계", ["(미지정)", "before", "after"], key=f"phase_{i}")
                meta = {"photo_id": pid}
                if shot != "(미지정)":
                    meta["declared_shot_type"] = shot
                if phase != "(미지정)":
                    meta["phase"] = phase
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
        with st.spinner("검사 중... (VLM 분석은 사진당 수 초가 걸립니다)"):
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
        label, box = VERDICT_STYLE[result["verdict"]]
        box(f"**{label}** - {result['summary']}")
        st.markdown("**판정 근거**")
        for reason in result["verdict_reasons"]:
            st.write(f"- {reason}")

        st.markdown("**체크리스트**")
        cl_cols = st.columns(min(4, max(1, len(result["checklist"]))))
        for i, item in enumerate(result["checklist"]):
            with cl_cols[i % len(cl_cols)]:
                mark = "✅" if item["satisfied"] else ("❌" if item["required"] else "➖")
                st.write(f"{mark} {item['name']}" + (" *(필수)*" if item["required"] else ""))
                if item["photo_ids"]:
                    st.caption(", ".join(item["photo_ids"]))

        st.markdown("**사진별 결과**")
        imgs = st.session_state.get("last_images", {})
        for p in result["photos"]:
            with st.expander(f"{STATUS_ICON[p['status']]} {p['photo_id']} - {p['status']} · 컷: {p.get('resolved_shot_type') or '미분류'}", expanded=p["status"] != "OK"):
                c1, c2 = st.columns([1, 2])
                with c1:
                    if p["photo_id"] in imgs:
                        st.image(imgs[p["photo_id"]], use_container_width=True)
                    q = p["quality"]
                    st.caption(f"{q['width']}x{q['height']} · 선명도 {q['blur_score']:.0f} · 밝기 {q['brightness']:.0f}")
                    a = p["analysis"]
                    if a.get("provider") and a["provider"] != "none":
                        st.caption(f"VLM: {a['provider']} ({a.get('model')}) · {a.get('latency_ms', 0):.0f} ms")
                with c2:
                    a = p["analysis"]
                    if a.get("scene_description"):
                        st.write(f"🖼️ {a['scene_description']}")
                    if p["declared_shot_type"] and p["declared_shot_type"] != p.get("resolved_shot_type"):
                        st.caption(f"신고 컷: {p['declared_shot_type']} → 판독 컷: {p.get('resolved_shot_type') or '해당 없음'}")
                    if not p["findings"]:
                        st.write("지적 사항 없음")
                    for f in p["findings"]:
                        sev = SEV_LABEL.get(f["severity"], f["severity"])
                        where = f" (위치: {f['location_hint']})" if f.get("location_hint") else ""
                        st.write(f"- **[{sev}]** {f['message']}{where}  \n  <sub>{f['source']} · 확신도 {f['confidence']:.2f}</sub>", unsafe_allow_html=True)
                        if f.get("evidence"):
                            st.caption(f"근거: {f['evidence']}")

        if result["job_findings"]:
            st.markdown("**작업 단위 지적 사항**")
            for f in result["job_findings"]:
                st.write(f"- **[{SEV_LABEL.get(f['severity'], f['severity'])}]** {f['message']} <sub>{f['source']}</sub>", unsafe_allow_html=True)

        s = result["stats"]
        st.caption(
            f"검사 ID {result['inspection_id']} · {s['latency_ms']:.0f} ms · VLM 호출 {s['vlm_calls']}회 "
            f"(오류 {s['vlm_errors']}) · 토큰 in {s['vlm_input_tokens']} / out {s['vlm_output_tokens']} · 공급자 {result.get('vlm_provider_used') or '-'}"
        )
        st.download_button("결과 JSON 다운로드", json.dumps(result, ensure_ascii=False, indent=2), file_name=f"{result['inspection_id']}.json", mime="application/json")

# -------------------------------------------------------------- history ----
with tab_history:
    f1, f2, f3 = st.columns(3)
    with f1:
        h_job = st.text_input("작업 번호 필터", value="")
    with f2:
        h_verdict = st.selectbox("판정 필터", ["(전체)", "PASS", "REVIEW", "FAIL"])
    with f3:
        h_limit = st.number_input("최대 건수", min_value=1, max_value=500, value=50)
    params = {"limit": int(h_limit)}
    if h_job:
        params["job_id"] = h_job
    if h_verdict != "(전체)":
        params["verdict"] = h_verdict
    try:
        rows = requests.get(f"{API_URL}/v1/inspections", params=params, headers=HEADERS, timeout=10).json()
    except requests.RequestException as exc:
        st.error(f"이력을 불러오지 못했습니다: {exc}")
        rows = []
    if rows:
        st.dataframe(
            [{"검사 ID": r["inspection_id"], "작업": r["job_id"], "카테고리": r["category"], "판정": r["verdict"], "사진": r["num_photos"], "시각": r["created_at"]} for r in rows],
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
