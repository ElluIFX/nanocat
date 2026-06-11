<div align="center">
  <img src="nanobot_logo.png" alt="NanoCat" width="420">
  <h1>NanoCat</h1>
  <p><b>A heavily-modified, ultra-lightweight personal AI assistant.</b></p>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

> **About this project** — NanoCat is a personal fork that has diverged substantially from
> its upstream. It started from [**nanobot**](https://github.com/HKUDS/nanobot) (HKUDS),
> which is itself inspired by [OpenClaw](https://github.com/openclaw/openclaw). NanoCat is
> no longer kept in sync with the upstream main line — the agent loop, memory system,
> context/prompt-cache strategy, session model, and security sandbox have been reworked.
> Credit for the original architecture and channel/provider integrations belongs to the
> nanobot authors; this repository is maintained independently for personal use, research,
> and technical exchange. It is unrelated to crypto and has no token or coin.

---

🐈 **NanoCat** keeps a minimal core agent (an LLM ↔ tool loop in a few thousand readable lines)
and trades the upstream's breadth for a tighter, more opinionated runtime tuned for a single user.

## Signature Features

### 🧠 Three-tier memory

NanoCat does not rely on a single flat memory file. Memory is layered by lifetime and access pattern:

| Tier | Where | Behavior |
|------|-------|----------|
| **Static long-term** | `MEMORY.md` in the workspace root | Always injected into context; the durable "who/what" the agent should never forget. |
| **Semantic (Nowledge)** | External Nowledge Mem REST backend | The agent **auto-extracts durable facts** from conversations and writes them to a vector-searchable store; recalled on demand via the memory tools. |
| **Session compaction** | Per-session compacted block | When a session's prompt crosses a **token budget** tied to the model's context window, older raw turns are compacted into a running summary instead of being dropped. |

Auto-injected memories are isolated in a dedicated `<AUTO-MEMORY>` block placed for maximum
provider **prompt-cache** hit rate — the system prompt is ordered by stability so the cacheable
prefix stays identical across turns.

### 🛠️ Enhanced built-in tools

| Group | Tools | Notes |
|-------|-------|-------|
| **Filesystem** | `read_file`, `write_file`, `edit_file`, `list_dir`, `grep_file`, `load_image` | Sandboxed to the workspace by default; `edit_file` does exact-match string edits. |
| **Shell** | `exec` | Allow/deny pattern guard, path containment, process-tree kill, centralized output truncation. |
| **Web** | `web_search`, `web_fetch` | Multi-provider search (Brave/Tavily/Jina/SearXNG/DuckDuckGo) with proxy support. |
| **Vision** | `parse_image`, `load_image` | `parse_image` accepts a `focus` hint to steer extraction. |
| **Memory** | `memory_search`, `memory_get`, `memory_add`, `memory_update`, `memory_delete`, `read_working_memory` | Full CRUD over the semantic memory store. |
| **Async / orchestration** | `spawn`, `gather`, `wait`, `message` | `spawn` runs background subagents that inherit the main tool registry; `gather` fans results back in. |
| **Productivity** | `todo`, `cron` | Native scheduled tasks; no shelling out to a `nanocat cron` CLI. |
| **MCP** | *dynamic* | Any [MCP](https://modelcontextprotocol.io/) server (stdio or HTTP) is discovered and registered as native tools at startup. |

The agent loop executes independent tool calls **in parallel** with correlation IDs in the logs,
and supports a **hard interrupt** — an incoming same-session message cancels the in-flight LLM turn
so replies stay responsive.

### ⌨️ Enhanced command set

Slash commands work from any channel:

| Command | Purpose |
|---------|---------|
| `/session list` · `/session view [id]` · `/session switch [id]` | Multiple UUID-keyed, auto-named sessions per channel. |
| `/model <role> <N>` | Switch the model for a role — `agent`, `subagent`, `assistant`, or `max`. |
| `/context` | Compact numeric context panel (token usage). |
| `/compact` | Force session compaction now. |
| `/status` | Runtime / busy status. |
| `/whoami` | Show channel, chat, and session routing IDs. |
| `/new` · `/stop` · `/restart` · `/help` | New session · cancel current turn · restart runtime · help. |

### 🛡️ Sandbox by default

`tools.restrictToWorkspace` defaults to **on**: all file and shell tools are confined to the
workspace, with env-dump and network-enumeration commands blocked. `tools.exec.pathAppend`
extends `PATH` when a command genuinely needs a binary outside the default search path.

### 🫀 Pulse

An optional internal `<pulse>` reflection block the model writes at the start of a turn
(Vibe / Echo / Read / Will). It is stripped before delivery and logged for inspection — a
lightweight inner-monologue channel that can color the reply without leaking into it.

## Architecture

```
nanocat/
├── agent/          🧠 Core agent
│   ├── loop.py     #   Agent loop (LLM ↔ tool execution, parallel calls, hard interrupt)
│   ├── context.py  #   Prompt builder (cache-ordered, <AUTO-MEMORY> isolation)
│   ├── memory.py   #   Three-tier memory: MEMORY.md · Nowledge · compaction
│   ├── pulse.py    #   Internal reflection block
│   ├── skills.py   #   Skills loader
│   ├── subagent.py #   Background task execution
│   └── tools/      #   Built-in tools (filesystem, shell, web, vision, memory, cron, spawn…)
├── channels/       📱 Chat platform integrations
├── providers/      🤖 LLM providers (registry-driven, 2-step to add one)
├── security/       🛡️ Command guard + workspace containment
├── session/        💬 Multi-session store
├── bus/  cron/  heartbeat/        🚌 ⏰ 💓 Routing, scheduling, proactive wake-up
├── config/  runtime/  utils/      ⚙️ 🚀 Config, bootstrap, helpers
└── skills/  templates/            🎯 Bundled skills + workspace templates
```

## Install

```bash
git clone <your-repo-url> nanocat
cd nanocat
pip install -e .
```

## Quick Start

NanoCat reads everything from `~/.nanocat/`. Create the config and workspace before the first run.

**1. Configure** — `~/.nanocat/config.json`:

```json
{
  "providers": {
    "openrouter": { "apiKey": "sk-or-v1-xxx" }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5",
      "provider": "openrouter"
    }
  }
}
```

**2. Run:**

```bash
nanocat
```

## Providers

Set one provider's API key, point `agents.defaults.model` at it. The provider registry
(`nanocat/providers/registry.py`) is the single source of truth — adding a provider is a two-step
change (a `ProviderSpec` entry + a config field).

| Provider | Notes |
|----------|-------|
| `openrouter` | Gateway to all models (recommended) |
| `anthropic` / `openai` / `gemini` | Direct first-party |
| `deepseek` | Direct, with thinking mode |
| `volcengine` / `byteplus` / `dashscope` / `zhipu` / `moonshot` / `minimax` | Regional |
| `groq` | LLM + Whisper voice transcription |
| `azure_openai` / `aihubmix` / `siliconflow` | Gateways / managed |
| `custom` | Any OpenAI-compatible endpoint (direct, no LiteLLM) |
| `ollama` / `vllm` | Local |
| `openai_codex` / `github_copilot` | OAuth token flow |

## Channels

Connect NanoCat to a chat platform via `channels.*` in `config.json`. Each channel gates senders
with `allowFrom` (empty denies all; `["*"]` allows everyone).

Supported: **Telegram · Discord · Slack · Feishu · DingTalk · QQ · WeCom · Matrix · WhatsApp · Email · Mochat**.

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

## Configuration Highlights

**Web search** — `tools.web.search.provider`: `brave` (default), `tavily`, `jina`, `searxng`,
`duckduckgo` (zero-config fallback). Route all web traffic through a proxy with `tools.web.proxy`.

**MCP** — Claude-Desktop-compatible config under `tools.mcpServers`, both stdio
(`command` + `args`) and HTTP (`url` + `headers`); `enabledTools` selects a subset.

**Heartbeat** — the runtime wakes every 30 minutes and runs tasks listed in
`~/.nanocat/workspace/HEARTBEAT.md`, delivering results to your most recently active channel.

## Docker

```bash
docker build -t nanocat .
docker run -v ~/.nanocat:/root/.nanocat -p 18790:18790 nanocat
```

## License

MIT. Derived from [nanobot](https://github.com/HKUDS/nanobot) (MIT). See [LICENSE](./LICENSE).

<p align="center"><sub>For educational, research, and technical exchange purposes only.</sub></p>
