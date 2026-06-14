<div align="center">
  <img src="nanobot_logo.png" alt="NanoCat" width="420">
  <h1>NanoCat 🐈</h1>
  <p><b>超轻量个人 AI 助手</b></p>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

> **关于本项目** —— NanoCat 是一个高度魔改的个人分支，源自
> [nanobot](https://github.com/HKUDS/nanobot)（HKUDS，灵感来自 OpenClaw），目前已与上游主线
> 大幅分离、独立维护。原始架构与各渠道/模型接入归功于 nanobot 作者。

---

## 一、特色功能（相比原仓库）

- 🧠 **三层记忆系统**：常驻长期记忆 + 可检索的语义记忆库（自动注入相关记忆、按 ID 取全文，基于 [Nowledge](https://mem.nowledge.co/)）+ 会话自动压缩。长对话也不丢上下文、不爆窗口。
- 🐈 **Pulse 内心独白**：回应前的情绪与联想反射，让回复更有"人味"（可开关）。
- 💬 **多会话管理**：每个聊天可拥有多个会话，自动起名，随时 `/session` 查看与切换，互不串味。
- ⌨️ **增强命令集**：`/model` 多角色切换模型、`/context` 查看上下文用量、`/compact` 手动整理记忆等。
- ⚡ **更快更稳**：工具并行调用；消息可打断（连发新消息会取消上一条还在跑的任务）；缓存优化让长对话更省更快。
- 🛡️ **默认安全沙箱**：所有文件/命令工具默认锁定在工作区内，防越权与路径穿越。
- 🔌 **模型兼容增强**：DeepSeek 直连（支持思考模式）；纯文本模型遇到图片会自动转成可解析的本地路径，不再报错。
- 🔎 **记忆检索增强**：中英文关键词智能提取，记忆搜索匹配更准。
- 📱 **渠道可靠性**：QQ 等渠道的发送重试、断线重连、异常消息兜底等大量稳定性增强。
- 🪶 **更轻量**：精简了上游的部分冗余（如 WhatsApp 桥接），更小更快。

## 二、内置工具

开箱即用的内置工具（按类别）：

| 类别 | 工具 | 说明 |
|------|------|------|
| **文件** | `read_file` · `write_file` · `edit_file` | 读取 / 写入 / 精确编辑文件 |
| | `insert_lines` · `delete_lines` · `file_hex` | 按行插入 / 删除、十六进制查看 |
| | `list_dir` · `grep_file` | 列目录、按内容搜索 |
| **视觉** | `parse_image` · `load_image` | 解析图片内容、加载图片 |
| **命令** | `exec` | 执行 Shell 命令（沙箱受限） |
| **联网** | `web_search` · `web_fetch` | 网页搜索、抓取网页正文 |
| **记忆** | `memory_search` · `memory_get` | 检索记忆、按 ID 取全文 |
| | `memory_add` · `memory_update` · `memory_delete` | 增 / 改 / 删记忆 |
| | `read_working_memory` | 读取工作记忆 |
| **任务** | `todo` · `cron` | 待办清单、定时任务 |
| **协作** | `spawn` · `gather` · `wait` · `message` | 后台子代理、汇总结果、等待、主动发消息 |
| **扩展** | *MCP* | 任意 [MCP](https://modelcontextprotocol.io/) 服务器的工具会自动接入 |

> 记忆类工具依赖记忆库 [Nowledge](https://mem.nowledge.co/)（需自行部署并在配置中连接）；MCP 工具按配置动态加载。

## 三、简要使用教程

**1. 安装**

```bash
git clone <你的仓库地址> nanocat
cd nanocat
pip install -e .
```

**2. 配置** —— 编辑 `~/.nanocat/config.json`，填入模型与 API Key：

```json
{
  "providers": {
    "deepseek": { "apiKey": "sk-xxx" }
  },
  "agents": {
    "defaults": {
      "model": "deepseek/deepseek-v4-pro"
    }
  }
}
```

> 也支持 OpenRouter、OpenAI、Gemini、Anthropic、Qwen、Kimi、智谱、火山等众多 provider，以及 Ollama / vLLM 本地模型。

**3. 接入聊天渠道（必需）**

NanoCat 没有终端交互界面，需通过聊天软件与它对话。在 `~/.nanocat/config.json` 的 `channels` 下开启一个渠道，填好凭证与 `allowFrom`（允许的用户白名单）：

> 支持 **Telegram · Discord · Slack · 飞书 · 钉钉 · QQ · 企业微信 · Matrix · 邮件 · Mochat**。

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "你的BotToken",
      "allowFrom": ["你的用户ID"]
    }
  }
}
```

**4. 启动**

```bash
nanocat
```

启动后即作为后台服务运行，用接入的聊天软件找它对话即可。工作区在 `~/.nanocat/workspace/`，记忆、技能、媒体都存在这里。

**5. 常用对话命令**

| 命令 | 作用 |
|------|------|
| `/session list` · `/session switch <id>` | 查看 / 切换会话 |
| `/model <角色> <序号>` | 切换某个角色使用的模型 |
| `/context` | 查看当前上下文用量 |
| `/compact` | 手动整理压缩当前会话 |
| `/status` · `/whoami` | 查看运行状态 / 身份信息 |
| `/new` · `/stop` · `/restart` | 新会话 / 停止当前任务 / 重启 |

---

<p align="center"><sub>仅用于教育、研究与技术交流。基于 <a href="https://github.com/HKUDS/nanobot">nanobot</a>（MIT）。</sub></p>
