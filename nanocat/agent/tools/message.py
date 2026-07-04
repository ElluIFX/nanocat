"""Message tool for sending messages to users."""

from typing import Any, Awaitable, Callable

from nanocat.agent.tools.base import Tool, tool_err, tool_ok
from nanocat.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._default_message_id = default_message_id
        self._sent_in_turn: bool = False

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id
        self._default_message_id = message_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    def start_turn(self) -> None:
        """Reset per-turn send tracking."""
        self._sent_in_turn = False

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
        channel = channel or self._default_channel
        chat_id = chat_id or self._default_chat_id
        message_id = message_id or self._default_message_id

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
            if channel == self._default_channel and chat_id == self._default_chat_id:
                self._sent_in_turn = True
            return tool_ok(
                channel=channel,
                chat_id=chat_id,
                attachments=len(attachments) if attachments else 0,
            )
        except Exception as e:
            return tool_err(f"Error sending message: {str(e)}")


class AskTool(Tool):
    """Ask the user a question mid-turn and block for their reply.

    Folds a clarifying round-trip into the tool-call chain: instead of ending the
    turn to ask (which stops the flow), the agent calls ``ask``, the question is
    sent to the user's channel, and their next reply is returned as this tool's
    result. The turn stays in-flight, so the reply arrives via the loop's steer
    buffer; ``reply_waiter`` consumes it (see ``AgentLoop._wait_for_reply``).
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        reply_waiter: Callable[[str, float | None], Awaitable[str | None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._reply_waiter = reply_waiter
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id
        self._session_key = ""

    def set_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Set the current message context (message_id accepted for a uniform signature)."""
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_session_key(self, session_key: str) -> None:
        """Set the exact session key (may be thread-scoped) used to match the user's reply."""
        self._session_key = session_key

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
        if not self._send_callback or not self._default_channel or not self._default_chat_id:
            return tool_err("Ask is unavailable: no channel/send context configured.")
        if not self._reply_waiter:
            return tool_err("Ask is unavailable: reply waiter not configured.")

        msg = OutboundMessage(
            channel=self._default_channel,
            chat_id=self._default_chat_id,
            content=question,
            media=[],
            metadata={},
        )
        try:
            await self._send_callback(msg)
        except Exception as e:
            return tool_err(f"Failed to send question: {e}")

        session_key = self._session_key or f"{self._default_channel}:{self._default_chat_id}"
        timeout = timeout_s if timeout_s and timeout_s > 0 else None
        answer = await self._reply_waiter(session_key, timeout)
        if answer is None:
            return tool_ok(answered=False, timed_out=True)
        return tool_ok(answered=True, answer=answer)
