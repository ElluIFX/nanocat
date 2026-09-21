"""Regression coverage for optional channel import costs."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanocat.runtime.web_assets import _source_fingerprint


class ChannelImportTests(unittest.TestCase):
    def test_telegram_metadata_does_not_import_sdk(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "from nanocat.channels.telegram import TelegramChannel; "
                "assert TelegramChannel.default_config()['enabled'] is False; "
                "assert 'telegram' not in sys.modules; "
                "assert 'telegramify_markdown' not in sys.modules; "
                "assert TelegramChannel._build_bot_commands()",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class AssetFingerprintTests(unittest.TestCase):
    def test_prunes_dependencies_and_hashes_source_content(self):
        import os

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "node_modules").mkdir()
            dependency = root / "node_modules" / "dependency.js"
            dependency.write_text("old", encoding="utf-8")
            source = root / "main.ts"
            source.write_text("old", encoding="utf-8")
            visited = []
            walk = os.walk

            def tracking_walk(path):
                for entry in walk(path):
                    visited.append(Path(entry[0]))
                    yield entry

            with patch("nanocat.runtime.web_assets.os.walk", tracking_walk):
                initial = _source_fingerprint(root)
            self.assertNotIn(root / "node_modules", visited)
            dependency.write_text("new", encoding="utf-8")
            self.assertEqual(initial, _source_fingerprint(root))
            source.write_text("new", encoding="utf-8")
            self.assertNotEqual(initial, _source_fingerprint(root))
