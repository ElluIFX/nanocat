import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nanocat.config.loader import ConfigLoadError, load_config


class ConfigNormalizationTests(unittest.TestCase):
    def test_startup_adds_kimi_and_preserves_configured_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "providers": {"openai": {"apiKey": "file-key"}},
                        "channels": {"plugin": {"enabled": True}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                load_config(path, normalize=True)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    saved["providers"]["kimi"],
                    {
                        "apiKey": "",
                        "apiBase": None,
                        "extraHeaders": None,
                        "billingMode": None,
                    },
                )
                self.assertEqual(saved["providers"]["openai"]["apiKey"], "file-key")
                self.assertEqual(saved["channels"]["plugin"], {"enabled": True})
                with patch("nanocat.config.loader.save_config") as save:
                    load_config(path, normalize=True)
                    save.assert_not_called()

    def test_environment_credentials_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch.dict(
                "os.environ",
                {
                    "NANOCAT_PROVIDERS__KIMI__API_KEY": "environment-key",
                    "NANOCAT_PROVIDERS__KIMI__BILLING_MODE": "subscription",
                },
                clear=True,
            ):
                effective = load_config(path, normalize=True)
            self.assertEqual(effective.providers.kimi.api_key, "environment-key")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["providers"]["kimi"]["apiKey"], "")
            self.assertIsNone(saved["providers"]["kimi"]["billingMode"])

    def test_invalid_config_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for original in ("{broken", '{"providers":{"kimi":{"apiKey":"file-key"}}}'):
                path.write_text(original, encoding="utf-8")
                with self.assertRaises(ConfigLoadError):
                    load_config(path, normalize=True)
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_regular_reads_do_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("{}", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                load_config(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")
