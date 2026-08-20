"""Single runtime composition owner around the legacy services."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from typing import Any

from loguru import logger

from nanocat.core.runtime import HealthState, ShutdownReason
from nanocat.observability.contracts import HealthReport
from nanocat.runtime.lifecycle import ComponentOwnerRegistry, ShutdownCoordinator


class RuntimeSupervisor:
    """Own component startup, task tracking and reverse-order shutdown."""

    def __init__(self, runtime: Any, *, close_timeout: float = 10.0):
        self.runtime = runtime
        self.owners = ComponentOwnerRegistry(close_timeout=close_timeout)
        self.shutdown_coordinator = ShutdownCoordinator(self.owners)
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False
        self._stop_requested = False
        self._restart_task: asyncio.Task[None] | None = None
        self._health: dict[str, HealthReport] = {}
        self._register_legacy_components()

    def _register_legacy_components(self) -> None:
        """Register current services without moving their business ownership yet."""
        log_sink_id = getattr(self.runtime, "log_sink_id", None)
        if log_sink_id is not None:
            self.owners.register(
                "logging",
                logger,
                closer=lambda sink_id=log_sink_id: logger.remove(sink_id),
            )
            self._set_health("logging", HealthState.STARTING)
        self.owners.register("message_bus", self.runtime.bus, closer=self.runtime.bus.close)
        self._set_health("message_bus", HealthState.STARTING)
        self.owners.register(
            "channels",
            self.runtime.channels,
            closer=self.runtime.channels.stop_all,
            critical=False,
        )
        self._set_health("channels", HealthState.STARTING)
        if getattr(self.runtime, "intervention", None) is not None:
            self.owners.register(
                "intervention",
                self.runtime.intervention,
                closer=self.runtime.intervention.close,
            )
            self._set_health("intervention", HealthState.STARTING)
        self.owners.register("agent", self.runtime.agent, closer=self._stop_agent)
        self._set_health("agent", HealthState.STARTING)
        if getattr(self.runtime, "command_dispatcher", None) is not None:
            self.owners.register(
                "commands",
                self.runtime.command_dispatcher,
                closer=self.runtime.command_dispatcher.close,
            )
            self._set_health("commands", HealthState.STARTING)
        self.owners.register("cron", self.runtime.cron, closer=self.runtime.cron.close)
        self._set_health("cron", HealthState.STARTING)
        self.owners.register(
            "heartbeat",
            self.runtime.heartbeat,
            closer=self.runtime.heartbeat.close,
            critical=False,
        )
        self._set_health("heartbeat", HealthState.STARTING)

    def _set_health(self, component: str, state: HealthState, reason: str = "") -> None:
        self._health[component] = HealthReport(component=component, state=state, reason=reason)

    async def _stop_agent(self) -> None:
        """Stop every application turn before releasing agent resources."""
        close = getattr(self.runtime.agent, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result
            return
        self.runtime.agent.stop()
        await self.runtime.agent.close_mcp()

    async def start(self) -> None:
        """Start the legacy services through one non-blocking composition root."""
        if self._started:
            return
        if self.shutdown_coordinator.report is not None:
            raise RuntimeError("runtime supervisor cannot start after shutdown")

        self._started = True
        try:
            await self.runtime.cron.start()
            self._set_health("cron", HealthState.READY)
            await self.runtime.heartbeat.start()
            self._set_health("heartbeat", HealthState.READY)
            self._set_health("message_bus", HealthState.READY)
            if "intervention" in self._health:
                self._set_health("intervention", HealthState.READY)
            self._tasks = [
                asyncio.create_task(
                    self.runtime.channels.start_all(), name="nanocat.channels"
                ),
                asyncio.create_task(self.runtime.agent.run(), name="nanocat.agent"),
            ]
            if getattr(self.runtime, "command_dispatcher", None) is not None:
                self._tasks.append(
                    await self.runtime.command_dispatcher.start()
                )
            channels_ready = await self.runtime.channels.wait_ready()
            self._set_health(
                "channels",
                HealthState.READY if channels_ready else HealthState.DEGRADED,
                "" if channels_ready else "one or more channels are not ready",
            )
            self._set_health("agent", HealthState.READY)
            if "commands" in self._health:
                self._set_health("commands", HealthState.READY)
        except Exception:
            for name in ("cron", "heartbeat", "channels", "agent", "commands"):
                self._set_health(name, HealthState.FAILED, "startup failed")
            await self.stop(ShutdownReason(kind="component_failure", detail="startup failed"))
            raise

    async def wait(self) -> None:
        """Wait for tracked service tasks and route failures into shutdown."""
        if not self._started:
            await self.start()
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            if self._stop_requested:
                await self.shutdown_coordinator.shutdown(
                    ShutdownReason(kind="shutdown", detail="shutdown already in progress")
                )
                return
            self._set_health("agent", HealthState.DRAINING, "runtime task cancelled")
            await self.stop(ShutdownReason(kind="signal", detail="runtime task cancelled"))
            raise
        except Exception:
            logger.exception("Runtime component task failed")
            for name in ("channels", "agent", "commands"):
                self._set_health(name, HealthState.FAILED, "runtime task failed")
            await self.stop(
                ShutdownReason(kind="component_failure", detail="runtime task failed")
            )
            raise

    async def run(self) -> None:
        """Start and wait for the runtime, with a single failure path."""
        await self.start()
        try:
            await self.wait()
        finally:
            if not self._stop_requested:
                await self.stop(ShutdownReason(kind="manual", detail="runtime exited"))

    async def stop(self, reason: ShutdownReason | None = None) -> None:
        """Stop services once, then join or cancel their tracked tasks."""
        if self._stop_requested:
            await self.shutdown_coordinator.shutdown(
                reason or ShutdownReason(kind="manual", detail="stop requested")
            )
            return
        self._stop_requested = True
        restart_task = self._restart_task
        if (
            restart_task is not None
            and restart_task is not asyncio.current_task()
            and not restart_task.done()
        ):
            restart_task.cancel()
            await asyncio.gather(restart_task, return_exceptions=True)
            self._restart_task = None
        for name, report in self._health.items():
            if report.state not in {HealthState.STOPPED, HealthState.FAILED}:
                self._set_health(name, HealthState.DRAINING, "shutdown requested")
        report = await self.shutdown_coordinator.shutdown(
            reason or ShutdownReason(kind="manual", detail="stop requested")
        )
        for task in self._tasks:
            if task.done():
                continue
            try:
                await asyncio.wait_for(task, timeout=self.owners.close_timeout)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Runtime task failed while stopping")
        if report.errors or report.timed_out:
            logger.warning(
                "Runtime shutdown completed with errors={} timeouts={}",
                len(report.errors),
                len(report.timed_out),
            )
        failed = set(report.failed)
        for name in self._health:
            if name in failed:
                self._set_health(name, HealthState.FAILED, "shutdown incomplete")
            else:
                self._set_health(name, HealthState.STOPPED, report.reason.detail)

    async def request_restart(self, channel: str, chat_id: str) -> None:
        """Schedule a controlled process restart after runtime shutdown begins."""
        if self._stop_requested:
            return
        if self._restart_task is not None and not self._restart_task.done():
            return

        notify_path = self.runtime.paths.restart_notification
        notify_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"channel": channel, "chat_id": chat_id})
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{notify_path.name}.",
            suffix=".tmp",
            dir=notify_path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, notify_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            raise

        async def _restart() -> None:
            await asyncio.sleep(1)
            await self.stop(ShutdownReason(kind="restart", detail="restart requested"))
            os.execv(sys.executable, [sys.executable, "-m", "nanocat"] + sys.argv[1:])

        self._restart_task = asyncio.create_task(_restart(), name="nanocat.restart")

    @property
    def started(self) -> bool:
        """Return whether startup has been requested."""
        return self._started

    @property
    def health(self) -> dict[str, HealthReport]:
        """Return a detached snapshot of component health."""
        return dict(self._health)
