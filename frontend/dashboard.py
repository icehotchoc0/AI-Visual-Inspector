"""
마이스터즈 AI 사진 검수 - 운영자 대시보드 (Streamlit)

백엔드(FastAPI)를 HTTP로 호출하는 얇은 클라이언트입니다. 시연 흐름을 그대로 따릅니다:
작업 정보 → 사진 업로드(컷은 AI가 자동 분류, 지정도 가능) → 검사 실행 → 판정·근거·사진별 결과.

실행:  INSPECTOR_API_URL=http://localhost:8000 INSPECTOR_API_KEY=<키> streamlit run frontend/dashboard.py
표기 원칙: 이모지 대신 색 표식·배지, 숫자로 말하기, 한국어 우선. 브랜드 색은 로고(검정·레드·옐로)에서 가져옴.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

import requests
import streamlit as st

API_URL = os.environ.get("INSPECTOR_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("INSPECTOR_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}
ASSETS = Path(__file__).resolve().parent / "assets"
BRAND = "마이스터즈 AI 사진 검수"

# ---- 브랜드 토큰 (로고에서 추출) --------------------------------------------
C_BLACK, C_RED, C_YELLOW = "#191A1A", "#C4191F", "#F3CB0E"
C_GREEN, C_GRAY, C_BG, C_LINE = "#1B8A4C", "#6B7280", "#F4F5F7", "#E5E7EB"
VERDICT = {
    "PASS": {"label": "합격", "color": C_GREEN, "bg": "#E8F5EE", "desc": "자동 통과"},
    "REVIEW": {"label": "검토 필요", "color": "#B8860B", "bg": "#FFF7D6", "desc": "애매한 건, 사람이 확인"},
    "FAIL": {"label": "불합격", "color": C_RED, "bg": "#FDECEC", "desc": "근거와 함께 반려"},
}
STATUS = {"OK": ("정상", C_GREEN), "FLAGGED": ("지적", "#B8860B"), "REJECTED": ("반려", C_RED)}
SEV = {"high": ("심각", C_RED), "medium": ("보통", "#B8860B"), "low": ("경미", C_GRAY)}
SOURCE = {"quality_check": "품질 검사", "fraud_check": "부정 검사", "checklist": "체크리스트", "vlm": "AI 분석", "anomaly_detector": "이상 탐지", "system": "시스템"}
GROUP_FALLBACK = {"appliance": "가전", "housing": "주거 시공", "commercial": "업무·교육 시설"}

st.set_page_config(page_title=BRAND, page_icon=str(ASSETS / "meisters_emblem.png"), layout="wide", initial_sidebar_state="expanded")

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;800&display=swap');
html, body, [class*="css"], .stMarkdown, .stText {{ font-family: 'Noto Sans KR', -apple-system, 'Apple SD Gothic Neo', sans-serif; }}
.block-container {{ padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1280px; }}
h1, h2, h3 {{ letter-spacing: -0.02em; }}
.ms-card {{ background: #fff; border: 1px solid {C_LINE}; border-radius: 14px; padding: 18px 20px; margin-bottom: 14px; }}
.ms-card.soft {{ background: {C_BG}; border-color: {C_BG}; }}
.ms-step {{ display: flex; align-items: center; gap: 10px; margin: 18px 0 8px; }}
.ms-step .n {{ width: 28px; height: 28px; border-radius: 50%; background: {C_BLACK}; color: #fff; font-weight: 800; display: flex; align-items: center; justify-content: center; font-size: 14px; }}
.ms-step .t {{ font-size: 18px; font-weight: 700; }}
.ms-step .s {{ color: {C_GRAY}; font-size: 13px; margin-left: 4px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; color: #fff; line-height: 20px; vertical-align: middle; }}
.badge.outline {{ background: #fff !important; border: 1px solid {C_LINE}; color: {C_BLACK}; font-weight: 500; }}
.chip {{ display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; border-radius: 999px; background: #fff; border: 1px solid {C_LINE}; font-size: 13px; margin: 0 6px 6px 0; }}
.chip .dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; }}
.hero {{ border-radius: 16px; padding: 22px 26px; display: flex; gap: 18px; align-items: center; border: 1px solid transparent; }}
.hero .v {{ font-size: 34px; font-weight: 800; letter-spacing: -0.02em; line-height: 1.1; }}
.hero .d {{ font-size: 13px; opacity: .75; margin-top: 2px; }}
.hero .sum {{ font-size: 16px; margin-top: 6px; }}
.kpi {{ background: #fff; border: 1px solid {C_LINE}; border-radius: 14px; padding: 14px 16px; }}
.kpi .l {{ color: {C_GRAY}; font-size: 12px; }}
.kpi .v {{ font-size: 24px; font-weight: 800; margin-top: 2px; letter-spacing: -0.02em; }}
.kpi .s {{ color: {C_GRAY}; font-size: 12px; margin-top: 2px; }}
.finding {{ padding: 8px 0; border-bottom: 1px dashed {C_LINE}; font-size: 14px; }}
.finding:last-child {{ border-bottom: none; }}
.finding .meta {{ color: {C_GRAY}; font-size: 12px; margin-top: 2px; }}
.photo-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
.photo-head .id {{ font-weight: 700; font-size: 14px; }}
.muted {{ color: {C_GRAY}; font-size: 13px; }}
.ms-brand {{ display: flex; align-items: center; gap: 14px; margin-bottom: 6px; }}
.ms-brand .t {{ font-size: 26px; font-weight: 800; letter-spacing: -0.02em; }}
.ms-brand .sub {{ color: {C_GRAY}; font-size: 14px; }}
.status-line {{ display: flex; gap: 8px; flex-wrap: wrap; }}
div[data-testid="stSidebar"] .block-container {{ padding-top: 1rem; }}
.stButton > button[kind="primary"] {{ font-weight: 700; font-size: 16px; padding: 10px 18px; border-radius: 10px; }}
</style>
""", unsafe_allow_html=True)


