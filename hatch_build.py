"""Hatch build hook for the bundled NanoCat web client."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Compile the locked frontend and include it in built wheels."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        root = Path(self.root)
        bundled = root / "nanocat" / "web" / "static"
        if os.environ.get("NANOCAT_SKIP_WEB_BUILD") == "1":
            if not (bundled / "index.html").is_file():
                raise RuntimeError("NANOCAT_SKIP_WEB_BUILD requires bundled web assets")
            return
        module_path = root / "nanocat" / "runtime" / "web_assets.py"
        spec = importlib.util.spec_from_file_location("_nanocat_web_assets", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Unable to load the NanoCat web asset builder")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        dist = module.ensure_web_assets()
        build_data.setdefault("force_include", {})[str(dist)] = "nanocat/web/static"
