"""Offline regression coverage for the HTTP/Web runtime boundaries."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import UploadFile
from fastapi.responses import JSONResponse

from nanocat.agent.context import ContextBuilder
from nanocat.agent.loop import (
    AgentLoop,
    _PendingDurabilityRecord,
    _PendingTurn,
    _StoppedTurn,
    _TurnCancellationClaim,
    _TurnRunState,
)
from nanocat.agent.memory import NowledgeThreadManager
from nanocat.agent.tools.ssh import SSHManager
from nanocat.api.app import (
    _bind_artifact_lease,
    _ClosingStreamingResponse,
    _control_response,
    _IdempotencyCache,
    _redact,
    _RequestBodyLimitMiddleware,
    create_api_app,
    create_web_app,
)
from nanocat.api.artifacts import ArtifactEntry, ArtifactRegistry, read_range
from nanocat.api.auth import (
    WEB_CSRF_COOKIE,
    ApiAuthenticator,
    LoginRateLimiter,
    WebSessionAuth,
)
from nanocat.api.events import SseBroker, SseSubscription
from nanocat.application.agent_service import AgentService
from nanocat.application.command_dispatcher import CommandDispatcher
from nanocat.application.command_router import CommandRouter
from nanocat.application.configuration import (
    ConfigurationError,
    ConfigurationService,
    restart_setting_paths,
    setting_apply_mode,
)
from nanocat.application.control import ApplicationControlService
from nanocat.application.intervention import InterventionBroker
from nanocat.application.projections import ConversationProjection, TrajectoryProjection
from nanocat.application.turns import TurnCoordinator, TurnState
from nanocat.bus.events import InboundMessage, OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.web import WebChannel, WebIngressRejectedError
from nanocat.config.loader import load_config
from nanocat.config.schema import Config
from nanocat.core.messages import ConversationRef
from nanocat.core.runtime import ShutdownReason
from nanocat.observability.activity import ActivityEvent
from nanocat.observability.redaction import redact_value
from nanocat.runtime.instance_lock import RuntimeInstanceLock
from nanocat.runtime.launcher import build_runtime
from nanocat.runtime.lifecycle import (
    ComponentOwnerRegistry,
    ShutdownCoordinator,
    ShutdownReport,
)
from nanocat.runtime.supervisor import RuntimeSupervisor
from nanocat.security.policy import SecurityDecisionKind, SecurityPolicy
from nanocat.session.manager import (
    SessionManager,
    SessionRevisionConflictError,
)


class RuntimeInstanceLockTests(unittest.TestCase):
    def test_lock_contention_and_release(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-lock-test-") as root:
            path = Path(root) / ".nanocat.lock"
            first = RuntimeInstanceLock(path)
            second = RuntimeInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError, "already owned"):
                    second.acquire()
            finally:
                first.close()
            second.acquire()
            second.close()


class SessionStorageTests(unittest.TestCase):
    def test_activity_projection_drops_transient_records_after_turn_terminal(self) -> None:
        events = [
            ActivityEvent(
                type="assistant.thinking",
                source="runtime",
                status="running",
                sequence=1,
                turn_id="turn-1",
            ),
            ActivityEvent(
                type="assistant.final",
                source="runtime",
                status="completed",
                sequence=2,
                turn_id="turn-1",
            ),
            ActivityEvent(
                type="assistant.thinking",
                source="runtime",
                status="running",
                sequence=3,
                turn_id="turn-1",
            ),
        ]

        projected = TrajectoryProjection.from_activity(events).to_dict()["items"]

        self.assertEqual(
            [item["type"] for item in projected],
            ["assistant.thinking", "assistant.final"],
        )

    def test_session_write_lock_identity_is_stable_across_threads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-session-lock-race-") as root:
            manager = SessionManager(Path(root) / "sessions")

            async def collect() -> list[threading.RLock]:
                return await asyncio.gather(
                    *(asyncio.to_thread(manager._write_lock, "web:chat") for _ in range(64))
                )

            locks = asyncio.run(collect())
            self.assertTrue(all(lock is locks[0] for lock in locks))

    def test_working_projection_redacts_tool_output_and_exposes_reasoning(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-tool-projection-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            secret = "projection-secret-sentinel"
            session.add_message("user", "inspect")
            session.add_message(
                "assistant",
                "",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "grep_file",
                            "arguments": json.dumps({"path": "fixture.txt"}),
                        },
                    },
                ],
                reasoning_content="Inspecting the requested file.",
                thinking_blocks=[
                    {
                        "type": "thinking",
                        "thinking": "x" * 40_000,
                        "signature": secret,
                    }
                ],
                turn_id="turn-working",
                turn_started_at="2026-08-23T00:00:00+00:00",
            )
            session.add_message(
                "tool",
                json.dumps({"content": "visible result", "password": secret}),
                tool_call_id="call-1",
                name="read_file",
                turn_id="turn-working",
            )
            session.add_message(
                "tool",
                json.dumps({"content": "second result"}),
                tool_call_id="call-2",
                name="grep_file",
                turn_id="turn-working",
            )
            session.add_message(
                "assistant",
                "done",
                turn_id="turn-working",
                turn_ended_at="2026-08-23T00:00:02+00:00",
                turn_duration_ms=2000,
                turn_status="completed",
            )

            conversation = json.dumps(
                ConversationProjection.from_session(session).to_dict()
            )
            trajectory = json.dumps(TrajectoryProjection.from_session(session).to_dict())
            self.assertNotIn(secret, conversation)
            self.assertNotIn(secret, trajectory)
            self.assertIn("visible result", conversation)
            self.assertIn("Inspecting the requested file", conversation)
            self.assertIn("turn-working", conversation)
            self.assertIn('"durationMs": 2000', conversation)
            self.assertNotIn('"signature"', conversation)
            self.assertLess(len(conversation), 100_000)
            projected = ConversationProjection.from_session(session).to_dict()["items"]
            self.assertEqual(
                [item["type"] for item in projected],
                ["user.message", "assistant.work", "tool.result", "tool.result", "assistant.final"],
            )
            self.assertEqual(
                [item.get("metadata", {}).get("toolCallId") for item in projected[2:4]],
                ["call-1", "call-2"],
            )
            repeated = ConversationProjection.from_session(session).to_dict()["items"]
            self.assertEqual(
                [item["id"] for item in projected],
                [item["id"] for item in repeated],
            )

    def test_text_attachment_path_is_prompt_only_and_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-text-attachment-") as root:
            root_path = Path(root)
            workspace = root_path / "workspace"
            attachment = workspace / "_runtime_temp" / "media" / "fixture.txt"
            attachment.parent.mkdir(parents=True)
            attachment.write_text("bounded attachment", encoding="utf-8")
            manager = SessionManager(root_path / "sessions")
            session = manager.get_or_create("web", "chat")
            engine = object.__new__(AgentLoop)
            engine.sessions = manager
            engine.context = ContextBuilder(workspace)
            private_content = engine.context._build_user_content("inspect", [str(attachment)])
            self.assertIn("_runtime_temp/media/fixture.txt", json.dumps(private_content))

            engine._persist_turn_entries(
                session,
                [
                    {"role": "user", "content": private_content},
                    {"role": "assistant", "content": "done"},
                ],
                artifact_refs=(
                    {
                        "id": "a" * 32,
                        "name": "fixture.txt",
                        "kind": "text",
                        "mediaType": "text/plain",
                    },
                ),
            )
            persisted = json.dumps(session.messages)
            public = json.dumps(ConversationProjection.from_session(session).to_dict())
            self.assertNotIn("_runtime_temp/media", persisted)
            self.assertNotIn("attachment_path", persisted)
            self.assertNotIn("_runtime_temp/media", public)
            self.assertEqual(
                session.messages[0]["content"][0],
                {"type": "text", "text": "[file attachment]"},
            )

    def test_conversation_projection_preserves_artifact_ids_without_local_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-session-artifact-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            session.add_message(
                "user",
                [{"type": "text", "text": r"[image: C:\private\upload.png]"}],
                artifact_refs=[
                    {
                        "id": "a" * 32,
                        "name": "upload.png",
                        "kind": "image",
                        "mediaType": "image/png",
                        "path": r"C:\private\upload.png",
                        "internal": "hidden",
                    },
                    {
                        "id": r"C:\private\secret.bin",
                        "name": r"C:\private\secret.bin",
                        "kind": "binary",
                    },
                ],
            )
            session.add_message("assistant", "received")
            page = ConversationProjection.from_session(session)
            user = page.items[0].to_dict()

            self.assertNotIn("C:\\private", json.dumps(user))
            self.assertEqual(user["content"], [{"type": "text", "text": "[image attachment]"}])
            self.assertEqual(user["metadata"]["artifactRefs"][0]["id"], "a" * 32)
            self.assertNotIn("path", user["metadata"]["artifactRefs"][0])
            self.assertEqual(
                user["metadata"]["artifactRefs"][1]["name"],
                "Unavailable attachment",
            )
            trajectory = TrajectoryProjection.from_session(session)
            self.assertNotIn("C:\\private", json.dumps(trajectory.to_dict()))

    def test_recent_session_projection_sanitizes_only_page_items(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-session-page-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            for index in range(1000):
                session.add_message("user", f"question {index}")
                session.add_message("assistant", f"answer {index}")

            with patch(
                "nanocat.application.projections._public_content",
                side_effect=lambda value: value,
            ) as sanitize:
                conversation = ConversationProjection.from_session_recent(session, limit=1)
            self.assertEqual(len(conversation.items), 1)
            self.assertEqual(sanitize.call_count, 1)

            with patch(
                "nanocat.application.projections._public_content",
                side_effect=lambda value: value,
            ) as sanitize:
                trajectory = TrajectoryProjection.from_session_recent(session, limit=1)
            self.assertEqual(len(trajectory.items), 1)
            self.assertEqual(sanitize.call_count, 1)

    def test_active_replacement_rolls_back_as_one_transaction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-session-replace-") as root:
            manager = SessionManager(Path(root) / "sessions")
            original = manager.get_or_create("web", "chat")
            original_path = manager._session_path("web", original.id)
            original_bytes = original_path.read_bytes()

            with patch.object(
                manager,
                "_write_metadata",
                side_effect=OSError("simulated metadata failure"),
            ):
                with self.assertRaisesRegex(OSError, "metadata failure"):
                    manager.delete_active_and_replace("web", original.id)

            self.assertEqual(original_path.read_bytes(), original_bytes)
            restored = manager.get_or_create("web", "chat")
            self.assertEqual(restored.id, original.id)
            self.assertEqual(
                [path.name for path in (Path(root) / "sessions" / "web").glob("*.jsonl")],
                [original_path.name],
            )

    def test_delete_session_rechecks_persisted_revision(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-session-cas-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            stale_revision = session.revision
            session.add_message("user", "new history")
            manager.save(session, expected_revision=stale_revision)

            with self.assertRaises(SessionRevisionConflictError):
                manager.delete_session(
                    "web",
                    session.id,
                    allow_active=True,
                    expected_revision=stale_revision,
                )
            self.assertTrue(manager._session_path("web", session.id).exists())

    def test_concurrent_session_creation_keeps_complete_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-session-race-") as root:
            manager = SessionManager(Path(root) / "sessions")
            count = 32

            def create(index: int) -> str:
                return manager.get_or_create("web", f"chat-{index}").id

            async def create_all() -> list[str]:
                return await asyncio.gather(
                    *(asyncio.to_thread(create, index) for index in range(count))
                )

            ids = asyncio.run(create_all())
            metadata = manager._read_metadata("web")
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(len(metadata["sessions"]), count)
            self.assertEqual(len(metadata["chats"]), count)
            for session_id in ids:
                self.assertTrue(manager._session_path("web", session_id).exists())


class AsyncRuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbound_progress_preserves_fifo_before_terminal(self) -> None:
        bus = MessageBus()
        progress = OutboundMessage(
            channel="web",
            chat_id="chat",
            content="thinking",
            turn_id="turn-1",
            metadata={"_progress": True, "_thinking": True},
        )
        terminal = OutboundMessage(
            channel="web",
            chat_id="chat",
            content="done",
            turn_id="turn-1",
        )

        await bus.publish_outbound(progress)
        await bus.publish_outbound(terminal)

        self.assertIs(await bus.consume_outbound(), progress)
        self.assertIs(await bus.consume_outbound(), terminal)
        await bus.close()

    @staticmethod
    def _minimal_recovery_engine(root: Path) -> AgentLoop:
        engine = object.__new__(AgentLoop)
        engine.sessions = SessionManager(root / "sessions")
        engine.context = ContextBuilder(root / "workspace")
        engine.runtime_files = SimpleNamespace(
            root=root / "workspace" / "_runtime_temp",
            max_file_bytes=1024 * 1024,
            max_session_bytes=4 * 1024 * 1024,
        )
        engine._stopped_turns = {}
        engine._post_stop_buf = {}
        engine._pending_durability = {}
        engine._durability_retry_task = None
        engine._session_locks = {}
        engine._ingress_ordinals = iter(range(1, 10000))
        engine._running = False
        engine._persistence_error_count = 0
        engine._background_tasks = []
        engine._background_scopes = {}
        engine._background_keys = {}
        engine._background_pending = {}
        engine.thread_manager = None
        engine.memory_compactor = SimpleNamespace()
        return engine

    async def test_stopped_turn_journal_replays_exactly_once_after_restart(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-recovery-") as root:
            root_path = Path(root)
            first = self._minimal_recovery_engine(root_path)
            session = first.sessions.get_or_create("web", "chat")
            stopped = _StoppedTurn(
                session=session,
                run_state=_TurnRunState(
                    turn_messages=[{"role": "user", "content": "preserve stopped input"}]
                ),
                transient=False,
                turn_id="turn-recovery",
                terminal_content="Stopped safely.",
            )
            first._stopped_turns[session.key] = [stopped]
            with patch.object(
                first,
                "_persist_turn_entries",
                side_effect=OSError("simulated durable write failure"),
            ):
                first._flush_stopped_turn_retries()

            journal = list(first._stopped_recovery_dir.glob("stopped_*.json"))
            self.assertEqual(len(journal), 1)
            self.assertEqual(first._stopped_turns, {})

            second = self._minimal_recovery_engine(root_path)
            second._recover_stopped_turn_journal()
            recovered = second.sessions.get_session("web", session.id)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(
                [message["content"] for message in recovered.messages],
                ["preserve stopped input", "Stopped safely."],
            )
            self.assertEqual(list(second._stopped_recovery_dir.glob("stopped_*.json")), [])

            second._recover_stopped_turn_journal()
            recovered_again = second.sessions.get_session("web", session.id)
            assert recovered_again is not None
            self.assertEqual(len(recovered_again.messages), 2)
            await first.sessions.close()
            await second.sessions.close()

    async def test_stopped_turn_recovery_replays_session_order_not_file_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-order-") as root:
            root_path = Path(root)
            first = self._minimal_recovery_engine(root_path)
            session = first.sessions.get_or_create("web", "chat")
            early = _StoppedTurn(
                session=session,
                run_state=_TurnRunState(
                    turn_messages=[{"role": "user", "content": "first"}]
                ),
                transient=False,
                order_key=(1, 1.0),
                recovery_id="f" * 32,
                terminal_content="first stopped",
                runtime_session_key="web:chat",
            )
            late = _StoppedTurn(
                session=session,
                run_state=_TurnRunState(
                    turn_messages=[{"role": "user", "content": "second"}]
                ),
                transient=False,
                order_key=(2, 2.0),
                recovery_id="0" * 32,
                terminal_content="second stopped",
                runtime_session_key="web:chat",
            )
            self.assertTrue(first._journal_stopped_turn(early, "first stopped"))
            self.assertTrue(first._journal_stopped_turn(late, "second stopped"))

            second = self._minimal_recovery_engine(root_path)
            second._recover_stopped_turn_journal()
            recovered = second.sessions.get_session("web", session.id)
            assert recovered is not None
            self.assertEqual(
                [message["content"] for message in recovered.messages],
                ["first", "first stopped", "second", "second stopped"],
            )
            await first.sessions.close()
            await second.sessions.close()

    async def test_failed_startup_recovery_owns_session_until_ordered_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-startup-recovery-gate-") as root:
            root_path = Path(root)
            first = self._minimal_recovery_engine(root_path)
            session = first.sessions.get_or_create("web", "chat")
            early = _StoppedTurn(
                session=session,
                run_state=_TurnRunState(
                    turn_messages=[{"role": "user", "content": "recovered first"}]
                ),
                transient=False,
                order_key=(1, 1.0),
                recovery_id="a" * 32,
                terminal_content="first interrupted",
                runtime_session_key="web:chat",
            )
            self.assertTrue(first._journal_stopped_turn(early, "first interrupted"))

            second = self._minimal_recovery_engine(root_path)
            with patch.object(
                second,
                "_persist_stopped_turn",
                side_effect=OSError("disk unavailable"),
            ):
                second._recover_stopped_turn_journal()
            self.assertTrue(second.has_pending_session_durability("web:chat"))
            self.assertTrue(
                (second._stopped_recovery_dir / f"stopped_{'a' * 32}.json").exists()
            )

            late = InboundMessage(
                "web",
                "user",
                "chat",
                "accepted later",
                ingress_ordinal=2,
            )
            second._pending_durability.setdefault("web:chat", []).append(
                _PendingDurabilityRecord(
                    message=late,
                    transient=False,
                    order_key=(2, late.timestamp.timestamp()),
                    terminal_content=second.tips.turn_interrupted,
                    terminal_control={"kind": "runtime_shutdown"},
                )
            )
            self.assertTrue(second.retry_session_durability("web:chat"))
            recovered = second.sessions.get_session("web", session.id)
            assert recovered is not None
            self.assertEqual(
                [message["content"] for message in recovered.messages],
                [
                    "recovered first",
                    "first interrupted",
                    "accepted later",
                    second.tips.turn_interrupted,
                ],
            )
            self.assertFalse(second.has_pending_session_durability("web:chat"))
            self.assertEqual(list(second._stopped_recovery_dir.glob("stopped_*.json")), [])
            await first.sessions.close()
            await second.sessions.close()

    async def test_durability_retry_owner_progresses_without_new_ingress(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-durability-owner-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = True
            session = engine.sessions.get_or_create("web", "chat")
            engine._stopped_turns["web:chat"] = [
                _StoppedTurn(
                    session=session,
                    run_state=_TurnRunState(
                        turn_messages=[{"role": "user", "content": "retry me"}]
                    ),
                    transient=False,
                    terminal_content="interrupted",
                    runtime_session_key="web:chat",
                )
            ]
            original = engine._persist_turn_entries
            attempts = 0

            def flaky(*args: Any, **kwargs: Any) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("disk unavailable")
                original(*args, **kwargs)

            with patch.object(engine, "_persist_turn_entries", side_effect=flaky):
                engine._ensure_session_durability_retry("web:chat")
                task = engine._durability_retry_task
                self.assertIsNotNone(task)
                assert task is not None
                await asyncio.wait_for(task, timeout=1.0)
            engine._running = False
            self.assertGreaterEqual(attempts, 2)
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            recovered = engine.sessions.get_session("web", session.id)
            assert recovered is not None
            self.assertEqual(
                [message["content"] for message in recovered.messages],
                ["retry me", "interrupted"],
            )
            await engine.sessions.close()

    async def test_runtime_fallback_wakes_durability_retry_without_new_ingress(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-durability-wakeup-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = True
            engine._stop_requested = set()
            message = InboundMessage("web", "user", "chat", "retain and retry")
            pending = engine._new_pending_turn(message)
            original = engine._persist_turn_entries
            attempts = 0

            def flaky(*args: Any, **kwargs: Any) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("disk unavailable")
                original(*args, **kwargs)

            with patch.object(engine, "_persist_turn_entries", side_effect=flaky):
                self.assertFalse(engine._persist_pending_failure(pending))
                task = engine._durability_retry_task
                self.assertIsNotNone(task)
                assert task is not None
                await asyncio.wait_for(task, timeout=1.0)
            engine._running = False
            self.assertGreaterEqual(attempts, 2)
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            await engine.sessions.close()

    async def test_durability_scheduler_skips_active_stop_domain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-domain-retry-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            blocked_session = engine.sessions.get_or_create("web", "blocked")
            engine._stopped_turns["web:blocked"] = [
                _StoppedTurn(
                    session=blocked_session,
                    run_state=_TurnRunState(
                        turn_messages=[{"role": "user", "content": "keep retrying"}]
                    ),
                    transient=False,
                    terminal_content="interrupted",
                    runtime_session_key="web:blocked",
                )
            ]
            stop_message = InboundMessage("web", "user", "target", "arrived during stop")
            engine._pending_durability = {
                "web:target": [
                    _PendingDurabilityRecord(
                        message=stop_message,
                        transient=False,
                        order_key=(0, stop_message.timestamp.timestamp()),
                        terminal_content=engine.tips.turn_interrupted,
                        terminal_control={"kind": "runtime_shutdown"},
                    )
                ]
            }
            engine._stop_requested = {"web:target"}
            engine._running = True
            original_retry = engine.retry_session_durability
            blocked_attempted = asyncio.Event()
            target_attempted = asyncio.Event()

            def selective_retry(session_key: str) -> bool:
                if session_key == "web:blocked":
                    blocked_attempted.set()
                    return False
                target_attempted.set()
                result = original_retry(session_key)
                engine._running = False
                return result

            with patch.object(
                engine,
                "retry_session_durability",
                side_effect=selective_retry,
            ):
                engine._ensure_session_durability_retry("web:blocked")
                retry = engine._durability_retry_task
                assert retry is not None
                await asyncio.wait_for(blocked_attempted.wait(), timeout=1)
                await asyncio.sleep(0.1)
                self.assertFalse(target_attempted.is_set())
                self.assertEqual(
                    engine._pending_durability["web:target"][0].message,
                    stop_message,
                )

                engine._stop_requested.clear()
                await asyncio.wait_for(target_attempted.wait(), timeout=1)
                await asyncio.wait_for(asyncio.shield(retry), timeout=1)

            self.assertFalse(engine.has_pending_session_durability("web:target"))
            target_session = engine.sessions.get_or_create("web", "target")
            self.assertEqual(
                [message["content"] for message in target_session.messages],
                ["arrived during stop", engine.tips.turn_interrupted],
            )
            await engine.sessions.close()

    async def test_stop_transfers_owner_before_terminal_and_retries_without_ingress(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-live-retry-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = True
            engine.turns = TurnCoordinator()
            turn_id = uuid4().hex
            engine.turns.start(turn_id, "web:chat", "web:local")
            session = engine.sessions.get_or_create("web", "chat")
            engine._stopped_turns["web:chat"] = [
                _StoppedTurn(
                    session=session,
                    run_state=_TurnRunState(
                        turn_messages=[{"role": "user", "content": "stop me"}]
                    ),
                    transient=False,
                    turn_id=turn_id,
                    runtime_session_key="web:chat",
                )
            ]
            engine.intervention = None
            engine.bus = MessageBus(maxsize=2)
            engine._stop_requested = set()
            engine._stop_operations = {}
            engine._turn_cancel_operations = {}
            engine._active_tasks = {}
            engine._direct_tasks = {}
            engine._task_pending_turns = {}
            engine._pending_buf = {}
            engine._session_gen = {}
            engine._steer_buf = {}
            engine._steer_events = {}
            engine._progressed = {}
            engine._handoff_messages = {}
            engine._web_steer_reservations = {}
            engine._web_turn_attachment_usage = {}

            class IdleSubagents:
                @staticmethod
                async def cancel_by_session(_session_key: str) -> int:
                    return 0

            engine.subagents = IdleSubagents()
            terminal_owner_state: list[bool] = []
            engine.turns.add_terminal_finalizer(
                turn_id,
                lambda: terminal_owner_state.append(
                    engine.has_pending_session_durability("web:chat")
                ),
            )
            original = engine._persist_stopped_turn
            attempts = 0

            def flaky(*args: Any, **kwargs: Any) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("disk unavailable")
                original(*args, **kwargs)

            with patch.object(engine, "_persist_stopped_turn", side_effect=flaky):
                response = await engine._handle_stop_locked(
                    InboundMessage("web", "user", "chat", "/stop")
                )
                task = engine._durability_retry_task
                if task is not None:
                    await asyncio.wait_for(task, timeout=1)

            engine._running = False
            self.assertTrue(response.metadata["persistence_failed"])
            self.assertEqual(terminal_owner_state, [True])
            self.assertGreaterEqual(attempts, 2)
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            self.assertEqual(engine.turns.get(turn_id).state, TurnState.FAILED)
            await engine.bus.close()
            await engine.sessions.close()

    async def test_cancelled_deferred_failure_retains_before_terminal_finalizer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-deferred-owner-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = True
            engine._stop_requested = set()
            engine.turns = TurnCoordinator()
            turn_id = uuid4().hex
            engine.turns.start(turn_id, "web:chat", "web:local")
            terminal_owner_state: list[bool] = []
            engine.turns.add_terminal_finalizer(
                turn_id,
                lambda: terminal_owner_state.append(
                    engine.has_pending_session_durability("web:chat")
                ),
            )
            session_lock = engine._session_locks.setdefault(
                "web:chat",
                asyncio.Lock(),
            )
            await session_lock.acquire()
            message = InboundMessage(
                "web",
                "user",
                "chat",
                "deferred input",
                turn_id=turn_id,
            )

            failure = asyncio.create_task(engine._deferred_delivery_failed(message))
            await asyncio.sleep(0)
            failure.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await failure

            self.assertEqual(terminal_owner_state, [True])
            self.assertEqual(engine.turns.get(turn_id).state, TurnState.FAILED)
            self.assertTrue(engine.has_pending_session_durability("web:chat"))
            session_lock.release()
            retry = engine._durability_retry_task
            if retry is not None:
                await asyncio.wait_for(retry, timeout=1)
            engine._running = False
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            session = engine.sessions.get_or_create("web", "chat")
            self.assertEqual(
                [item["content"] for item in session.messages],
                ["deferred input", engine.tips.turn_cancelled],
            )
            await engine.sessions.close()

    async def test_deferred_delivery_batch_is_owned_after_producer_returns(self) -> None:
        started = asyncio.Event()
        release_sink = asyncio.Event()
        delivered: list[str] = []
        released: list[str] = []
        failed: list[str] = []

        async def request_user(_request: Any) -> bool:
            return True

        async def sink(message: InboundMessage) -> None:
            started.set()
            await release_sink.wait()
            delivered.append(message.content)

        async def failure(message: InboundMessage) -> None:
            failed.append(message.content)

        broker = InterventionBroker(request_user, deferred_sink=sink)
        broker.set_deferred_release(lambda message: released.append(message.content))
        broker.set_deferred_failure(failure)
        messages = [
            InboundMessage("web", "user", "chat", f"message-{index}")
            for index in range(3)
        ]
        await broker._deliver_deferred(messages)
        await started.wait()
        worker = broker._deferred_delivery_task
        self.assertIsNotNone(worker)
        release_sink.set()
        assert worker is not None
        await asyncio.wait_for(asyncio.shield(worker), timeout=1)
        self.assertEqual(delivered, [message.content for message in messages])
        self.assertEqual(released, [message.content for message in messages])
        self.assertEqual(failed, [])
        self.assertEqual(len(broker._deferred_delivery_backlog), 0)
        await broker.close()

    async def test_deferred_sink_cancellation_and_close_transfer_to_failure_owner(
        self,
    ) -> None:
        sink_calls: list[str] = []
        failed: list[str] = []
        released: list[str] = []

        async def request_user(_request: Any) -> bool:
            return True

        async def sink(message: InboundMessage) -> None:
            sink_calls.append(message.content)
            if message.content == "cancelled-sink":
                raise asyncio.CancelledError

        async def failure(message: InboundMessage) -> None:
            failed.append(message.content)

        broker = InterventionBroker(request_user, deferred_sink=sink)
        broker.set_deferred_release(lambda message: released.append(message.content))
        broker.set_deferred_failure(failure)
        messages = [
            InboundMessage("web", "user", "chat", content)
            for content in ("first", "cancelled-sink", "third")
        ]
        await broker._deliver_deferred(messages)
        worker = broker._deferred_delivery_task
        assert worker is not None
        await asyncio.wait_for(asyncio.shield(worker), timeout=1)
        self.assertEqual(sink_calls, ["first", "cancelled-sink", "third"])
        self.assertEqual(failed, ["cancelled-sink"])

        conversation = ConversationRef("web", "chat", "web:chat")
        await broker.begin_defer_scope(conversation)
        held = InboundMessage("web", "user", "chat", "held-at-close")
        self.assertTrue(broker.defer(conversation, held))
        await broker.close()
        self.assertEqual(failed, ["cancelled-sink", "held-at-close"])
        self.assertEqual(
            released,
            ["first", "cancelled-sink", "third", "held-at-close"],
        )
        self.assertEqual(len(broker._deferred_delivery_backlog), 0)

    async def test_deferred_close_cancels_blocked_sink_into_failure_owner(self) -> None:
        sink_started = asyncio.Event()
        failed: list[str] = []
        released: list[str] = []

        async def request_user(_request: Any) -> bool:
            return True

        async def sink(_message: InboundMessage) -> None:
            sink_started.set()
            await asyncio.Future()

        async def failure(message: InboundMessage) -> None:
            failed.append(message.content)

        broker = InterventionBroker(request_user, deferred_sink=sink)
        broker.set_deferred_release(lambda message: released.append(message.content))
        broker.set_deferred_failure(failure)
        message = InboundMessage("web", "user", "chat", "blocked-at-close")
        delivery = asyncio.create_task(broker._deliver_deferred([message]))
        await sink_started.wait()
        await asyncio.wait_for(broker.close(), timeout=1)
        await asyncio.wait_for(delivery, timeout=1)
        self.assertEqual(failed, [message.content])
        self.assertEqual(released, [message.content])
        self.assertEqual(len(broker._deferred_delivery_backlog), 0)

    async def test_deferred_failure_does_not_hold_producer_session_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-deferred-deadlock-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = True
            engine._stop_requested = set()
            engine.turns = TurnCoordinator()
            sink_calls: list[str] = []

            async def request_user(_request: Any) -> bool:
                return True

            async def sink(message: InboundMessage) -> None:
                sink_calls.append(message.content)
                if message.content == "first":
                    raise RuntimeError("fixture sink failure")

            broker = InterventionBroker(request_user, deferred_sink=sink)
            broker.set_deferred_failure(engine._deferred_delivery_failed)
            messages = [
                InboundMessage(
                    "web",
                    "user",
                    "chat",
                    content,
                    turn_id=uuid4().hex,
                )
                for content in ("first", "second")
            ]
            for message in messages:
                assert message.turn_id is not None
                engine.turns.start(message.turn_id, message.session_key, "web:local")

            session_lock = engine._session_locks.setdefault("web:chat", asyncio.Lock())
            await session_lock.acquire()
            await asyncio.wait_for(broker._deliver_deferred(messages), timeout=0.1)
            worker = broker._deferred_delivery_task
            assert worker is not None
            await asyncio.sleep(0.1)
            self.assertFalse(worker.done())
            session_lock.release()
            await asyncio.wait_for(asyncio.shield(worker), timeout=1)

            self.assertEqual(sink_calls, ["first", "second"])
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            await broker.close()
            engine._running = False
            await engine.sessions.close()

    async def test_active_deferred_sink_is_not_claimed_after_bus_commit(self) -> None:
        bus = MessageBus(maxsize=2)
        committed = asyncio.Event()
        allow_return = asyncio.Event()
        original_put = bus.inbound.put

        async def controlled_put(priority: int, message: InboundMessage) -> None:
            await original_put(priority, message)
            committed.set()
            await allow_return.wait()

        bus.inbound.put = controlled_put  # type: ignore[method-assign]

        async def request_user(_request: Any) -> bool:
            return True

        broker = InterventionBroker(request_user, deferred_sink=bus.publish_inbound)
        message = InboundMessage(
            "web",
            "user",
            "chat",
            "committed once",
            turn_id=uuid4().hex,
        )
        await broker._deliver_deferred([message])
        await committed.wait()

        self.assertEqual(broker.extract_deferred_session(message.session_key), [])
        self.assertEqual(bus.drain_inbound(message.session_key), [message])
        allow_return.set()
        worker = broker._deferred_delivery_task
        assert worker is not None
        await asyncio.wait_for(asyncio.shield(worker), timeout=1)
        self.assertEqual(bus.drain_inbound(message.session_key), [])
        await broker.close()
        await bus.close()

    async def test_exact_cancel_failure_retains_every_claimed_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-exact-cancel-owner-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = True
            engine._stop_requested = set()
            engine.turns = TurnCoordinator()
            engine.bus = MessageBus(maxsize=4)
            engine.intervention = None
            engine._handoff_messages = {}
            engine._turn_admission_slots = asyncio.Semaphore(0)
            engine._web_steer_reservations = {"web:chat": (2, 11)}
            engine._web_turn_attachment_usage = {}
            turn_id = uuid4().hex
            engine.turns.start(turn_id, "web:chat", "web:local")
            engine.turns.begin_cancel(turn_id, "fixture exact cancellation")
            messages = [
                InboundMessage(
                    "web",
                    "user",
                    "chat",
                    content,
                    metadata={
                        "_web_steer_reserved": True,
                        "_runtime_admission_held": True,
                    },
                    turn_id=turn_id,
                )
                for content in ("first late", "second late")
            ]
            claim = _TurnCancellationClaim(messages=messages, task=None, pending_turn=None)

            async def fail_first(_pending_turn: _PendingTurn, **_kwargs: Any) -> bool:
                return False

            with patch.object(
                engine,
                "_persist_pending_interruption_ordered",
                side_effect=fail_first,
            ):
                self.assertTrue(
                    await engine._cancel_turn_owned(turn_id, "web:chat", claim)
                )

            self.assertEqual(engine.turns.get(turn_id).state, TurnState.CANCELLED)
            self.assertTrue(engine.has_pending_session_durability("web:chat"))
            self.assertEqual(engine._turn_admission_slots._value, 2)
            self.assertEqual(engine._web_steer_reservations, {})
            retry = engine._durability_retry_task
            if retry is not None:
                await asyncio.wait_for(asyncio.shield(retry), timeout=1)
            engine._running = False
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            session = engine.sessions.get_or_create("web", "chat")
            encoded = json.dumps(session.messages, ensure_ascii=False)
            self.assertEqual(encoded.count("first late"), 1)
            self.assertEqual(encoded.count("second late"), 1)
            await engine.bus.close()
            await engine.sessions.close()

    async def test_exact_cancel_gate_transfers_late_handoff_immediately(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-exact-gate-owner-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = False
            engine._stop_requested = set()
            engine._handoff_messages = {}
            engine._turn_admission_slots = asyncio.Semaphore(0)
            engine._web_steer_reservations = {"web:chat": (1, 13)}
            engine._web_turn_attachment_usage = {}
            message = InboundMessage(
                "web",
                "user",
                "chat",
                "late handoff",
                metadata={"_web_steer_reserved": True},
                turn_id=uuid4().hex,
            )
            engine._handoff_messages[7] = message

            engine._retain_cancelling_handoff(7, message)

            self.assertEqual(engine._handoff_messages, {})
            self.assertTrue(engine.has_pending_session_durability("web:chat"))
            self.assertEqual(engine._turn_admission_slots._value, 1)
            self.assertEqual(engine._web_steer_reservations, {})
            engine._running = True
            engine._wake_durability_retry("web:chat")
            retry = engine._durability_retry_task
            assert retry is not None
            await asyncio.wait_for(asyncio.shield(retry), timeout=1)
            engine._running = False
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            session = engine.sessions.get_or_create("web", "chat")
            encoded = json.dumps(session.messages, ensure_ascii=False)
            self.assertEqual(encoded.count("late handoff"), 1)
            await engine.sessions.close()

    async def test_durability_retry_only_runs_under_the_session_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-durability-lock-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._running = False
            engine._stop_requested = set()
            message = InboundMessage("web", "user", "chat", "ordered fallback")
            engine._retain_pending_interruption(engine._new_pending_turn(message))
            session_lock = engine._session_locks.setdefault("web:chat", asyncio.Lock())
            await session_lock.acquire()

            engine._running = True
            self.assertFalse(engine.try_reserve_session_operation("web:chat"))
            retry = engine._durability_retry_task
            assert retry is not None
            await asyncio.sleep(0.05)
            session = engine.sessions.get_or_create("web", "chat")
            self.assertEqual(session.messages, [])

            session_lock.release()
            await asyncio.wait_for(asyncio.shield(retry), timeout=1)
            engine._running = False
            session = engine.sessions.get_or_create("web", "chat")
            encoded = json.dumps(session.messages, ensure_ascii=False)
            self.assertEqual(encoded.count("ordered fallback"), 1)
            await engine.sessions.close()

    async def test_recovery_journal_failure_retains_live_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-owner-") as root:
            root_path = Path(root)
            engine = self._minimal_recovery_engine(root_path)
            session = engine.sessions.get_or_create("web", "chat")
            stopped = _StoppedTurn(
                session=session,
                run_state=_TurnRunState(
                    turn_messages=[{"role": "user", "content": "retained"}]
                ),
                transient=False,
                runtime_session_key="web:chat",
            )
            engine._stopped_turns["web:chat"] = [stopped]
            blocked_root = root_path / "blocked-runtime-root"
            blocked_root.write_text("not a directory", encoding="utf-8")
            engine.runtime_files.root = blocked_root
            with patch.object(
                engine,
                "_persist_turn_entries",
                side_effect=OSError("simulated session failure"),
            ):
                engine._flush_stopped_turn_retries()
            retained = engine._stopped_turns["web:chat"]
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].run_state.turn_messages, stopped.run_state.turn_messages)
            self.assertEqual(retained[0].runtime_session_key, "web:chat")
            await engine.sessions.close()

    async def test_recovery_payload_strips_inline_image_bulk(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-image-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine.runtime_files.max_file_bytes = 1024
            session = engine.sessions.get_or_create("web", "chat")
            stopped = _StoppedTurn(
                session=session,
                run_state=_TurnRunState(
                    turn_messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64," + "A" * 4096
                                    },
                                    "_meta": {"path": "C:/private/image.png"},
                                },
                                {"type": "text", "text": "inspect"},
                            ],
                        }
                    ]
                ),
                transient=False,
                runtime_session_key="web:chat",
            )
            self.assertTrue(engine._journal_stopped_turn(stopped, "stopped"))
            payload = next(engine._stopped_recovery_dir.glob("stopped_*.json")).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("data:image", payload)
            self.assertNotIn("C:/private", payload)
            self.assertIn("[image]", payload)
            await engine.sessions.close()

    async def test_durability_gate_retries_in_order_and_uses_runtime_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-durability-gate-") as root:
            engine = self._minimal_recovery_engine(Path(root))
            engine._exclusive_sessions = set()
            engine._command_dispatcher = None
            engine._stop_operations = {}
            engine._active_tasks = {}
            engine._session_locks = {}
            engine._handoff_messages = {}
            engine._turn_admission_slots = asyncio.BoundedSemaphore(2)
            engine._ingress_ordinals = iter(range(1, 100))
            engine.bus = MessageBus(maxsize=2)
            engine.turns = TurnCoordinator()
            session = engine.sessions.get_or_create("web", "chat")
            stopped = _StoppedTurn(
                session,
                _TurnRunState([{"role": "user", "content": "first"}]),
                False,
                terminal_content="interrupted",
                runtime_session_key="web:chat",
            )
            engine._stopped_turns["web:chat"] = [stopped]
            with patch.object(
                engine,
                "_persist_turn_entries",
                side_effect=OSError("disk unavailable"),
            ):
                self.assertFalse(engine.retry_session_durability("web:chat"))
                self.assertTrue(engine.is_session_busy("web:chat"))
            self.assertTrue(engine.retry_session_durability("web:chat"))
            self.assertFalse(engine.has_pending_session_durability("web:chat"))
            recovered = engine.sessions.get_session("web", session.id)
            assert recovered is not None
            self.assertEqual(
                [message["content"] for message in recovered.messages],
                ["first", "interrupted"],
            )

            system = engine.sessions.get_system_session("heartbeat")
            with patch.object(
                engine,
                "_persist_turn_entries",
                side_effect=OSError("disk unavailable"),
            ):
                self.assertFalse(
                    engine._persist_interrupted_turn(
                        system,
                        _TurnRunState([{"role": "user", "content": "pulse"}]),
                        True,
                        runtime_session_key="heartbeat",
                    )
                )
            self.assertIn("heartbeat", engine._stopped_turns)
            self.assertNotIn(system.key, engine._stopped_turns)
            await engine.bus.close()
            await engine.sessions.close()

    async def test_corrupt_session_recovery_does_not_skip_later_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-stop-corrupt-") as root:
            root_path = Path(root)
            engine = self._minimal_recovery_engine(root_path)
            broken = engine.sessions.get_or_create("web", "broken")
            healthy = engine.sessions.get_or_create("web", "healthy")
            engine.sessions._session_path("web", broken.id).write_text("{", encoding="utf-8")
            engine._stopped_turns = {
                "web:broken": [
                    _StoppedTurn(
                        broken,
                        _TurnRunState([{"role": "user", "content": "broken"}]),
                        False,
                        runtime_session_key="web:broken",
                    )
                ],
                "web:healthy": [
                    _StoppedTurn(
                        healthy,
                        _TurnRunState([{"role": "user", "content": "healthy"}]),
                        False,
                        runtime_session_key="web:healthy",
                    )
                ],
            }
            with patch.object(
                engine,
                "_persist_turn_entries",
                side_effect=OSError("simulated session failure"),
            ):
                engine._flush_stopped_turn_retries()
            self.assertEqual(engine._stopped_turns, {})
            self.assertEqual(len(list(engine._stopped_recovery_dir.glob("stopped_*.json"))), 2)
            await engine.sessions.close()

    async def test_nowledge_ack_stops_at_pre_request_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-nowledge-ack-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            session.add_message("user", "first question")
            session.add_message("assistant", "first answer")
            manager.save(session, expected_revision=0)
            create_started = asyncio.Event()
            create_release = asyncio.Event()
            appended: list[list[dict[str, str]]] = []

            class Client:
                async def create_thread(self, *, thread_id: str, **_kwargs: Any) -> str:
                    create_started.set()
                    await create_release.wait()
                    return thread_id

                async def append_messages(
                    self,
                    _thread_id: str,
                    messages: list[dict[str, str]],
                    **_kwargs: Any,
                ) -> None:
                    appended.append(messages)

            threads = NowledgeThreadManager(
                Client(),
                manager,
                Path(root) / "workspace",
                auto_distill_enabled=False,
            )
            first_sync = asyncio.create_task(threads.append_turn(session, session.messages[-2:]))
            await create_started.wait()
            revision = session.revision
            session.add_message("user", "second question")
            session.add_message("assistant", "second answer")
            manager.save(session, expected_revision=revision)
            create_release.set()
            await first_sync
            sync = session.metadata["_nowledge_thread_sync"]
            self.assertEqual(sync["acked_source_index"], 2)

            await threads.append_turn(session, session.messages[-2:])
            self.assertEqual(
                [item["content"] for item in appended[0]],
                ["second question", "second answer"],
            )
            self.assertEqual(sync["acked_source_index"], 4)
            await manager.close()

    async def test_nowledge_metadata_conflict_merges_remote_ack_monotonically(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-nowledge-cas-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            session.add_message("user", "question")
            session.add_message("assistant", "answer")
            manager.save(session, expected_revision=0)
            created_ids: list[str] = []

            class Client:
                async def create_thread(self, *, thread_id: str, **_kwargs: Any) -> str:
                    created_ids.append(thread_id)
                    return thread_id

            threads = NowledgeThreadManager(
                Client(),
                manager,
                Path(root) / "workspace",
                auto_distill_enabled=False,
            )
            original_save = manager.save
            attempts = 0

            def conflict_once(*args: Any, **kwargs: Any) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise SessionRevisionConflictError("simulated concurrent save")
                original_save(*args, **kwargs)

            with patch.object(manager, "save", side_effect=conflict_once):
                await threads.append_turn(session, session.messages)

            self.assertEqual(attempts, 2)
            self.assertEqual(len(created_ids), 1)
            sync = session.metadata["_nowledge_thread_sync"]
            self.assertEqual(sync["thread_id"], created_ids[0])
            self.assertEqual(sync["acked_source_index"], 2)
            persisted = manager.get_session("web", session.id, chat_id="chat")
            assert persisted is not None
            self.assertEqual(
                persisted.metadata["_nowledge_thread_sync"]["acked_source_index"],
                2,
            )
            await manager.close()

    async def test_nowledge_save_failure_rolls_back_only_owned_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-nowledge-rollback-") as root:
            manager = SessionManager(Path(root) / "sessions")
            session = manager.get_or_create("web", "chat")
            session.add_message("user", "question")
            session.add_message("assistant", "answer")
            manager.save(session, expected_revision=0)
            create_started = asyncio.Event()
            create_release = asyncio.Event()

            class Client:
                async def create_thread(self, *, thread_id: str, **_kwargs: Any) -> str:
                    create_started.set()
                    await create_release.wait()
                    return thread_id

            threads = NowledgeThreadManager(
                Client(),
                manager,
                Path(root) / "workspace",
                auto_distill_enabled=False,
            )
            with patch.object(manager, "save", side_effect=OSError("disk unavailable")):
                sync_task = asyncio.create_task(threads.append_turn(session, session.messages))
                await create_started.wait()
                session.metadata["_todo_lists"] = {"active": ["preserve"]}
                create_release.set()
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    await sync_task

            self.assertEqual(
                session.metadata["_todo_lists"],
                {"active": ["preserve"]},
            )
            self.assertNotIn("_nowledge_thread_sync", session.metadata)
            self.assertNotIn("nowledge_thread_id", session.metadata)
            await manager.close()

    async def test_raw_pending_journal_replays_when_session_materializes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-pending-recovery-") as root:
            root_path = Path(root)
            first = self._minimal_recovery_engine(root_path)
            pending = _PendingTurn(
                message=InboundMessage(
                    "web",
                    "user",
                    "chat",
                    "preserve raw ingress",
                    turn_id="turn-raw-recovery",
                ),
                session_key=None,
                transient=False,
                ordinal=7,
            )
            self.assertTrue(
                first._journal_pending_turn(
                    _PendingDurabilityRecord(
                        message=pending.message,
                        transient=pending.transient,
                        order_key=first._pending_order_key(pending),
                        terminal_content="Recovered after restart.",
                        terminal_control={"kind": "runtime_shutdown"},
                    )
                )
            )

            second = self._minimal_recovery_engine(root_path)
            second._recover_stopped_turn_journal()
            sessions = second.sessions.list_sessions("web", min_turns=0)
            self.assertEqual(len(sessions), 1)
            recovered = second.sessions.get_session("web", sessions[0]["id"])
            assert recovered is not None
            self.assertEqual(
                [message["content"] for message in recovered.messages],
                ["preserve raw ingress", "Recovered after restart."],
            )
            await first.sessions.close()
            await second.sessions.close()

    async def test_background_work_is_coalesced_and_globally_bounded(self) -> None:
        engine = object.__new__(AgentLoop)
        engine._background_tasks = []
        engine._background_scopes = {}
        engine._background_keys = {}
        engine._background_pending = {}
        engine._MAX_BACKGROUND_TASKS = 2
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def work(name: str, wait: bool = False) -> None:
            calls.append(name)
            if wait:
                started.set()
                await release.wait()

        engine._schedule_background(work("first", wait=True), storage_scope="a", kind="x")
        await started.wait()
        engine._schedule_background(work("superseded"), storage_scope="a", kind="x")
        engine._schedule_background(work("latest"), storage_scope="a", kind="x")
        engine._schedule_background(work("second", wait=True), storage_scope="b", kind="x")
        engine._schedule_background(work("over-limit"), storage_scope="c", kind="x")
        await asyncio.sleep(0)
        self.assertEqual(len(engine._background_keys), 2)
        release.set()
        await asyncio.gather(*tuple(engine._background_tasks), return_exceptions=True)
        self.assertEqual(calls, ["first", "second", "latest"])
        self.assertEqual(engine._background_pending, {})

    async def test_joined_steer_defers_and_can_take_over_terminal_transition(self) -> None:
        turns = TurnCoordinator()
        turns.start("turn-a", "web:chat", "user")
        self.assertTrue(turns.reserve_join("turn-a"))
        turns.complete("turn-a")
        self.assertEqual(turns.get("turn-a").state, TurnState.RUNNING)
        self.assertTrue(turns.terminal_deferred("turn-a"))
        self.assertTrue(turns.activate_join("turn-a"))
        self.assertFalse(turns.terminal_deferred("turn-a"))
        turns.start("turn-a", "web:chat", "user")
        turns.complete("turn-a")
        self.assertEqual(turns.get("turn-a").state, TurnState.COMPLETED)

    async def test_terminal_deferred_decision_is_frozen_before_outbound_delivery(self) -> None:
        engine = object.__new__(AgentLoop)
        engine.turns = TurnCoordinator()
        engine.turns.start("turn-a", "web:chat", "user")
        self.assertTrue(engine.turns.reserve_join("turn-a"))
        engine.turns.complete("turn-a")
        message = InboundMessage(
            "web",
            "user",
            "chat",
            "steer",
            metadata={"turn_id": "turn-a"},
        )
        response = OutboundMessage("web", "chat", "first response")

        engine._snapshot_terminal_deferred(message, response)
        self.assertTrue(response.metadata["_terminal_deferred"])
        self.assertTrue(engine.turns.activate_join("turn-a"))
        self.assertTrue(response.metadata["_terminal_deferred"])

    async def test_idempotent_mutation_finishes_and_is_reused_after_caller_cancel(self) -> None:
        cache = _IdempotencyCache(max_entries=2)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def operation() -> JSONResponse:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return JSONResponse({"call": calls}, status_code=201)

        first = asyncio.create_task(cache.execute("key-a", "same", operation))
        await started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(cache.execute("key-a", "same", operation))
        await asyncio.sleep(0)
        release.set()
        response = await second
        self.assertEqual(response.status_code, 201)
        self.assertEqual(json.loads(response.body), {"call": 1})
        self.assertEqual(calls, 1)
        await cache.close()

    async def test_idempotency_capacity_counts_detached_runtime_flights(self) -> None:
        cache = _IdempotencyCache(max_entries=1)
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked() -> JSONResponse:
            started.set()
            await release.wait()
            return JSONResponse({"ok": True})

        caller = asyncio.create_task(cache.execute("key-a", "a", blocked))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        rejected = await cache.execute(
            "key-b",
            "b",
            lambda: asyncio.sleep(0, result=JSONResponse({"duplicate": True})),
        )
        self.assertEqual(rejected.status_code, 503)
        release.set()
        await cache.close()

    async def test_stream_owner_closes_when_downstream_send_fails(self) -> None:
        closed = asyncio.Event()

        async def content():
            yield b"chunk"

        async def close_stream() -> None:
            closed.set()

        async def receive() -> dict[str, str]:
            await asyncio.Future()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body":
                raise OSError("downstream closed")

        response = _ClosingStreamingResponse(content(), close_stream=close_stream)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 2),
        }
        with self.assertRaises(Exception):
            await response(scope, receive, send)
        self.assertTrue(closed.is_set())

    async def test_artifact_stream_releases_lease_before_first_chunk(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-stream-") as root:
            source = Path(root) / "source.txt"
            source.write_text("bounded", encoding="utf-8")
            registry = ArtifactRegistry(Path(root) / "registry")
            entry = registry.register_path(source)
            opened = registry.open_for_read(entry.id)
            self.assertIsNotNone(opened)
            assert opened is not None
            _, handle, lease_id = opened
            closed = False

            async def close_stream() -> None:
                nonlocal closed
                if closed:
                    return
                closed = True
                handle.close()
                registry.release_lease(lease_id)

            response = _ClosingStreamingResponse(
                read_range(handle, 0, entry.size - 1),
                close_stream=close_stream,
            )

            async def receive() -> dict[str, str]:
                await asyncio.Future()
                return {"type": "http.disconnect"}

            async def send(_message: dict[str, Any]) -> None:
                raise OSError("response start failed")

            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/artifact",
                "raw_path": b"/artifact",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 2),
            }
            with self.assertRaises(Exception):
                await response(scope, receive, send)
            self.assertTrue(closed)
            self.assertTrue(handle.closed)
            self.assertEqual(registry._leases, {})
            self.assertEqual(registry._pins, {})
            await registry.close()

    async def test_failed_artifact_publish_keeps_previous_manifest_and_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-transaction-") as root:
            registry = ArtifactRegistry(
                Path(root) / "registry",
                max_file_bytes=16,
                max_total_bytes=16,
                max_entries=1,
            )
            from fastapi import UploadFile

            first = await registry.upload(
                UploadFile(file=io.BytesIO(b"first"), filename="first.txt")
            )
            fd, temp_name = tempfile.mkstemp(dir=registry.root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(b"second")
            artifact_id = uuid4().hex
            entry = ArtifactEntry(
                id=artifact_id,
                filename=f"{artifact_id}.bin",
                name="second.txt",
                size=6,
                media_type="text/plain",
                kind="text",
                created_at=0.0,
                last_access=0.0,
            )
            with patch.object(
                registry,
                "_save_entries_locked",
                side_effect=OSError("simulated manifest failure"),
            ):
                with self.assertRaisesRegex(OSError, "manifest failure"):
                    registry._publish_upload(entry, temp_name, entry.size)

            resolved = registry.resolve(first.id)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(resolved[1].read_bytes(), b"first")
            self.assertFalse((registry.root / entry.filename).exists())
            await registry.close()

    async def test_failed_artifact_reap_remains_accounted_and_blocks_growth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-reap-") as root:
            registry = ArtifactRegistry(
                Path(root) / "registry",
                max_file_bytes=10,
                max_total_bytes=10,
                max_entries=2,
            )
            first = await registry.upload(
                UploadFile(file=io.BytesIO(b"0123456789"), filename="first.txt")
            )
            with patch.object(registry, "_reap_pending_locked", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "physical quota"):
                    await registry.upload(
                        UploadFile(file=io.BytesIO(b"x"), filename="second.txt")
                    )
                self.assertIsNone(registry.resolve(first.id))
                self.assertIn(first.filename, registry._pending_deletes)
                self.assertEqual(registry._physical_owned_bytes_locked(), 10)
                with self.assertRaisesRegex(
                    RuntimeError,
                    "physical quota|capacity is leased",
                ):
                    await registry.upload(UploadFile(file=io.BytesIO(b"y"), filename="third.txt"))
            registry._reap_pending_locked()
            await registry.close()

    async def test_active_artifact_uploads_share_the_physical_quota(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-active-quota-") as root:
            registry = ArtifactRegistry(
                Path(root) / "registry",
                max_file_bytes=8,
                max_total_bytes=8,
                max_concurrent_uploads=2,
            )
            release = asyncio.Event()

            class HeldUpload(UploadFile):
                def __init__(self) -> None:
                    super().__init__(file=io.BytesIO(), filename="held.txt")
                    self._sent = False

                async def read(self, size: int = -1) -> bytes:
                    if not self._sent:
                        self._sent = True
                        return b"123456"
                    await release.wait()
                    return b""

            held = asyncio.create_task(registry.upload(HeldUpload()))
            try:
                for _ in range(100):
                    if registry._physical_owned_bytes_locked() == 6:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(registry._physical_owned_bytes_locked(), 6)
                with self.assertRaisesRegex(RuntimeError, "capacity is leased"):
                    await registry.upload(
                        UploadFile(file=io.BytesIO(b"789"), filename="blocked.txt")
                    )
                self.assertLessEqual(
                    registry._physical_owned_bytes_locked(),
                    registry.max_total_bytes,
                )
            finally:
                release.set()
                await held
            await registry.close()

    async def test_cancelled_artifact_publish_rolls_back_unacknowledged_entry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-cancel-") as root:
            registry = ArtifactRegistry(
                Path(root) / "registry",
                max_file_bytes=16,
                max_total_bytes=16,
                max_entries=1,
            )
            first = await registry.upload(
                UploadFile(file=io.BytesIO(b"first"), filename="first.txt")
            )
            original = registry._publish_upload
            published = threading.Event()
            release = threading.Event()

            def delayed_publish(*args: Any, **kwargs: Any) -> Any:
                receipt = original(*args, **kwargs)
                published.set()
                release.wait(timeout=5)
                return receipt

            with patch.object(registry, "_publish_upload", side_effect=delayed_publish):
                upload = asyncio.create_task(
                    registry.upload(UploadFile(file=io.BytesIO(b"second"), filename="second.txt"))
                )
                self.assertTrue(await asyncio.to_thread(published.wait, 5))
                upload.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await upload

            self.assertIsNotNone(registry.resolve(first.id))
            self.assertEqual(list(registry._entries), [first.id])
            self.assertEqual(registry._pending_publications, {})
            self.assertEqual(registry._protected_pending_files, {})
            self.assertEqual(
                [path.name for path in registry.root.glob("*.bin")],
                [first.filename],
            )
            await registry.close()

    async def test_blocked_artifact_publish_cancels_and_shutdown_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-blocked-publish-") as root:
            registry_root = Path(root) / "registry"
            registry = ArtifactRegistry(registry_root)
            original = registry._publish_upload
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def blocked_publish(*args: Any, **kwargs: Any) -> Any:
                with registry._lock:
                    started.set()
                    release.wait(timeout=5)
                    try:
                        return original(*args, **kwargs)
                    finally:
                        finished.set()

            with (
                patch.object(registry, "_publish_upload", side_effect=blocked_publish),
                patch("nanocat.api.artifacts._OWNER_CANCEL_TIMEOUT_S", 0.05),
                patch("nanocat.api.artifacts._CLOSE_LOCK_TIMEOUT_S", 0.05),
            ):
                upload = asyncio.create_task(
                    registry.upload(
                        UploadFile(file=io.BytesIO(b"blocked"), filename="blocked.txt")
                    )
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                upload.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(upload, timeout=0.5)
                await asyncio.wait_for(registry.close(), timeout=0.5)

            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 1))
            recovered = ArtifactRegistry(registry_root)
            self.assertEqual(recovered._entries, {})
            self.assertEqual(list(registry_root.glob(".*.tmp")), [])
            self.assertEqual(list(registry_root.glob("*.bin")), [])
            await recovered.close()

    async def test_detached_artifact_publication_releases_capacity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-detached-") as root:
            registry = ArtifactRegistry(Path(root) / "registry")
            original = registry._publish_upload
            committed = threading.Event()
            release = threading.Event()

            def delayed_return(*args: Any, **kwargs: Any) -> Any:
                receipt = original(*args, **kwargs)
                committed.set()
                release.wait(timeout=5)
                return receipt

            with (
                patch.object(registry, "_publish_upload", side_effect=delayed_return),
                patch("nanocat.api.artifacts._OWNER_CANCEL_TIMEOUT_S", 0.05),
            ):
                upload = asyncio.create_task(
                    registry.upload(
                        UploadFile(file=io.BytesIO(b"detached"), filename="detached.txt")
                    )
                )
                self.assertTrue(await asyncio.to_thread(committed.wait, 1))
                upload.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(upload, timeout=0.5)
                self.assertEqual(len(registry._pending_publications), 1)
                release.set()
                for _ in range(100):
                    if not registry._pending_publications:
                        break
                    await asyncio.sleep(0.01)

            self.assertEqual(registry._pending_publications, {})
            self.assertEqual(registry._entries, {})
            self.assertEqual(registry._pins, {})
            await registry.close()

    async def test_blocked_artifact_ack_keeps_event_loop_and_close_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-blocked-ack-") as root:
            registry = ArtifactRegistry(Path(root) / "registry")
            ack_started = threading.Event()
            ack_release = threading.Event()
            ack_finished = threading.Event()
            original_save = registry._save_locked

            def blocked_save() -> None:
                try:
                    ack_started.set()
                    ack_release.wait(timeout=5)
                    original_save()
                finally:
                    ack_finished.set()

            with (
                patch.object(registry, "_save_locked", side_effect=blocked_save),
                patch("nanocat.api.artifacts._OWNER_CANCEL_TIMEOUT_S", 0.05),
                patch("nanocat.api.artifacts._CLOSE_LOCK_TIMEOUT_S", 0.05),
            ):
                upload = asyncio.create_task(
                    registry.upload(
                        UploadFile(file=io.BytesIO(b"blocked"), filename="blocked.txt")
                    )
                )
                self.assertTrue(await asyncio.to_thread(ack_started.wait, 1))
                upload.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(upload, timeout=0.5)
                await asyncio.wait_for(registry.close(), timeout=0.5)

            ack_release.set()
            self.assertTrue(await asyncio.to_thread(ack_finished.wait, 1))
            for _ in range(100):
                if (
                    not registry._pending_publications
                    and not registry._pins
                    and not list(registry.root.glob(".*.tmp"))
                ):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(registry._pending_publications, {})
            self.assertEqual(registry._pins, {})
            self.assertEqual(list(registry.root.glob(".*.tmp")), [])

    async def test_artifact_close_owner_survives_caller_cancellation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-close-owner-") as root:
            registry = ArtifactRegistry(Path(root) / "registry")
            close_started = threading.Event()
            close_release = threading.Event()
            close_finished = threading.Event()
            original_save = registry._save_locked

            def blocked_save() -> None:
                try:
                    close_started.set()
                    close_release.wait(timeout=5)
                    original_save()
                finally:
                    close_finished.set()

            with (
                patch.object(registry, "_save_locked", side_effect=blocked_save),
                patch("nanocat.api.artifacts._CLOSE_LOCK_TIMEOUT_S", 0.05),
            ):
                first_close = asyncio.create_task(registry.close())
                self.assertTrue(await asyncio.to_thread(close_started.wait, 1))
                first_close.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first_close

                await asyncio.wait_for(registry.close(), timeout=0.5)
                owner = registry._close_task
                self.assertIsNotNone(owner)
                assert owner is not None
                self.assertTrue(owner.done())

            close_release.set()
            self.assertTrue(await asyncio.to_thread(close_finished.wait, 1))

    async def test_artifact_slow_storage_lock_does_not_block_upload_or_lease_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-lock-isolation-") as root:
            root_path = Path(root)
            source = root_path / "source.txt"
            source.write_text("source", encoding="utf-8")
            registry = ArtifactRegistry(root_path / "registry")
            first = registry.register_path(source)
            _, lease_id = registry.lease_paths([first.id])
            lock_started = threading.Event()
            lock_release = threading.Event()

            def hold_storage_lock() -> None:
                with registry._lock:
                    lock_started.set()
                    lock_release.wait(timeout=1)

            holder = threading.Thread(target=hold_storage_lock, daemon=True)
            holder.start()
            self.assertTrue(await asyncio.to_thread(lock_started.wait, 1))
            watchdog = threading.Timer(0.25, lock_release.set)
            watchdog.start()

            started_at = asyncio.get_running_loop().time()
            upload = asyncio.create_task(
                registry.upload(
                    UploadFile(file=io.BytesIO(b"second"), filename="second.txt")
                )
            )
            await asyncio.sleep(0.02)
            registry.schedule_release_lease(lease_id)
            elapsed = asyncio.get_running_loop().time() - started_at
            lock_release.set()
            await asyncio.wait_for(upload, timeout=1)
            await asyncio.to_thread(holder.join, 1)
            watchdog.cancel()

            self.assertLess(elapsed, 0.1)
            self.assertNotIn(lease_id, registry._leases)
            await registry.close()

    async def test_artifact_chunk_writer_completes_short_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-short-write-") as root:
            registry = ArtifactRegistry(Path(root) / "registry")

            class ShortWriter(io.BytesIO):
                def write(self, value: Any) -> int:
                    chunk = bytes(value)
                    limit = max(1, len(chunk) // 2)
                    return super().write(chunk[:limit])

            handle = ShortWriter()
            payload = b"short writes must be completed"
            written = registry._write_upload_chunk(handle, payload)
            self.assertEqual(written, len(payload))
            self.assertEqual(handle.getvalue(), payload)
            await registry.close()

    async def test_artifact_lease_releases_after_terminal_retention_race(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-retention-race-") as root:
            root_path = Path(root)
            source = root_path / "source.txt"
            source.write_text("retained", encoding="utf-8")
            registry = ArtifactRegistry(root_path / "registry")
            entry = registry.register_path(source)
            _, lease_id = registry.lease_paths([entry.id])
            assert lease_id is not None
            turns = TurnCoordinator(terminal_retention=1)
            turns.register("target", "web:chat", "web:local")
            turns.complete("target")
            turns.register("replacement", "web:chat", "web:local")
            turns.complete("replacement")
            self.assertIsNone(turns.get("target"))

            _bind_artifact_lease(turns, registry, "target", lease_id)
            self.assertNotIn(lease_id, registry._leases)
            self.assertEqual(registry._pins, {})
            await registry.close()

    async def test_artifact_reader_release_survives_close_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-reader-close-") as root:
            root_path = Path(root)
            source = root_path / "source.txt"
            source.write_text("payload", encoding="utf-8")
            registry = ArtifactRegistry(root_path / "registry")
            entry = registry.register_path(source)
            _, lease_id = registry.lease_paths([entry.id])
            released = False

            class FailingClose(io.BytesIO):
                def close(self) -> None:
                    super().close()
                    raise OSError("fixture close failure")

            handle = FailingClose(b"payload")
            with self.assertRaisesRegex(OSError, "close failure"):
                await registry.close_reader(handle, lease_id)
            self.assertEqual(registry._leases, {})
            self.assertEqual(registry._pins, {})

            second = FailingClose(b"payload")

            def release() -> None:
                nonlocal released
                released = True

            with self.assertRaisesRegex(OSError, "close failure"):
                list(read_range(second, 0, 6, on_close=release))
            self.assertTrue(released)
            await registry.close()

    async def test_detached_artifact_resource_results_are_reclaimed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-detached-results-") as root:
            root_path = Path(root)
            source = root_path / "source.txt"
            source.write_text("payload", encoding="utf-8")
            registry = ArtifactRegistry(root_path / "registry")
            entry = registry.register_path(source)

            lease_started = threading.Event()
            lease_release = threading.Event()
            original_lease_paths = registry.lease_paths

            def delayed_lease_paths(artifact_ids: list[str]) -> tuple[list[str], str | None]:
                result = original_lease_paths(artifact_ids)
                lease_started.set()
                lease_release.wait(timeout=5)
                return result

            with patch.object(registry, "lease_paths", side_effect=delayed_lease_paths):
                lease_task = asyncio.create_task(registry.lease_paths_async([entry.id]))
                self.assertTrue(await asyncio.to_thread(lease_started.wait, 1))
                lease_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await lease_task
                lease_release.set()
                for _ in range(100):
                    if not registry._leases:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(registry._leases, {})
            self.assertEqual(registry._pins, {})

            open_started = threading.Event()
            open_release = threading.Event()
            opened_results: list[tuple[ArtifactEntry, Any, str]] = []
            original_open = registry.open_for_read

            def delayed_open(artifact_id: str) -> tuple[ArtifactEntry, Any, str] | None:
                opened = original_open(artifact_id)
                assert opened is not None
                opened_results.append(opened)
                open_started.set()
                open_release.wait(timeout=5)
                return opened

            with patch.object(registry, "open_for_read", side_effect=delayed_open):
                open_task = asyncio.create_task(registry.open_for_read_async(entry.id))
                self.assertTrue(await asyncio.to_thread(open_started.wait, 1))
                open_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await open_task
                open_release.set()
                for _ in range(100):
                    if opened_results and opened_results[0][1].closed:
                        break
                    await asyncio.sleep(0.01)
            self.assertTrue(opened_results[0][1].closed)
            self.assertEqual(registry._leases, {})
            self.assertEqual(registry._pins, {})
            await registry.close()

    async def test_cancellation_during_upload_close_rolls_back_publication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-close-cancel-") as root:
            registry = ArtifactRegistry(Path(root) / "registry")
            close_started = asyncio.Event()
            close_release = asyncio.Event()

            class BlockingCloseUpload(UploadFile):
                async def close(self) -> None:
                    close_started.set()
                    await close_release.wait()
                    await super().close()

            upload = asyncio.create_task(
                registry.upload(
                    BlockingCloseUpload(
                        file=io.BytesIO(b"unacknowledged"),
                        filename="pending.txt",
                    )
                )
            )
            await close_started.wait()
            self.assertEqual(len(registry._pending_publications), 1)
            upload.cancel()
            close_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await upload
            self.assertEqual(registry._entries, {})
            self.assertEqual(registry._pending_publications, {})
            self.assertEqual(list(registry.root.glob("*.bin")), [])
            await registry.close()

    async def test_late_upload_io_reclaims_windows_locked_temp_capacity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-late-io-") as root:
            registry = ArtifactRegistry(
                Path(root) / "registry",
                max_file_bytes=8,
                max_total_bytes=8,
            )
            write_started = threading.Event()
            write_release = threading.Event()
            original_write = registry._write_upload_chunk
            original_remove = registry._remove_temp_file

            def delayed_write(handle: Any, chunk: bytes) -> int:
                written = original_write(handle, chunk)
                write_started.set()
                write_release.wait(timeout=5)
                return written

            def windows_locked_remove(temp_name: str) -> None:
                if write_release.is_set():
                    original_remove(temp_name)

            with (
                patch("nanocat.api.artifacts._OWNER_CANCEL_TIMEOUT_S", 0.1),
                patch.object(registry, "_write_upload_chunk", side_effect=delayed_write),
                patch.object(registry, "_remove_temp_file", side_effect=windows_locked_remove),
            ):
                pending = asyncio.create_task(
                    registry.upload(
                        UploadFile(file=io.BytesIO(b"12345678"), filename="pending.txt")
                    )
                )
                self.assertTrue(await asyncio.to_thread(write_started.wait, 1))
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, timeout=1)
                self.assertEqual(len(list(registry.root.glob(".*.tmp"))), 1)

                write_release.set()
                for _ in range(100):
                    if not list(registry.root.glob(".*.tmp")):
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(list(registry.root.glob(".*.tmp")), [])

            recovered = await registry.upload(
                UploadFile(file=io.BytesIO(b"abcdefgh"), filename="recovered.txt")
            )
            self.assertEqual(recovered.size, 8)
            await registry.close()

    async def test_corrupt_artifact_manifest_preserves_all_payloads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-corrupt-") as root:
            registry_root = Path(root) / "registry"
            registry = ArtifactRegistry(registry_root)
            entry = await registry.upload(
                UploadFile(file=io.BytesIO(b"preserve"), filename="preserve.txt")
            )
            await registry.close()
            payload_path = registry_root / entry.filename
            registry._manifest.write_text("{", encoding="utf-8")

            degraded = ArtifactRegistry(registry_root)
            self.assertTrue(degraded._degraded)
            self.assertTrue(payload_path.is_file())
            with self.assertRaisesRegex(RuntimeError, "degraded"):
                await degraded.upload(
                    UploadFile(file=io.BytesIO(b"blocked"), filename="blocked.txt")
                )
            await degraded.close()
            self.assertEqual(registry._manifest.read_text(encoding="utf-8"), "{")
            self.assertTrue(payload_path.is_file())

    async def test_invalid_artifact_manifest_item_preserves_referenced_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-item-corrupt-") as root:
            registry_root = Path(root) / "registry"
            registry = ArtifactRegistry(registry_root)
            entry = await registry.upload(
                UploadFile(file=io.BytesIO(b"preserve"), filename="preserve.txt")
            )
            await registry.close()
            manifest = json.loads(registry._manifest.read_text(encoding="utf-8"))
            manifest["items"][0].pop("name")
            registry._manifest.write_text(json.dumps(manifest), encoding="utf-8")

            degraded = ArtifactRegistry(registry_root)
            self.assertTrue(degraded._degraded)
            self.assertTrue((registry_root / entry.filename).is_file())
            await degraded.close()

    async def test_artifact_crash_temps_are_cleaned_and_unrelated_files_remain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-crash-temp-") as root:
            registry_root = Path(root) / "registry"
            registry_root.mkdir()
            artifact_id = uuid4().hex
            upload_temp = registry_root / f".{artifact_id}.orphan.tmp"
            registry_temp = registry_root / ".registry.orphan.tmp"
            unrelated = registry_root / ".keep.tmp"
            upload_temp.write_bytes(b"upload")
            registry_temp.write_bytes(b"manifest")
            unrelated.write_bytes(b"keep")

            registry = ArtifactRegistry(registry_root)
            self.assertFalse(upload_temp.exists())
            self.assertFalse(registry_temp.exists())
            self.assertTrue(unrelated.exists())
            await registry.close()

    async def test_failed_temp_unlink_remains_in_physical_quota(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-temp-quota-") as root:
            registry = ArtifactRegistry(
                Path(root) / "registry",
                max_file_bytes=4,
                max_total_bytes=4,
            )
            temp_path = registry.root / f".{uuid4().hex}.busy.tmp"
            temp_path.write_bytes(b"12345")
            self.assertEqual(registry._physical_owned_bytes_locked(), 5)
            with self.assertRaisesRegex(RuntimeError, "physical quota"):
                await registry.upload(
                    UploadFile(file=io.BytesIO(b"x"), filename="blocked.txt")
                )
            temp_path.unlink(missing_ok=True)
            await registry.close()

    async def test_shutdown_completion_survives_repeated_caller_cancellation(self) -> None:
        owners = ComponentOwnerRegistry(close_timeout=1.0)
        started = asyncio.Event()
        release = asyncio.Event()

        async def close() -> None:
            started.set()
            await release.wait()

        owners.register("fixture", object(), closer=close)
        coordinator = ShutdownCoordinator(owners)
        reason = ShutdownReason(kind="test", detail="repeated cancellation")
        caller = asyncio.create_task(coordinator.shutdown(reason))
        await started.wait()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        report = await coordinator.shutdown(reason)
        self.assertEqual(report.reason, reason)
        self.assertEqual(report.failed, ())

    async def test_critical_agent_close_survives_registry_timeout(self) -> None:
        release = asyncio.Event()
        dependency_closed = asyncio.Event()

        class Engine:
            stopped = False
            cancelled = 0

            def stop(self) -> None:
                self.stopped = True

            async def close_mcp(self) -> None:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise

        engine = Engine()
        service = AgentService(engine)
        owners = ComponentOwnerRegistry(close_timeout=0.05)
        owners.register(
            "dependency",
            object(),
            closer=lambda: dependency_closed.set(),
        )
        owners.register("agent", service, closer=service.close)

        report = await owners.close_all()
        self.assertIn("agent", report.timed_out)
        self.assertFalse(dependency_closed.is_set())
        self.assertEqual(engine.cancelled, 0)
        release.set()
        owner = service._close_task
        self.assertIsNotNone(owner)
        assert owner is not None
        await asyncio.wait_for(asyncio.shield(owner), timeout=1)
        for _ in range(100):
            if dependency_closed.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(dependency_closed.is_set())
        self.assertTrue(engine.stopped)
        self.assertEqual(engine.cancelled, 0)
        await service.close()

    async def test_agent_close_propagates_owned_cancellation_without_spinning(self) -> None:
        class Engine:
            def stop(self) -> None:
                return None

            async def close_mcp(self) -> None:
                raise asyncio.CancelledError

        service = AgentService(Engine())
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(service.close(), timeout=0.2)
        owner = service._close_task
        self.assertIsNotNone(owner)
        assert owner is not None
        self.assertTrue(owner.cancelled())

    async def test_critical_close_continuation_handles_owned_cancellation(self) -> None:
        release = asyncio.Event()
        dependency_closed = asyncio.Event()

        async def close_critical() -> None:
            await release.wait()
            raise asyncio.CancelledError

        owners = ComponentOwnerRegistry(close_timeout=0.05)
        owners.register(
            "dependency",
            object(),
            closer=lambda: dependency_closed.set(),
        )
        owners.register("critical", object(), closer=close_critical)

        report = await owners.close_all()
        self.assertIn("critical", report.timed_out)
        self.assertFalse(dependency_closed.is_set())
        release.set()
        for _ in range(100):
            if dependency_closed.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(dependency_closed.is_set())
        continuations = tuple(owners._continuations)
        if continuations:
            await asyncio.gather(*continuations, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertFalse(owners._continuations)

    async def test_restart_is_rejected_when_session_stop_cannot_persist(self) -> None:
        class CommandService:
            async def dispatch_intervention(self, _msg: InboundMessage) -> None:
                return None

        class Engine:
            command_service = CommandService()
            command_router = CommandRouter.legacy_compatibility()
            restart_calls = 0

            async def _handle_stop(self, msg: InboundMessage) -> OutboundMessage:
                return OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content="terminal persistence failed",
                    metadata={"persistence_failed": True},
                )

            async def _handle_restart(self, msg: InboundMessage) -> OutboundMessage:
                self.restart_calls += 1
                return OutboundMessage(msg.channel, msg.chat_id, "restarting")

        engine = Engine()
        bus = MessageBus(maxsize=2)
        dispatcher = CommandDispatcher(engine, bus)
        response = await dispatcher.execute(
            InboundMessage("web", "user", "chat", "/restart"),
            publish=False,
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.content, "terminal persistence failed")
        self.assertEqual(engine.restart_calls, 0)

        class Supervisor:
            requested = False

            async def request_restart(self, _channel: str, _chat_id: str) -> None:
                self.requested = True

        supervisor = Supervisor()
        control = ApplicationControlService(
            engine=engine,
            config=None,
            session_manager=None,
            intervention=None,
            supervisor=supervisor,
        )
        engine.command_dispatcher = dispatcher
        cancelled = await control.execute(
            "turn.cancel",
            {"channel": "web", "chat_id": "chat", "session_key": "web:chat"},
        )
        self.assertFalse(cancelled["ok"])
        self.assertEqual(cancelled["code"], "persistence_failed")
        response = _control_response(cancelled)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "1")
        result = await control.execute(
            "runtime.restart",
            {"channel": "web", "chat_id": "chat", "session_key": "web:chat"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "persistence_failed")
        self.assertFalse(supervisor.requested)
        await bus.close()

    async def test_session_switch_guard_is_reentrant_and_serializes_admission(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-routing-guard-") as root:
            sessions = SessionManager(Path(root) / "sessions")
            first = sessions.get_or_create("web", "local")
            second = sessions._new_session("web", "local")
            sessions.set_active("web", "local", first.id)

            class Engine:
                def __init__(self) -> None:
                    self._exclusive_sessions: set[str] = set()

                def try_reserve_session_operation(self, session_key: str) -> bool:
                    if session_key in self._exclusive_sessions:
                        return False
                    self._exclusive_sessions.add(session_key)
                    return True

                def release_session_operation(self, session_key: str) -> None:
                    self._exclusive_sessions.discard(session_key)

            engine = Engine()
            control = ApplicationControlService(
                engine=engine,
                config=None,
                session_manager=sessions,
                intervention=None,
                supervisor=None,
            )
            params = {
                "channel": "web",
                "chat_id": "local",
                "session_id": second.id,
            }

            async with control.routing_guard("web:local"):
                nested = await asyncio.wait_for(
                    control.execute("session.switch", params),
                    timeout=1,
                )
            self.assertTrue(nested["ok"])
            sessions.set_active("web", "local", first.id)

            admission_entered = asyncio.Event()
            admission_release = asyncio.Event()

            async def hold_admission() -> None:
                async with control.routing_guard("web:local"):
                    admission_entered.set()
                    await admission_release.wait()
                    self.assertEqual(
                        sessions.get_or_create("web", "local").id,
                        first.id,
                    )

            admission = asyncio.create_task(hold_admission())
            await admission_entered.wait()
            activation = asyncio.create_task(control.execute("session.switch", params))
            await asyncio.sleep(0.05)
            self.assertFalse(activation.done())
            self.assertEqual(sessions.get_or_create("web", "local").id, first.id)

            admission_release.set()
            await admission
            switched = await asyncio.wait_for(activation, timeout=1)
            self.assertTrue(switched["ok"])
            self.assertEqual(sessions.get_or_create("web", "local").id, second.id)
            self.assertEqual(control._routing_locks, {})
            self.assertEqual(control._routing_owners, {})
            await sessions.close()

    async def test_session_activation_cannot_overtake_turn_admission(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-routing-admission-") as root:
            sessions = SessionManager(Path(root) / "sessions")
            first = sessions.get_or_create("web", "local")
            second = sessions._new_session("web", "local")
            sessions.set_active("web", "local", first.id)
            turns = TurnCoordinator()

            class Engine:
                def __init__(self) -> None:
                    self.turns = turns
                    self._exclusive_sessions: set[str] = set()

                def has_pending_session_durability(self, _session_key: str) -> bool:
                    return False

                def try_reserve_session_operation(self, session_key: str) -> bool:
                    if self.turns.active_for_session(session_key) is not None:
                        return False
                    if session_key in self._exclusive_sessions:
                        return False
                    self._exclusive_sessions.add(session_key)
                    return True

                def release_session_operation(self, session_key: str) -> None:
                    self._exclusive_sessions.discard(session_key)

                @staticmethod
                def track_web_turn_attachments(
                    _turn_id: str,
                    _attachment_count: int,
                    _attachment_bytes: int,
                ) -> bool:
                    return True

            engine = Engine()
            control = ApplicationControlService(
                engine=engine,
                config=None,
                session_manager=sessions,
                intervention=None,
                supervisor=None,
            )
            bus = MessageBus(maxsize=2)
            channel = WebChannel(Config().channels.web, bus)
            channel.bind_control(control)
            lease_started = asyncio.Event()
            lease_release = asyncio.Event()

            async def admit_first() -> dict[str, str]:
                async with control.routing_guard("web:local"):
                    sessions.set_active("web", "local", first.id)
                    lease_started.set()
                    await lease_release.wait()
                    return await channel.submit(
                        "bound to first",
                        session_id=first.id,
                    )

            admission = asyncio.create_task(admit_first())
            await lease_started.wait()
            activation = asyncio.create_task(
                control.execute(
                    "session.switch",
                    {
                        "channel": "web",
                        "chat_id": "local",
                        "session_id": second.id,
                    },
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(activation.done())

            lease_release.set()
            accepted = await asyncio.wait_for(admission, timeout=1)
            switched = await asyncio.wait_for(activation, timeout=1)
            self.assertFalse(switched["ok"])
            self.assertEqual(switched["code"], "invalid_state")
            self.assertEqual(sessions.get_or_create("web", "local").id, first.id)
            self.assertEqual(turns.get(accepted["turnId"]).conversation_id, first.id)
            inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
            self.assertEqual(inbound.metadata["_web_session_id"], first.id)

            turns.fail(accepted["turnId"], "fixture cleanup")
            await bus.close()
            await sessions.close()

    async def test_compact_status_reads_inactive_session_without_activating_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-compact-read-route-") as root:
            sessions = SessionManager(Path(root) / "sessions")
            first = sessions.get_or_create("web", "local")
            second = sessions._new_session("web", "local")
            sessions.set_active("web", "local", first.id)
            inspected: list[str] = []

            class Compactor:
                @staticmethod
                def status(session: Any) -> dict[str, Any]:
                    inspected.append(session.id)
                    return {"checkpoint": None, "estimated_prompt_tokens": 0}

            control = ApplicationControlService(
                engine=SimpleNamespace(memory_compactor=Compactor()),
                config=None,
                session_manager=sessions,
                intervention=None,
                supervisor=None,
            )
            result = await control.execute(
                "compact.status",
                {
                    "channel": "web",
                    "chat_id": "local",
                    "session_id": second.id,
                },
            )
            self.assertTrue(result["ok"])
            self.assertEqual(inspected, [second.id])
            self.assertEqual(sessions.get_or_create("web", "local").id, first.id)
            await sessions.close()

    async def test_restart_exec_is_blocked_by_critical_shutdown_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-restart-gate-") as root:
            supervisor = object.__new__(RuntimeSupervisor)
            supervisor.runtime = SimpleNamespace(
                paths=SimpleNamespace(restart_notification=Path(root) / "restart.json")
            )
            supervisor.owners = ComponentOwnerRegistry(close_timeout=1.0)
            supervisor.owners.register("agent", object(), closer=lambda: None)
            supervisor._stop_requested = False
            supervisor._restart_task = None

            async def failed_stop(
                _self: Any,
                reason: ShutdownReason | None = None,
            ) -> ShutdownReport:
                return ShutdownReport(
                    reason=reason or ShutdownReason(kind="test"),
                    errors=("agent: persistence failed",),
                    failed=("agent",),
                )

            supervisor.stop = MethodType(failed_stop, supervisor)
            with (
                patch("nanocat.runtime.supervisor.asyncio.sleep", return_value=None),
                patch("nanocat.runtime.supervisor.os.execv") as execv,
            ):
                await supervisor.request_restart("web", "chat")
                assert supervisor._restart_task is not None
                await supervisor._restart_task
            execv.assert_not_called()
            self.assertFalse(supervisor.runtime.paths.restart_notification.exists())

    async def test_exact_cancel_survives_caller_cancellation(self) -> None:
        engine = object.__new__(AgentLoop)
        engine.bus = MessageBus(maxsize=2)
        engine.turns = TurnCoordinator()
        engine.intervention = None
        engine._turn_cancel_operations = {}
        engine._turn_tasks = {}
        engine._task_pending_turns = {}
        engine._handoff_messages = {}
        engine._steer_buf = {}
        engine._pending_buf = {}
        engine._post_stop_buf = {}
        engine._web_steer_reservations = {}
        engine._turn_admission_slots = asyncio.BoundedSemaphore(2)
        engine._persistence_error_count = 0
        turn_id = "turn-cancel-owned"
        session_key = "web:chat"
        message = InboundMessage(
            "web",
            "user",
            "chat",
            "preserve me",
            metadata={"turn_id": turn_id},
        )
        engine.turns.register(turn_id, session_key, "user")
        await engine._turn_admission_slots.acquire()
        engine._handoff_messages[1] = message
        started = asyncio.Event()
        release = asyncio.Event()
        persisted: list[str] = []

        async def persist(_self: Any, pending: Any, **_kwargs: Any) -> bool:
            started.set()
            await release.wait()
            persisted.append(pending.message.content)
            return True

        engine._persist_pending_interruption_ordered = MethodType(persist, engine)
        caller = asyncio.create_task(engine.cancel_turn(turn_id))
        await started.wait()
        owned = engine._turn_cancel_operations[turn_id]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(owned.done())
        engine._persistence_error_count += 1
        release.set()
        self.assertTrue(await owned)
        self.assertEqual(persisted, ["preserve me"])
        self.assertEqual(engine._handoff_messages, {})
        self.assertEqual(engine._turn_admission_slots._value, 2)
        self.assertEqual(engine.turns.get(turn_id).state, TurnState.CANCELLED)
        await engine.bus.close()

    async def test_web_plain_turn_is_rejected_while_session_is_active(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-web-active-turn-") as root:
            sessions = SessionManager(Path(root) / "sessions")
            session = sessions.get_or_create("web", "local")
            turns = TurnCoordinator()
            turns.start(
                "active-turn",
                "web:local",
                "web:local",
                conversation_id=session.id,
            )
            engine = SimpleNamespace(
                turns=turns,
                has_pending_session_durability=lambda _key: False,
            )
            bus = MessageBus(maxsize=2)
            channel = WebChannel(Config().channels.web, bus)
            channel.bind_control(SimpleNamespace(_engine=engine, _sessions=sessions))

            with self.assertRaises(WebIngressRejectedError) as rejected:
                await channel.submit(
                    "second ordinary turn",
                    session_id=session.id,
                    artifact_refs=[{"id": uuid4().hex, "size": 8}],
                )
            self.assertEqual(rejected.exception.reason, "busy")
            self.assertEqual(bus.pending_inbound("web:local"), 0)
            await bus.close()
            await sessions.close()

    async def test_web_slash_input_is_rejected_before_turn_or_steer_reservation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-web-command-input-") as root:
            sessions = SessionManager(Path(root) / "sessions")
            session = sessions.get_or_create("web", "local")
            turns = TurnCoordinator()
            reserve_calls = 0

            def reserve_web_steer(*_args: Any, **_kwargs: Any) -> bool:
                nonlocal reserve_calls
                reserve_calls += 1
                return True

            engine = SimpleNamespace(
                turns=turns,
                has_pending_session_durability=lambda _key: False,
                reserve_web_steer=reserve_web_steer,
            )
            bus = MessageBus(maxsize=2)
            channel = WebChannel(Config().channels.web, bus)
            channel.bind_control(SimpleNamespace(_engine=engine, _sessions=sessions))

            for content, steer in ((" /help", False), ("/unknown", False)):
                with self.assertRaises(WebIngressRejectedError) as rejected:
                    await channel.submit(content, session_id=session.id, steer=steer)
                self.assertEqual(rejected.exception.reason, "command_input")
            self.assertEqual(turns.snapshot(), ())

            turns.start(
                "active-turn",
                "web:local",
                "web:local",
                conversation_id=session.id,
            )
            with self.assertRaises(WebIngressRejectedError) as rejected:
                await channel.submit("/stop", session_id=session.id, steer=True)
            self.assertEqual(rejected.exception.reason, "command_input")
            self.assertEqual(reserve_calls, 0)
            self.assertEqual(turns._join_counts, {})
            self.assertEqual(bus.pending_inbound("web:local"), 0)
            self.assertEqual(bus.pending_commands("web:local"), 0)
            await bus.close()
            await sessions.close()

    async def test_scp_timeout_kills_the_owned_process_tree(self) -> None:
        released = asyncio.Event()
        stderr = asyncio.StreamReader()
        stderr.feed_eof()

        class Process:
            pid = 4242
            returncode: int | None = None

            async def wait(self) -> int:
                await released.wait()
                return int(self.returncode or 0)

        process = Process()
        spawn_kwargs: dict[str, Any] = {}
        loop = asyncio.get_running_loop()

        async def spawn(*_args: Any, **kwargs: Any) -> Any:
            spawn_kwargs.update(kwargs)
            process.stderr = stderr
            return process

        def kill_tree(target: Any) -> None:
            self.assertIs(target, process)
            process.returncode = -9
            loop.call_soon_threadsafe(released.set)

        manager = SSHManager(Path.cwd())
        with (
            patch("nanocat.agent.tools.ssh.asyncio.create_subprocess_exec", new=spawn),
            patch("nanocat.agent.tools.ssh.ExecTool._kill_tree", side_effect=kill_tree) as kill,
        ):
            with self.assertRaises(TimeoutError):
                await manager._run_scp(["scp", "source", "host:target"], 0.001)
        kill.assert_called_once_with(process)
        self.assertIn("start_new_session", spawn_kwargs)

    async def test_ssh_identity_path_obeys_workspace_restriction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-ssh-policy-") as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()
            config = Config.model_validate(
                {"tools": {"policy": {"restrictPathToWorkspace": True}}}
            )
            policy = SecurityPolicy(config, workspace)
            outside_identity = str(Path(root) / "outside-key")
            self.assertEqual(
                SSHManager(workspace)._resolve_identity("key"),
                str((workspace / "key").resolve()),
            )
            rejected_host = json.loads(await SSHManager(workspace).open("-F"))
            self.assertFalse(rejected_host["ok"])
            self.assertIn("unsupported", rejected_host["error"])

            for tool_name, params in (
                (
                    "ssh_upload",
                    {
                        "local_path": str(workspace / "payload.bin"),
                        "host": "example",
                        "remote_path": "/tmp/payload.bin",
                        "identity": outside_identity,
                    },
                ),
                ("ssh_open", {"host": "example", "identity": outside_identity}),
            ):
                decision = policy.evaluate(tool_name, params)
                self.assertEqual(decision.kind, SecurityDecisionKind.HARD_DENY)
                self.assertEqual(decision.capability, "ssh.identity")

    async def test_exact_cancel_preserves_ingress_order_across_all_buffers(self) -> None:
        engine = object.__new__(AgentLoop)
        engine.bus = MessageBus(maxsize=4)
        engine.turns = TurnCoordinator()
        engine.intervention = None
        engine._turn_cancel_operations = {}
        engine._turn_tasks = {}
        engine._task_pending_turns = {}
        engine._handoff_messages = {}
        engine._steer_buf = {}
        engine._pending_buf = {}
        engine._post_stop_buf = {}
        engine._stopped_turns = {}
        engine._web_steer_reservations = {}
        engine._web_turn_attachment_usage = {}
        engine._turn_admission_slots = asyncio.BoundedSemaphore(4)
        engine._persistence_error_count = 0
        engine._ingress_ordinals = iter(range(100, 200))
        turn_id = "turn-cancel-order"
        session_key = "web:chat"

        def message(content: str, ordinal: int) -> InboundMessage:
            return InboundMessage(
                "web",
                "user",
                "chat",
                content,
                metadata={"turn_id": turn_id},
                ingress_ordinal=ordinal,
            )

        early_steer = message("first steer", 1)
        middle_handoff = message("second handoff", 2)
        late_bus = message("third bus", 3)
        await engine._turn_admission_slots.acquire()
        engine._hold_buffered_admission(early_steer)
        engine._steer_buf[session_key] = [early_steer]
        await engine._turn_admission_slots.acquire()
        engine._handoff_messages[1] = middle_handoff
        await engine.bus.publish_inbound(late_bus)
        engine.turns.register(turn_id, session_key, "user")
        persisted: list[str] = []

        async def persist(
            _self: Any,
            pending: _PendingTurn,
            **_kwargs: Any,
        ) -> bool:
            persisted.append(pending.message.content)
            pending.history_committed = True
            return True

        engine._persist_pending_interruption_ordered = MethodType(persist, engine)
        self.assertTrue(await engine.cancel_turn(turn_id))
        self.assertEqual(
            persisted,
            ["first steer", "second handoff", "third bus"],
        )
        self.assertEqual(engine.turns.get(turn_id).state, TurnState.CANCELLED)
        self.assertEqual(engine._turn_admission_slots._value, 4)
        await engine.bus.close()

    async def test_exact_cancel_persists_dispatch_cancelled_before_first_step(self) -> None:
        engine = object.__new__(AgentLoop)
        engine.bus = MessageBus(maxsize=2)
        engine.turns = TurnCoordinator()
        engine.intervention = None
        engine._turn_cancel_operations = {}
        engine._turn_tasks = {}
        engine._task_pending_turns = {}
        engine._handoff_messages = {}
        engine._steer_buf = {}
        engine._pending_buf = {}
        engine._post_stop_buf = {}
        engine._web_steer_reservations = {}
        engine._web_turn_attachment_usage = {}
        engine._turn_admission_slots = asyncio.BoundedSemaphore(2)
        engine._persistence_error_count = 0
        engine._ingress_ordinals = iter(range(1, 100))
        engine._superseded_tasks = set()
        engine._stop_requested = set()
        engine._stopped_turns = {}
        turn_id = "turn-prestart-cancel"
        session_key = "web:chat"
        message = InboundMessage(
            "web",
            "user",
            "chat",
            "persist before first task step",
            metadata={"turn_id": turn_id},
        )
        engine.turns.register(turn_id, session_key, "user")
        persisted: list[str] = []

        async def persist(_self: Any, pending: Any, **_kwargs: Any) -> bool:
            persisted.append(pending.message.content)
            pending.history_committed = True
            return True

        engine._persist_pending_interruption_ordered = MethodType(persist, engine)
        pending_turn = engine._new_pending_turn(message)
        task = asyncio.create_task(engine._dispatch(message, pending_turn=pending_turn))
        engine._track_turn_task(message, task, pending_turn)
        task.cancel()

        self.assertTrue(await engine.cancel_turn(turn_id))
        self.assertEqual(persisted, ["persist before first task step"])
        self.assertTrue(pending_turn.history_committed)
        self.assertEqual(engine.turns.get(turn_id).state, TurnState.CANCELLED)
        self.assertEqual(engine._task_pending_turns, {})
        await engine.bus.close()

    async def test_request_body_timeout_is_absolute(self) -> None:
        async def app(_scope: Any, receive: Any, _send: Any) -> None:
            while True:
                message = await receive()
                if not message.get("more_body"):
                    return

        async def receive() -> dict[str, Any]:
            await asyncio.sleep(0.03)
            return {"type": "http.request", "body": b"x", "more_body": True}

        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        middleware = _RequestBodyLimitMiddleware(
            app,
            default_limit=1024,
            read_timeout_s=0.05,
        )
        await middleware(
            {"type": "http", "method": "POST", "path": "/slow", "headers": []},
            receive,
            send,
        )
        self.assertEqual(sent[0]["status"], 408)

    async def test_cancelled_blocked_publish_does_not_leave_pending_count(self) -> None:
        bus = MessageBus(maxsize=1)
        first = InboundMessage("web", "user", "chat", "first")
        second = InboundMessage("web", "user", "chat", "second")
        await bus.publish_inbound(first)
        blocked = asyncio.create_task(bus.publish_inbound(second))
        await asyncio.sleep(0)
        blocked.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked
        self.assertEqual(bus.pending_inbound(first.session_key), 1)
        self.assertEqual((await bus.consume_inbound()).content, "first")
        self.assertEqual(bus.pending_inbound(first.session_key), 0)
        await bus.close()

    async def test_artifact_leases_block_eviction_until_stream_closes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-artifact-lease-") as root:
            root_path = Path(root)
            source_a = root_path / "a.txt"
            source_b = root_path / "b.txt"
            source_a.write_text("a", encoding="utf-8")
            source_b.write_text("b", encoding="utf-8")
            registry = ArtifactRegistry(root_path / "registry", max_entries=1)
            first = registry.register_path(source_a)
            paths, turn_lease = registry.lease_paths([first.id])
            self.assertEqual(paths, [str(source_a.resolve())])
            with self.assertRaisesRegex(RuntimeError, "capacity is leased"):
                registry.register_path(source_b)
            registry.release_lease(turn_lease)

            opened = registry.open_for_read(first.id)
            self.assertIsNotNone(opened)
            assert opened is not None
            _, handle, stream_lease = opened
            with self.assertRaisesRegex(RuntimeError, "capacity is leased"):
                registry.register_path(source_b)
            self.assertEqual(
                b"".join(
                    read_range(
                        handle,
                        0,
                        0,
                        on_close=lambda: registry.release_lease(stream_lease),
                    )
                ),
                b"a",
            )
            second = registry.register_path(source_b)
            self.assertIsNotNone(registry.resolve(second.id))
            await registry.close()

    async def test_turn_terminal_finalizer_runs_once_including_late_binding(self) -> None:
        turns = TurnCoordinator()
        turns.register("turn-a", "web:chat", "user")
        calls: list[str] = []
        turns.add_terminal_finalizer("turn-a", lambda: calls.append("early"))
        turns.begin_cancel("turn-a")
        self.assertEqual(calls, [])
        turns.cancel("turn-a")
        self.assertEqual(calls, [])
        turns.finalize_cancel("turn-a")
        turns.fail("turn-a", "ignored")
        turns.add_terminal_finalizer("turn-a", lambda: calls.append("late"))
        self.assertEqual(calls, ["early", "late"])

    async def test_sse_subscription_starts_immediately(self) -> None:
        broker = SseBroker()
        stream = await broker.open_subscription(session_id="session-a")
        try:
            self.assertEqual(await anext(stream), b": connected\n\n")
        finally:
            await stream.aclose()
            await broker.close()

    async def test_sse_subscription_close_before_iteration_releases_capacity(self) -> None:
        broker = SseBroker(max_subscribers=1)
        first = await broker.open_subscription()
        await first.aclose()
        second = await broker.open_subscription()
        self.assertEqual(await anext(second), b": connected\n\n")
        await second.aclose()
        self.assertEqual(len(broker._subscribers), 0)
        await broker.close()

    async def test_sse_close_survives_caller_cancellation(self) -> None:
        unsubscribe_started = asyncio.Event()
        release = asyncio.Event()
        stream_closed = asyncio.Event()
        unsubscribe_calls = 0

        async def source():
            try:
                yield b"ready"
            finally:
                stream_closed.set()

        async def unsubscribe() -> None:
            nonlocal unsubscribe_calls
            unsubscribe_calls += 1
            unsubscribe_started.set()
            await release.wait()

        subscription = SseSubscription(source(), unsubscribe)
        self.assertEqual(await anext(subscription), b"ready")
        closer = asyncio.create_task(subscription.aclose())
        await unsubscribe_started.wait()
        closer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closer
        release.set()
        await subscription.aclose()
        self.assertEqual(unsubscribe_calls, 1)
        self.assertTrue(stream_closed.is_set())

    async def test_sse_compatibility_generator_closes_on_early_exit(self) -> None:
        broker = SseBroker(max_subscribers=1)
        stream = broker.subscribe()
        self.assertEqual(await anext(stream), b": connected\n\n")
        await stream.aclose()
        self.assertEqual(len(broker._subscribers), 0)
        await broker.close()

    async def test_sse_oversize_payload_drops_raw_preview(self) -> None:
        broker = SseBroker(max_event_bytes=1024)
        stream = await broker.open_subscription()
        try:
            self.assertEqual(await anext(stream), b": connected\n\n")
            secret = "sensitive-value-" * 300
            await broker.publish("tool.event", {"output": secret})
            chunk = await anext(stream)
            self.assertNotIn(secret.encode(), chunk)
            self.assertIn(b'"truncated":true', chunk)
        finally:
            await stream.aclose()
            await broker.close()

    async def test_login_failure_backoff_and_source_bound(self) -> None:
        limiter = LoginRateLimiter(max_sources=3)
        delays = [await limiter.failure("same-source") for _ in range(5)]
        self.assertEqual(delays, [0.5, 0.5, 0.5, 1.0, 2.0])
        self.assertGreaterEqual(await limiter.before_attempt("same-source"), 1.0)
        for index in range(8):
            await limiter.failure(f"source-{index}")
        self.assertLessEqual(len(limiter._states), 3)

    async def test_protected_web_login_csrf_backoff_and_body_limit(self) -> None:
        auth = WebSessionAuth("correct-password")
        app = create_web_app(
            core_api_url="http://127.0.0.1:1",
            api_token="internal-token",
            auth=auth,
            static_dir=None,
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"Origin": "http://testserver"}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/auth/login",
                headers=headers,
                json={"password": "correct-password"},
            )
            self.assertEqual(login.status_code, 200)
            status = await client.get("/auth/status")
            self.assertEqual(status.json(), {"protected": True, "authenticated": True})
            csrf = client.cookies.get(WEB_CSRF_COOKIE)
            self.assertTrue(csrf)
            logout = await client.post(
                "/auth/logout",
                headers={**headers, "X-CSRF-Token": str(csrf)},
            )
            self.assertEqual(logout.status_code, 200)
            self.assertFalse((await client.get("/auth/status")).json()["authenticated"])
            oversized = await client.post(
                "/auth/login",
                headers=headers,
                content=b"x" * 8193,
            )
            self.assertEqual(oversized.status_code, 413)

        limited_auth = WebSessionAuth("correct-password")
        limited_app = create_web_app(
            core_api_url="http://127.0.0.1:1",
            api_token="internal-token",
            auth=limited_auth,
            static_dir=None,
        )
        limited_transport = httpx.ASGITransport(app=limited_app)
        async with httpx.AsyncClient(
            transport=limited_transport,
            base_url="http://testserver",
        ) as client:
            responses = [
                await client.post(
                    "/auth/login",
                    headers=headers,
                    json={"password": "wrong-password"},
                )
                for _ in range(4)
            ]
            self.assertEqual([item.status_code for item in responses], [401, 401, 401, 429])
            self.assertEqual(responses[-1].headers.get("Retry-After"), "1")
            locked = await client.post(
                "/auth/login",
                headers=headers,
                json={"password": "correct-password"},
            )
            self.assertEqual(locked.status_code, 429)
            self.assertEqual(locked.headers.get("Retry-After"), "1")

    async def test_runtime_configuration_mutations_do_not_lose_updates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-config-race-") as root:
            path = Path(root) / "config.json"
            initial = Config()
            path.write_text(
                json.dumps(initial.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            service = ConfigurationService(path, effective_config=initial)

            async def add(model: str) -> None:
                def mutate(current: Config) -> Mapping[str, Any]:
                    choices = list(current.agents.defaults.model_choice)
                    if model not in choices:
                        choices.append(model)
                    return {"agents.defaults.modelChoice": choices}

                await service.mutate_runtime(mutate)

            models = [f"custom/concurrent-{index}" for index in range(24)]
            await asyncio.gather(*(add(model) for model in models))
            snapshot = await service.snapshot()
            persisted = snapshot["config"].agents.defaults.model_choice
            self.assertTrue(set(models).issubset(persisted))
            self.assertEqual(initial.agents.defaults.model_choice, persisted)

    async def test_channel_descriptor_defaults_are_visible_and_editable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-channel-settings-") as root:
            path = Path(root) / "config.json"
            initial = Config()
            path.write_text(
                json.dumps(initial.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            service = ConfigurationService(
                path,
                effective_config=initial,
                channel_defaults={
                    "telegram": {"enabled": False, "token": "", "allowFrom": []},
                    "qq": {"enabled": False, "appId": "", "secret": ""},
                },
            )

            snapshot = await service.snapshot()
            channels = snapshot["config"].model_dump(by_alias=True, mode="json")["channels"]
            self.assertIn("telegram", channels)
            self.assertIn("qq", channels)

            updated = await service.update(
                {"channels.telegram.enabled": True},
                expected_revision=snapshot["revision"],
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))["channels"]
            self.assertTrue(persisted["telegram"]["enabled"])
            self.assertIn("qq", persisted)
            self.assertIn("channels.telegram.enabled", updated["reconnectedPaths"])

    async def test_runtime_configuration_cancel_finishes_live_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-config-cancel-") as root:
            path = Path(root) / "config.json"
            initial = Config()
            path.write_text(
                json.dumps(initial.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            service = ConfigurationService(path, effective_config=initial)
            started = threading.Event()
            release = threading.Event()
            original = service._prepare_runtime_update_sync

            def delayed(values: Mapping[str, Any]) -> Any:
                started.set()
                release.wait(timeout=5)
                return original(values)

            service._prepare_runtime_update_sync = delayed  # type: ignore[method-assign]
            task = asyncio.create_task(
                service.update_runtime({"agents.defaults.reasoningEffort": "high"})
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

            snapshot = await service.snapshot()
            self.assertEqual(snapshot["config"].agents.defaults.reasoning_effort, "high")
            self.assertEqual(initial.agents.defaults.reasoning_effort, "high")

    async def test_settings_owner_failure_rolls_back_file_and_effective_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-config-rollback-") as root:
            path = Path(root) / "config.json"
            initial = Config()
            path.write_text(
                json.dumps(initial.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            service = ConfigurationService(path, effective_config=initial)

            async def fail_owner(_config: Config, _paths: tuple[str, ...]) -> None:
                raise RuntimeError("owner rejected update")

            service.set_runtime_applier(fail_owner)
            revision = (await service.snapshot())["revision"]
            with self.assertRaises(ConfigurationError):
                await service.update(
                    {"providers.openai.apiKey": "temporary-key"},
                    expected_revision=revision,
                )
            snapshot = await service.snapshot()
            self.assertEqual(snapshot["revision"], revision)
            self.assertEqual(snapshot["config"].providers.openai.api_key, "")
            self.assertEqual(initial.providers.openai.api_key, "")

    async def test_settings_apply_modes_match_runtime_owner_boundaries(self) -> None:
        self.assertEqual(
            restart_setting_paths(),
            [
                "api.enabled",
                "api.host",
                "api.port",
                "channels.web.enabled",
                "channels.web.host",
                "channels.web.port",
            ],
        )
        self.assertEqual(setting_apply_mode("api.authToken"), "live")
        self.assertEqual(setting_apply_mode("api.corsOrigins"), "live")
        self.assertEqual(setting_apply_mode("channels.web.password"), "live")
        self.assertEqual(setting_apply_mode("channels.telegram.token"), "reconnect")
        self.assertEqual(setting_apply_mode("tools.mcpServers"), "reconnect")
        self.assertEqual(setting_apply_mode("memory.apiUrl"), "reconnect")
        self.assertEqual(setting_apply_mode("runtimeFiles.maxFileBytes"), "next_turn")

    async def test_auth_reconfiguration_rotates_token_and_revokes_web_sessions(self) -> None:
        api_auth = ApiAuthenticator("a" * 16)
        api_auth.reconfigure("b" * 16)
        self.assertEqual(api_auth.token, "b" * 16)
        web_auth = WebSessionAuth("old-password")
        web_auth.sessions["session"] = SimpleNamespace(
            csrf_token="csrf",
            created_at=0.0,
            last_seen_at=0.0,
        )
        await web_auth.reconfigure(
            "new-password",
            trusted_proxies=["127.0.0.1"],
        )
        self.assertEqual(web_auth.password, "new-password")
        self.assertEqual(web_auth.trusted_proxies, frozenset({"127.0.0.1"}))
        self.assertFalse(web_auth.sessions)

    async def test_memory_settings_swap_runtime_owners_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-memory-config-") as root:
            workdir = Path(root)
            config = Config.model_validate(
                {
                    "api": {"enabled": False},
                    "channels": {"web": {"enabled": False}},
                    "memory": {"enabled": False},
                    "heartbeat": {"enabled": False},
                }
            )
            (workdir / "config.json").write_text(
                json.dumps(config.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            runtime = build_runtime(workdir=str(workdir))
            try:
                engine = runtime.agent.engine
                self.assertIsNone(engine.nowledge_client)
                snapshot = await runtime.configuration.snapshot()
                updated = await runtime.configuration.update(
                    {
                        "memory.enabled": True,
                        "memory.apiUrl": "http://127.0.0.1:14243",
                        "memory.threadCaptureEnabled": True,
                    },
                    expected_revision=snapshot["revision"],
                )
                self.assertEqual(updated["reconnectedPaths"], [
                    "memory.enabled",
                    "memory.apiUrl",
                    "memory.threadCaptureEnabled",
                ])
                self.assertIsNotNone(engine.nowledge_client)
                self.assertIsNotNone(engine.thread_manager)
                self.assertIs(engine.command_handlers._memory, engine.nowledge_client)
                self.assertIsNotNone(engine.tools.get("memory_search"))
            finally:
                await runtime.supervisor.stop(
                    ShutdownReason(kind="test", detail="memory config test complete")
                )

    async def test_persisted_defaults_ignore_environment_secrets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-config-defaults-") as root:
            secret = "environment-secret-that-must-not-be-written"
            missing_path = Path(root) / "missing-config.json"
            partial_path = Path(root) / "partial-config.json"
            partial_path.write_text('{"channels":{"web":{"port":19400}}}', encoding="utf-8")
            with patch.dict(
                os.environ,
                {"NANOCAT_API__AUTH_TOKEN": secret},
                clear=False,
            ):
                effective = Config.model_validate({})
                missing_service = ConfigurationService(
                    missing_path,
                    effective_config=effective,
                )
                partial_service = ConfigurationService(
                    partial_path,
                    effective_config=effective,
                )
                missing = await missing_service.snapshot()
                partial = await partial_service.snapshot()
            self.assertIsNone(missing["config"].api.auth_token)
            self.assertIsNone(partial["config"].api.auth_token)
            self.assertEqual(partial["config"].channels.web.port, 19400)

    async def test_runtime_env_overrides_never_rewrite_persisted_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-config-env-") as root:
            path = Path(root) / "config.json"
            original = '{"schemaVersion":2,"api":{"enabled":true}}'
            path.write_text(original, encoding="utf-8")
            secret = "environment-secret-that-must-stay-ephemeral"
            with patch.dict(
                os.environ,
                {
                    "NANOCAT_API__AUTH_TOKEN": secret,
                    "NANOCAT_CHANNELS": '{"web":{"port":19401}}',
                },
                clear=True,
            ):
                effective = load_config(path)
                service = ConfigurationService(path, effective_config=effective)
                snapshot = await service.snapshot()

            self.assertEqual(effective.api.auth_token, secret)
            self.assertEqual(effective.channels.web.port, 19401)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            encoded = json.dumps(snapshot["effectiveOverrides"], ensure_ascii=False)
            self.assertNotIn(secret, encoded)
            self.assertTrue(snapshot["effectiveOverrides"]["api.authToken"]["configured"])
            self.assertEqual(
                snapshot["effectiveOverrides"]["channels.web.port"],
                19401,
            )

    async def test_settings_redaction_covers_secret_maps_and_url_userinfo(self) -> None:
        redacted = _redact(
            {
                "providers": {
                    "custom": {
                        "extraHeaders": {"APP-Code": "header-secret"},
                    }
                },
                "tools": {
                    "proxy": "socks5://proxy-user:proxy-secret@proxy.example:1080",
                    "mcpServers": {
                        "example": {
                            "env": {"UNUSUAL_VALUE": "environment-secret"},
                            "headers": {"X-Custom": "mcp-secret"},
                        }
                    },
                },
            }
        )
        encoded = json.dumps(redacted, ensure_ascii=False)
        for secret in (
            "header-secret",
            "proxy-secret",
            "environment-secret",
            "mcp-secret",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(
            redacted["providers"]["custom"]["extraHeaders"]["keys"],
            ["APP-Code"],
        )
        log_line = (
            '{"access_token":"raw-json-secret","headers":{"X-Custom":"header-secret"}} '
            "https://example.test/path?api_key=query-secret"
        )
        safe_log = str(redact_value(log_line))
        for secret in ("raw-json-secret", "header-secret", "query-secret"):
            self.assertNotIn(secret, safe_log)

    async def test_web_turn_identity_reaches_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-turn-test-") as root:
            workdir = Path(root)
            config = Config.model_validate(
                {
                    "api": {"enabled": False},
                    "channels": {"web": {"enabled": True, "host": "127.0.0.1", "port": 0}},
                    "memory": {"enabled": False},
                    "heartbeat": {"enabled": False},
                }
            )
            (workdir / "config.json").write_text(
                json.dumps(config.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            static_dir = workdir / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("fixture", encoding="utf-8")
            with patch(
                "nanocat.runtime.launcher.ensure_web_assets",
                return_value=static_dir,
            ):
                runtime = build_runtime(workdir=str(workdir))
            engine = runtime.agent.engine
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            loop_calls = 0

            async def fake_loop(
                _self: Any,
                initial_messages: list[dict[str, Any]],
                *,
                run_state: Any,
                **_kwargs: Any,
            ) -> tuple[str, list[str], list[dict[str, Any]]]:
                nonlocal loop_calls
                loop_calls += 1
                if loop_calls == 1:
                    first_started.set()
                    await release_first.wait()
                messages = [*initial_messages, {"role": "assistant", "content": "done"}]
                run_state.turn_messages.append(messages[-1])
                return "done", [], messages

            engine._run_agent_loop = MethodType(fake_loop, engine)
            supervisor = runtime.supervisor
            assert supervisor is not None
            stream = None
            try:
                await supervisor.start()
                channel = runtime.channels.get_channel("web")
                self.assertIsInstance(channel, WebChannel)
                assert isinstance(channel, WebChannel)
                session = runtime.session_manager.get_or_create("web", "lifecycle")
                stream = await runtime.api_runtime.broker.open_subscription(session_id=session.id)
                self.assertEqual(await anext(stream), b": connected\n\n")

                accepted = await channel.submit(
                    "hello",
                    chat_id=session.chat_id,
                    session_id=session.id,
                )
                turn_id = accepted["turnId"]
                await first_started.wait()
                steered = await channel.submit(
                    "late guidance",
                    chat_id=session.chat_id,
                    session_id=session.id,
                    steer=True,
                )
                self.assertEqual(steered["turnId"], turn_id)
                self.assertNotEqual(steered["requestId"], accepted["requestId"])
                release_first.set()

                async def terminal_record() -> Any:
                    for _ in range(200):
                        record = engine.turns.get(turn_id)
                        if record is not None and record.state in {
                            TurnState.COMPLETED,
                            TurnState.CANCELLED,
                            TurnState.FAILED,
                        }:
                            return record
                        await asyncio.sleep(0.01)
                    self.fail("turn did not reach a terminal state")

                record = await terminal_record()
                self.assertEqual(record.state, TurnState.COMPLETED)
                self.assertIsNotNone(record.ended_at)
                self.assertGreaterEqual(record.duration_ms, 0)
                self.assertEqual(record.request_id, accepted["requestId"])
                self.assertEqual(record.conversation_id, session.id)
                self.assertEqual(loop_calls, 2)
                stored = runtime.session_manager.get_session("web", session.id)
                self.assertIsNotNone(stored)
                self.assertIn(
                    "late guidance",
                    json.dumps(stored.messages, ensure_ascii=False),
                )
                terminal_message = stored.messages[-1]
                self.assertEqual(terminal_message["turn_id"], turn_id)
                self.assertEqual(terminal_message["turn_status"], "completed")
                self.assertIn("turn_started_at", terminal_message)
                self.assertIn("turn_ended_at", terminal_message)
                self.assertGreaterEqual(terminal_message["turn_duration_ms"], 0)

                final_chunk = b""
                for _ in range(20):
                    chunk = await asyncio.wait_for(anext(stream), timeout=1.0)
                    if b"event: assistant.final" in chunk:
                        final_chunk = chunk
                        break
                self.assertIn(turn_id.encode(), final_chunk)
                self.assertIn(b'"startedAt"', final_chunk)
                self.assertIn(b'"endedAt"', final_chunk)
                self.assertIn(b'"durationMs"', final_chunk)
                event_types = [
                    event.type
                    for event in runtime.api_runtime.broker._history
                    if event.turn_id == turn_id
                ]
                self.assertIn("assistant.progress", event_types)
                self.assertIn("assistant.final", event_types)

                await channel.send(
                    OutboundMessage(
                        channel="web",
                        chat_id=session.chat_id,
                        content="Inspecting dependencies",
                        turn_id=turn_id,
                        metadata={
                            "_web_session_id": session.id,
                            "_progress": True,
                            "_thinking": True,
                            "_thinking_payload": {
                                "reasoningContent": "Inspecting dependencies",
                                "thinkingBlocks": [
                                    {
                                        "thinking": "Check the dependency graph",
                                        "signature": "opaque-provider-proof",
                                    }
                                ],
                            },
                        },
                    )
                )
                thinking_chunk = await asyncio.wait_for(anext(stream), timeout=1.0)
                self.assertIn(b"event: assistant.thinking", thinking_chunk)
                self.assertIn(b"Inspecting dependencies", thinking_chunk)
                self.assertIn(b"Check the dependency graph", thinking_chunk)
                self.assertNotIn(b"opaque-provider-proof", thinking_chunk)
                thinking_events = [
                    event
                    for event in runtime.activity_journal.read_recent(
                        channel._storage_scope(session.id), limit=100
                    ).events
                    if event.type == "assistant.thinking"
                ]
                self.assertEqual(
                    thinking_events[-1].redacted_output["content"],
                    "Inspecting dependencies",
                )

                secret = "tool-secret-value"
                await channel.send(
                    OutboundMessage(
                        channel="web",
                        chat_id=session.chat_id,
                        content=secret,
                        turn_id=turn_id,
                        metadata={
                            "_web_session_id": session.id,
                            "_tool_event": {
                                "phase": "complete",
                                "calls": [
                                    {
                                        "id": "call-1",
                                        "name": "read_file",
                                        "status": "completed",
                                        "args": {"token": secret, "path": "fixture.txt"},
                                        "resultPreview": {
                                            "content": "visible result",
                                            "password": secret,
                                        },
                                        "result": secret,
                                    }
                                ],
                            },
                        },
                    )
                )
                tool_chunk = await asyncio.wait_for(anext(stream), timeout=1.0)
                self.assertIn(b"event: tool.event", tool_chunk)
                self.assertNotIn(secret.encode(), tool_chunk)
                self.assertIn(b'"redactedInput"', tool_chunk)
                self.assertIn(b'"redactedOutput"', tool_chunk)
                self.assertIn(b"visible result", tool_chunk)
                self.assertIn(b"fixture.txt", tool_chunk)
            finally:
                if stream is not None:
                    await stream.aclose()
                await supervisor.stop(ShutdownReason(kind="test", detail="completed"))

    async def test_session_cancel_api_cancels_exact_turn_and_persists_terminal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="nanocat-cancel-api-test-") as root:
            workdir = Path(root)
            config = Config.model_validate(
                {
                    "api": {"enabled": False},
                    "channels": {
                        "web": {"enabled": True, "host": "127.0.0.1", "port": 0}
                    },
                    "memory": {"enabled": False},
                    "heartbeat": {"enabled": False},
                }
            )
            (workdir / "config.json").write_text(
                json.dumps(config.model_dump(by_alias=True, mode="json")),
                encoding="utf-8",
            )
            static_dir = workdir / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("fixture", encoding="utf-8")
            with patch(
                "nanocat.runtime.launcher.ensure_web_assets",
                return_value=static_dir,
            ):
                runtime = build_runtime(workdir=str(workdir))
            engine = runtime.agent.engine
            started: dict[str, asyncio.Event] = {
                "first": asyncio.Event(),
                "second": asyncio.Event(),
            }

            async def blocked_loop(
                _self: Any,
                initial_messages: list[dict[str, Any]],
                *,
                run_state: Any,
                **_kwargs: Any,
            ) -> tuple[str, list[str], list[dict[str, Any]]]:
                content = str(initial_messages[-1].get("content") or "")
                label = "first" if content.endswith("first") else "second"
                gate = started[label]
                gate.set()
                await asyncio.Event().wait()
                return "", [], initial_messages

            engine._run_agent_loop = MethodType(blocked_loop, engine)
            supervisor = runtime.supervisor
            assert supervisor is not None
            try:
                await supervisor.start()
                channel = runtime.channels.get_channel("web")
                self.assertIsInstance(channel, WebChannel)
                assert isinstance(channel, WebChannel)
                first = runtime.session_manager.get_or_create("web", "cancel-first")
                second = runtime.session_manager.get_or_create("web", "cancel-second")
                accepted_first = await channel.submit(
                    "first", chat_id=first.chat_id, session_id=first.id
                )
                accepted_second = await channel.submit(
                    "second", chat_id=second.chat_id, session_id=second.id
                )
                await asyncio.gather(started["first"].wait(), started["second"].wait())

                api_runtime = runtime.api_runtime
                assert api_runtime is not None
                app = create_api_app(
                    control=runtime.control,
                    channel=channel,
                    broker=api_runtime.broker,
                    authenticator=api_runtime.api_auth,
                    config=runtime.config,
                    configuration=runtime.configuration,
                    activity_journal=runtime.activity_journal,
                    artifact_registry=api_runtime.artifacts,
                )
                transport = httpx.ASGITransport(app=app)
                headers = {"Authorization": f"Bearer {api_runtime.api_auth.token}"}
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers=headers,
                ) as client:
                    response = await client.post(
                        f"/api/v1/sessions/{first.id}/cancel"
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    self.assertEqual(response.json()["turnId"], accepted_first["turnId"])
                    self.assertTrue(
                        response.headers["location"].endswith(accepted_first["turnId"])
                    )

                    for _ in range(200):
                        record = engine.turns.get(accepted_first["turnId"])
                        if record is not None and record.state in {
                            TurnState.CANCELLED,
                            TurnState.FAILED,
                        }:
                            break
                        await asyncio.sleep(0.01)
                    else:
                        self.fail("session cancellation did not reach a terminal state")

                    self.assertEqual(record.state, TurnState.CANCELLED)
                    status = await client.get(
                        f"/api/v1/turns/{accepted_first['turnId']}"
                    )
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertEqual(status.json()["status"], "cancelled")
                    self.assertIsNotNone(status.json()["endedAt"])
                    self.assertGreaterEqual(status.json()["durationMs"], 0)
                    other = engine.turns.get(accepted_second["turnId"])
                    self.assertIsNotNone(other)
                    self.assertEqual(other.state, TurnState.RUNNING)
                    stored = runtime.session_manager.get_session("web", first.id)
                    self.assertIsNotNone(stored)
                    terminal = stored.messages[-1]
                    self.assertEqual(terminal["turn_id"], accepted_first["turnId"])
                    self.assertEqual(terminal["turn_status"], "cancelled")
                    self.assertGreaterEqual(terminal["turn_duration_ms"], 0)
                    event_types = [
                        event.type
                        for event in api_runtime.broker._history
                        if event.turn_id == accepted_first["turnId"]
                    ]
                    self.assertIn("turn.cancelling", event_types)
                    self.assertIn("turn.cancelled", event_types)
                    terminal_event = next(
                        event
                        for event in api_runtime.broker._history
                        if event.turn_id == accepted_first["turnId"]
                        and event.type == "turn.cancelled"
                    )
                    self.assertIn("endedAt", terminal_event.payload)
                    self.assertGreaterEqual(terminal_event.payload["durationMs"], 0)

                    idle = await client.post(f"/api/v1/sessions/{first.id}/cancel")
                    self.assertEqual(idle.status_code, 409)
                    self.assertEqual(idle.json()["code"], "no_active_turn")

                self.assertTrue(await engine.cancel_turn(accepted_second["turnId"]))
            finally:
                await supervisor.stop(ShutdownReason(kind="test", detail="completed"))

if __name__ == "__main__":
    unittest.main()
