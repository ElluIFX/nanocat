"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelsConfig(Base):
    """Configuration for chat channels.

    Built-in and plugin channel configs are stored as extra fields (dicts).
    Each channel parses its own config in __init__.
    """

    model_config = ConfigDict(extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))


class AgentDefaults(Base):
    """Default agent configuration."""

    model_config = ConfigDict(extra="ignore")

    model: str = "openai/gpt-4o"
    max_model: str | None = None  # Optional high-capability model for /max command
    assistant_model: str | None = (
        None  # lightweight model for auxiliary tasks (memory, evaluate, heartbeat); None = use model
    )
    subagent_model: str | None = (
        None  # model for subagents (spawn / gather); None → assistant_model → model
    )
    vision_model: str | None = (
        None  # model for ParseImageTool / adaptive image loading; None → assistant_model → model
    )
    pulse_enabled: bool = False  # enable <pulse> internal emotional reflection
    model_choice: list[str] = Field(default_factory=lambda: ["openai/gpt-4o"])
    max_tokens: int | None = 8192
    context_window_tokens: int = 65_536
    temperature: float | None = None
    max_tool_iterations: int = 40
    # Deprecated compatibility field: accepted from old configs but ignored at runtime.
    memory_window: int | None = Field(default=None, exclude=True)
    reasoning_effort: str | None = None  # low / medium / high — enables LLM thinking mode

    @property
    def should_warn_deprecated_memory_window(self) -> bool:
        """Return True when old memoryWindow is present without contextWindowTokens."""
        return (
            self.memory_window is not None and "context_window_tokens" not in self.model_fields_set
        )


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(
        default_factory=ProviderConfig
    )  # Azure OpenAI (model = deployment name)
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


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    safety_check: bool = True  # If false, skip SSRF and URL validation checks
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class CmdToolConfig(Base):
    """Command tool configuration, shared by the exec and proc tools."""

    safety_check: bool = True  # If false, skip command safety guard checks
    timeout: int = 60  # exec only (proc is long-lived)
    path_append: list[str] = Field(default_factory=list)  # dirs appended to exec/proc child PATH
    env: dict[str, str] = Field(default_factory=dict)  # extra env injected into exec/proc children
    deny_regex: list[str] = Field(default_factory=list)  # extra command-guard deny patterns
    allow_regex: list[str] = Field(default_factory=list)  # command-guard strict allow-list
    # Confine exec/proc command paths to the working dir (enforced by the command guard).
    restrict_to_workspace: bool = False


class FilesystemToolConfig(Base):
    """Filesystem tool configuration."""

    safety_check: bool = True  # If false, skip path restriction checks
    force_del_to_trash: bool = True  # delete tool only trashes; hides the permanent option
    # Confine file tools (read/write/edit/delete/list/...) to the workspace boundary.
    restrict_to_workspace: bool = False
    deny_regex: list[str] = Field(default_factory=list)  # block file tools on matching paths
    allow_regex: list[str] = Field(default_factory=list)  # strict allow-list for file-tool paths


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
    todo: bool = True
    subagent_tools: bool = True
    ssh_tools: bool = False  # SSH tools (ssh_open/send/read/close/list)
    proc_tools: bool = True  # background process tools (proc_start/read/stop/list)
    http_request: bool = True  # structured HTTP request tool


class ToolsConfig(Base):
    """Tools configuration."""

    max_return_chars: int = 10000  # Truncate tool results exceeding this; 0 = no limit
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
    """Auto-inject Nowledge search results into the system prompt on every user turn."""

    enabled: bool = False
    with_content: bool = False  # include memory content in the injected system prompt
    score_threshold: float = 0.7  # minimum similarity_score to include a result
    max_length: int = 128  # truncate memory content beyond this char count
    max_num: int = 5  # maximum number of memories to inject
    extract_keywords: bool = True  # extract zh/en keywords (jieba) instead of sending raw text


class NowledgeConfig(Base):
    """Nowledge Mem connection configuration."""

    enabled: bool = True
    auto_extract_memories: bool = False  # extract durable memories before compaction
    api_url: str = "http://127.0.0.1:14242"
    api_key: str | None = None
    thread_source: str = "nanocat"
    auto_inject: NowledgeAutoInjectConfig = Field(default_factory=NowledgeAutoInjectConfig)


class MemoryConfig(Base):
    """Memory system configuration.

    Compaction settings always apply to session history.
    MEMORY.md is a static, manually maintained long-term memory block.
    Nowledge-specific extraction behavior is configured under memory.nowledge.
    """

    compaction_threshold: float = (
        0.5  # trigger compression when prompt exceeds this fraction of context_window_tokens
    )
    no_compact_history_num: int = 3  # keep this many recent user/assistant turns raw
    nowledge: NowledgeConfig = Field(default_factory=NowledgeConfig)


