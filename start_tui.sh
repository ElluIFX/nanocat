#!/usr/bin/env sh
# Launch NanoCat's local TUI.
# Flow: probe Python (must pre-exist, never auto-installed) -> ensure uv
# (pip-install if missing) -> cd to this folder -> uv sync (with the optional
# `tui` extra: textual) -> uv run nanocat tui.
set -eu

# cd to this script's own directory (repo root holding pyproject.toml).
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# --- Python: probe only, do not auto-install ---
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3.11+ not found. Please install it first: https://www.python.org/downloads/" >&2
  exit 1
fi

# --- uv: install via pip if missing ---
if command -v uv >/dev/null 2>&1; then
  UV="uv"
else
  echo "uv not found, installing via pip..."
  "$PY" -m pip install uv
  if command -v uv >/dev/null 2>&1; then UV="uv"; else UV="$PY -m uv"; fi
fi

# --- sync deps (textual lives in the optional `tui` extra) and launch ---
$UV sync --extra tui
exec $UV run nanocat tui
