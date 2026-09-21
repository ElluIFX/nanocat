"""Offline security review lifecycle and execution regression coverage."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from loguru import logger

from nanocat.application.auto_approval import AutoApprovalResult, AutoApprovalReviewer
from nanocat.application.tool_executor import ToolExecutionContext, ToolExecutor
from nanocat.core.messages import ConversationRef
from nanocat.security.policy import SecurityDecision, SecurityDecisionKind


def decision(kind=SecurityDecisionKind.REQUIRE_INTERVENTION, fingerprint="fingerprint"):
    return SecurityDecision(kind, "shell", "Run command", "Needs review", fingerprint)


class AutoApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewer_timeout_cancels_provider(self):
        cancelled = asyncio.Event()

        async def chat(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        provider = SimpleNamespace(chat_with_retry=chat)
        reviewer = AutoApprovalReviewer(
            SimpleNamespace(resolve=lambda _: provider), "test", Path.cwd(), timeout_seconds=0.01
        )
        result = await reviewer.review("exec", decision(), user_input="Run command")
        self.assertEqual(result.decision, "unavailable")
        self.assertTrue(cancelled.is_set())

    async def test_reviewer_preserves_cancellation(self):
        provider = SimpleNamespace(chat_with_retry=AsyncMock(side_effect=asyncio.CancelledError))
        reviewer = AutoApprovalReviewer(
            SimpleNamespace(resolve=lambda _: provider), "test", Path.cwd()
        )
        with self.assertRaises(asyncio.CancelledError):
            await reviewer.review("exec", decision(), user_input="Run command")

    async def test_invalid_response_retries_once(self):
        provider = SimpleNamespace(
            chat_with_retry=AsyncMock(
                side_effect=[
                    SimpleNamespace(content="invalid"),
                    SimpleNamespace(content='{"decision":"approve","reason":"Read-only command"}'),
                ]
            )
        )
        reviewer = AutoApprovalReviewer(
            SimpleNamespace(resolve=lambda _: provider), "test", Path.cwd()
        )
        result = await reviewer.review("exec", decision(), user_input="Run command")
        self.assertEqual(result.decision, "approve")
        self.assertEqual(provider.chat_with_retry.await_count, 2)

    def executor(self, reviewer, *, policy=None, concurrency=2):
        registry = SimpleNamespace(execute=AsyncMock(return_value='{"ok":true,"content":"done"}'))
        broker = SimpleNamespace(
            begin_defer_scope=AsyncMock(),
            end_defer_scope=AsyncMock(),
            has_session_grant=Mock(return_value=False),
            has_turn_grant=Mock(return_value=False),
        )
        policy = policy or SimpleNamespace(evaluate=Mock(return_value=decision()))
        executor = ToolExecutor(
            registry, policy, broker, reviewer, max_concurrent_calls=concurrency
        )
        context = ToolExecutionContext(
            "turn", "session", "scope", ConversationRef("web", "chat", "session"), "user"
        )
        return executor, registry, broker, context

    async def test_approved_call_is_quiet_and_keeps_once_authorization(self):
        reviewer = SimpleNamespace(
            review=AsyncMock(return_value=AutoApprovalResult("approve", "Safe"))
        )
        executor, registry, broker, context = self.executor(reviewer)
        warnings = []
        sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
        try:
            result = await executor.execute("exec", {"command": "pwd"}, context)
        finally:
            logger.remove(sink)
        self.assertEqual(warnings, [])
        self.assertEqual(result, '{"content": "done"}')
        self.assertEqual(registry.execute.call_args.kwargs["authorization"].scope, "once")
        broker.end_defer_scope.assert_awaited_once()

    async def test_hard_denial_bypasses_reviewer_and_execution(self):
        reviewer = SimpleNamespace(review=AsyncMock())
        policy = SimpleNamespace(
            evaluate=Mock(return_value=decision(SecurityDecisionKind.HARD_DENY))
        )
        executor, registry, _, context = self.executor(reviewer, policy=policy)
        result = await executor.execute("exec", {}, context)
        self.assertIn("security_policy_denied", result)
        reviewer.review.assert_not_awaited()
        registry.execute.assert_not_awaited()

    async def test_changed_fingerprint_prevents_execution(self):
        reviewer = SimpleNamespace(
            review=AsyncMock(return_value=AutoApprovalResult("approve", "Safe"))
        )
        policy = SimpleNamespace(
            evaluate=Mock(side_effect=[decision(), decision(fingerprint="changed")])
        )
        executor, registry, _, context = self.executor(reviewer, policy=policy)
        result = await executor.execute("exec", {}, context)
        self.assertIn("security_call_changed", result)
        registry.execute.assert_not_awaited()

    async def test_reviews_respect_concurrency_limit(self):
        active = peak = 0

        async def review(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return AutoApprovalResult("approve", "Safe")

        executor, registry, _, context = self.executor(SimpleNamespace(review=review))
        await executor.execute_batch(
            [("exec", {"command": str(index)}) for index in range(6)], context
        )
        self.assertEqual(peak, 2)
        self.assertEqual(registry.execute.await_count, 6)

    async def test_review_failure_drains_siblings_before_releasing_scope(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def review(name, *args, **kwargs):
            if name == "failing":
                await started.wait()
                raise RuntimeError("review failed")
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        executor, registry, broker, context = self.executor(SimpleNamespace(review=review))
        with self.assertRaisesRegex(RuntimeError, "review failed"):
            await executor.execute_batch([("failing", {}), ("waiting", {})], context)
        self.assertTrue(cancelled.is_set())
        broker.end_defer_scope.assert_awaited_once()
        registry.execute.assert_not_awaited()