# ---- helpers ----------------------------------------------------------------
def api_get(path, **kw):
    r = requests.get(f"{API_URL}{path}", headers=HEADERS, timeout=kw.pop("timeout", 15), **kw)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=60)
def fetch_categories():
    return api_get("/v1/categories")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def badge(text, color, outline=False) -> str:
    cls = "badge outline" if outline else "badge"
    style = f"background:{color}" if not outline else f"border-color:{color};color:{color}"
    return f'<span class="{cls}" style="{style}">{esc(text)}</span>'


def chip(text, color=None, sub=None) -> str:
    dot = f'<span class="dot" style="background:{color}"></span>' if color else ""
    extra = f' <span class="muted">{esc(sub)}</span>' if sub else ""
    return f'<span class="chip">{dot}{esc(text)}{extra}</span>'


def step(n, title, sub=""):
    st.markdown(f'<div class="ms-step"><div class="n">{n}</div><div class="t">{esc(title)}</div><div class="s">{esc(sub)}</div></div>', unsafe_allow_html=True)


def kpi(label, value, sub=""):
    st.markdown(f'<div class="kpi"><div class="l">{esc(label)}</div><div class="v">{esc(value)}</div><div class="s">{esc(sub)}</div></div>', unsafe_allow_html=True)


def hero(verdict, summary):
    v = VERDICT[verdict]
    summ = summary
    if summ.startswith(v["label"]):
        summ = summ[len(v["label"]):].lstrip(" -")
    st.markdown(
        f'<div class="hero" style="background:{v["bg"]};border-color:{v["color"]}33">'
        f'<div style="width:14px;height:64px;border-radius:7px;background:{v["color"]}"></div>'
        f'<div><div class="v" style="color:{v["color"]}">{esc(v["label"])}</div><div class="d">{esc(v["desc"])}</div><div class="sum">{esc(summ)}</div></div></div>',
        unsafe_allow_html=True,
    )


