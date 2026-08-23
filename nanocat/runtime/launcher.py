"""Non-CLI runtime launcher."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanocat import __version__
from nanocat.agent.loop import AgentLoop
from nanocat.api.server import ApiRuntime
from nanocat.application.agent_service import AgentService
from nanocat.application.configuration import ConfigurationService
from nanocat.application.intervention import (
    DeliveryTracker,
    InterventionBroker,
    make_bus_presenter,
)
from nanocat.application.providers import RuntimeProviderResolver
from nanocat.application.system_turns import SystemTurnGateway, SystemTurnRequest
from nanocat.application.vision_fallback import VisionFallbackService
from nanocat.bus.queue import MessageBus
from nanocat.channels.manager import ChannelManager
from nanocat.config.loader import get_config_path, load_config, set_config_path
from nanocat.config.schema import Config
from nanocat.core.messages import ConversationRef
from nanocat.core.runtime import ShutdownReason
from nanocat.cron.service import CronService
from nanocat.cron.types import CronJob
from nanocat.heartbeat.service import HeartbeatService
from nanocat.observability.activity import ActivityJournal
from nanocat.observability.redaction import redact_value
from nanocat.runtime.context import ConfigSnapshot, RuntimeContext
from nanocat.runtime.instance_lock import RuntimeInstanceLock
from nanocat.runtime.paths import RuntimePaths
from nanocat.runtime.supervisor import RuntimeSupervisor
from nanocat.runtime.web_assets import ensure_web_assets
from nanocat.session.manager import SessionManager
from nanocat.utils.helpers import sync_workspace_templates


@dataclass(frozen=True, slots=True)
class HeartbeatTarget:
    """Immutable recipient and principal selected for one heartbeat execution."""

    channel: str
    chat_id: str
    principal_id: str


def load_runtime_config(workdir: str | None = None) -> Config:
    """Load config from the working directory.

    *workdir* is the single anchor for all runtime state: ``config.json``,
    ``workspace/``, ``sessions/``, ``cron/`` and ``logs/`` all live under it.
    Defaults to the current working directory when not given.
    """
    # Pin an absolute config path up front (default: the CWD). This makes the
    # working dir the single, stable anchor: get_config_path() never falls back
    # to a re-evaluated Path.cwd(), so workspace_path and every path derived from
    # it stays identical for all consumers even if the process CWD later changes.
    wd = Path(workdir).expanduser().resolve() if workdir else Path.cwd().resolve()
    wd.mkdir(parents=True, exist_ok=True)
    set_config_path(wd / "config.json")

    return load_config(get_config_path())


def configure_logging(verbose: bool = False) -> int:
    """Set up stderr and rotating runtime-file logging."""
    level = "DEBUG" if verbose else "INFO"
    os.environ["NANOCAT_LOG_LEVEL"] = level

    def redact_record(record: dict[str, Any]) -> None:
        record["message"] = str(redact_value(record.get("message", "")))
        exception = record.get("exception")
        if exception is not None:
            value = getattr(exception, "value", None)
            summary = str(redact_value(str(value))) if value is not None else ""
            exception_type = getattr(getattr(exception, "type", None), "__name__", "Exception")
            record["message"] = f"{record['message']} [{exception_type}: {summary}]"
            record["exception"] = None

    logger.configure(patcher=redact_record)

    logger.remove()  # drop loguru's default (DEBUG) stderr sink
    logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)

    logs_dir = get_config_path().parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_sink_id = logger.add(
        logs_dir / "runtime.log",
        rotation="5 MB",
        retention=5,
        level=level,
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{line} - {message}",
    )
    return file_sink_id


class _PartialBuild:
    """Own resources until a complete RuntimeContext takes over."""

    def __init__(self) -> None:
        self._closers: list[Any] = []

    def own(self, closer: Any) -> None:
        self._closers.append(closer)

    def release(self) -> None:
        self._closers.clear()

    async def close(self) -> None:
        for closer in reversed(self._closers):
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Partial runtime resource cleanup failed")
        self._closers.clear()


def _finish_partial_cleanup(partial: _PartialBuild) -> None:
    """Finish cleanup even when construction runs inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(partial.close())
        return

    errors: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(partial.close())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, name="nanocat.partial-cleanup")
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


