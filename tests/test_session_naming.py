"""Automatic title generation and notification boundaries."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from nanocat.session.manager import SessionManager


class SessionNamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = SessionManager(Path(self.directory.name))
        self.generator = AsyncMock(return_value="First conversation title")
        self.updated = AsyncMock()
        self.manager.set_name_generator(self.generator)
        self.manager.set_name_updated_callback(self.updated)
        self.session = self.manager.get_or_create("web", "chat")

    async def asyncTearDown(self):
        await self.manager.close()
        self.directory.cleanup()

    async def finish_turn(self):
        self.session.add_message("user", "Inspect the startup path")
        self.session.add_message("assistant", "The startup path is ready")
        self.manager.save(self.session)
        await asyncio.gather(*self.manager._background_tasks)
        await asyncio.sleep(0)

    async def test_names_after_first_complete_turn_and_notifies_after_persistence(self):
        self.session.add_message("user", "Inspect the startup path")
        self.manager.save(self.session)
        self.assertFalse(self.manager._naming_tasks)
        self.session.add_message("assistant", "The startup path is ready")
        self.manager.save(self.session)
        await asyncio.gather(*self.manager._background_tasks)
        self.generator.assert_awaited_once()
        self.updated.assert_awaited_once_with(self.session)
        self.assertEqual(self.session.name, "First conversation title")
        persisted = self.manager._read_metadata("web")["sessions"][self.session.id]
        self.assertEqual(persisted["name"], self.session.name)

    async def test_empty_result_retries_after_next_completed_turn(self):
        self.generator.side_effect = [None, "Recovered title"]
        await self.finish_turn()
        self.updated.assert_not_awaited()
        self.session.add_message("user", "Continue")
        self.manager.save(self.session)
        self.assertFalse(self.manager._naming_tasks)
        self.session.add_message("assistant", "Done")
        self.manager.save(self.session)
        await asyncio.gather(*self.manager._background_tasks)
        self.assertEqual(self.session.name, "Recovered title")
        self.updated.assert_awaited_once()

    async def test_manual_name_wins_over_late_generator_result(self):
        async def generate(*args):
            self.manager.set_name("web", self.session.id, "Manual title")
            return "Automatic title"

        self.generator.side_effect = generate
        await self.finish_turn()
        self.assertEqual(self.session.name, "Manual title")
        self.updated.assert_not_awaited()

    async def test_repeated_save_shares_one_naming_task(self):
        self.session.add_message("user", "Question")
        self.session.add_message("assistant", "Answer")
        self.manager.save(self.session)
        self.manager.save(self.session, expected_revision=self.session.revision)
        self.assertEqual(len(self.manager._naming_tasks), 1)
        await asyncio.gather(*self.manager._background_tasks)
        self.generator.assert_awaited_once()
