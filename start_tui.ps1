# Launch NanoCat's local TUI.
# Flow: probe Python (must pre-exist, never auto-installed) -> ensure uv
# (pip-install if missing) -> cd to this folder -> uv sync (with the optional
# `tui` extra: textual) -> uv run nanocat tui.
$ErrorActionPreference = "Stop"

# cd to this script's own directory (repo root holding pyproject.toml).
Set-Location -LiteralPath $PSScriptRoot

# --- Python: probe only, do not auto-install ---
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Error "Python 3.11+ not found. Please install it first: https://www.python.org/downloads/"
    exit 1
}

# --- uv: install via pip if missing ---
$hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
if (-not $hasUv) {
    Write-Host "uv not found, installing via pip..."
    & $py.Source -m pip install uv
    $hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
}

# --- sync deps (textual lives in the optional `tui` extra) and launch ---
if ($hasUv) {
    uv sync --extra tui
    uv run nanocat tui
} else {
    & $py.Source -m uv sync --extra tui
    & $py.Source -m uv run nanocat tui
}