def finding_html(f, show_photo=False) -> str:
    sev_label, sev_color = SEV.get(f["severity"], (f["severity"], C_GRAY))
    where = f' · 위치: {esc(f["location_hint"])}' if f.get("location_hint") else ""
    who = f' · {esc(f["photo_id"])}' if show_photo and f.get("photo_id") else ""
    ev = f'<div class="meta">근거: {esc(f["evidence"])}</div>' if f.get("evidence") else ""
    return (f'<div class="finding">{badge(sev_label, sev_color)} {esc(f["message"])}'
            f'<div class="meta">{esc(SOURCE.get(f["source"], f["source"]))} · 확신도 {f["confidence"]:.2f}{where}{who}</div>{ev}</div>')


def fetch_photo(inspection_id, photo_id):
    try:
        pr = requests.get(f"{API_URL}/v1/inspections/{inspection_id}/photos/{photo_id}", headers=HEADERS, timeout=20)
        if pr.ok and pr.headers.get("content-type", "").startswith("image/"):
            return pr.content, None
        return None, f"{pr.status_code}: {pr.text[:80]}"
    except requests.RequestException as exc:
        return None, str(exc)


def render_photos(result, images: dict | None, cols_n=4):
    photos = result["photos"]
    cols = st.columns(min(cols_n, max(1, len(photos))))
    shot_names = st.session_state.get("shot_names", {})
    for i, p in enumerate(photos):
        with cols[i % len(cols)]:
            s_label, s_color = STATUS[p["status"]]
            shot = shot_names.get(p.get("resolved_shot_type"), p.get("resolved_shot_type")) or "미분류"
            st.markdown(f'<div class="photo-head"><span class="id">{esc(p["photo_id"])}</span>{badge(s_label, s_color)}</div>', unsafe_allow_html=True)
            img = (images or {}).get(p["photo_id"])
            if img is None and p.get("stored"):
                img, err = fetch_photo(result["inspection_id"], p["photo_id"])
            if img is not None:
                st.image(img, use_container_width=True)
            else:
                st.markdown(f'<div class="ms-card soft muted" style="text-align:center">사진 미보관</div>', unsafe_allow_html=True)
            a = p["analysis"]
            declared = p.get("declared_shot_type")
            mismatch = declared and declared != p.get("resolved_shot_type")
            st.markdown(chip(f"컷: {shot}", C_RED if mismatch else C_BLACK) + (chip(f"신고: {shot_names.get(declared, declared)}", C_GRAY) if mismatch else ""), unsafe_allow_html=True)
            if a.get("scene_description"):
                st.markdown(f'<div class="muted">{esc(a["scene_description"])}</div>', unsafe_allow_html=True)
            if p["findings"]:
                st.markdown("".join(finding_html(f) for f in p["findings"]), unsafe_allow_html=True)
            else:
                st.markdown('<div class="muted">지적 사항 없음</div>', unsafe_allow_html=True)
            q = p["quality"]
            st.caption(f"{q['width']}×{q['height']} · 선명도 {q.get('sharpness', 0):.0f} · 밝기 {q['brightness']:.0f}" + (f" · AI {a['provider']}" if a.get("provider") not in (None, "none") else ""))


