"""Non-CLI runtime launcher."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from nanocat import __version__
from nanocat.agent.loop import AgentLoop
from nanocat.application.agent_service import AgentService
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
from nanocat.runtime.context import ConfigSnapshot, RuntimeContext
from nanocat.runtime.paths import RuntimePaths
from nanocat.runtime.supervisor import RuntimeSupervisor
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


def configure_logging(verbose: bool = False, local_mode: bool = False) -> int:
    """Set up loguru sinks. INFO by default; DEBUG with ``--verbose``.

    The level is exported as ``NANOCAT_LOG_LEVEL`` so the TUI pane sink (built
    later, inside the channel) picks the same level. In gateway mode a stderr
    sink is installed; in local (TUI) mode Textual owns the screen, so stderr is
    left off and the TUI adds its own pane sink. A rotating file sink under
    ``<workdir>/logs`` is installed in both modes.
    """
    level = "DEBUG" if verbose else "INFO"
    os.environ["NANOCAT_LOG_LEVEL"] = level

    logger.remove()  # drop loguru's default (DEBUG) stderr sink
    if not local_mode:
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


def build_runtime(
    *,
    workdir: str | None = None,
    verbose: bool = False,
    local_mode: bool = False,
) -> RuntimeContext:
    """Construct the runtime services needed to run the gateway.

    *workdir* anchors all runtime state (see :func:`load_runtime_config`). When
    *local_mode* is set, only the ``tui`` channel is started and all network
    channels stay disabled regardless of config.
    """
    config = load_runtime_config(workdir)  # sets the config path before paths derive
    paths = RuntimePaths.from_config_path(get_config_path(), workspace=config.workspace_path)
    config_snapshot = ConfigSnapshot(config=config, paths=paths)
    log_sink_id = configure_logging(verbose=verbose, local_mode=local_mode)
    sync_workspace_templates(config.workspace_path, silent=True)

    bus = MessageBus()
    delivery_tracker = DeliveryTracker()
    intervention = InterventionBroker(
        make_bus_presenter(bus, delivery_tracker),
        deferred_sink=bus.publish_inbound,
        delivery_tracker=delivery_tracker,
    )
    session_manager = SessionManager(paths.sessions_dir)
    cron = CronService(paths.cron_dir / "jobs.json")
    provider_resolver = RuntimeProviderResolver(config)
    vision_fallback = VisionFallbackService(
        provider_resolver,
        config,
        workspace=config.workspace_path,
    )

    agent_engine = AgentLoop(
        bus=bus,
        config=config,
        session_manager=session_manager,
        cron_service=cron,
        intervention_broker=intervention,
        provider_resolver=provider_resolver,
        vision_fallback=vision_fallback,
    )
    agent = AgentService(agent_engine)
    system_turns = SystemTurnGateway(agent, bus)

    from nanocat.application.command_dispatcher import CommandDispatcher
    from nanocat.application.control import ApplicationControlService

    control = ApplicationControlService(
        engine=agent_engine,
        config=config,
        session_manager=session_manager,
        intervention=intervention,
        supervisor=None,  # bound after the supervisor is constructed below
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
        force_channel="tui" if local_mode else None,
        delivery_sink=delivery_tracker.resolve,
        delivery_guard=delivery_tracker.is_pending,
    )
    logger.info("NanoCat v{} ready — workspace: {}", __version__, config.workspace_path)

    def pick_heartbeat_target() -> HeartbeatTarget:
        configured_principal = config.gateway.heartbeat.principal_id
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

    runtime = RuntimeContext(
        config=config,
        bus=bus,
        session_manager=session_manager,
        cron=cron,
        agent=agent,
        channels=channels,
        heartbeat=heartbeat,
        system_turns=system_turns,
        paths=paths,
        config_snapshot=config_snapshot,
        log_sink_id=log_sink_id,
        intervention=intervention,
        command_dispatcher=command_dispatcher,
    )
    runtime.supervisor = RuntimeSupervisor(runtime)
    agent.set_runtime_supervisor(runtime.supervisor)
    control.set_supervisor(runtime.supervisor)
    tui_channel = channels.get_channel("tui")
    bind_control = getattr(tui_channel, "bind_control", None)
    if callable(bind_control):
        bind_control(control)
    return runtime


async def run_gateway_async(
    *,
    workdir: str | None = None,
    verbose: bool = False,
) -> None:
    """Run the gateway services until interrupted."""
    runtime = build_runtime(workdir=workdir, verbose=verbose)
    logger.info("Starting NanoCat runtime v{}", __version__)
    if runtime.channels.enabled_channels:
        logger.info("Channels enabled: {}", ", ".join(runtime.channels.enabled_channels))
    else:
        logger.warning("No channels enabled")

    supervisor = runtime.supervisor or RuntimeSupervisor(runtime)
    runtime.supervisor = supervisor
    try:
        await supervisor.run()
    finally:
        await supervisor.stop(ShutdownReason(kind="signal", detail="gateway exited"))


async def _shutdown_runtime(runtime: RuntimeContext) -> None:
    """Tear down runtime services through the single supervisor owner."""
    supervisor = runtime.supervisor or RuntimeSupervisor(runtime)
    runtime.supervisor = supervisor
    await supervisor.stop(ShutdownReason(kind="manual", detail="local TUI closed"))


def run_local_tui(
    *,
    workdir: str | None = None,
    verbose: bool = False,
) -> None:
    """Run the local TUI: runtime on a background loop, Textual UI on main thread.

    Decoupling the loops keeps the agent's synchronous work from starving the
    UI compositor (which otherwise freezes the screen until a turn completes).
    """
    import threading

    runtime = build_runtime(workdir=workdir, verbose=verbose, local_mode=True)
    tui = runtime.channels.get_channel("tui")
    if tui is None or not hasattr(tui, "run_ui"):
        raise SystemExit("Error: TUI channel unavailable (is 'textual' installed?)")

    # Preload the local session transcript so it renders on startup.
    try:
        session = runtime.session_manager.get_or_create("tui", "local")
        history = session.get_history()
        if history:
            logger.info("Restoring {} prior message(s)…", len(history))
        tui.preload_history(history)  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning("Could not preload TUI session history: {}", e)

    loop = asyncio.new_event_loop()
    started = threading.Event()

    async def _serve() -> None:
        supervisor = runtime.supervisor or RuntimeSupervisor(runtime)
        runtime.supervisor = supervisor
        await supervisor.run()

    def _runtime_thread() -> None:
        asyncio.set_event_loop(loop)
        loop.create_task(_serve())
        started.set()
        loop.run_forever()
        loop.close()

    worker = threading.Thread(target=_runtime_thread, name="nanocat-runtime", daemon=True)
    worker.start()
    started.wait()
    tui.bind_runtime_loop(loop)  # type: ignore[attr-defined]

    logger.info("Starting NanoCat runtime v{} (local TUI)", __version__)

    try:
        tui.run_ui()  # type: ignore[attr-defined]  # blocks until the user quits
    except KeyboardInterrupt:
        pass
    finally:
        try:
            asyncio.run_coroutine_threadsafe(_shutdown_runtime(runtime), loop).result(timeout=10)
        except Exception as e:
            logger.warning("Local TUI shutdown error: {}", e)
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)


def run_gateway(
    *,
    workdir: str | None = None,
    verbose: bool = False,
    local_mode: bool = False,
) -> None:
    """Synchronous wrapper for gateway runtime."""
    if local_mode:
        run_local_tui(workdir=workdir, verbose=verbose)
        return
    try:
        asyncio.run(run_gateway_async(workdir=workdir, verbose=verbose))
    except KeyboardInterrupt:
        logger.info("Shutting down runtime")


def main() -> None:
    """Module/script entrypoint. A start mode (``gateway`` or ``tui``) is required."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="nanocat",
        description="NanoCat — ultra-lightweight personal AI assistant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "modes:\n"
            "  gateway    Run the network channels (Telegram, Slack, …) defined in config.\n"
            "  tui        Local split-screen terminal UI; all network channels disabled.\n\n"
            "The working directory (-w, default: current directory) anchors all\n"
            "runtime state: config.json, workspace/, sessions/, cron/ and logs/.\n\n"
            "examples:\n"
            "  nanocat gateway\n"
            "  nanocat tui -w ~/.nanocat"
        ),
    )
    parser.add_argument(
        "mode",
        choices=["gateway", "tui"],
        help="Start mode: 'gateway' (network channels) or 'tui' (local terminal UI).",
    )
    parser.add_argument(
        "-w",
        "--workdir",
        help="Working directory holding config.json and all runtime state "
        "(default: current directory).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")

    # Zero args: show full help instead of an argparse usage error.
    if len(sys.argv) == 1:
        parser.print_help()
        return

    args = parser.parse_args()

    if args.workdir is None:
        repo_root = Path(__file__).resolve().parents[2]
        if Path.cwd().resolve() == repo_root:
            args.workdir = str(repo_root / "data")

    run_gateway(
        workdir=args.workdir,
        verbose=args.verbose,
        local_mode=args.mode == "tui",
    )
