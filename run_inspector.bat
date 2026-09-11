@echo off
echo Starting AI-Visual-Inspector...
start "AI-Visual-Inspector Backend" cmd /k "venv\Scripts\uvicorn.exe backend.app:app --host 0.0.0.0 --port 8000"
timeout /t 3 /nobreak >nul
start "AI-Visual-Inspector Dashboard" cmd /k "venv\Scripts\streamlit.exe run frontend/dashboard.py"
echo Backend:   http://localhost:8000/docs
echo Dashboard: http://localhost:8501
