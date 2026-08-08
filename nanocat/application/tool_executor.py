"""Application-owned tool execution and human intervention bridge."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from loguru import logger

from nanocat.application.auto_approval import AutoApprovalResult, AutoApprovalReviewer
from nanocat.application.intervention import InterventionBroker, new_intervention_request
from nanocat.application.turns import TurnState
from nanocat.core.intervention import (
    InterventionAction,
    InterventionFlow,
    InterventionKind,
    InterventionState,
    ResumeMode,
)
from nanocat.core.messages import ConversationRef
from nanocat.security import ToolAuthorization
from nanocat.security.policy import SecurityDecision, SecurityDecisionKind, SecurityPolicy

if TYPE_CHECKING:
    from nanocat.agent.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Runtime identity needed for a security decision."""

    turn_id: str
    session_key: str
    conversation: ConversationRef
    principal_id: str
    state_hook: Callable[[TurnState], None] | None = None
    message_id: str | None = None
    session: Any | None = None
    model: str | None = None
    user_input: str = ""


class ToolTurnAbortedError(RuntimeError):
    """Raised when a tool call ends the current turn outside the LLM loop."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ToolExecutor:
    """Single application boundary for policy, intervention and tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: SecurityPolicy,
        intervention: InterventionBroker | None = None,
        auto_reviewer: AutoApprovalReviewer | None = None,
        max_concurrent_calls: int = 16,
    ):
        if max_concurrent_calls <= 0:
            raise ValueError("max_concurrent_calls must be positive")
        self._registry = registry
        self._policy = policy
        self._intervention = intervention
        self._auto_reviewer = auto_reviewer
        self._max_concurrent_calls = max_concurrent_calls
        self._call_slots = asyncio.Semaphore(max_concurrent_calls)

    def with_registry(self, registry: ToolRegistry) -> "ToolExecutor":
        """Create a facade with the same policy and a restricted tool catalog."""
        return ToolExecutor(
            registry,
            self._policy,
            self._intervention,
            self._auto_reviewer,
            max_concurrent_calls=self._max_concurrent_calls,
        )

    @staticmethod
    def _strip_success_markers(result: Any) -> Any:
        """Remove successful JSON envelope markers before returning to the LLM."""
        def strip(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: strip(item)
                    for key, item in value.items()
                    if not (key == "ok" and item is True)
                }
            if isinstance(value, list):
                return [strip(item) for item in value]
            return value

        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except (TypeError, ValueError):
                return result
            cleaned = strip(payload)
            if cleaned == payload:
                return result
            return json.dumps(cleaned, ensure_ascii=False)
        if isinstance(result, (dict, list)):
            return strip(result)
        return result

    async def finish_turn(self, context: ToolExecutionContext) -> None:
        """Release runtime-scoped grants when an owning turn ends."""
        if self._intervention is not None:
            await self._intervention.finish_turn(context.turn_id)

    async def execute(
        self,
        name: str,
        params: dict[str, Any],
        context: ToolExecutionContext,
        *,
        fallback_skill_loader: Any = None,
    ) -> str:
        return (
            await self.execute_batch(
                [(name, params)],
                context,
                fallback_skill_loader=fallback_skill_loader,
            )
        )[0]

    async def execute_batch(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        context: ToolExecutionContext,
        *,
        fallback_skill_loader: Any = None,
    ) -> list[str]:
        """Preflight a complete model batch before executing any call.

        Sensitive calls are resolved before ordinary calls in the same batch,
        so a pending user decision cannot receive partial tool results from the
        LLM.  Once all decisions are settled, independent calls execute in
        parallel and results retain model order.
        """
        decisions = [(name, params, self._policy.evaluate(name, params)) for name, params in calls]
        for name, _params, decision in decisions:
            if decision.kind is not SecurityDecisionKind.ALLOW or decision.reason:
                logger.warning(
                    "[SECURITY] decision={} tool={} capability={} fingerprint={} reason={}",
                    decision.kind.value,
                    name,
                    decision.capability,
                    decision.call_fingerprint[:16],
                    decision.reason or decision.summary,
                )
        hard_denial = next(
            (
                (index, name, decision)
                for index, (name, _params, decision) in enumerate(decisions)
                if decision.kind is SecurityDecisionKind.HARD_DENY
            ),
            None,
        )
        if hard_denial is not None:
            blocked_index, _blocked_name, _blocked_decision = hard_denial
            return [
                self._security_result(
                    name,
                    decision,
                    code=(
                        "security_policy_denied"
                        if index == blocked_index
                        else "security_batch_aborted"
                    ),
                )
                for index, (name, _params, decision) in enumerate(decisions)
            ]

        approval_scopes: list[str | None] = [None] * len(decisions)
        needs_defer_scope = any(
            decision.kind is SecurityDecisionKind.REQUIRE_INTERVENTION
            for _name, _params, decision in decisions
        )
        if needs_defer_scope and self._intervention is not None:
            await self._intervention.begin_defer_scope(context.conversation)
        try:
            pending: list[tuple[int, str, dict[str, Any], SecurityDecision]] = []
            for index, (name, params, decision) in enumerate(decisions):
                if decision.kind is SecurityDecisionKind.ALLOW:
                    continue
                if self._intervention is None:
                    return [
                        self._security_result(
                            item_name,
                            item_decision,
                            code="approval_channel_unavailable",
                        )
                        if item_index == index
                        else self._security_result(
                            item_name,
                            item_decision,
                            code="security_batch_aborted",
                        )
                        for item_index, (item_name, _item_params, item_decision) in enumerate(
                            decisions
                        )
                    ]
                if self._intervention.has_session_grant(
                    context.conversation,
                    context.principal_id,
                    decision.capability,
                    name,
                ):
                    approval_scopes[index] = "session"
                    continue
                if self._intervention.has_turn_grant(
                    context.conversation,
                    context.principal_id,
                    context.turn_id,
                    decision.capability,
                ):
                    approval_scopes[index] = "turn"
                    continue

                pending.append((index, name, params, decision))

            automatic: dict[int, AutoApprovalResult] = {}
            if self._auto_reviewer is not None and pending:
                review_tasks = [
                    asyncio.create_task(
                        self._auto_reviewer.review(
                            name,
                            decision,
                            user_input=context.user_input,
                        ),
                        name=f"nanocat.approval.{name}",
                    )
                    for _index, name, _params, decision in pending
                ]
                review_results = await asyncio.gather(*review_tasks)
                automatic = {
                    item[0]: result for item, result in zip(pending, review_results, strict=True)
                }

            for index, name, params, decision in pending:
                review = automatic.get(index)
                if review is not None and review.decision == "approve":
                    result = None
                    scope = "once"
                else:
                    result = await self._request_approval(decision, context, review=review)
                    scope = result.scope or "once"
                if result is not None and result.state not in {
                    InterventionState.APPROVED_ONCE,
                    InterventionState.APPROVED_TURN,
                    InterventionState.APPROVED_FOREVER,
                }:
                    return [
                        self._security_result(
                            item_name,
                            item_decision,
                            code=(
                                "security_intervention_rejected"
                                if item_index == index
                                else "security_batch_aborted"
                            ),
                            state=result.state.value,
                            review_reason=result.review_reason,
                        )
                        for item_index, (item_name, _item_params, item_decision) in enumerate(
                            decisions
                        )
                    ]
                rechecked = self._policy.evaluate(name, params)
                if rechecked.kind is SecurityDecisionKind.HARD_DENY:
                    return [
                        self._security_result(
                            item_name,
                            item_decision,
                            code=(
                                "security_policy_changed"
                                if item_index == index
                                else "security_batch_aborted"
                            ),
                            state="policy_changed",
                        )
                        for item_index, (item_name, _item_params, item_decision) in enumerate(
                            decisions
                        )
                    ]
                if rechecked.call_fingerprint != decision.call_fingerprint:
                    return [
                        self._security_result(
                            item_name,
                            item_decision,
                            code=(
                                "security_call_changed"
                                if item_index == index
                                else "security_batch_aborted"
                            ),
                            state="call_changed",
                        )
                        for item_index, (item_name, _item_params, item_decision) in enumerate(
                            decisions
                        )
                    ]
                approval_scopes[index] = scope
        finally:
            if needs_defer_scope and self._intervention is not None:
                await self._intervention.end_defer_scope(context.conversation)

        async def _run(index: int) -> tuple[int, str]:
            async with self._call_slots:
                name, params = calls[index]
                if approval_scopes[index] is not None:
                    result = await self._execute_approved(
                        name,
                        params,
                        fallback_skill_loader,
                        context,
                        scope=approval_scopes[index] or "once",
                        decision=decisions[index][2],
                    )
                else:
                    result = await self._registry.execute(
                        name,
                        params,
                        fallback_skill_loader,
                        execution_context=context,
                    )
                return index, result

        tasks = [
            asyncio.create_task(_run(index), name=f"nanocat.tool.{calls[index][0]}")
            for index in range(len(calls))
        ]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return [
            self._strip_success_markers(result)
            for _, result in sorted(results)
        ]

    @staticmethod
    def _security_result(
        name: str,
        decision: SecurityDecision,
        *,
        code: str,
        state: str | None = None,
        review_reason: str | None = None,
    ) -> str:
        """Return a non-retryable structured result for the model."""
        reason = decision.reason or decision.summary
        if code == "security_intervention_rejected":
            reason = "user rejected the sensitive operation"
        elif code == "security_batch_aborted":
            reason = "another tool call in the same batch was not executed"
        if code == "security_intervention_rejected":
            guidance = (
                "The user explicitly rejected this tool call. Do not try to bypass "
                "the approval. If the operation is necessary, stop, explain why to "
                "the user, and ask whether the command should be modified."
            )
        elif code == "security_policy_denied":
            guidance = (
                "The security policy rejected this tool call. Do not try to bypass "
                "the approval or evade the policy with another tool. If the operation "
                "is necessary, stop, explain why to the user, and ask whether the "
                "command should be modified."
            )
        else:
            guidance = None
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "type": "security_decision",
                "code": code,
                "tool": name,
                "capability": decision.capability,
                "reason": reason,
            },
            "security": {
                "decision": decision.kind.value,
                "action": "do_not_retry",
                "retryable": False,
            },
        }
        if guidance:
            payload["error"]["guidance"] = guidance
        if code == "security_intervention_rejected" and decision.reason:
            payload["error"]["policy_reason"] = decision.reason
        if code == "security_intervention_rejected" and review_reason:
            payload["error"]["auto_review_reason"] = review_reason
        if state:
            payload["error"]["state"] = state
        return json.dumps(payload, ensure_ascii=False)

    async def _request_approval(
        self,
        decision: SecurityDecision,
        context: ToolExecutionContext,
        *,
        review: AutoApprovalResult | None = None,
    ) -> Any:
        tool_name = str(decision.metadata.get("tool_name") or "unknown")
        tool_params = str(decision.metadata.get("tool_params") or "{}")
        request = new_intervention_request(
            kind=InterventionKind.APPROVAL,
            turn_id=context.turn_id,
            conversation=context.conversation,
            principal_id=context.principal_id,
            capability=decision.capability,
            summary=decision.summary,
            call_fingerprint=decision.call_fingerprint,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=self._intervention.default_timeout_seconds),
            resume_mode=ResumeMode.RETRY_CALL,
            tool_call_id=uuid4().hex,
            tool_name=tool_name,
            tool_params=tool_params,
            flow=InterventionFlow.AUTO_REVIEW if review is not None else InterventionFlow.MANUAL,
            review_decision=review.decision if review is not None else None,
            review_reason=review.reason if review is not None else None,
            allowed_actions=(
                (InterventionAction.APPROVE_ONCE, InterventionAction.REJECT)
                if review is not None
                else None
            ),
            metadata={
                "tool_name": tool_name,
                "tool_params": tool_params,
            },
        )
        if context.state_hook is not None:
            context.state_hook(TurnState.WAITING_FOR_USER)
        try:
            return await self._intervention.suspend(request)
        finally:
            if context.state_hook is not None:
                context.state_hook(TurnState.RUNNING)

    async def _execute_approved(
        self,
        name: str,
        params: dict[str, Any],
        fallback_skill_loader: Any,
        context: ToolExecutionContext,
        *,
        scope: str,
        decision: SecurityDecision,
    ) -> str:
        authorization = ToolAuthorization(
            capability=decision.capability,
            call_fingerprint=decision.call_fingerprint,
            scope=scope,
            tool_name=name,
        )
        return await self._registry.execute(
            name,
            params,
            fallback_skill_loader,
            authorization=authorization,
            execution_context=context,
        )
