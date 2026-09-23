"""Regression coverage for session YOLO commands and approval replies."""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nanocat.agent.loop import AgentLoop
from nanocat.application.command_dispatcher import CommandDispatcher
from nanocat.application.intervention import (
    InterventionBroker,
    new_intervention_request,
    parse_intervention_action,
)
from nanocat.bus.events import InboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.core.intervention import (
    InterventionAction,
    InterventionKind,
    InterventionState,
    ResumeMode,
)
from nanocat.core.messages import ConversationRef


class YoloTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = InterventionBroker(AsyncMock(return_value=True))
        self.conversation = ConversationRef("web", "local", "web:local")
        self.addAsyncCleanup(self.broker.close)

    def test_parser(self):
        self.assertEqual(parse_intervention_action("/yolo").action, InterventionAction.TOGGLE_YOLO)
        self.assertEqual(
            parse_intervention_action(" YOLO ").action, InterventionAction.APPROVE_FOREVER
        )
        self.assertIsNotNone(parse_intervention_action("/approve forever").error)
        self.assertIsNotNone(parse_intervention_action("/yolo on").error)
        self.assertIsNone(parse_intervention_action("please yolo"))

    async def test_toggle_is_scoped_and_reversible(self):
        result = await self.broker.resolve(
            "user", self.conversation, InterventionAction.TOGGLE_YOLO
        )
        self.assertEqual(result.state, InterventionState.APPROVED_FOREVER)
        self.assertTrue(self.broker.has_session_grant(self.conversation, "user", "tool:*"))
        self.assertFalse(self.broker.has_session_grant(self.conversation, "other", "tool:*"))
        result = await self.broker.resolve(
            "user", self.conversation, InterventionAction.TOGGLE_YOLO
        )
        self.assertEqual(result.state, InterventionState.REVOKED)
        self.assertFalse(self.broker.has_session_grant(self.conversation, "user", "tool:*"))

    async def test_stale_reply_cannot_enable_yolo(self):
        result = await self.broker.resolve(
            "user", self.conversation, InterventionAction.APPROVE_FOREVER, require_pending=True
        )
        self.assertIsNone(result)
        self.assertFalse(self.broker.has_session_grant(self.conversation, "user", "tool:*"))

    async def test_reply_routes_to_control_and_approves_pending(self):
        request = new_intervention_request(
            kind=InterventionKind.APPROVAL,
            turn_id="turn",
            conversation=self.conversation,
            principal_id="user",
            capability="shell",
            summary="Run command",
            call_fingerprint="fingerprint",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            resume_mode=ResumeMode.RETRY_CALL,
        )
        task = asyncio.create_task(self.broker.suspend(request))
        await asyncio.sleep(0)
        bus = MessageBus()
        owner = SimpleNamespace(intervention=self.broker)
        bus.intervention_reply_classifier = lambda msg: AgentLoop._is_intervention_reply(owner, msg)
        msg = InboundMessage(channel="web", chat_id="local", sender_id="user", content="yolo")
        self.assertEqual(CommandDispatcher._command_name(msg), "yolo")
        await bus.publish_inbound(msg)
        self.assertIs(await bus.consume_command(), msg)
        result = await self.broker.resolve(
            "user", self.conversation, InterventionAction.APPROVE_FOREVER, require_pending=True
        )
        self.assertEqual(result.state, InterventionState.APPROVED_FOREVER)
        self.assertEqual(await task, result)
        self.assertFalse(bus.is_intervention_reply(msg))
        self.assertTrue(self.broker.has_session_grant(self.conversation, "user", "shell", "exec"))
