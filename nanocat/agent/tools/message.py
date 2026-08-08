"""Message tool for sending messages to users."""

import json
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from nanocat.agent.tools.base import Tool, tool_err, tool_ok
from nanocat.bus.events import OutboundMessage


class MessageTool(Tool):
    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._channel_context: ContextVar[str] = ContextVar(
            "message_channel", default=default_channel
        )
        self._chat_context: ContextVar[str] = ContextVar("message_chat", default=default_chat_id)
        self._message_context: ContextVar[str | None] = ContextVar(
            "message_id", default=default_message_id
        )
        self._sent_turns: set[str] = set()

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._channel_context.set(channel)
        self._chat_context.set(chat_id)
        self._message_context.set(message_id)

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def sent_in_turn(self, turn_id: str) -> bool:
        """Return whether this tool sent to its owning conversation in a turn."""
        return turn_id in self._sent_turns

    def forget_turn(self, turn_id: str) -> None:
        """Release turn-local delivery state after the owning turn is persisted."""
        self._sent_turns.discard(turn_id)

    def record_execution(self, context: Any, params: dict[str, Any], result: str) -> None:
        """Track successful sends without using shared mutable turn state."""
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            return
        if not payload.get("ok"):
            return
        channel = params.get("channel") or context.conversation.channel
        if channel != context.conversation.channel:
            return
        chat_id = params.get("chat_id") or context.conversation.chat_id
        if chat_id != context.conversation.chat_id:
            return
        self._sent_turns.add(context.turn_id)

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something or sending files/images to the user."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The message content to send"},
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)",
                },
                "chat_id": {"type": "string", "description": "Optional: target chat/user ID"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents, etc.)",
                },
            },
            "required": ["content"],
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        attachments: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        default_channel = self._channel_context.get()
        default_chat_id = self._chat_context.get()
        default_message_id = self._message_context.get()
        channel = channel or default_channel
        chat_id = chat_id or default_chat_id
        message_id = message_id or default_message_id

        if not channel or not chat_id:
            return tool_err("No target channel/chat specified")

        if not self._send_callback:
            return tool_err("Message sending not configured")

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=attachments or [],
            metadata={
                "message_id": message_id,
            },
        )

        try:
            await self._send_callback(msg)
            return tool_ok(
                channel=channel,
                chat_id=chat_id,
                attachments=len(attachments) if attachments else 0,
            )
        except Exception as e:
            return tool_err(f"Error sending message: {str(e)}")


class AskTool(Tool):
    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        reply_waiter: Callable[[str, float | None], Awaitable[str | None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._reply_waiter = reply_waiter
        self._channel_context: ContextVar[str] = ContextVar(
            "ask_channel", default=default_channel
        )
        self._chat_context: ContextVar[str] = ContextVar("ask_chat", default=default_chat_id)
        self._session_context: ContextVar[str] = ContextVar("ask_session", default="")

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context (message_id accepted for a uniform signature)."""
        self._channel_context.set(channel)
        self._chat_context.set(chat_id)

    def set_session_key(self, session_key: str) -> None:
        """Set the exact session key (may be thread-scoped) used to match the user's reply."""
        self._session_context.set(session_key)

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        self._send_callback = callback

    def set_reply_waiter(
        self, waiter: Callable[[str, float | None], Awaitable[str | None]]
    ) -> None:
        self._reply_waiter = waiter

    @property
    def name(self) -> str:
        return "ask"

    @property
    def description(self) -> str:
        return (
            "Ask the user a question and block up to timeout_s for their reply, returning "
            "it as this tool's result. Folds a clarifying round-trip into the tool-call "
            "chain: instead of ending your turn to ask (which stops the flow), call ask, "
            "keep your plan, and branch on the answer. Use it for a real decision only the "
            "user can settle — which of two candidates, confirming a risky/irreversible "
            "action, a missing value or credential. Not for rhetorical questions or progress "
            "notes (use message for those). If no reply arrives in time the result is "
            "timed_out; then decide how to proceed on your own."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to send to the user"},
                "timeout_s": {
                    "type": "number",
                    "default": 300,
                    "description": "Seconds to wait for a reply; <=0 waits indefinitely "
                    "(the user can still /stop). On expiry the result is timed_out.",
                },
            },
            "required": ["question"],
        }

    async def execute(self, question: str, timeout_s: float = 300, **kwargs: Any) -> str:
        channel = self._channel_context.get()
        chat_id = self._chat_context.get()
        if not self._send_callback or not channel or not chat_id:
            return tool_err("Ask is unavailable: no channel/send context configured.")
        if not self._reply_waiter:
            return tool_err("Ask is unavailable: reply waiter not configured.")

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=question,
            media=[],
            metadata={},
        )
        try:
            await self._send_callback(msg)
        except Exception as e:
            return tool_err(f"Failed to send question: {e}")

        session_key = self._session_context.get() or f"{channel}:{chat_id}"
        timeout = timeout_s if timeout_s and timeout_s > 0 else None
        answer = await self._reply_waiter(session_key, timeout)
        if answer is None:
            return tool_ok(answered=False, timed_out=True)
        return tool_ok(answered=True, answer=answer)
