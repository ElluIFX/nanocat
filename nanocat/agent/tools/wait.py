"""Wait tool for pipeline flow control."""

import asyncio
from typing import Any, Awaitable, Callable

from nanocat.agent.tools.base import Tool, tool_ok
from nanocat.bus.events import OutboundMessage
from nanocat.utils.helpers import current_time_str


class WaitTool(Tool):
    """Tool to pause execution and optionally notify the user of progress.

    Designed for long-running pipelines and automated workflows where the agent
    needs to wait between steps and keep the user informed without breaking the
    tool-calling flow.
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context."""
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "wait"

    @property
    def description(self) -> str:
        return (
            "Pause execution for N seconds, optionally notifying the user of progress. "
            "Use between pipeline stages to wait for external actions to complete. "
            "Prefer this over breaking the tool-calling flow to ask the user what to do next — "
            "interrupting the flow stops the pipeline from completing."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "wait_s": {
                    "type": "number",
                    "description": "Number of seconds to wait (float)",
                },
                "message": {
                    "type": "string",
                    "description": "Progress update / reason for waiting to send to the user",
                },
            },
            "required": ["wait_s"],
        }

    async def execute(self, wait_s: float, message: str | None = None, **kwargs: Any) -> str:
        if message and self._send_callback and self._default_channel and self._default_chat_id:
            message = f"[Wait {wait_s}s]: {message}"
            msg = OutboundMessage(
                channel=self._default_channel,
                chat_id=self._default_chat_id,
                content=message,
                media=[],
                metadata={},
            )
            try:
                await self._send_callback(msg)
            except Exception:
                pass  # Don't let notification failure abort the wait

        await asyncio.sleep(wait_s)
        return tool_ok(waited_s=wait_s, now=current_time_str(timezone=False))
