"""Runtime-owned tool catalog and long-lived tool resources."""

from __future__ import annotations

from typing import Any

from nanocat.bus.queue import MessageBus


class ToolHost:
    """Own the runtime-scoped tool registry and stateful tool resources."""

    def __init__(self, bus: MessageBus, config: Any, provider_resolver: Any):
        from nanocat.agent.subagent import SubagentManager
        from nanocat.agent.tools.http import HttpSessionManager
        from nanocat.agent.tools.proc import ProcManager
        from nanocat.agent.tools.registry import ToolRegistry
        from nanocat.agent.tools.ssh import SSHManager

        self.registry = ToolRegistry()
        self.subagents = SubagentManager(
            bus=bus,
            tools=self.registry,
            provider_resolver=provider_resolver,
            config=config,
        )
        self.ssh = SSHManager()
        self.processes = ProcManager()
        self.http_sessions = HttpSessionManager(proxy=config.tools.web.proxy)
        self._closed = False

    async def close(self) -> None:
        """Close all stateful resources created for this runtime's tools."""
        if self._closed:
            return
        self._closed = True
        await self.subagents.close()
        await self.ssh.close_all()
        await self.processes.close_all()
        await self.http_sessions.close_all()