def render_result(result, images=None):
    hero(result["verdict"], result["summary"])
    s = result["stats"]
    req_items = [c for c in result["checklist"] if c["required"]]
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        kpi("처리 시간", f"{s['latency_ms']/1000:.1f}초", f"AI 호출 {s['vlm_calls']}회" + (f" · 실패 {s['vlm_errors']}" if s.get("vlm_errors") else ""))
    with k2:
        n_ok = sum(1 for p in result["photos"] if p["status"] == "OK")
        kpi("사진", f"{len(result['photos'])}장", f"정상 {n_ok} · 지적 {s.get('num_flagged', 0)} · 반려 {s.get('num_rejected', 0)}")
    with k3:
        kpi("필수 컷", f"{sum(1 for c in req_items if c['satisfied'])}/{len(req_items)}", "충족 / 필수")
    with k4:
        n_f = sum(len(p["findings"]) for p in result["photos"]) + len(result["job_findings"])
        kpi("지적 사항", f"{n_f}건", f"AI 공급자 {result.get('vlm_provider_used') or '없음'}")

    st.markdown("")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**판정 근거**")
        st.markdown('<div class="ms-card">' + "".join(f'<div class="finding">{esc(r)}</div>' for r in result["verdict_reasons"]) + "</div>", unsafe_allow_html=True)
    with right:
        st.markdown("**체크리스트**")
        chips = []
        for c in result["checklist"]:
            color = C_GREEN if c["satisfied"] else (C_RED if c["required"] else C_GRAY)
            chips.append(chip(c["name"], color, "필수" if c["required"] else "선택"))
        st.markdown('<div class="ms-card">' + "".join(chips) + "</div>", unsafe_allow_html=True)

    st.markdown("**사진별 결과**")
    render_photos(result, images)

    if result["job_findings"]:
        st.markdown("**작업 단위 확인 사항**")
        st.markdown('<div class="ms-card">' + "".join(finding_html(f) for f in result["job_findings"]) + "</div>", unsafe_allow_html=True)

    with st.expander("기술 상세 (토큰·버전·원본 JSON)"):
        st.caption(f"검사 번호 {result['inspection_id']} · 파이프라인 {result['pipeline_version']} · 프롬프트 {s.get('prompt_version', '-')} · 토큰 입력 {s['vlm_input_tokens']} / 출력 {s['vlm_output_tokens']} / 캐시 {s.get('vlm_cache_read_tokens', 0)}")
        st.download_button("결과 JSON 다운로드", json.dumps(result, ensure_ascii=False, indent=2), file_name=f"{result['inspection_id']}.json", mime="application/json")


# ---- sidebar ------------------------------------------------------------------
with st.sidebar:
    st.image(str(ASSETS / "meisters_logo.png"), use_container_width=True)
    st.markdown('<div class="muted" style="margin-top:-6px">AI 사진 검수 · 운영자 콘솔</div>', unsafe_allow_html=True)
    st.markdown("---")
    try:
        health = requests.get(f"{API_URL}/health", timeout=3).json()
    except requests.RequestException:
        st.error(f"검사 서비스에 연결할 수 없습니다\n{API_URL}")
        st.stop()
    ai_ok = any((i or {}).get("reachable") for i in (health.get("vlm_providers") or {}).values())
    lines = [chip("검사 서비스", C_GREEN, f"v{health.get('version')}")]
    for name, info in (health.get("vlm_providers") or {}).items():
        label = {"claude": "Claude", "ollama": "로컬 모델"}.get(name, name)
        lines.append(chip(f"AI {label}", C_GREEN if info.get("reachable") else C_RED))
    lines.append(chip("인증", C_GREEN if health.get("auth_enabled") else "#B8860B", "사용" if health.get("auth_enabled") else "개발 모드"))
    st.markdown('<div class="status-line">' + "".join(lines) + "</div>", unsafe_allow_html=True)
    store = health.get("store", {})
    st.caption(f"작업 종류 {len(health.get('categories', []))}종 · 저장된 검사 {store.get('inspections', 0)}건 · 검수 대기 {store.get('review_pending', 0)}건")
    if not ai_ok:
        st.warning("AI 분석 공급자에 연결되지 않았습니다. 규칙 검사만 수행되고 판정은 '검토 필요'가 됩니다.")
    st.markdown("---")
    st.markdown("**시연 순서**")
    st.markdown("1. 작업 종류 선택\n2. 사진 업로드 (컷은 자동 분류)\n3. 검사 실행 → 판정·근거 확인\n4. 같은 사진을 다른 작업 번호로 재제출 → 재사용 적발")

# ---- header -------------------------------------------------------------------
st.markdown(f'<div class="ms-brand"><div class="t">AI 사진 검수</div><div class="sub">현장 작업 사진을 제출 즉시 검사합니다 · 사진 품질 · 부정 제출 · 체크리스트 · 시공 하자</div></div>', unsafe_allow_html=True)

tab_run, tab_review, tab_history, tab_stats = st.tabs([":material/play_circle: 검사 실행", ":material/rate_review: 검수 큐", ":material/history: 검사 이력", ":material/groups: 기사별 통계"])

