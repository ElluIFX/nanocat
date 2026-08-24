"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ApiConfig(Base):
    """Public HTTP API listener and bearer authentication configuration."""

    enabled: bool = False
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=18791, ge=0, le=65535)
    auth_token: str | None = Field(default=None, min_length=16)
    cors_origins: list[str] = Field(default_factory=list)


class WebChannelConfig(Base):
    """Local single-user web channel configuration."""

    enabled: bool = True
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=18790, ge=0, le=65535)
    password: str | None = Field(default=None, min_length=1)
    trusted_proxies: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=lambda: ["web:local"])


class ChannelsConfig(Base):
    """Configuration for built-in and plugin chat channels."""

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True
    send_tool_hints: bool = False
    outbound_max_attempts: int = 3
    outbound_retry_delay_s: float = 0.25
    web: WebChannelConfig = Field(default_factory=WebChannelConfig)


class AgentDefaults(Base):
    """Default agent configuration."""

    model_config = ConfigDict(extra="ignore")

    model: str = "openai/gpt-4o"
    reasoning_effort: str | None = None  # low / medium / high / xhigh / max
    assistant_model: str | None = (
        None  # lightweight model for auxiliary tasks (memory, evaluate, heartbeat); None = use model
    )
    subagent_model: str | None = (
        None  # model for subagents (spawn / gather); None → assistant_model → model
    )
    vision_model: str | None = (
        None  # global vision fallback model; None → assistant_model
    )
    compaction_model: str | None = None  # independent low-cost model for session compaction
    # Saved model catalog used by /model and rich clients.
    model_choice: list[str] = Field(default_factory=lambda: ["openai/gpt-4o"])
    max_tokens: int | None = 8192
    context_window_tokens: int = 65_536
    temperature: float | None = None
    max_tool_iterations: int = 40
    compaction_enabled: bool = True
    compaction_threshold: float = Field(
        default=0.5,
        ge=0.05,
        le=0.95,
        description="Trigger session compaction at this fraction of the context window.",
    )
    no_compact_history_num: int = Field(
        default=3,
        ge=1,
        le=1000,
        description="Keep this many recent completed turns uncompressed.",
    )
    pulse_enabled: bool = False


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class RuntimeLimitsConfig(Base):
    """Runtime-wide concurrency limits shared by interactive and tool work."""

    max_concurrent_turns: int = Field(default=8, gt=0)
    max_concurrent_tool_calls: int = Field(default=16, gt=0)


class RuntimeFilesConfig(Base):
    """Limits for runtime-owned, agent-readable workspace files."""

    max_file_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    max_session_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    max_total_bytes: int = Field(default=2 * 1024 * 1024 * 1024, gt=0)

    @model_validator(mode="after")
    def validate_limit_order(self) -> "RuntimeFilesConfig":
        """Require each wider quota to contain the narrower quota."""
        if self.max_file_bytes > self.max_session_bytes:
            raise ValueError("maxFileBytes cannot exceed maxSessionBytes")
        if self.max_session_bytes > self.max_total_bytes:
            raise ValueError("maxSessionBytes cannot exceed maxTotalBytes")
        return self


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(
        default_factory=ProviderConfig
    )  # VolcEngine Coding Plan
    byteplus: ProviderConfig = Field(
        default_factory=ProviderConfig
    )  # BytePlus (VolcEngine international)
    byteplus_coding_plan: ProviderConfig = Field(
        default_factory=ProviderConfig
    )  # BytePlus Coding Plan
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)

    @model_validator(mode="after")
    def validate_registry_parity(self) -> "ProvidersConfig":
        """Keep provider configuration fields identical to the runtime registry."""
        from nanocat.providers.registry import PROVIDERS

        schema_names = set(type(self).model_fields)
        registry_names = {spec.name for spec in PROVIDERS}
        if schema_names != registry_names:
            raise ValueError("ProvidersConfig and provider registry are out of sync")
        return self


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes
    principal_id: str | None = None  # Optional owner used for scheduler-originated interventions


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class CmdToolConfig(Base):
    """Command tool configuration, shared by the exec and proc tools."""

    timeout: int = 60  # exec only (proc is long-lived)
    path_append: list[str] = Field(default_factory=list)  # dirs appended to exec/proc child PATH
    env: dict[str, str] = Field(default_factory=dict)  # extra env injected into exec/proc children


class FilesystemToolConfig(Base):
    """Filesystem tool configuration."""

    force_del_to_trash: bool = True  # delete tool only trashes; hides the permanent option


class ToolsPolicyConfig(Base):
    """Runtime-wide tool security policy configuration."""

    safty_check: bool = True
    safty_safe_tool: list[str] = Field(default_factory=list)
    safty_allow_regex: list[str] = Field(default_factory=list)
    safty_deny_regex: list[str] = Field(default_factory=list)
    auto_approve_mode: bool = False
    restrict_path_to_workspace: bool = False
    restrict_url_outside_local: bool = False


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    tool_timeout: int = 30  # seconds before a tool call is cancelled
    enabled_tools: list[str] = Field(
        default_factory=lambda: ["*"]
    )  # Only register these tools; accepts raw MCP names or wrapped mcp_<server>_<tool> names; ["*"] = all tools; [] = no tools


class EnabledBuiltinToolsConfig(Base):
    """Per-tool registration switches. Each field gates whether that built-in tool
    is registered; all default enabled except ssh, which is opt-in."""

    file_tools: bool = True
    image_tools: bool = True  # Image tools (read/parse)
    screenshot: bool = False
    delete: bool = True
    exec: bool = True
    web_search: bool = True
    web_fetch: bool = True
    wait: bool = True
    ask: bool = (
        True  # ask tool: pause mid-turn to block for a user reply (needs an interactive channel)
    )
    todo: bool = True
    subagent_tools: bool = True
    ssh_tools: bool = False  # SSH session and file transfer tools
    proc_tools: bool = True  # background process tools (proc_start/read/stop/list)
    http_request: bool = True  # structured HTTP request tool
    memory_tools: bool = True  # Nowledge memory and captured Thread tools


