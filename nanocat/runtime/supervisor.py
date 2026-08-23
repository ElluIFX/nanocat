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
from nanocat.runtime.lifecycle import (
    ComponentOwnerRegistry,
    ShutdownCoordinator,
    ShutdownReport,
)


class RuntimeSupervisor:
    """Own component startup, task tracking and reverse-order shutdown."""

    def __init__(self, runtime: Any, *, close_timeout: float = 10.0):
        self.runtime = runtime
        self.owners = ComponentOwnerRegistry(close_timeout=close_timeout)
        self.shutdown_coordinator = ShutdownCoordinator(self.owners)
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False
        self._stop_requested = False
        self._stop_task: asyncio.Task[ShutdownReport] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._health: dict[str, HealthReport] = {}
        self._register_legacy_components()

    def _register_legacy_components(self) -> None:
        """Register current services without moving their business ownership yet."""
        if getattr(self.runtime, "instance_lock", None) is not None:
            self.owners.register(
                "instance_lock",
                self.runtime.instance_lock,
                closer=self.runtime.instance_lock.close,
            )
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
        if getattr(self.runtime, "intervention", None) is not None:
            self.owners.register(
                "intervention",
                self.runtime.intervention,
                closer=self.runtime.intervention.close,
            )
            self._set_health("intervention", HealthState.STARTING)
        if getattr(self.runtime, "activity_journal", None) is not None:
            self.owners.register(
                "activity",
                self.runtime.activity_journal,
                closer=self.runtime.activity_journal.close,
                critical=False,
            )
            self._set_health("activity", HealthState.STARTING)
        if getattr(self.runtime, "runtime_files", None) is not None:
            self.owners.register(
                "runtime_files",
                self.runtime.runtime_files,
                closer=self.runtime.runtime_files.close,
                critical=False,
            )
            self._set_health("runtime_files", HealthState.STARTING)
        self.owners.register(
            "channels",
            self.runtime.channels,
            closer=self.runtime.channels.stop_all,
            critical=False,
        )
        self._set_health("channels", HealthState.STARTING)
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
        self.owners.register(
            "channel_ingress",
            self.runtime.channels,
            closer=self.runtime.channels.begin_shutdown,
            critical=False,
        )
        if getattr(self.runtime, "api_runtime", None) is not None:
            self.owners.register(
                "api",
                self.runtime.api_runtime,
                closer=self.runtime.api_runtime.close,
            )
            self._set_health("api", HealthState.STARTING)

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
        try:
            async with self._lifecycle_lock:
                if self._started:
                    return
                if self._stop_requested:
                    return
                if self.shutdown_coordinator.report is not None:
                    raise RuntimeError("runtime supervisor cannot start after shutdown")

                self._started = True
                await self.runtime.cron.start()
                self._raise_if_stopping()
                self._set_health("cron", HealthState.READY)
                await self.runtime.heartbeat.start()
                self._raise_if_stopping()
                self._set_health("heartbeat", HealthState.READY)
                self._set_health("message_bus", HealthState.READY)
                if "intervention" in self._health:
                    self._set_health("intervention", HealthState.READY)
                if "activity" in self._health:
                    self._set_health("activity", HealthState.READY)
                if "runtime_files" in self._health:
                    self._set_health("runtime_files", HealthState.READY)
                self._tasks = [
                    asyncio.create_task(
                        self.runtime.channels.start_all(),
                        name="nanocat.channels",
                    ),
                    asyncio.create_task(self.runtime.agent.run(), name="nanocat.agent"),
                ]
                if getattr(self.runtime, "command_dispatcher", None) is not None:
                    self._tasks.append(await self.runtime.command_dispatcher.start())
                    self._raise_if_stopping()
                channels_ready = await self.runtime.channels.wait_ready()
                self._raise_if_stopping()
                self._set_health(
                    "channels",
                    HealthState.READY if channels_ready else HealthState.DEGRADED,
                    "" if channels_ready else "one or more channels are not ready",
                )
                self._set_health("agent", HealthState.READY)
                if "commands" in self._health:
                    self._set_health("commands", HealthState.READY)
                api_runtime = getattr(self.runtime, "api_runtime", None)
                if api_runtime is not None and api_runtime.enabled:
                    await api_runtime.start()
                    self._raise_if_stopping()
                    self._tasks.append(
                        asyncio.create_task(api_runtime.wait(), name="nanocat.http")
                    )
                    self._set_health("api", HealthState.READY)
                elif "api" in self._health:
                    self._set_health("api", HealthState.STOPPED, "HTTP surfaces disabled")
                logger.info("NanoCat runtime is ready")
        except BaseException:
            for name in (
                "cron",
                "heartbeat",
                "channels",
                "agent",
                "commands",
                "api",
            ):
                if name in self._health:
                    self._set_health(name, HealthState.FAILED, "startup failed")
            await self.stop(ShutdownReason(kind="component_failure", detail="startup failed"))
            raise

    def _raise_if_stopping(self) -> None:
        if self._stop_requested:
            raise RuntimeError("runtime startup interrupted by shutdown")

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
            for name in ("channels", "agent", "commands", "api"):
                if name in self._health:
                    self._set_health(name, HealthState.FAILED, "runtime task failed")
            await self.stop(ShutdownReason(kind="component_failure", detail="runtime task failed"))
            raise

    async def run(self) -> None:
        """Start and wait for the runtime, with a single failure path."""
        await self.start()
        if self._stop_requested:
            return
        try:
            await self.wait()
        finally:
            if not self._stop_requested:
                await self.stop(ShutdownReason(kind="manual", detail="runtime exited"))

    async def stop(self, reason: ShutdownReason | None = None) -> ShutdownReport:
        """Share one complete shutdown operation across every caller."""
        if self._stop_task is None:
            self._stop_requested = True
            stop_reason = reason or ShutdownReason(kind="manual", detail="stop requested")
            self._stop_task = asyncio.create_task(
                self._stop_once(stop_reason),
                name="nanocat.supervisor-stop",
            )
        task = self._stop_task
        cancelled = False
        while True:
            try:
                report = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return report

    async def _stop_once(self, reason: ShutdownReason) -> ShutdownReport:
        """Stop owners, join service tasks, and publish final health exactly once."""
        async with self._lifecycle_lock:
            for name, report in self._health.items():
                if report.state not in {HealthState.STOPPED, HealthState.FAILED}:
                    self._set_health(name, HealthState.DRAINING, "shutdown requested")
            report = await self.shutdown_coordinator.shutdown(reason)
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
        return report

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
            report = await self.stop(
                ShutdownReason(kind="restart", detail="restart requested")
            )
            critical_failed = [
                name
                for name in report.failed
                if name not in self.owners.names() or self.owners.get(name).critical
            ]
            if critical_failed:
                try:
                    notify_path.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.error(
                    "Restart aborted after critical shutdown failures: {}",
                    ", ".join(critical_failed),
                )
                return
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
