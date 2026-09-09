@echo off
echo Starting AI-Visual-Inspector...
start "AI-Visual-Inspector Backend" cmd /k "uvicorn backend.app:app --host 0.0.0.0 --port 8000"
timeout /t 3 /nobreak >nul
start "AI-Visual-Inspector Dashboard" cmd /k "streamlit run frontend/dashboard.py"
echo Backend:   http://localhost:8000/docs
echo Dashboard: http://localhost:8501
