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
from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.manager import ChannelManager
from nanocat.config.loader import get_config_path, load_config, set_config_path
from nanocat.config.paths import get_cron_dir, get_sessions_dir
from nanocat.config.schema import Config
from nanocat.cron.service import CronService
from nanocat.cron.types import CronJob
from nanocat.heartbeat.service import HeartbeatService
from nanocat.session.manager import SessionManager
from nanocat.utils.helpers import sync_workspace_templates


@dataclass
class RuntimeContext:
    config: Config
    bus: MessageBus
    session_manager: SessionManager
    cron: CronService
    agent: AgentLoop
    channels: ChannelManager
    heartbeat: HeartbeatService


def load_runtime_config(workdir: str | None = None) -> Config:
    """Load config from the working directory.

    *workdir* is the single anchor for all runtime state: ``config.json``,
    ``workspace/``, ``sessions/``, ``cron/`` and ``logs/`` all live under it.
    Defaults to the current working directory when not given.
    """
    if workdir:
        wd = Path(workdir).expanduser().resolve()
        wd.mkdir(parents=True, exist_ok=True)
        set_config_path(wd / "config.json")

    return load_config(get_config_path())


def configure_logging(verbose: bool = False, local_mode: bool = False) -> None:
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
    logger.add(
        logs_dir / "runtime.log",
        rotation="5 MB",
        retention=5,
        level=level,
        enqueue=True,
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{line} - {message}",
    )


def warn_deprecated_memory_window(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        logger.warning(
            "Deprecated 'memoryWindow' detected without 'contextWindowTokens'; "
            "memoryWindow is ignored. Refresh your config template if needed."
        )


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
    configure_logging(verbose=verbose, local_mode=local_mode)
    warn_deprecated_memory_window(config)
    sync_workspace_templates(config.workspace_path, silent=True)

    bus = MessageBus()
    session_manager = SessionManager(get_sessions_dir())
    cron = CronService(get_cron_dir() / "jobs.json")

    agent = AgentLoop(
        bus=bus,
        config=config,
        session_manager=session_manager,
        cron_service=cron,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        from nanocat.agent.tools.cron import CronTool
        from nanocat.agent.tools.message import MessageTool
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
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "system",
                chat_id=job.payload.to or "direct",
                transient=True,
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        notify_mode = job.payload.notify_mode
        if notify_mode != "never" and job.payload.to and response:
            if notify_mode == "always":
                should_notify = True
            elif notify_mode == "smart":
                should_notify = await evaluate_response(response, job.payload.message)
            else:
                should_notify = False

            if should_notify:
                await bus.publish_outbound(
                    OutboundMessage(
                        channel=job.payload.channel or "system",
                        chat_id=job.payload.to,
                        content=response,
                    )
                )
        return response

    cron.on_job = on_cron_job
    channels = ChannelManager(config, bus, force_channel="tui" if local_mode else None)

    def pick_heartbeat_target() -> tuple[str, str]:
        enabled = set(channels.enabled_channels)
        for ch in enabled:
            sessions = session_manager.list_sessions(ch, min_turns=1, limit=1)
            if sessions:
                item = sessions[0]
                return ch, item.get("chat_id", "direct")
        return "system", "direct"

    async def on_heartbeat_execute(tasks: str) -> str:
        channel, chat_id = pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            return None

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
            transient=True,
        )

    async def on_heartbeat_notify(response: str) -> None:
        channel, chat_id = pick_heartbeat_target()
        if channel == "system":
            return
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=response)
        )

    heartbeat = HeartbeatService(
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
    )

    return RuntimeContext(
        config=config,
        bus=bus,
        session_manager=session_manager,
        cron=cron,
        agent=agent,
        channels=channels,
        heartbeat=heartbeat,
    )


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

    try:
        await runtime.cron.start()
        await runtime.heartbeat.start()
        await asyncio.gather(runtime.agent.run(), runtime.channels.start_all())
    finally:
        await runtime.agent.close_mcp()
        runtime.heartbeat.stop()
        runtime.cron.stop()
        runtime.agent.stop()
        await runtime.channels.stop_all()


async def _shutdown_runtime(runtime: RuntimeContext) -> None:
    """Tear down runtime services (mirrors run_gateway_async's finally block)."""
    await runtime.agent.close_mcp()
    runtime.heartbeat.stop()
    runtime.cron.stop()
    runtime.agent.stop()
    await runtime.channels.stop_all()


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
        tui.preload_history(session.get_history())  # type: ignore[attr-defined]
    except Exception as e:
        logger.warning("Could not preload TUI session history: {}", e)

    loop = asyncio.new_event_loop()
    started = threading.Event()

    async def _serve() -> None:
        await runtime.cron.start()
        await runtime.heartbeat.start()
        await asyncio.gather(runtime.agent.run(), runtime.channels.start_all())

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

    run_gateway(
        workdir=args.workdir,
        verbose=args.verbose,
        local_mode=args.mode == "tui",
    )