# ================================================================= 검사 실행 ==
with tab_run:
    try:
        categories = fetch_categories()
    except requests.RequestException as exc:
        st.error(f"작업 종류 목록을 불러오지 못했습니다: {exc}")
        st.stop()
    cat_by_id = {c["id"]: c for c in categories}
    groups: dict[str, str] = {}
    for c in categories:
        groups.setdefault(c["group"], c.get("group_name") or GROUP_FALLBACK.get(c["group"], c["group"]))

    step(1, "작업 정보", "어떤 작업의 사진인지 고르면 필수 컷과 하자 항목이 정해집니다")
    c1, c2 = st.columns([1, 2])
    with c1:
        group_id = st.pills("작업 그룹", list(groups), format_func=lambda g: f"{groups[g]} {sum(1 for c in categories if c['group'] == g)}", default=list(groups)[0], key="group_pill") or list(groups)[0]
        in_group = [c["id"] for c in categories if c["group"] == group_id]
        cat_id = st.selectbox("작업 종류", in_group, format_func=lambda i: cat_by_id[i]["name"])
        job_id = st.text_input("작업 번호", value="demo-001")
        technician = st.text_input("기사 ID", value="", placeholder="예: T-17 (선택)")
    cat = cat_by_id[cat_id]
    shot_names = {s["id"]: s["name"] for s in cat["shots"]}
    st.session_state["shot_names"] = shot_names
    with c2:
        req = [s for s in cat["shots"] if s["required"]]
        opt = [s for s in cat["shots"] if not s["required"]]
        st.markdown(
            f'<div class="ms-card"><div style="font-size:18px;font-weight:800">{esc(cat["name"])}</div>'
            f'<div class="muted" style="margin:4px 0 12px">{esc(cat["description"])}</div>'
            f'<div style="font-size:13px;font-weight:700;margin-bottom:6px">필수 컷 {len(req)}개</div>' + "".join(chip(s["name"], C_BLACK) for s in req) +
            (f'<div style="font-size:13px;font-weight:700;margin:10px 0 6px">선택 컷 {len(opt)}개</div>' + "".join(chip(s["name"], C_GRAY) for s in opt) if opt else "") +
            f'<div style="font-size:13px;font-weight:700;margin:10px 0 6px">검사하는 하자 {len(cat["defect_codes"])}항목</div>' + "".join(chip(d["name"], C_RED if d["default_severity"] == "high" else ("#B8860B" if d["default_severity"] == "medium" else C_GRAY)) for d in cat["defect_codes"]) +
            "</div>", unsafe_allow_html=True)

    step(2, "사진 업로드", "컷을 지정하지 않으면 AI가 사진을 보고 분류합니다. 지정하면 AI가 맞는지 대조합니다")
    uploads = st.file_uploader("작업 사진 (여러 장, JPG·PNG·HEIC)", type=["png", "jpg", "jpeg", "heic", "heif"], accept_multiple_files=True, label_visibility="collapsed")
    shot_options = ["auto"] + [s["id"] for s in cat["shots"]]
    phase_options = {"(자동)": None, "작업 전": "before", "작업 후": "after"}
    photo_meta = []
    if uploads:
        cols = st.columns(min(4, len(uploads)))
        for i, up in enumerate(uploads):
            with cols[i % len(cols)]:
                st.image(up.getvalue(), use_container_width=True)
                pid = up.name.rsplit(".", 1)[0]
                st.markdown(f'<div class="muted" style="margin-top:-6px">{esc(pid)}</div>', unsafe_allow_html=True)
                shot = st.selectbox("컷", shot_options, key=f"shot_{i}", format_func=lambda s: "AI 자동 분류" if s == "auto" else shot_names[s], label_visibility="collapsed")
                meta = {"photo_id": pid}
                if shot != "auto":
                    meta["declared_shot_type"] = shot
                    spec = next(s for s in cat["shots"] if s["id"] == shot)
                    if spec.get("phase") in ("before", "after"):
                        meta["phase"] = spec["phase"]
                photo_meta.append(meta)

    step(3, "검사 실행")
    b1, b2 = st.columns([1, 3])
    with b1:
        run = st.button("검사 실행", type="primary", disabled=not uploads, use_container_width=True)
    with b2:
        with st.popover("옵션"):
            run_vlm = st.checkbox("AI 하자 분석 실행", value=True)
            history_check = st.checkbox("이전 작업 사진 재사용 검사", value=True)
            local_only = st.checkbox("로컬 모델만 사용 (사진 외부 전송 안 함)", value=False)
            notes = st.text_area("기사 메모 (AI 컨텍스트)", height=70)
    if run:
        job = {"job_id": job_id, "category": cat_id}
        if technician:
            job["technician_id"] = technician
        if notes:
            job["notes"] = notes
        files = [("files", (up.name, up.getvalue(), up.type or "image/jpeg")) for up in uploads]
        data = {"job": json.dumps(job, ensure_ascii=False), "photos": json.dumps(photo_meta, ensure_ascii=False),
                "options": json.dumps({"run_vlm": run_vlm, "run_fraud_history": history_check, "local_only": local_only})}
        with st.status("검사 중입니다", expanded=True) as status:
            st.write("규칙 검사: 선명도·밝기 · 사진 지문 · 메타데이터")
            st.write("AI 분석: 컷 분류 · 하자 탐지 · 전/후 비교 (사진당 수 초)")
            try:
                r = requests.post(f"{API_URL}/v1/inspections/upload", data=data, files=files, headers=HEADERS, timeout=600)
                r.raise_for_status()
                st.session_state["last_result"] = r.json()
                st.session_state["last_images"] = {up.name.rsplit(".", 1)[0]: up.getvalue() for up in uploads}
                status.update(label="검사 완료", state="complete", expanded=False)
            except requests.HTTPError as exc:
                status.update(label="검사 실패", state="error")
                st.error(f"{exc.response.status_code}: {exc.response.text}")
            except requests.RequestException as exc:
                status.update(label="검사 실패", state="error")
                st.error(str(exc))

    if st.session_state.get("last_result"):
        st.markdown("")
        render_result(st.session_state["last_result"], st.session_state.get("last_images"))

