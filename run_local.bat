@echo off
echo ========================================================
echo  FIRE HARMONIX - NASA Space Apps 2026 Local Server
echo ========================================================
python -m pip install -r requirements.txt
python -c "from backend.database import init_db; init_db()"
echo Starting Uvicorn on http://127.0.0.1:8003 ...
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8003
pause
