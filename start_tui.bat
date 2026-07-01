@echo off
REM Launch NanoCat's local TUI.
REM Flow: probe Python (must pre-exist, never auto-installed) -> ensure uv
REM (pip-install if missing) -> cd to this folder -> uv sync (with the optional
REM `tui` extra: textual) -> uv run nanocat tui.
setlocal

REM cd to this script's own directory (repo root holding pyproject.toml).
cd /d "%~dp0"

REM --- Python: probe only, do not auto-install ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY (
    echo Python 3.11+ not found. Please install it first: https://www.python.org/downloads/
    exit /b 1
)

REM --- uv: install via pip if missing ---
where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found, installing via pip...
    %PY% -m pip install uv
)

REM --- sync deps (textual lives in the optional `tui` extra) and launch ---
where uv >nul 2>nul
if errorlevel 1 (
    %PY% -m uv sync --extra tui
    %PY% -m uv run nanocat tui
) else (
    uv sync --extra tui
    uv run nanocat tui
)
