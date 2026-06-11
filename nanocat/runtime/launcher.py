"""Non-CLI runtime launcher."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from nanocat import __version__
from nanocat.agent.loop import AgentLoop
from nanocat.bus.events import OutboundMessage
from nanocat.bus.queue import MessageBus
from nanocat.channels.manager import ChannelManager
from nanocat.config.loader import load_config, set_config_path
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


def load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        set_config_path(config_path)

    loaded = load_config(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def warn_deprecated_memory_window(config: Config) -> None:
    """Warn when running with old memoryWindow-only config."""
    if config.agents.defaults.should_warn_deprecated_memory_window:
        logger.warning(
            "Deprecated 'memoryWindow' detected without 'contextWindowTokens'; "
            "memoryWindow is ignored. Refresh your config template if needed."
        )


def build_runtime(
    *,
    config_path: str | None = None,
    workspace: str | None = None,
    verbose: bool = False,
) -> RuntimeContext:
    """Construct the runtime services needed to run the gateway."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    config = load_runtime_config(config_path, workspace)
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
    channels = ChannelManager(config, bus)

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
    config_path: str | None = None,
    workspace: str | None = None,
    verbose: bool = False,
) -> None:
    """Run the gateway services until interrupted."""
    runtime = build_runtime(config_path=config_path, workspace=workspace, verbose=verbose)
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


def run_gateway(
    *,
    config_path: str | None = None,
    workspace: str | None = None,
    verbose: bool = False,
) -> None:
    """Synchronous wrapper for gateway runtime."""
    try:
        asyncio.run(
            run_gateway_async(config_path=config_path, workspace=workspace, verbose=verbose)
        )
    except KeyboardInterrupt:
        logger.info("Shutting down runtime")


def main() -> None:
    """Default module/script entrypoint."""
    run_gateway()
