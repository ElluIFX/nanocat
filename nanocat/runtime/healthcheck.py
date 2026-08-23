"""Container health probe that follows the enabled HTTP surface."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path


def main() -> None:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data")
    try:
        raw = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    channels = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    web = channels.get("web") if isinstance(channels.get("web"), dict) else {}
    api = raw.get("api") if isinstance(raw.get("api"), dict) else {}
    workspace = Path(raw.get("workspacePath") or data_dir / "workspace")
    if not workspace.is_absolute():
        workspace = data_dir / workspace
    endpoint_file = workspace / "_runtime_temp" / "http-endpoints.json"
    try:
        endpoints = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        endpoints = {}
    if web.get("enabled", True) and endpoints.get("web"):
        url = f"{str(endpoints['web']).rstrip('/')}/auth/status"
    elif api.get("enabled", False) and endpoints.get("coreApi"):
        url = f"{str(endpoints['coreApi']).rstrip('/')}/api/v1/health"
    elif web.get("enabled", True):
        port = int(os.environ.get("NANOCAT_WEB_PORT") or web.get("port") or 18790)
        url = f"http://127.0.0.1:{port}/auth/status"
    elif api.get("enabled", False) and int(api.get("port") or 0) > 0:
        url = f"http://127.0.0.1:{int(api['port'])}/api/v1/health"
    elif web.get("enabled", True) or api.get("enabled", False):
        raise SystemExit(1)
    else:
        return
    with urllib.request.urlopen(url, timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