class ToolsConfig(Base):
    """Tools configuration."""

    max_return_chars: int = 10000  # Truncate tool results exceeding this; 0 = no limit
    policy: ToolsPolicyConfig = Field(default_factory=ToolsPolicyConfig)
    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    cmd: CmdToolConfig = Field(default_factory=CmdToolConfig)
    filesystem: FilesystemToolConfig = Field(default_factory=FilesystemToolConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    enabled_builtin_tools: EnabledBuiltinToolsConfig = Field(
        default_factory=EnabledBuiltinToolsConfig
    )


class WhisperTranscriptionConfig(Base):
    """Whisper API endpoint configuration."""

    api_key: str = ""
    api_url: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    model: str = "whisper-large-v3"


class TranscriptionConfig(Base):
    """Voice transcription configuration."""

    enabled: bool = False
    whisper: WhisperTranscriptionConfig = Field(default_factory=WhisperTranscriptionConfig)


class NowledgeAutoInjectConfig(Base):
    """Automatic Nowledge retrieval and context injection policy."""

    enabled: bool = False
    mode: Literal["auto", "fast", "deep"] = "auto"
    max_num: int = Field(default=3, ge=1, le=20)
    preview_length: int = Field(default=512, ge=128, le=4096)
    deep_on_recall: bool = True
    min_score: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="Low recall guardrail; Nowledge ranking remains the primary signal.",
    )
    query_min_length: int = Field(default=4, ge=0, le=256)
    dedupe_window: int = Field(default=32, ge=0, le=256)
    short_recall_max_length: int = Field(default=24, ge=0, le=512)
    recall_context_messages: int = Field(default=2, ge=0, le=10)
    query_max_length: int = Field(default=2_000, ge=256, le=10_000)


class MemoryConfig(Base):
    """Nowledge Mem connection and retrieval configuration."""

    enabled: bool = False
    api_url: str = "http://127.0.0.1:14242"
    api_key: str | None = None
    thread_source: str = "nanocat"
    space_id: str | None = None
    thread_capture_enabled: bool = True
    thread_message_max_chars: int = Field(default=12_000, ge=512, le=100_000)
    auto_distill_enabled: bool = True
    distill_min_messages: int = Field(default=8, ge=1, le=1000)
    distill_extraction_level: Literal["swift", "guided", "expert"] = "guided"
    distill_preferred_language: str = "zh"
    working_memory_enabled: bool = True
    working_memory_timeout_s: float = Field(default=3.0, gt=0.0, le=30.0)
    working_memory_max_chars: int = Field(default=6_000, ge=512, le=50_000)
    request_timeout_s: float = Field(default=15.0, gt=0.0, le=120.0)
    max_request_attempts: int = Field(default=2, ge=1, le=5)
    retry_delay_s: float = Field(default=0.1, ge=0.0, le=10.0)
    health_timeout_s: float = Field(default=2.0, gt=0.0, le=30.0)
    health_cache_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    max_connections: int = Field(default=20, ge=1, le=500)
    max_keepalive_connections: int = Field(default=10, ge=0, le=500)
    auto_inject: NowledgeAutoInjectConfig = Field(default_factory=NowledgeAutoInjectConfig)


class Config(BaseSettings):
    """Root configuration for NanoCat."""

    schema_version: Literal[2] = Field(default=2, alias="schemaVersion")
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    runtime: RuntimeLimitsConfig = Field(default_factory=RuntimeLimitsConfig)
    runtime_files: RuntimeFilesConfig = Field(
        default_factory=RuntimeFilesConfig,
        alias="runtimeFiles",
    )
    api: ApiConfig = Field(default_factory=ApiConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @property
    def workspace_path(self) -> Path:
        """The agent workspace, always ``<workdir>/workspace``.

        The working directory (the config file's parent) is the single anchor
        for all runtime state, so the workspace cannot be relocated independently
        via config — pointing ``-w`` somewhere relocates everything together.
        """
        from nanocat.config.loader import get_config_path

        return get_config_path().parent / "workspace"

    def _match_provider(
        self, model: str | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name from the model identifier alone."""
        from nanocat.providers.registry import PROVIDERS

        model_lower = (model or self.agents.defaults.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or p.api_key:
                    return p, spec.name

        # Fallback: configured local providers can route models without
        # provider-specific keywords (for example plain "llama3.2" on Ollama).
        # Prefer providers whose detect_by_base_keyword matches the configured api_base
        # (e.g. Ollama's "11434" in "http://localhost:11434") over plain registry order.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and p.api_key:
                return p, spec.name
        return None, None

    def get_provider(self, model: str | None = None) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model)
        return p

    def get_provider_name(self, model: str | None = None) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model)
        return name

    def get_api_key(self, model: str | None = None) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model)
        return p.api_key if p else None

    def get_api_base(self, model: str | None = None) -> str | None:
        """Get API base URL for the given model. Applies default URLs for gateway/local providers."""
        from nanocat.providers.registry import find_by_name

        p, name = self._match_provider(model)
        if p and p.api_base:
            return p.api_base
        # Only gateways get a default api_base here. Standard providers
        # (like Moonshot) set their base URL via env vars in _setup_env
        # to avoid polluting the global litellm.api_base.
        if name:
            spec = find_by_name(name)
            if spec and (spec.is_gateway or spec.is_local) and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(  # pyright: ignore
        env_prefix="NANOCAT_",
        env_nested_delimiter="__",
        extra="ignore",
    )