class TipsConfig(Base):
    """Configurable system response strings shown to users.

    Strings marked with placeholders use Python .format() substitution.
    Available placeholders per field are listed in the comments.
    """

    # /restart
    restart: str = "Restarting NanoCat, will be back soon..."
    restart_done: str = "Bot restarted."
    # /new
    new_session: str = "New session started."
    # /stop — {count}
    stop_tasks: str = "Stopped {count} task(s)."
    stop_idle: str = "No active task to stop."
    # unhandled exception
    error: str = "Sorry, I encountered an error."
    # background (subagent) task with no reply
    background_done: str = "Background task completed."
    # /help (full text, newlines supported)
    help: str = (
        "## 🐈 NanoCat commands:\n"
        "- /new — Start a new conversation\n"
        "- /stop — Stop the current task\n"
        "- /restart — Restart the bot\n"
        "- /model — View or configure models\n"
        "- /context — Show current context info\n"
        "- /whoami — Show channel/chat identity\n"
        "- /help — Show available commands\n"
        "- /compact — Manually compact old session turns\n"
        "- /session — View and switch sessions\n"
        "- /status — Show agent status and recent logs\n"
        "- /approve <N=5> — Approve safety check for N minutes\n"
        "- /max <prompt> — Use high-capability model for this turn"
    )
    # /model (no args) — {model_name}, {provider_name}
    model_info: str = (
        "## 🐈 Model info\n"
        "- Main Model: {main_model}\n"
        "- Max Model: {max_model}\n"
        "- Assistant Model: {assistant_model}\n"
        "- Subagent Model: {subagent_model}\n"
        "- Provider: {provider_name}\n\n"
        "- Available models:\n{model_choice}\n\n"
        "## Usage:\n"
        "- /model add <provider> <model_name>\n"
        "- /model agent|subagent|assistant|max <N>\n"
        "- /model delete <N>"
    )
    # /model set (success) — {target}, {model_name}
    model_set: str = "{target} model set: {model_name}"
    # /model delete (success) — {model_name}
    model_deleted: str = "Model deleted: {model_name}"
    # /model add (success) — {model_name}
    model_added: str = "Model added: {model_name}"
    # /model (error) — {error}
    model_error: str = "Error updating model: {error}"
    # /model (choice invalid) — {choice_number}
    model_choice_invalid: str = "Invalid choice number: {choice_number}"
    # /session - usage shown when subcommand is unknown or missing
    session_usage: str = (
        "## Usage:\n\n- /session list [N=10]\n- /session view <id>\n- /session switch <id>"
    )
    # /session list — no eligible sessions
    session_list_empty: str = "No named sessions yet. Keep chatting to auto-generate session names."
    # /session list — {items} (pre-formatted markdown list)
    session_list: str = (
        "## Sessions:\n\n{items}\n\n## Usage:\n\n- /session view <id>\n- /session switch <id>"
    )
    # /session view — {name}, {id}, {turns}
    session_view: str = "## {name} ({id})\n\n{turns}"
    # /session switch — {session_id}, {name}
    session_switched: str = "Switched to session `{session_id}` ({name})."
    # /session view/switch — not found — {session_id}
    session_not_found: str = "Session `{session_id}` not found."
    # /whoami — {channel}, {chat_id}, {session_id}, {session_key}
    whoami_info: str = (
        "## 🐈 Session Identity\n\n"
        "- Channel: {channel}\n"
        "- Chat ID: {chat_id}\n"
        "- Session: {session_id}\n"
        "- Key: {session_key}"
    )
    # /context panel body
    context_panel: str = (
        "## 🐈 Context Usage ({model_name})\n\n"
        "- prompt = {estimated_prompt_tokens}/{context_window_tokens} ({context_usage_percent}%)\n"
        "- overflow = {overflow_tokens}/{context_window_tokens} ({overflow_percent}%)\n"
        "- msgs = {messages_uncompacted}/{messages_total} ({uncompacted_percent}%)\n"
        "- history = {history_messages}/{messages_total}\n"
    )
    # agent loop finished with no content
    no_response: str = "I've completed processing but have no response to give."
    # Compaction tips
    compact_completed: str = "Session compaction completed."
    compact_failed: str = "No completed turns are eligible for compaction."


class Config(BaseSettings):
    """Root configuration for NanoCat."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    tips: TipsConfig = Field(default_factory=TipsConfig)
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

    model_config = ConfigDict(env_prefix="NANOCAT_", env_nested_delimiter="__")
