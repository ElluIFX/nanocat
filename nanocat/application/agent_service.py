"""Application facade for turn processing and agent lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from nanocat.application.turns import TurnRequest
from nanocat.core.messages import ConversationRef


class AgentService:
    """Own the application-facing agent port during the incremental migration.

    The legacy AgentLoop remains the private execution engine for now.  This
    facade gives the runtime composition a stable owner for ingress, direct
    turns, cancellation and shutdown while individual pipeline stages move out
    of the loop in later batches.
    """

    def __init__(self, engine: Any):
        self._engine = engine
        self._inflight: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def engine(self) -> Any:
        """Return the compatibility engine for migration-only integrations."""
        return self._engine

    async def run(self) -> None:
        """Run the application ingress loop."""
        await self._engine.run()

    def stop(self) -> None:
        """Request cancellation of the ingress loop and active turns."""
        self._engine.stop()

    async def close(self) -> None:
        """Close application-owned turns and engine resources."""
        self._closed = True
        self.stop()
        current = asyncio.current_task()
        inflight = tuple(task for task in self._inflight if task is not current and not task.done())
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await self._engine.close_mcp()

    async def close_mcp(self) -> None:
        """Compatibility lifecycle name used by the current supervisor."""
        await self._engine.close_mcp()

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        transient: bool = False,
        principal_id: str = "user",
        metadata: dict[str, Any] | None = None,
        deadline_at: datetime | None = None,
    ) -> str:
        """Process a direct turn through the same application ingress path."""
        return await self.submit(
            TurnRequest(
                content=content,
                conversation=ConversationRef(
                    channel=channel,
                    chat_id=chat_id,
                    session_key=session_key,
                ),
                principal_id=principal_id,
                source="direct",
                transient=transient,
                metadata=metadata or {},
                deadline_at=deadline_at,
            ),
            on_progress=on_progress,
        )

    async def submit(
        self,
        request: TurnRequest,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Submit a normalized turn request to the current execution engine."""
        if self._closed:
            raise RuntimeError("agent service is closed")
        current = asyncio.current_task()
        if current is not None:
            self._inflight.add(current)

        async def _run() -> str:
            return await self._engine.process_direct(
                request.content,
                session_key=request.conversation.session_key,
                channel=request.conversation.channel,
                chat_id=request.conversation.chat_id,
                on_progress=on_progress,
                transient=request.transient,
                principal_id=request.principal_id,
                metadata={**dict(request.metadata), "_turn_source": request.source},
            )

        try:
            if request.deadline_at is None:
                return await _run()
            deadline = request.deadline_at
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise TimeoutError(f"turn deadline expired for {request.source}")
            return await asyncio.wait_for(_run(), timeout=remaining)
        finally:
            if current is not None:
                self._inflight.discard(current)

    def set_runtime_supervisor(self, supervisor: Any) -> None:
        """Bind the composition owner used by runtime-control commands."""
        self._engine.set_runtime_supervisor(supervisor)

    def __getattr__(self, name: str) -> Any:
        """Keep legacy integrations source-compatible during extraction."""
        return getattr(self._engine, name)
