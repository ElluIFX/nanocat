<div align="center">
  <img src="nanobot_logo.png" alt="NanoCat" width="420">
  <h1>NanoCat</h1>
  <p><b>个人 AI 助手运行时</b></p>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

NanoCat 是一个面向个人部署的 AI 助手运行时，提供会话管理、工具执行、用户命令、模型路由、记忆和多渠道接入。项目基于 [nanobot](https://github.com/HKUDS/nanobot)，当前独立维护。

## 功能

- **运行时调度**：有界消息总线、按会话排序、跨会话并发、统一生命周期管理和出站分发。
- **命令控制面**：命令在 Provider 之前解析；未知命令、缺参和错参直接反馈，不会误发给 LLM。
- **安全审批**：安全许可由运行时持有，不进入 LLM 请求。高风险操作可暂停当前会话并直接请求用户批准；支持单次、当前轮和会话级授权。
- **上下文管理**：上下文预算、工具结果归档、会话压缩、压缩状态查询和手动触发；压缩模型独立配置。
- **Nowledge 记忆**：自动检索注入、Working Memory、Thread 捕获、后台蒸馏和用户侧记忆命令。
- **模型与 Provider**：Provider registry、OpenAI-compatible 接口、DeepSeek、OpenAI、OpenAI Codex/OAuth、Anthropic、OpenRouter 及本地模型适配。
- **渠道与 TUI**：统一渠道生命周期和能力声明；TUI 提供会话、工具调用、审批卡片、模型和 effort 控件。
- **扩展**：MCP、子代理、定时任务和 heartbeat 通过统一的应用入口接入。

## 架构

```text
Channel / TUI
    -> InboundEvent -> MessageBus
    -> CommandRouter / AgentService
    -> Session ordering -> Provider / ToolExecutor
    -> OutboundDispatcher -> Channel
```

主要源码层：

| 目录 | 职责 |
| --- | --- |
| `nanocat/core` | 稳定类型、事件、错误和 port |
| `nanocat/runtime` | composition root、生命周期、健康和重启 |
| `nanocat/application` | 命令、会话请求、工具执行、审批和出站编排 |
| `nanocat/agent` | LLM 上下文、记忆、子代理和内置工具 |
| `nanocat/security` | 命令、路径、网络和工具安全策略 |
| `nanocat/session` | 会话模型、索引和持久化 |
| `nanocat/providers` | Provider registry、resolver 和适配器 |
| `nanocat/channels` | 渠道适配、ACL、能力和生命周期 |

## 内置工具

| 类别 | 工具 |
| --- | --- |
| 文件 | `read_file` `write_file` `edit_file` `insert_lines` `delete_lines` `delete` `list_dir` `grep_file` `file_hex` |
| 命令与进程 | `exec` `proc_start` `proc_send` `proc_read` `proc_stop` `proc_list` |
| SSH | `ssh_open` `ssh_send` `ssh_read` `ssh_close` `ssh_list` |
| 网络与媒体 | `web_search` `web_fetch` `http_request` `parse_image` `load_image` `screenshot` |
| 记忆 | `memory_search` `memory_get` `memory_add` `memory_update` `memory_delete` `read_working_memory` |
| 任务与协作 | `todo` `cron` `wait` `message` `subagent_spawn` `subagent_gather` `subagent_list` `subagent_steer` `subagent_kill` |
| 扩展 | MCP 工具按配置动态注册 |

工具返回值对 LLM 保持结构化 JSON；用户命令结果在出站边界转换为跨渠道 Markdown。文件、命令、网络、进程、SSH 和 MCP 等副作用操作经过统一安全策略。部分工具默认关闭，具体以配置为准。

## 安装

```bash
git clone https://github.com/ElluIFX/nanocat.git
cd nanocat
uv sync --extra tui
```

不使用 `uv` 时可执行：

```bash
pip install -e ".[tui]"
```

## 配置与启动

`workdir` 是运行时锚点。`config.json`、`workspace/`、`sessions/`、`cron/`、`logs/` 和媒体文件均位于该目录下。

例如使用 `data/`：

```json
{
  "providers": {
    "deepseek": { "apiKey": "YOUR_API_KEY" }
  },
  "agents": {
    "defaults": {
      "model": "deepseek/YOUR_MODEL",
      "reasoningEffort": "auto",
      "compactionEnabled": true,
      "compactionModel": "deepseek/YOUR_COMPACTION_MODEL"
    }
  },
  "tools": {
    "globalSaftyCheck": true
  }
}
```

Nowledge 为可选记忆后端：

```json
{
  "memory": {
    "enabled": true,
    "apiUrl": "http://127.0.0.1:14242",
    "apiKey": "YOUR_NOWLEDGE_KEY",
    "spaceId": "YOUR_SPACE_ID",
    "autoInject": { "enabled": true }
  }
}
```

凭证只放在本地配置中，不要提交到 Git、日志或工具返回值。

启动本地 TUI：

```bash
uv run nanocat tui --workdir ./data
```

启动配置中的网络渠道：

```bash
uv run nanocat gateway --workdir ./data
```

TUI 模式不会启动网络渠道；gateway 模式只启动配置中启用且通过 ACL 配置的渠道。可用渠道包括 Telegram、Discord、Slack、飞书、钉钉、QQ、企业微信、Mochat 和 Email。

## 命令

所有命令在 Provider 之前执行。`/help` 可查看当前注册命令及参数。

| 命令 | 作用 |
| --- | --- |
| `/help [command\|group]` | 查看命令帮助 |
| `/logs [N]` | 查看最近 N 行运行日志 |
| `/new` `/stop` `/restart` `/whoami` | 新会话、停止任务、重启运行时、查看身份 |
| `/model` | 查看模型；支持 `add`、`delete`、`agent`、`subagent`、`assistant` 和 `effort` |
| `/model effort auto\|low\|medium\|high\|xhigh\|max` | 设置思考强度 |
| `/compact` `/compact status` | 手动压缩会话；查看上下文预算和压缩状态 |
| `/session list\|use\|delete` | 查看、切换和删除会话 |
| `/memory status\|spaces\|search\|show\|add\|update\|delete\|preview\|distill\|processing` | 管理 Nowledge 记忆 |
| `/cron list\|show\|add\|remove\|run\|enable\|disable` | 管理定时任务 |
| `/approve once\|turn` | 批准当前操作一次或直到当前轮结束 |
| `/approve forever\|cancel` | 启用或撤销当前会话的会话级授权 |
| `/deny` | 拒绝当前敏感操作；`/reject` 为别名 |

`/compact status` 已包含上下文状态查询，不再使用独立的 `/context` 命令。TUI 中的 `AUTO/YOLO` 切换对应会话级审批状态。

## 开发

```bash
uv sync --extra dev --extra tui
uv run ruff check nanocat
```

项目许可证为 [MIT](LICENSE)。