# ================================================================= 검수 큐 ==
with tab_review:
    st.markdown('<div class="muted">자동 판정이 "검토 필요"인 작업을 사람이 확인하고 최종 판정을 남깁니다. 저장된 판정은 정확도 측정과 기준값 조정의 정답 데이터가 됩니다.</div>', unsafe_allow_html=True)
    try:
        queue = api_get("/v1/reviews/queue", params={"limit": 100})
    except requests.RequestException as exc:
        st.error(f"검수 큐를 불러오지 못했습니다: {exc}")
        queue = []
    if not queue:
        st.success("검수 대기 중인 작업이 없습니다.")
    else:
        rc1, rc2 = st.columns([1, 3])
        with rc1:
            st.markdown(f'<div class="kpi"><div class="l">검수 대기</div><div class="v">{len(queue)}건</div></div>', unsafe_allow_html=True)
            pick = st.radio("작업 선택", [q["inspection_id"] for q in queue], format_func=lambda i: next(f"{q['job_id']} · {q['category']}" for q in queue if q["inspection_id"] == i), label_visibility="collapsed")
            reviewer = st.text_input("검수자", value=st.session_state.get("reviewer", ""), placeholder="이름 또는 ID")
            st.session_state["reviewer"] = reviewer
        with rc2:
            detail = api_get(f"/v1/inspections/{pick}")
            try:
                cat_detail = next(c for c in fetch_categories() if c["id"] == detail["category"])
                st.session_state["shot_names"] = {s["id"]: s["name"] for s in cat_detail["shots"]}
            except Exception:  # noqa: BLE001
                pass
            hero(detail["verdict"], detail["summary"])
            reasons = [r for r in detail["verdict_reasons"] if r not in detail["summary"]]  # the hero already shows the headline reason
            if reasons:
                st.markdown('<div class="ms-card">' + "".join(f'<div class="finding">{esc(r)}</div>' for r in reasons) + "</div>", unsafe_allow_html=True)
            render_photos(detail, None, cols_n=3)
            st.markdown("**최종 판정**")
            f1, f2 = st.columns([1, 2])
            with f1:
                final = st.radio("최종 판정", ["합격", "불합격", "검토 필요"], label_visibility="collapsed")
            with f2:
                notes = st.text_area("검수 메모", placeholder="확인한 하자는 #코드 로 적으면 평가 라벨이 됩니다. 예: #exposed_wiring 실내기 우측 확인", height=90, label_visibility="collapsed")
            if st.button("판정 저장", type="primary", disabled=not reviewer):
                body = {"verdict": {"합격": "PASS", "불합격": "FAIL", "검토 필요": "REVIEW"}[final], "reviewer": reviewer, "notes": notes or None}
                r = requests.post(f"{API_URL}/v1/inspections/{pick}/review", json=body, headers=HEADERS, timeout=10)
                if r.ok:
                    st.toast("판정을 저장했습니다")
                    st.rerun()
                else:
                    st.error(f"저장 실패 ({r.status_code}): {r.text}")

