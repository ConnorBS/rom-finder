@echo off
setlocal

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt -q

echo.
echo Starting ROM Finder at http://127.0.0.1:8080
echo Press Ctrl+C to stop.
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
