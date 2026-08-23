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
        self._continuations: set[asyncio.Task[None]] = set()

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
        """Close all components in reverse order and report any failed owners."""
        errors: list[str] = []
        timed_out: list[str] = []
        failed: list[str] = []
        reason = ShutdownReason(kind="manual", detail="component registry close")
        close_order = list(reversed(tuple(self._components)))
        for index, name in enumerate(close_order):
            component = self._components[name]
            if component.closed:
                continue
            close_task = asyncio.create_task(
                _invoke(component.closer),
                name=f"nanocat.close.{name}",
            )
            try:
                done, _ = await asyncio.wait((close_task,), timeout=self.close_timeout)
                if not done:
                    if component.critical:
                        remaining = tuple(close_order[index + 1 :])

                        async def finish_critical_close(
                            owner: asyncio.Task[None] = close_task,
                            owned: OwnedComponent = component,
                        ) -> None:
                            cancelled = False
                            while True:
                                try:
                                    await asyncio.shield(owner)
                                    break
                                except asyncio.CancelledError:
                                    if owner.done():
                                        try:
                                            owner.result()
                                        except asyncio.CancelledError:
                                            logger.error(
                                                "Detached critical close was cancelled for {}",
                                                owned.name,
                                            )
                                        break
                                    cancelled = True
                                except Exception:
                                    logger.exception(
                                        "Detached critical close failed for {}",
                                        owned.name,
                                    )
                                    break
                            owned.closed = True
                            await self.close_all()
                            if cancelled:
                                raise asyncio.CancelledError

                        continuation = asyncio.create_task(
                            finish_critical_close(),
                            name=f"nanocat.close-continuation.{name}",
                        )
                        self._continuations.add(continuation)
                        continuation.add_done_callback(self._continuations.discard)
                        for blocked_name in remaining:
                            if blocked_name not in failed:
                                failed.append(blocked_name)
                                errors.append(
                                    f"{blocked_name}: close deferred behind {name}"
                                )
                        raise TimeoutError

                    close_task.cancel()

                    def consume(task: asyncio.Task[None], component_name: str = name) -> None:
                        if task.cancelled():
                            return
                        if error := task.exception():
                            logger.error(
                                "Detached close task failed for {} ({})",
                                component_name,
                                type(error).__name__,
                            )

                    close_task.add_done_callback(consume)
                    raise TimeoutError
                await close_task
            except TimeoutError:
                timed_out.append(name)
                failed.append(name)
                logger.error("Timed out closing runtime component {}", name)
                if component.critical:
                    break
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
    """Idempotent coordinator used by service and failure paths."""

    def __init__(self, owners: ComponentOwnerRegistry):
        self.owners = owners
        self._lock = asyncio.Lock()
        self._complete = asyncio.Event()
        self._stopping = False
        self._report: ShutdownReport | None = None
        self._shutdown_task: asyncio.Task[ShutdownReport] | None = None

    @property
    def report(self) -> ShutdownReport | None:
        """Return the completed report, if shutdown has finished."""
        return self._report

    async def shutdown(self, reason: ShutdownReason) -> ShutdownReport:
        """Run the reverse owner graph once and share the result with callers."""
        async with self._lock:
            if self._report is not None:
                return self._report
            if self._shutdown_task is None:
                self._stopping = True
                self._shutdown_task = asyncio.create_task(
                    self._run_shutdown(reason),
                    name="nanocat.shutdown",
                )
            shutdown_task = self._shutdown_task
        cancelled = False
        while True:
            try:
                report = await asyncio.shield(shutdown_task)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return report

    async def _run_shutdown(self, reason: ShutdownReason) -> ShutdownReport:
        """Own shutdown completion independently from any requesting task."""
        try:
            base_report = await self.owners.close_all()
            report = ShutdownReport(
                reason=reason,
                errors=base_report.errors,
                timed_out=base_report.timed_out,
                failed=base_report.failed,
            )
        except Exception as exc:
            logger.exception("Runtime shutdown coordinator failed")
            report = ShutdownReport(
                reason=reason,
                errors=(f"shutdown coordinator: {type(exc).__name__}",),
                failed=("shutdown_coordinator",),
            )
        self._report = report
        self._complete.set()
        return report