# ================================================================= 검사 이력 ==
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
    if h_verdict != "(전체)":
        params["verdict"] = {"합격": "PASS", "검토 필요": "REVIEW", "불합격": "FAIL"}[h_verdict]
    try:
        rows = api_get("/v1/inspections", params=params)
    except requests.RequestException as exc:
        st.error(f"이력을 불러오지 못했습니다: {exc}")
        rows = []
    if rows:
        counts = {"PASS": 0, "REVIEW": 0, "FAIL": 0}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        k1, k2, k3, k4 = st.columns(4)
        with k1:
            kpi("조회 건수", f"{len(rows)}건")
        for col, v in zip((k2, k3, k4), ("PASS", "REVIEW", "FAIL")):
            with col:
                kpi(VERDICT[v]["label"], f"{counts.get(v, 0)}건", f"{counts.get(v, 0)/len(rows)*100:.0f}%")
        st.dataframe(
            [{"검사 번호": r["inspection_id"], "작업 번호": r["job_id"], "기사": r.get("technician_id") or "", "작업 종류": r["category"],
              "자동 판정": VERDICT[r["verdict"]]["label"], "검수 판정": VERDICT[r["review_verdict"]]["label"] if r.get("review_verdict") else "",
              "사진": r["num_photos"], "검사 시각": r["created_at"][:19].replace("T", " ")} for r in rows],
            use_container_width=True, hide_index=True,
        )
        pick = st.selectbox("상세 보기", [r["inspection_id"] for r in rows], format_func=lambda i: next(f"{r['job_id']} · {r['category']} · {VERDICT[r['verdict']]['label']}" for r in rows if r["inspection_id"] == i))
        if st.button("결과 열기"):
            detail = api_get(f"/v1/inspections/{pick}")
            try:
                cat_detail = next(c for c in fetch_categories() if c["id"] == detail["category"])
                st.session_state["shot_names"] = {s["id"]: s["name"] for s in cat_detail["shots"]}
            except Exception:  # noqa: BLE001
                pass
            render_result(detail, None)
    else:
        st.info("저장된 검사가 없습니다.")

# ================================================================= 기사별 통계 ==
with tab_stats:
    days = st.slider("기간 (일)", min_value=7, max_value=180, value=30)
    try:
        stats = api_get("/v1/stats/technicians", params={"days": days})
    except requests.RequestException as exc:
        st.error(f"통계를 불러오지 못했습니다: {exc}")
        stats = []
    if stats:
        st.dataframe(
            [{"기사": s_["technician_id"], "검사": s_["total"], "합격": s_["PASS"], "검토": s_["REVIEW"], "불합격": s_["FAIL"], "합격률": f"{s_['pass_rate']*100:.0f}%", "불합격률": f"{s_['fail_rate']*100:.0f}%"} for s_ in stats],
            use_container_width=True, hide_index=True,
        )
        st.bar_chart({s_["technician_id"]: s_["fail_rate"] * 100 for s_ in stats}, color=C_RED, height=220)
        st.caption("불합격률(%) 높은 순. 기사 앱이 technician_id 를 보내는 작업만 집계됩니다.")
    else:
        st.info("집계할 데이터가 없습니다. 작업 번호와 함께 기사 ID를 넣어 검사하면 쌓입니다.")
