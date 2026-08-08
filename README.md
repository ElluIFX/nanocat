<div align="center">
  <img src="nanobot_logo.png" alt="NanoCat" width="420">
  <h1>NanoCat</h1>
  <p><b>自托管个人 AI 助手</b></p>
  <p>
    <img src="https://img.shields.io/badge/python-%E2%89%A53.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

NanoCat 是一个面向个人部署的 AI 助手，支持多渠道对话、模型路由、工具调用、会话管理和长期记忆。

## 特性

- 多渠道：TUI、Telegram、Discord、Slack、飞书、钉钉、QQ、企业微信、Mochat 和 Email
- 多模型：OpenAI-compatible、OpenAI Codex、Anthropic、DeepSeek、OpenRouter 等
- 工具调用：文件、终端、进程、SSH、网络、图片、定时任务、子代理和 MCP
- 长期记忆：Nowledge 记忆后端、自动检索、Working Memory、Thread 和记忆管理命令
- 上下文管理：Token 预算、工具结果归档、会话压缩和压缩状态查询
- 安全控制：敏感操作可在执行前请求用户批准，支持单次、当前轮和会话级授权
- TUI：实时对话、工具调用进度、审批交互、模型和思考强度控制

## 安装

需要 Python 3.11 或更高版本，并推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/ElluIFX/nanocat.git
cd nanocat
uv sync --extra tui
```

也可以使用 pip：

```bash
pip install -e ".[tui]"
```

## 配置

运行目录由 `-w` 指定。NanoCat 从该目录读取 `config.json`，并在其中保存会话、日志、工作区和定时任务数据。

最小配置示例：

```json
{
  "providers": {
    "deepseek": {
      "apiKey": "YOUR_API_KEY"
    }
  },
  "agents": {
    "defaults": {
      "model": "deepseek/YOUR_MODEL"
    }
  }
}
```

启用 Nowledge 记忆：

```json
{
  "memory": {
    "enabled": true,
    "apiUrl": "YOUR_NOWLEDGE_URL",
    "apiKey": "YOUR_NOWLEDGE_KEY",
    "spaceId": "YOUR_SPACE_ID",
    "autoInject": {
      "enabled": true
    }
  }
}
```

凭证只应保存在本地配置中。

## 启动

启动本地 TUI：

```bash
uv run nanocat tui -w ./data
```

启动配置中启用的渠道：

```bash
uv run nanocat gateway -w ./data
```

运行：

```text
nanocat [gateway|tui] [-w WORKDIR] [-v]
```

## 常用命令

在对话中输入 `/help` 查看完整帮助。

| 命令 | 用途 |
| --- | --- |
| `/new` | 新建会话 |
| `/model` | 查看和管理模型 |
| `/model effort auto\|low\|medium\|high\|xhigh\|max` | 设置思考强度 |
| `/logs [N]` | 查看最近 N 行日志 |
| `/compact` / `/compact status` | 压缩会话或查看上下文状态 |
| `/session list\|use\|delete` | 管理会话 |
| `/memory ...` | 管理 Nowledge 记忆 |
| `/cron ...` | 管理定时任务 |
| `/approve once\|turn` | 批准当前操作一次或直到当前轮结束 |
| `/approve forever\|cancel` | 启用或撤销当前会话的授权 |
| `/deny` | 拒绝当前敏感操作 |

## 开发

```bash
uv sync --extra dev --extra tui
uv run ruff check nanocat
```

## 许可证

[MIT](LICENSE)
