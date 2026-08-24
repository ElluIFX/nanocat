@echo off
setlocal

cd /d "%~dp0"

python -m uv --version >nul 2>&1
if errorlevel 1 (
    echo Installing uv...
    python -m ensurepip --upgrade >nul 2>&1
    python -m pip install --user --upgrade uv
    if errorlevel 1 exit /b 1
)

start "NanoCat WebUI" cmd /k "cd /d ""%~dp0"" && python -m uv run --frozen nanocat -w ""%~dp0data"""
timeout /t 5 /nobreak >nul
start "" "http://127.0.0.1:18790"

endlocal
