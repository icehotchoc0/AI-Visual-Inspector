# Convenience launcher: starts the FastAPI backend, then the Streamlit
# dashboard, each in its own window. Mirrors AI-Dev-Crew's run_crew.ps1.
#
# Uses the venv's own uvicorn.exe/streamlit.exe directly (rather than
# relying on the new window inheriting an activated venv, which it
# doesn't - each Start-Process window is a fresh shell with the base
# PATH, not this one's activated venv).
Write-Host "Starting AI-Visual-Inspector..." -ForegroundColor Cyan

Start-Process powershell -ArgumentList "-NoExit", "-Command", ".\venv\Scripts\uvicorn.exe backend.app:app --host 0.0.0.0 --port 8000"
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList "-NoExit", "-Command", ".\venv\Scripts\streamlit.exe run frontend/dashboard.py"

Write-Host "Backend:   http://localhost:8000/docs  (health: /health)" -ForegroundColor Green
Write-Host "Dashboard: http://localhost:8501" -ForegroundColor Green
