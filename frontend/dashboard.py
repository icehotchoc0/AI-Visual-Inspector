"""
Streamlit demo dashboard for AI-Visual-Inspector.

Talks to the FastAPI backend over HTTP - keeps the dashboard a thin
client, same separation of concerns as AI-Portfolio-Agent's
frontend/backend split.

Run (after the backend is already running, see SETUP_GUIDE.md):
    streamlit run frontend/dashboard.py
"""
import base64
import io
import os
import sys
from pathlib import Path

import requests
import streamlit as st
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.config import settings  # noqa: E402

API_URL = os.environ.get("INSPECTOR_API_URL", f"http://localhost:{settings.api_port}")

st.set_page_config(page_title="AI-Visual-Inspector", page_icon="🔍", layout="wide")
st.title("🔍 AI-Visual-Inspector")
st.caption(
    "Local-first industrial visual quality control - anomaly detection "
    "(Anomalib) + natural-language explanation (Qwen2.5-VL via Ollama)."
)

with st.sidebar:
    st.header("Settings")
    category = st.selectbox("Part category", settings.categories, index=0)
    backend_choice = st.selectbox("Model backend", ["patchcore", "efficient_ad"], index=0)

    st.divider()
    try:
        health = requests.get(f"{API_URL}/health", timeout=3).json()
        st.success("Backend: online")
        st.json(health)
    except requests.RequestException:
        st.error(f"Backend not reachable at {API_URL}. Start it first (see SETUP_GUIDE.md).")

st.subheader("1. Provide an image to inspect")
tab_upload, tab_sample = st.tabs(["Upload a photo", "Use a sample test image"])

uploaded_bytes = None

with tab_upload:
    uploaded_file = st.file_uploader("Upload a part photo", type=["png", "jpg", "jpeg"])
    if uploaded_file is not None:
        uploaded_bytes = uploaded_file.read()

with tab_sample:
    sample_dir = settings.mvtec_root_path / category / "test"
    sample_paths: list[Path] = []
    if sample_dir.exists():
        for defect_folder in sorted(sample_dir.iterdir()):
            sample_paths.extend(sorted(defect_folder.glob("*.png"))[:3])

    if not sample_paths:
        st.info(
            f"No sample images found yet under {sample_dir}. "
            "They appear once the MVTec AD dataset has been downloaded "
            "(happens automatically the first time you run a training script)."
        )
    else:
        choice = st.selectbox(
            "Pick a sample test image (includes both good and defective parts)",
            sample_paths,
            format_func=lambda p: f"{p.parent.name} / {p.name}",
        )
        if choice:
            uploaded_bytes = choice.read_bytes()

if uploaded_bytes:
    st.image(uploaded_bytes, caption="Input image", width=300)

st.subheader("2. Run inspection")
if st.button("Run Inspection", type="primary", disabled=uploaded_bytes is None):
    with st.spinner("Running anomaly detection..."):
        try:
            resp = requests.post(
                f"{API_URL}/inspect",
                params={"category": category, "backend": backend_choice},
                files={"file": ("image.png", uploaded_bytes, "image/png")},
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as exc:
            st.error(f"Inspection failed: {exc}")
            result = None

    if result:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Original**")
            st.image(uploaded_bytes, use_container_width=True)
        with col2:
            st.markdown("**Anomaly heatmap**")
            if result.get("heatmap_png_base64"):
                heatmap_img = Image.open(io.BytesIO(base64.b64decode(result["heatmap_png_base64"])))
                st.image(heatmap_img, use_container_width=True)
            else:
                st.write("No heatmap returned.")

        if result["is_defective"]:
            st.error(f"❌ FAIL - anomaly score {result['anomaly_score']:.3f}")
        else:
            st.success(f"✅ PASS - anomaly score {result['anomaly_score']:.3f}")

        if result.get("defect_description"):
            st.markdown("### Inspection report")
            st.write(f"**Defect type:** {result['defect_description']}")
            if result.get("likely_cause"):
                st.write(f"**Likely cause:** {result['likely_cause']}")
            if result.get("severity"):
                st.write(f"**Severity:** {result['severity']}")
            st.caption(f"Explanation source: {result['explanation_source']}")
        elif result["is_defective"]:
            st.warning(
                f"Defect detected but no explanation available "
                f"(source: {result['explanation_source']})."
            )

        st.caption(
            f"Detector latency: {result['inference_latency_ms']:.1f} ms"
            + (
                f" | VLM latency: {result['explanation_latency_ms']:.1f} ms"
                if result.get("explanation_latency_ms")
                else ""
            )
        )
