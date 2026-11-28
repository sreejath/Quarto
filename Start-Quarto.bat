@echo off
REM Double-click to start Quarto in your default browser on Windows.
cd /d "%~dp0"
echo Starting Quarto...
if not exist ".venv" (
    echo Setting up Python virtual environment (first run only)...
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -c "import yaml, requests" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r requirements.txt
)
python -m quarto.ui
pause
