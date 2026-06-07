"""Non-CLI runtime launcher."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot import __version__
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.loader import load_config, set_config_path
from nanobot.config.paths import get_cron_dir
from nanobot.config.schema import Config
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob
from nanobot.heartbeat.service import HeartbeatService
from nanobot.session.manager import SessionManager
from nanobot.utils.helpers import sync_workspace_templates


@dataclass
class RuntimeContext:
    config: Config
    bus: MessageBus
    provider: Any
    session_manager: SessionManager
    cron: CronService
    agent: AgentLoop
    channels: ChannelManager
    heartbeat: HeartbeatService


def make_provider(config: Config, override_model: str | None = None):
    """Create the appropriate LLM provider from config."""
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.base import GenerationSettings
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = override_model or config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    provider_cfg = config.get_provider(model)

    if provider_name == "deepseek":
        from nanobot.providers.deepseek_provider import DeepSeekProvider

        provider = DeepSeekProvider(
            api_key=provider_cfg.api_key if provider_cfg else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
        )
    elif provider_name == "openai_codex" or model.startswith("openai-codex/"):
        provider = OpenAICodexProvider(default_model=model)
    elif provider_name == "custom":
        from nanobot.providers.custom_provider import CustomProvider

        provider = CustomProvider(
            api_key=provider_cfg.api_key if provider_cfg else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
        )
    elif provider_name == "azure_openai":
        if not provider_cfg or not provider_cfg.api_key or not provider_cfg.api_base:
            raise RuntimeError(
                "Azure OpenAI requires api_key and api_base in providers.azure_openai."
            )
        provider = AzureOpenAIProvider(
            api_key=provider_cfg.api_key,
            api_base=provider_cfg.api_base,
            default_model=model,
        )
    else:
        from nanobot.providers.litellm_provider import LiteLLMProvider
        from nanobot.providers.registry import find_by_name

        spec = find_by_name(provider_name)
        if (
            not model.startswith("bedrock/")
            and not (provider_cfg and provider_cfg.api_key)
            and not (spec and (spec.is_oauth or spec.is_local))
        ):
            raise RuntimeError(
                f"No API key configured for provider '{provider_name}'. "
                "Set it in ~/.nanobot/config.json under providers."
            )
        provider = LiteLLMProvider(
            api_key=provider_cfg.api_key if provider_cfg else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=provider_cfg.extra_headers if provider_cfg else None,
            provider_name=provider_name,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )
    return provider


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
    provider = make_provider(config)
    session_manager = SessionManager(config.workspace_path)
    cron = CronService(get_cron_dir() / "jobs.json")

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        assistant_model=config.agents.defaults.assistant_model,
        subagent_model=config.agents.defaults.subagent_model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        context_window_tokens=config.agents.defaults.context_window_tokens,
        web_search_config=config.tools.web.search,
        web_proxy=config.tools.web.proxy or None,
        web_safety_check=config.tools.web.safety_check,
        exec_config=config.tools.exec,
        cron_service=cron,
        filesystem_config=config.tools.filesystem,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        tips_config=config.tips,
        memory_config=config.memory,
    )

    async def on_cron_job(job: CronJob) -> str | None:
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool
        from nanobot.utils.evaluator import evaluate_response

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
                should_notify = await evaluate_response(
                    response,
                    job.payload.message,
                    provider,
                    agent.assistant_model,
                )
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
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
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
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.assistant_model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    return RuntimeContext(
        config=config,
        bus=bus,
        provider=provider,
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
    logger.info("Starting nanobot runtime v{}", __version__)
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
        asyncio.run(run_gateway_async(config_path=config_path, workspace=workspace, verbose=verbose))
    except KeyboardInterrupt:
        logger.info("Shutting down runtime")


def main() -> None:
    """Default module/script entrypoint."""
    run_gateway()
