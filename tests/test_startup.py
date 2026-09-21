"""Regression coverage for optional channel import costs."""

import subprocess
import sys
import unittest


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
