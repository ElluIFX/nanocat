"""Runtime-owned MCP transport lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from loguru import logger


class MCPHost:
    """Own lazy MCP connection, transport task and async exit stack."""

    def __init__(self, servers: dict[str, Any], registry: Any):
        self._servers = servers
        self._registry = registry
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._ready: asyncio.Event | None = None
        self._connected = False
        self._lock = asyncio.Lock()
        self._registered_tools: set[str] = set()

    @property
    def connected(self) -> bool:
        """Return whether all configured MCP transports are ready."""
        return self._connected

    async def connect(self) -> None:
        """Connect once and make concurrent callers share the same readiness wait."""
        if not self._servers:
            return
        async with self._lock:
            if self._connected:
                return
            if self._task is None or self._task.done():
                self._ready = asyncio.Event()
                self._stop = asyncio.Event()
                self._task = asyncio.create_task(
                    self._run_worker(self._ready, self._stop),
                    name="nanocat.mcp-host",
                )
            ready = self._ready
        if ready is not None:
            await ready.wait()

    async def _run_worker(self, ready: asyncio.Event, stop: asyncio.Event) -> None:
        from nanocat.agent.tools.mcp import connect_mcp_servers

        stack = AsyncExitStack()
        failure: BaseException | None = None
        try:
            await stack.__aenter__()
            before = set(self._registry.names())
            await connect_mcp_servers(self._servers, self._registry, stack)
            self._registered_tools = set(self._registry.names()) - before
            self._connected = True
            logger.info("MCP ready — {} server(s) connected", len(self._servers))
            ready.set()
            await stop.wait()
        except BaseException as exc:
            failure = exc
        finally:
            ready.set()
            try:
                await stack.aclose()
            except BaseException as exc:
                failure = exc
            self._connected = False
            if failure is not None and not stop.is_set():
                logger.error(
                    "MCP transport stopped unexpectedly: {}: {}",
                    type(failure).__name__,
                    failure,
                )

    async def reconfigure(self, servers: dict[str, Any]) -> None:
        """Replace MCP transports and their registered tools as one owner operation."""
        was_connected = self._connected or self._task is not None
        await self.close()
        for name in self._registered_tools:
            self._registry.unregister(name)
        self._registered_tools.clear()
        self._servers = servers
        if was_connected and servers:
            await self.connect()

    async def close(self) -> None:
        """Stop the MCP worker and close all transports exactly once."""
        async with self._lock:
            task = self._task
            stop = self._stop
            self._task = None
            self._stop = None
            self._ready = None
        if task is None:
            return
        if stop is not None:
            stop.set()
        try:
            await task
        except (asyncio.CancelledError, RuntimeError, BaseExceptionGroup):
            pass