def _compose_runtime(
    *,
    workdir: str | None = None,
    verbose: bool = False,
    partial: _PartialBuild,
) -> RuntimeContext:
    """Construct the channel-service runtime and its HTTP surfaces."""
    config = load_runtime_config(workdir)  # sets the config path before paths derive
    paths = RuntimePaths.from_config_path(get_config_path(), workspace=config.workspace_path)
    sync_workspace_templates(config.workspace_path, silent=True)
    static_dir = ensure_web_assets() if config.channels.web.enabled else None
    log_sink_id = configure_logging(verbose=verbose)
    partial.own(lambda: logger.remove(log_sink_id))

    from nanocat.channels.registry import discover_channel_names, discover_descriptors

    channel_descriptors = discover_descriptors()
    channel_defaults = {
        name: dict(descriptor.config_schema)
        for name, descriptor in channel_descriptors.items()
    }
    for name in discover_channel_names():
        channel_defaults.setdefault(name, {"enabled": False})
    configuration = ConfigurationService(
        get_config_path(),
        effective_config=config,
        channel_defaults=channel_defaults,
    )
    config_snapshot = ConfigSnapshot(config=config, paths=paths)

    bus = MessageBus()
    partial.own(bus.close)
    delivery_tracker = DeliveryTracker()
    intervention = InterventionBroker(
        make_bus_presenter(bus, delivery_tracker),
        deferred_sink=bus.publish_inbound,
        delivery_tracker=delivery_tracker,
    )
    partial.own(intervention.close)
    session_manager = SessionManager(paths.sessions_dir)
    partial.own(session_manager.close)
    from nanocat.agent.runtime_files import RuntimeFileStore

    runtime_file_config = config.runtime_files
    runtime_files = RuntimeFileStore(
        paths.workspace,
        runtime_dir=paths.runtime_dir,
        max_file_bytes=runtime_file_config.max_file_bytes,
        max_session_bytes=runtime_file_config.max_session_bytes,
        max_total_bytes=runtime_file_config.max_total_bytes,
    )
    partial.own(runtime_files.close)
    activity_journal = ActivityJournal(paths.activity_dir)
    partial.own(activity_journal.close)
    cron = CronService(paths.cron_dir / "jobs.json")
    partial.own(cron.close)
    provider_resolver = RuntimeProviderResolver(config)
    partial.own(provider_resolver.close)
    vision_fallback = VisionFallbackService(
        provider_resolver,
        config,
        workspace=config.workspace_path,
    )
    partial.own(vision_fallback.close)

    agent_engine = AgentLoop(
        bus=bus,
        config=config,
        session_manager=session_manager,
        cron_service=cron,
        intervention_broker=intervention,
        provider_resolver=provider_resolver,
        vision_fallback=vision_fallback,
        runtime_files=runtime_files,
        configuration=configuration,
    )
    agent = AgentService(agent_engine)
    partial.own(agent.close)
    system_turns = SystemTurnGateway(agent, bus)

    from nanocat.application.command_dispatcher import CommandDispatcher
    from nanocat.application.control import ApplicationControlService

    control = ApplicationControlService(
        engine=agent_engine,
        config=config,
        session_manager=session_manager,
        intervention=intervention,
        supervisor=None,  # bound after the supervisor is constructed below
        activity_journal=activity_journal,
        configuration=configuration,
    )
    command_dispatcher = CommandDispatcher(agent_engine, bus)
    agent_engine.set_command_dispatcher(command_dispatcher)

    async def on_cron_job(job: CronJob) -> str | None:
        from nanocat.agent.tools.cron import CronTool
        from nanocat.utils.evaluator import evaluate_response

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            request = SystemTurnRequest(
                source="cron",
                content=reminder_note,
                conversation=ConversationRef(
                    channel=job.payload.channel or "system",
                    chat_id=job.payload.to or "direct",
                    session_key=f"cron:{job.id}",
                ),
                principal_id=job.payload.principal_id or job.payload.to or "user",
            )
            response = await system_turns.submit(request)
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        notify_mode = job.payload.notify_mode
        if notify_mode != "never" and job.payload.to and response:
            if notify_mode == "always":
                should_notify = True
            elif notify_mode == "smart":
                should_notify = await evaluate_response(
                    response,
                    job.payload.message,
                    provider_resolver=agent.provider_resolver,
                    config=config,
                )
            else:
                should_notify = False

            if should_notify:
                await system_turns.deliver(
                    request,
                    response,
                )
        return response

    cron.on_job = on_cron_job
    channels = ChannelManager(
        config,
        bus,
        delivery_sink=delivery_tracker.resolve,
        delivery_guard=delivery_tracker.is_pending,
        descriptors=channel_descriptors,
    )
    partial.own(channels.stop_all)
    logger.info("NanoCat v{} configured — workspace: {}", __version__, config.workspace_path)

    def pick_heartbeat_target() -> HeartbeatTarget:
        configured_principal = config.heartbeat.principal_id
        enabled = set(channels.enabled_channels)
        candidates: list[tuple[str, str, str, str | None]] = []
        for ch in sorted(enabled):
            sessions = session_manager.list_sessions(ch, min_turns=1, limit=1)
            if sessions:
                item = sessions[0]
                chat_id = item.get("chat_id", "direct")
                session = session_manager.get_session(ch, item.get("id", ""))
                principal_id = (
                    session.metadata.get("_last_principal_id") if session is not None else None
                )
                candidates.append((str(item.get("last_active", "")), ch, chat_id, principal_id))
        if candidates:
            _, channel, chat_id, session_principal = max(
                candidates,
                key=lambda item: (item[0], item[1], item[2]),
            )
            return HeartbeatTarget(
                channel=channel,
                chat_id=chat_id,
                principal_id=configured_principal or session_principal or chat_id,
            )
        return HeartbeatTarget(channel="system", chat_id="direct", principal_id="system")

    heartbeat_target: HeartbeatTarget | None = None

    async def on_heartbeat_execute(tasks: str) -> str:
        nonlocal heartbeat_target
        heartbeat_target = pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            return None

        return await system_turns.submit(
            SystemTurnRequest(
                source="heartbeat",
                content=tasks,
                conversation=ConversationRef(
                    channel=heartbeat_target.channel,
                    chat_id=heartbeat_target.chat_id,
                    session_key="heartbeat",
                ),
                principal_id=heartbeat_target.principal_id,
            ),
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        target = heartbeat_target or pick_heartbeat_target()
        if target.channel == "system":
            return
        await system_turns.deliver(
            SystemTurnRequest(
                source="heartbeat",
                content="",
                conversation=ConversationRef(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    session_key="heartbeat",
                ),
                principal_id=target.principal_id,
            ),
            response,
        )

    heartbeat = HeartbeatService(
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        provider_resolver=agent.provider_resolver,
        config=config,
    )
    partial.own(heartbeat.close)

    web_channel = channels.get_channel("web")
    api_runtime = ApiRuntime(
        control=control,
        config=config,
        configuration=configuration,
        web_channel=web_channel,  # type: ignore[arg-type]
        activity_journal=activity_journal,
        static_dir=static_dir,
        endpoint_file=paths.runtime_dir / "http-endpoints.json",
    )
    partial.own(api_runtime.close)

    async def apply_runtime_configuration(
        current: Config,
        changed_paths: tuple[str, ...],
    ) -> None:
        await agent_engine.apply_configuration(current, changed_paths)
        if any(path.startswith("providers.") for path in changed_paths):
            await provider_resolver.reconfigure()
        if any(path.startswith("channels.") for path in changed_paths):
            channels.apply_live_config(current)
            await channels.reconfigure(current, changed_paths)
        if any(path.startswith("transcription.") for path in changed_paths):
            channels.apply_transcription_config(current)
        if any(path.startswith("heartbeat.") for path in changed_paths):
            await heartbeat.apply_config()
        await api_runtime.apply_configuration(current, changed_paths)

    configuration.set_runtime_applier(apply_runtime_configuration)

    runtime = RuntimeContext(
        config=config,
        bus=bus,
        session_manager=session_manager,
        cron=cron,
        agent=agent,
        channels=channels,
        heartbeat=heartbeat,
        system_turns=system_turns,
        runtime_files=runtime_files,
        activity_journal=activity_journal,
        api_runtime=api_runtime,
        control=control,
        paths=paths,
        config_snapshot=config_snapshot,
        log_sink_id=log_sink_id,
        intervention=intervention,
        command_dispatcher=command_dispatcher,
        configuration=configuration,
    )
    return runtime


def build_runtime(
    *,
    workdir: str | None = None,
    verbose: bool = False,
) -> RuntimeContext:
    """Construct one exclusively-owned runtime for a workdir."""
    data_dir = Path(workdir).expanduser().resolve() if workdir else Path.cwd().resolve()
    instance_lock = RuntimeInstanceLock(data_dir / ".nanocat.lock")
    instance_lock.acquire()
    partial = _PartialBuild()
    try:
        runtime = _compose_runtime(
            workdir=workdir,
            verbose=verbose,
            partial=partial,
        )
        runtime.instance_lock = instance_lock
        runtime.supervisor = RuntimeSupervisor(runtime)
        runtime.agent.set_runtime_supervisor(runtime.supervisor)
        runtime.control.set_supervisor(runtime.supervisor)
    except BaseException:
        try:
            _finish_partial_cleanup(partial)
        except BaseException:
            logger.exception("Partial runtime cleanup could not be completed")
        try:
            instance_lock.close()
        except BaseException:
            logger.exception("Runtime instance lock cleanup could not be completed")
        raise
    partial.release()
    return runtime


async def run_service_async(
    *,
    workdir: str | None = None,
    verbose: bool = False,
) -> None:
    """Run NanoCat channel and HTTP services until interrupted."""
    runtime = build_runtime(workdir=workdir, verbose=verbose)
    logger.info("Starting NanoCat runtime v{}", __version__)
    if runtime.channels.enabled_channels:
        logger.info("Channels enabled: {}", ", ".join(runtime.channels.enabled_channels))
    else:
        logger.warning("No channels enabled")

    supervisor = runtime.supervisor or RuntimeSupervisor(runtime)
    runtime.supervisor = supervisor
    loop = asyncio.get_running_loop()
    loop_handlers: list[signal.Signals] = []
    fallback_handlers: dict[signal.Signals, Any] = {}

    def request_shutdown(received: signal.Signals) -> None:
        asyncio.create_task(
            supervisor.stop(
                ShutdownReason(kind="signal", detail=f"received {received.name}")
            ),
            name="nanocat.signal-stop",
        )

    for received in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(received, request_shutdown, received)
            loop_handlers.append(received)
        except (NotImplementedError, RuntimeError):
            try:
                previous = signal.getsignal(received)

                def fallback_handler(
                    _signum: int,
                    _frame: Any,
                    received_signal: signal.Signals = received,
                ) -> None:
                    loop.call_soon_threadsafe(request_shutdown, received_signal)

                signal.signal(received, fallback_handler)
                fallback_handlers[received] = previous
            except (OSError, ValueError):
                logger.debug("Signal handler unavailable for {}", received.name)
    try:
        await supervisor.run()
    finally:
        try:
            await supervisor.stop(ShutdownReason(kind="signal", detail="service exited"))
        finally:
            for received in loop_handlers:
                loop.remove_signal_handler(received)
            for received, previous in fallback_handlers.items():
                try:
                    signal.signal(received, previous)
                except (OSError, ValueError):
                    pass


def run_service(
    *,
    workdir: str | None = None,
    verbose: bool = False,
) -> None:
    """Synchronous channel-service entrypoint."""
    try:
        asyncio.run(run_service_async(workdir=workdir, verbose=verbose))
    except KeyboardInterrupt:
        logger.info("Shutting down runtime")


def main() -> None:
    """Start NanoCat in channel-service mode."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="nanocat",
        description="NanoCat — personal AI agent service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The working directory (-w, default: current directory) anchors all\n"
            "runtime state: config.json, workspace/, sessions/, activity/, cron/ and logs/.\n\n"
            "examples:\n"
            "  nanocat\n"
            "  nanocat -w ~/.nanocat"
        ),
    )
    parser.add_argument(
        "-w",
        "--workdir",
        help="Working directory holding config.json and all runtime state "
        "(default: current directory).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    args = parser.parse_args()

    if args.workdir is None:
        repo_root = Path(__file__).resolve().parents[2]
        if Path.cwd().resolve() == repo_root:
            args.workdir = str(repo_root / "data")

    run_service(
        workdir=args.workdir,
        verbose=args.verbose,
    )
