"""Runtime-owned component registry and idempotent shutdown coordination."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger

from nanocat.core.runtime import ShutdownReason

Closer = Callable[[], Any]


@dataclass(slots=True)
class OwnedComponent:
    """One named lifecycle owner in the runtime composition root."""

    name: str
    component: Any
    closer: Closer
    critical: bool = True
    closed: bool = False


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """Result of a coordinated shutdown attempt."""

    reason: ShutdownReason
    errors: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


async def _invoke(closer: Closer) -> None:
    result = closer()
    if inspect.isawaitable(result):
        await result


class ComponentOwnerRegistry:
    """Registry enforcing one close owner per named runtime component."""

    def __init__(self, *, close_timeout: float = 10.0):
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self.close_timeout = close_timeout
        self._components: dict[str, OwnedComponent] = {}

    def register(
        self,
        name: str,
        component: Any,
        *,
        closer: Closer | None = None,
        critical: bool = True,
    ) -> None:
        """Register a component exactly once in this runtime."""
        if name in self._components:
            raise ValueError(f"runtime component already registered: {name}")
        close = closer or getattr(component, "close", None) or getattr(component, "stop", None)
        if not callable(close):
            raise TypeError(f"runtime component {name!r} has no close/stop hook")
        self._components[name] = OwnedComponent(name, component, close, critical)

    def names(self) -> tuple[str, ...]:
        """Return components in registration/start order."""
        return tuple(self._components)

    def get(self, name: str) -> OwnedComponent:
        """Return one registered component."""
        return self._components[name]

    async def close_all(self) -> ShutdownReport:
        """Close all components in reverse order and retain failed owners for retry."""
        errors: list[str] = []
        timed_out: list[str] = []
        failed: list[str] = []
        reason = ShutdownReason(kind="manual", detail="component registry close")
        for name in reversed(tuple(self._components)):
            component = self._components[name]
            if component.closed:
                continue
            try:
                await asyncio.wait_for(_invoke(component.closer), self.close_timeout)
            except asyncio.TimeoutError:
                timed_out.append(name)
                failed.append(name)
                logger.error("Timed out closing runtime component {}", name)
            except asyncio.CancelledError:
                errors.append(f"{name}: close cancelled")
                failed.append(name)
                logger.error("Closing runtime component {} was cancelled", name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                failed.append(name)
                logger.exception("Error closing runtime component {}", name)
            else:
                component.closed = True
        return ShutdownReport(
            reason=reason,
            errors=tuple(errors),
            timed_out=tuple(timed_out),
            failed=tuple(failed),
        )


class ShutdownCoordinator:
    """Idempotent coordinator used by gateway, TUI and failure paths."""

    def __init__(self, owners: ComponentOwnerRegistry):
        self.owners = owners
        self._lock = asyncio.Lock()
        self._complete = asyncio.Event()
        self._stopping = False
        self._report: ShutdownReport | None = None

    @property
    def report(self) -> ShutdownReport | None:
        """Return the completed report, if shutdown has finished."""
        return self._report

    async def shutdown(self, reason: ShutdownReason) -> ShutdownReport:
        """Run the reverse owner graph once and share the result with callers."""
        async with self._lock:
            if self._report is not None:
                return self._report
            if self._stopping:
                wait_for = self._complete
            else:
                self._stopping = True
                wait_for = None

        if wait_for is not None:
            await wait_for.wait()
            return self._report or ShutdownReport(reason=reason)

        close_task = asyncio.create_task(self.owners.close_all(), name="nanocat.shutdown")
        cancelled = False
        try:
            base_report = await asyncio.shield(close_task)
        except asyncio.CancelledError:
            cancelled = True
            base_report = await asyncio.shield(close_task)
        finally:
            if close_task.done() and not close_task.cancelled() and close_task.exception() is not None:
                raise close_task.exception()
        self._report = ShutdownReport(
            reason=reason,
            errors=base_report.errors,
            timed_out=base_report.timed_out,
            failed=base_report.failed,
        )
        self._complete.set()
        if cancelled:
            raise asyncio.CancelledError
        return self._report
