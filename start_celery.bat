@echo off
REM Start Celery worker + beat for Peter Dashboard (Windows).
REM Requires Redis at redis://127.0.0.1:6379/0
REM Run from Backend folder with venv activated.

cd /d %~dp0

echo Starting Celery worker (solo pool for Windows)...
start "celery-worker" cmd /k "venv\Scripts\celery.exe -A config worker --pool=solo --loglevel=INFO"

timeout /t 2 >nul

echo Starting Celery beat (database scheduler)...
start "celery-beat" cmd /k "venv\Scripts\celery.exe -A config beat --loglevel=INFO"

echo.
echo Worker + Beat started in new windows.
echo Seed schedules (once): venv\Scripts\python.exe manage.py setup_celery_schedules
echo Manual run: POST /api/operations/celery/run/  {"task":"process_cancellations"}
echo Status:     GET  /api/operations/celery/status/
