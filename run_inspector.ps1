# Convenience launcher: starts the FastAPI backend, then the Streamlit
# dashboard, each in its own window. Mirrors AI-Dev-Crew's run_crew.ps1.
Write-Host "Starting AI-Visual-Inspector..." -ForegroundColor Cyan

Start-Process powershell -ArgumentList "-NoExit", "-Command", "uvicorn backend.app:app --host 0.0.0.0 --port 8000"
Start-Sleep -Seconds 3
Start-Process powershell -ArgumentList "-NoExit", "-Command", "streamlit run frontend/dashboard.py"

Write-Host "Backend:   http://localhost:8000/docs" -ForegroundColor Green
Write-Host "Dashboard: http://localhost:8501" -ForegroundColor Green
