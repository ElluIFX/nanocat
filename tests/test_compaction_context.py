import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from nanocat.agent.memory import MemoryCompactor
from nanocat.config.loader import validate_persisted_config
from nanocat.providers.base import LLMResponse, ToolCallRequest
from nanocat.session.manager import Session


class CompactionContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.provider = SimpleNamespace(
            chat_with_retry=AsyncMock(
                return_value=LLMResponse(
                    content=json.dumps(
                        {
                            "summary": "Work verified; continue with deployment.",
                            "state": {"active_work": ["deploy"], "completed_work": ["fix"]},
                        }
                    )
                )
            )
        )
        self.resolver = SimpleNamespace(resolve=Mock(return_value=self.provider))
        defaults = SimpleNamespace(
            model="main/model",
            assistant_model="helper/model",
            context_window_tokens=65536,
            reasoning_effort="high",
        )
        self.config = SimpleNamespace(
            agents=SimpleNamespace(defaults=defaults), workspace_path=Path(self.directory.name)
        )
        self.tools = [{"type": "function", "function": {"name": "read_file"}}]
        self.build = Mock(
            side_effect=lambda **kw: [
                {"role": "system", "content": "Original system instructions"},
                *(
                    [{"role": "user", "content": kw["compacted_memory"]}]
                    if kw["compacted_memory"]
                    else []
                ),
                *kw["history"],
                {"role": "user", "content": kw["current_message"]},
            ]
        )
        self.compactor = MemoryCompactor(
            sessions=SimpleNamespace(save=Mock()),
            build_messages=self.build,
            get_tool_definitions=lambda: self.tools,
            config=self.config,
            provider_resolver=self.resolver,
            no_compact_turns=1,
        )
        self.compactor.estimate_session_prompt_tokens = Mock(return_value=(40000, "test"))
        self.compactor._target_compacted_chars = Mock(return_value=8000)
        self.session = Session(id="test", channel="web", chat_id="local")
        for i in range(3):
            self.session.add_message("user", f"Request {i}")
            self.session.add_message("assistant", f"Result {i}")

    async def test_original_prefix_model_and_tools_are_preserved(self):
        messages = [
            {"role": "system", "content": "Original system with pulse"},
            {"role": "user", "content": "Original request with runtime context"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "reasoning",
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"file"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call1", "content": "file contents"},
            {"role": "assistant", "content": "Done"},
        ]
        original = copy.deepcopy(messages)
        result = await self.compactor.compact_messages(
            self.session,
            self.session.messages[:2],
            2,
            request_messages=messages,
            model="original/model",
            reasoning_effort="low",
        )
        self.assertTrue(result)
        request = self.provider.chat_with_retry.call_args.kwargs
        self.assertEqual(request["messages"][:-1], original)
        self.assertEqual(messages, original)
        self.assertIn("Immediately suspend", request["messages"][-1]["content"])
        self.assertEqual(request["model"], "original/model")
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertEqual(request["tools"], self.tools)
        self.assertEqual(request["tool_choice"], "none")
        self.assertEqual(self.session.compaction_checkpoint["compaction_model"], "original/model")
        self.assertEqual(len(self.session.messages), 6)

    async def test_reconstructed_context_includes_tail_and_replaces_old_tasks(self):
        self.session.compacted_memory = "Previous checkpoint"
        self.session.compaction_checkpoint = {"state": {"active_work": ["fix"]}}
        self.assertTrue(
            await self.compactor.compact_messages(
                self.session,
                self.session.messages[:2],
                2,
            )
        )
        request = self.provider.chat_with_retry.call_args.kwargs
        self.assertEqual(request["model"], "main/model")
        self.assertEqual(request["messages"][1]["content"], "Previous checkpoint")
        self.assertEqual(request["messages"][-2]["content"], "Result 2")
        self.assertEqual(self.session.compaction_checkpoint["state"]["active_work"], ["deploy"])

    async def test_incomplete_or_tool_response_keeps_checkpoint(self):
        for response in (
            LLMResponse(content="partial", finish_reason="length"),
            LLMResponse(content="error", finish_reason="error"),
            LLMResponse(content=""),
            LLMResponse(content="text", tool_calls=[ToolCallRequest("1", "read_file", {})]),
        ):
            self.provider.chat_with_retry.return_value = response
            self.assertFalse(
                await self.compactor.compact_messages(
                    self.session,
                    self.session.messages[:2],
                    2,
                )
            )
            self.assertEqual(self.session.last_compacted, 0)
            self.compactor.sessions.save.assert_not_called()

    async def test_stale_response_and_cancel_preserve_history(self):
        async def stale(**kwargs):
            self.session.revision += 1
            return LLMResponse(content="summary")

        self.provider.chat_with_retry.side_effect = stale
        self.assertFalse(
            await self.compactor.compact_messages(
                self.session,
                self.session.messages[:2],
                2,
            )
        )
        self.provider.chat_with_retry.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.compactor.compact_messages(self.session, self.session.messages[:2], 2)
        self.assertEqual(self.session.last_compacted, 0)
        self.compactor.sessions.save.assert_not_called()

    async def test_stale_queued_snapshot_is_skipped(self):
        self.assertFalse(
            await self.compactor.maybe_compact_by_tokens(
                self.session,
                force=True,
                request_messages=[],
                expected_revision=-1,
            )
        )
        self.provider.chat_with_retry.assert_not_called()

    async def test_timeout_and_save_failure_keep_previous_checkpoint(self):
        self.session.compacted_memory = "previous memory"
        self.session.compaction_checkpoint = {"summary": "previous memory"}
        original = copy.deepcopy(self.session)
        self.provider.chat_with_retry.side_effect = TimeoutError
        with patch("nanocat.agent.memory.logger"):
            self.assertFalse(
                await self.compactor.compact_messages(
                    self.session,
                    self.session.messages[:2],
                    2,
                )
            )
            self.provider.chat_with_retry.side_effect = None
            self.compactor.sessions.save.side_effect = OSError("save failed")
            self.assertFalse(
                await self.compactor.compact_messages(
                    self.session,
                    self.session.messages[:2],
                    2,
                )
            )
        self.assertEqual(self.session, original)

    def test_removed_model_setting_is_not_saved(self):
        with patch.dict("os.environ", {}, clear=True):
            config = validate_persisted_config(
                {
                    "agents": {
                        "defaults": {
                            "compactionModel": "old/model",
                        }
                    }
                }
            )
        self.assertNotIn("compactionModel", config.model_dump(by_alias=True)["agents"]["defaults"])
