# NanoCat 配置 Schema 参考

配置文件为运行目录中的 `config.json`。字段以 camelCase 写入；核心结构来自 `nanocat/config/schema.py`，聊天渠道字段来自 `nanocat/channels/` 中的配置模型。本文描述源码默认值，实例中的环境变量和已保存设置可能覆盖这些值。

## 读取与保存

- `schemaVersion` 固定为 `2`，Web 设置将其作为只读字段。
- 省略字段时使用默认值。`null`、空字符串和空集合的含义由各字段决定。
- 核心配置忽略未知字段；`channels` 允许扩展渠道配置，插件字段由其 descriptor 提供。
- JSON 解析失败或已知字段校验失败时，加载报错并保留原文件。
- 环境变量使用 `NANOCAT_` 前缀、`__` 分隔层级，例如 `NANOCAT_PROVIDERS__KIMI__BILLINGMODE=subscription`。`NANOCAT_WEB_HOST` 和 `NANOCAT_WEB_PORT` 对应 Web 监听地址和端口。
- Web 设置通过 revision/ETag 比较后原子保存；直接编辑文件后重启进程加载。

生效方式：`live` 为运行时应用，`next_turn` 为后续任务使用，`reconnect` 为重建相关连接或客户端，`restart` 为重启进程后应用。表中方式对应 Web 设置服务；活动任务仍使用接收任务时的配置快照。

## Kimi 计费类型与端点

填写 `providers.kimi.apiKey` 时，必须同时显式选择 `providers.kimi.billingMode`。留空的 `apiBase` 由计费类型决定；填写 `apiBase` 时使用指定地址。

| billingMode | Key 来源 | 默认 OpenAI 兼容 Base URL | K3 模型示例 |
| --- | --- | --- | --- |
| `subscription` | Kimi Code 会员控制台 | `https://api.kimi.com/coding/v1` | `kimi/k3`、`kimi/k3-256k` |
| `pay_as_you_go` | Moonshot 开放平台 | `https://api.moonshot.cn/v1` | `kimi/kimi-k3` |

两种模式均使用 Bearer API Key，聊天请求为 `POST {apiBase}/chat/completions`，模型发现为 `GET {apiBase}/models`。海外订阅地址为 `https://api.kimi.ai/coding/v1`，海外按量付费地址为 `https://api.moonshot.ai/v1`。套餐权限及模型 ID 以服务端返回为准。端点依据 [Kimi Code 官方文档](https://www.kimi.com/code/docs/)，K3 开放平台模型与思考协议依据 [MoonshotAI/Kimi-K3](https://github.com/MoonshotAI/Kimi-K3#6-model-usage)。

```json
{
  "providers": {
    "kimi": {
      "apiKey": "<KIMI_CODE_API_KEY>",
      "billingMode": "subscription",
      "apiBase": null
    }
  },
  "agents": {
    "defaults": {
      "model": "kimi/k3",
      "modelChoice": ["kimi/k3"]
    }
  }
}
```

`/model discover kimi` 查询所配置计费渠道的模型；`/model discover` 查询当前主模型所属渠道。发现结果只展示，使用 `/model add kimi <model_id>` 加入模型目录。OAuth 渠道目前返回不支持发现的提示；其他渠道需要可用的模型列表端点，缺少默认地址时填写 `apiBase`。

## 字段间约束与运行含义

- `runtimeFiles.maxFileBytes <= maxSessionBytes <= maxTotalBytes`。
- `agents.defaults.assistantModel=null` 使用主模型；`subagentModel=null` 使用 assistantModel，再回退主模型。
- `visionModel=null` 使用辅助模型；会话压缩使用当前主模型，在原始上下文末尾追加总结指令。
- `reasoningEffort` 的命令可选值为 `auto/low/medium/high/xhigh/max`，`auto` 保存为 `null`。Kimi 将 `medium` 映射为 `high`，`xhigh` 映射为 `max`，保留其余值。
- `tools.maxReturnChars=0` 表示关闭工具输出字符数限制。
- `tools.policy` 中的 `safty*` 是实际字段拼写。`HARD_DENY` 与路径限制优先于 YOLO 或自动批准。
- `tools.mcpServers.<name>` 的 `<name>` 是用户指定的服务器名称；`enabledTools=["*"]` 注册全部工具，`[]` 注册零个工具。
- `api.authToken=null` 在每次启动时生成随机 token；`channels.web.password=null` 跳过 Web 登录并输出终端告警。
- `api.enabled=false` 且 Web 开启时，Web 使用 loopback 随机端口上的内部 core API。

## 核心配置字段

### schemaVersion

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `schemaVersion` | integer | `2` | 固定值：`2` | `live` |

### agents

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `agents` | object | 见子字段 | Agent configuration. | `live` |
| `agents.defaults` | object | 见子字段 | Default agent configuration. | `next_turn` |
| `agents.defaults.model` | string | `"openai/gpt-4o"` | — | `next_turn` |
| `agents.defaults.reasoningEffort` | string / null | `null` | — | `next_turn` |
| `agents.defaults.assistantModel` | string / null | `null` | — | `next_turn` |
| `agents.defaults.subagentModel` | string / null | `null` | — | `next_turn` |
| `agents.defaults.visionModel` | string / null | `null` | — | `next_turn` |
| `agents.defaults.modelChoice` | array<string> | `["openai/gpt-4o"]` | — | `next_turn` |
| `agents.defaults.maxTokens` | integer / null | `8192` | — | `next_turn` |
| `agents.defaults.contextWindowTokens` | integer | `65536` | — | `next_turn` |
| `agents.defaults.temperature` | number / null | `null` | — | `next_turn` |
| `agents.defaults.maxToolIterations` | integer | `40` | — | `next_turn` |
| `agents.defaults.compactionEnabled` | boolean | `true` | — | `next_turn` |
| `agents.defaults.compactionThreshold` | number | `0.5` | >= 0.05; <= 0.95; Trigger session compaction at this fraction of the context window. | `next_turn` |
| `agents.defaults.noCompactHistoryNum` | integer | `3` | >= 1; <= 1000; Keep this many recent completed turns uncompressed. | `next_turn` |
| `agents.defaults.pulseEnabled` | boolean | `false` | — | `next_turn` |

### runtime

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `runtime` | object | 见子字段 | Runtime-wide concurrency limits shared by interactive and tool work. | `live` |
| `runtime.maxConcurrentTurns` | integer | `8` | > 0 | `next_turn` |
| `runtime.maxConcurrentToolCalls` | integer | `16` | > 0 | `next_turn` |

### runtimeFiles

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `runtimeFiles` | object | 见子字段 | Limits for runtime-owned, agent-readable workspace files. | `live` |
| `runtimeFiles.maxFileBytes` | integer | `67108864` | > 0 | `next_turn` |
| `runtimeFiles.maxSessionBytes` | integer | `268435456` | > 0 | `next_turn` |
| `runtimeFiles.maxTotalBytes` | integer | `2147483648` | > 0 | `next_turn` |

### api

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `api` | object | 见子字段 | Public HTTP API listener and bearer authentication configuration. | `live` |
| `api.enabled` | boolean | `false` | — | `restart` |
| `api.host` | string | `"127.0.0.1"` | 最短长度 1 | `restart` |
| `api.port` | integer | `18791` | >= 0; <= 65535 | `restart` |
| `api.authToken` | string / null | `null` | 最短长度 16 | `live` |
| `api.corsOrigins` | array<string> | `[]` | — | `live` |

### channels

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels` | object | 见子字段 | Configuration for built-in and plugin chat channels. | `live` |
| `channels.sendProgress` | boolean | `true` | — | `live` |
| `channels.sendToolHints` | boolean | `false` | — | `live` |
| `channels.outboundMaxAttempts` | integer | `3` | — | `live` |
| `channels.outboundRetryDelayS` | number | `0.25` | — | `live` |
| `channels.web` | object | 见子字段 | Local single-user web channel configuration. | `reconnect` |
| `channels.web.enabled` | boolean | `true` | — | `restart` |
| `channels.web.host` | string | `"127.0.0.1"` | 最短长度 1 | `restart` |
| `channels.web.port` | integer | `18790` | >= 0; <= 65535 | `restart` |
| `channels.web.password` | string / null | `null` | 最短长度 1 | `live` |
| `channels.web.trustedProxies` | array<string> | `[]` | — | `live` |
| `channels.web.allowFrom` | array<string> | `["web:local"]` | — | `live` |

### providers

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `providers` | object | 见子字段 | Configuration for LLM providers. | `live` |
| `providers.custom` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.custom.apiKey` | string | `""` | — | `reconnect` |
| `providers.custom.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.custom.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.anthropic` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.anthropic.apiKey` | string | `""` | — | `reconnect` |
| `providers.anthropic.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.anthropic.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.openai` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.openai.apiKey` | string | `""` | — | `reconnect` |
| `providers.openai.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.openai.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.openrouter` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.openrouter.apiKey` | string | `""` | — | `reconnect` |
| `providers.openrouter.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.openrouter.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.deepseek` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.deepseek.apiKey` | string | `""` | — | `reconnect` |
| `providers.deepseek.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.deepseek.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.groq` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.groq.apiKey` | string | `""` | — | `reconnect` |
| `providers.groq.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.groq.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.zhipu` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.zhipu.apiKey` | string | `""` | — | `reconnect` |
| `providers.zhipu.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.zhipu.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.dashscope` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.dashscope.apiKey` | string | `""` | — | `reconnect` |
| `providers.dashscope.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.dashscope.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.vllm` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.vllm.apiKey` | string | `""` | — | `reconnect` |
| `providers.vllm.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.vllm.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.ollama` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.ollama.apiKey` | string | `""` | — | `reconnect` |
| `providers.ollama.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.ollama.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.gemini` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.gemini.apiKey` | string | `""` | — | `reconnect` |
| `providers.gemini.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.gemini.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.moonshot` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.moonshot.apiKey` | string | `""` | — | `reconnect` |
| `providers.moonshot.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.moonshot.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.kimi` | object | 见子字段 | — | `reconnect` |
| `providers.kimi.apiKey` | string | `""` | — | `reconnect` |
| `providers.kimi.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.kimi.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.kimi.billingMode` | string / null | `null` | 可选值：`"subscription"`, `"pay_as_you_go"`; Select the billing type of this API key: Kimi Code subscription or Moonshot pay-as-you-go. | `reconnect` |
| `providers.minimax` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.minimax.apiKey` | string | `""` | — | `reconnect` |
| `providers.minimax.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.minimax.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.aihubmix` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.aihubmix.apiKey` | string | `""` | — | `reconnect` |
| `providers.aihubmix.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.aihubmix.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.siliconflow` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.siliconflow.apiKey` | string | `""` | — | `reconnect` |
| `providers.siliconflow.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.siliconflow.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.volcengine` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.volcengine.apiKey` | string | `""` | — | `reconnect` |
| `providers.volcengine.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.volcengine.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.volcengineCodingPlan` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.volcengineCodingPlan.apiKey` | string | `""` | — | `reconnect` |
| `providers.volcengineCodingPlan.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.volcengineCodingPlan.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.byteplus` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.byteplus.apiKey` | string | `""` | — | `reconnect` |
| `providers.byteplus.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.byteplus.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.byteplusCodingPlan` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.byteplusCodingPlan.apiKey` | string | `""` | — | `reconnect` |
| `providers.byteplusCodingPlan.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.byteplusCodingPlan.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.openaiCodex` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.openaiCodex.apiKey` | string | `""` | — | `reconnect` |
| `providers.openaiCodex.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.openaiCodex.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |
| `providers.githubCopilot` | object | 见子字段 | LLM provider configuration. | `reconnect` |
| `providers.githubCopilot.apiKey` | string | `""` | — | `reconnect` |
| `providers.githubCopilot.apiBase` | string / null | `null` | — | `reconnect` |
| `providers.githubCopilot.extraHeaders` | map<string, string> / null | `null` | — | `reconnect` |

### heartbeat

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `heartbeat` | object | 见子字段 | Heartbeat service configuration. | `live` |
| `heartbeat.enabled` | boolean | `true` | — | `next_turn` |
| `heartbeat.intervalS` | integer | `1800` | — | `next_turn` |
| `heartbeat.principalId` | string / null | `null` | — | `next_turn` |

### tools

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `tools` | object | 见子字段 | Tools configuration. | `live` |
| `tools.maxReturnChars` | integer | `10000` | — | `next_turn` |
| `tools.policy` | object | 见子字段 | Runtime-wide tool security policy configuration. | `next_turn` |
| `tools.policy.saftyCheck` | boolean | `true` | — | `next_turn` |
| `tools.policy.saftySafeTool` | array<string> | `[]` | — | `next_turn` |
| `tools.policy.saftyAllowRegex` | array<string> | `[]` | — | `next_turn` |
| `tools.policy.saftyDenyRegex` | array<string> | `[]` | — | `next_turn` |
| `tools.policy.autoApproveMode` | boolean | `false` | — | `next_turn` |
| `tools.policy.restrictPathToWorkspace` | boolean | `false` | — | `next_turn` |
| `tools.policy.restrictUrlOutsideLocal` | boolean | `false` | — | `next_turn` |
| `tools.web` | object | 见子字段 | Web tools configuration. | `next_turn` |
| `tools.web.proxy` | string / null | `null` | — | `next_turn` |
| `tools.web.search` | object | 见子字段 | Web search tool configuration. | `next_turn` |
| `tools.web.search.provider` | string | `"brave"` | — | `next_turn` |
| `tools.web.search.apiKey` | string | `""` | — | `next_turn` |
| `tools.web.search.baseUrl` | string | `""` | — | `next_turn` |
| `tools.web.search.maxResults` | integer | `5` | — | `next_turn` |
| `tools.cmd` | object | 见子字段 | Command tool configuration, shared by the exec and proc tools. | `next_turn` |
| `tools.cmd.timeout` | integer | `60` | — | `next_turn` |
| `tools.cmd.pathAppend` | array<string> | `[]` | — | `next_turn` |
| `tools.cmd.env` | map<string, string> | `{}` | — | `next_turn` |
| `tools.filesystem` | object | 见子字段 | Filesystem tool configuration. | `next_turn` |
| `tools.filesystem.forceDelToTrash` | boolean | `true` | — | `next_turn` |
| `tools.mcpServers` | map<string, object> | `{}` | — | `reconnect` |
| `tools.mcpServers.<name>.type` | string / null | `null` | 可选值：`"stdio"`, `"sse"`, `"streamableHttp"` | `reconnect` |
| `tools.mcpServers.<name>.command` | string | `""` | — | `reconnect` |
| `tools.mcpServers.<name>.args` | array<string> | `[]` | — | `reconnect` |
| `tools.mcpServers.<name>.env` | map<string, string> | `{}` | — | `reconnect` |
| `tools.mcpServers.<name>.url` | string | `""` | — | `reconnect` |
| `tools.mcpServers.<name>.headers` | map<string, string> | `{}` | — | `reconnect` |
| `tools.mcpServers.<name>.toolTimeout` | integer | `30` | — | `reconnect` |
| `tools.mcpServers.<name>.enabledTools` | array<string> | `["*"]` | — | `reconnect` |
| `tools.enabledBuiltinTools` | object | 见子字段 | Per-tool registration switches. Each field gates whether that built-in tool is registered; all default enabled except ssh, which is opt-in. | `next_turn` |
| `tools.enabledBuiltinTools.fileTools` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.imageTools` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.screenshot` | boolean | `false` | — | `next_turn` |
| `tools.enabledBuiltinTools.delete` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.exec` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.webSearch` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.webFetch` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.wait` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.ask` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.todo` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.subagentTools` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.sshTools` | boolean | `false` | — | `next_turn` |
| `tools.enabledBuiltinTools.procTools` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.httpRequest` | boolean | `true` | — | `next_turn` |
| `tools.enabledBuiltinTools.memoryTools` | boolean | `true` | — | `next_turn` |

### transcription

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `transcription` | object | 见子字段 | Voice transcription configuration. | `live` |
| `transcription.enabled` | boolean | `false` | — | `next_turn` |
| `transcription.whisper` | object | 见子字段 | Whisper API endpoint configuration. | `next_turn` |
| `transcription.whisper.apiKey` | string | `""` | — | `next_turn` |
| `transcription.whisper.apiUrl` | string | `"https://api.groq.com/openai/v1/audio/transcriptions"` | — | `next_turn` |
| `transcription.whisper.model` | string | `"whisper-large-v3"` | — | `next_turn` |

### memory

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `memory` | object | 见子字段 | Nowledge Mem connection and retrieval configuration. | `live` |
| `memory.enabled` | boolean | `false` | — | `reconnect` |
| `memory.apiUrl` | string | `"http://127.0.0.1:14242"` | — | `reconnect` |
| `memory.apiKey` | string / null | `null` | — | `reconnect` |
| `memory.threadSource` | string | `"nanocat"` | — | `reconnect` |
| `memory.spaceId` | string / null | `null` | — | `reconnect` |
| `memory.threadCaptureEnabled` | boolean | `true` | — | `reconnect` |
| `memory.threadMessageMaxChars` | integer | `12000` | >= 512; <= 100000 | `reconnect` |
| `memory.autoDistillEnabled` | boolean | `true` | — | `reconnect` |
| `memory.distillMinMessages` | integer | `8` | >= 1; <= 1000 | `reconnect` |
| `memory.distillExtractionLevel` | string | `"guided"` | 可选值：`"swift"`, `"guided"`, `"expert"` | `reconnect` |
| `memory.distillPreferredLanguage` | string | `"zh"` | — | `reconnect` |
| `memory.workingMemoryEnabled` | boolean | `true` | — | `reconnect` |
| `memory.workingMemoryTimeoutS` | number | `3.0` | > 0.0; <= 30.0 | `reconnect` |
| `memory.workingMemoryMaxChars` | integer | `6000` | >= 512; <= 50000 | `reconnect` |
| `memory.requestTimeoutS` | number | `15.0` | > 0.0; <= 120.0 | `reconnect` |
| `memory.maxRequestAttempts` | integer | `2` | >= 1; <= 5 | `reconnect` |
| `memory.retryDelayS` | number | `0.1` | >= 0.0; <= 10.0 | `reconnect` |
| `memory.healthTimeoutS` | number | `2.0` | > 0.0; <= 30.0 | `reconnect` |
| `memory.healthCacheSeconds` | number | `5.0` | >= 0.0; <= 60.0 | `reconnect` |
| `memory.maxConnections` | integer | `20` | >= 1; <= 500 | `reconnect` |
| `memory.maxKeepaliveConnections` | integer | `10` | >= 0; <= 500 | `reconnect` |
| `memory.autoInject` | object | 见子字段 | Automatic Nowledge retrieval and context injection policy. | `reconnect` |
| `memory.autoInject.enabled` | boolean | `false` | — | `reconnect` |
| `memory.autoInject.mode` | string | `"auto"` | 可选值：`"auto"`, `"fast"`, `"deep"` | `reconnect` |
| `memory.autoInject.maxNum` | integer | `3` | >= 1; <= 20 | `reconnect` |
| `memory.autoInject.previewLength` | integer | `512` | >= 128; <= 4096 | `reconnect` |
| `memory.autoInject.deepOnRecall` | boolean | `true` | — | `reconnect` |
| `memory.autoInject.minScore` | number | `0.25` | >= 0.0; <= 1.0; Low recall guardrail; Nowledge ranking remains the primary signal. | `reconnect` |
| `memory.autoInject.queryMinLength` | integer | `4` | >= 0; <= 256 | `reconnect` |
| `memory.autoInject.dedupeWindow` | integer | `32` | >= 0; <= 256 | `reconnect` |
| `memory.autoInject.shortRecallMaxLength` | integer | `24` | >= 0; <= 512 | `reconnect` |
| `memory.autoInject.recallContextMessages` | integer | `2` | >= 0; <= 10 | `reconnect` |
| `memory.autoInject.queryMaxLength` | integer | `2000` | >= 256; <= 10000 | `reconnect` |

## 内置聊天渠道扩展字段

以下字段在启用对应渠道时由渠道配置模型加载。第三方插件的字段以插件配置定义为准。

### channels.dingtalk

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.dingtalk.enabled` | boolean | `false` | — | `reconnect` |
| `channels.dingtalk.clientId` | string | `""` | — | `reconnect` |
| `channels.dingtalk.clientSecret` | string | `""` | — | `reconnect` |
| `channels.dingtalk.allowFrom` | array<string> | `[]` | — | `reconnect` |

### channels.discord

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.discord.enabled` | boolean | `false` | — | `reconnect` |
| `channels.discord.token` | string | `""` | — | `reconnect` |
| `channels.discord.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.discord.gatewayUrl` | string | `"wss://gateway.discord.gg/?v=10&encoding=json"` | — | `reconnect` |
| `channels.discord.intents` | integer | `37377` | — | `reconnect` |
| `channels.discord.groupPolicy` | string | `"mention"` | 可选值：`"mention"`, `"open"` | `reconnect` |

### channels.email

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.email.enabled` | boolean | `false` | — | `reconnect` |
| `channels.email.consentGranted` | boolean | `false` | — | `reconnect` |
| `channels.email.imapHost` | string | `""` | — | `reconnect` |
| `channels.email.imapPort` | integer | `993` | — | `reconnect` |
| `channels.email.imapUsername` | string | `""` | — | `reconnect` |
| `channels.email.imapPassword` | string | `""` | — | `reconnect` |
| `channels.email.imapMailbox` | string | `"INBOX"` | — | `reconnect` |
| `channels.email.imapUseSsl` | boolean | `true` | — | `reconnect` |
| `channels.email.imapTimeoutSeconds` | number | `30.0` | — | `reconnect` |
| `channels.email.smtpHost` | string | `""` | — | `reconnect` |
| `channels.email.smtpPort` | integer | `587` | — | `reconnect` |
| `channels.email.smtpUsername` | string | `""` | — | `reconnect` |
| `channels.email.smtpPassword` | string | `""` | — | `reconnect` |
| `channels.email.smtpUseTls` | boolean | `true` | — | `reconnect` |
| `channels.email.smtpUseSsl` | boolean | `false` | — | `reconnect` |
| `channels.email.fromAddress` | string | `""` | — | `reconnect` |
| `channels.email.autoReplyEnabled` | boolean | `true` | — | `reconnect` |
| `channels.email.pollIntervalSeconds` | integer | `30` | — | `reconnect` |
| `channels.email.markSeen` | boolean | `true` | — | `reconnect` |
| `channels.email.maxBodyChars` | integer | `12000` | — | `reconnect` |
| `channels.email.subjectPrefix` | string | `"Re: "` | — | `reconnect` |
| `channels.email.allowFrom` | array<string> | `[]` | — | `reconnect` |

### channels.feishu

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.feishu.enabled` | boolean | `false` | — | `reconnect` |
| `channels.feishu.appId` | string | `""` | — | `reconnect` |
| `channels.feishu.appSecret` | string | `""` | — | `reconnect` |
| `channels.feishu.encryptKey` | string | `""` | — | `reconnect` |
| `channels.feishu.verificationToken` | string | `""` | — | `reconnect` |
| `channels.feishu.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.feishu.reactEmoji` | string | `"THUMBSUP"` | — | `reconnect` |
| `channels.feishu.groupPolicy` | string | `"mention"` | 可选值：`"open"`, `"mention"` | `reconnect` |
| `channels.feishu.replyToMessage` | boolean | `false` | — | `reconnect` |

### channels.mochat

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.mochat.enabled` | boolean | `false` | — | `reconnect` |
| `channels.mochat.baseUrl` | string | `"https://mochat.io"` | — | `reconnect` |
| `channels.mochat.socketUrl` | string | `""` | — | `reconnect` |
| `channels.mochat.socketPath` | string | `"/socket.io"` | — | `reconnect` |
| `channels.mochat.socketDisableMsgpack` | boolean | `false` | — | `reconnect` |
| `channels.mochat.socketReconnectDelayMs` | integer | `1000` | — | `reconnect` |
| `channels.mochat.socketMaxReconnectDelayMs` | integer | `10000` | — | `reconnect` |
| `channels.mochat.socketConnectTimeoutMs` | integer | `10000` | — | `reconnect` |
| `channels.mochat.refreshIntervalMs` | integer | `30000` | — | `reconnect` |
| `channels.mochat.watchTimeoutMs` | integer | `25000` | — | `reconnect` |
| `channels.mochat.watchLimit` | integer | `100` | — | `reconnect` |
| `channels.mochat.retryDelayMs` | integer | `500` | — | `reconnect` |
| `channels.mochat.maxRetryAttempts` | integer | `0` | — | `reconnect` |
| `channels.mochat.clawToken` | string | `""` | — | `reconnect` |
| `channels.mochat.agentUserId` | string | `""` | — | `reconnect` |
| `channels.mochat.sessions` | array<string> | `[]` | — | `reconnect` |
| `channels.mochat.panels` | array<string> | `[]` | — | `reconnect` |
| `channels.mochat.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.mochat.mention` | object | 见子字段 | Mochat mention behavior configuration. | `reconnect` |
| `channels.mochat.mention.requireInGroups` | boolean | `false` | — | `reconnect` |
| `channels.mochat.groups` | map<string, object> | `{}` | — | `reconnect` |
| `channels.mochat.groups.<name>.requireMention` | boolean | `false` | — | `reconnect` |
| `channels.mochat.replyDelayMode` | string | `"non-mention"` | — | `reconnect` |
| `channels.mochat.replyDelayMs` | integer | `120000` | — | `reconnect` |

### channels.qq

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.qq.enabled` | boolean | `false` | — | `reconnect` |
| `channels.qq.appId` | string | `""` | — | `reconnect` |
| `channels.qq.secret` | string | `""` | — | `reconnect` |
| `channels.qq.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.qq.msgFormat` | string | `"plain"` | 可选值：`"plain"`, `"markdown"` | `reconnect` |
| `channels.qq.timeout` | integer | `20` | — | `reconnect` |

### channels.slack

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.slack.enabled` | boolean | `false` | — | `reconnect` |
| `channels.slack.mode` | string | `"socket"` | — | `reconnect` |
| `channels.slack.webhookPath` | string | `"/slack/events"` | — | `reconnect` |
| `channels.slack.botToken` | string | `""` | — | `reconnect` |
| `channels.slack.appToken` | string | `""` | — | `reconnect` |
| `channels.slack.userTokenReadOnly` | boolean | `true` | — | `reconnect` |
| `channels.slack.replyInThread` | boolean | `true` | — | `reconnect` |
| `channels.slack.reactEmoji` | string | `"eyes"` | — | `reconnect` |
| `channels.slack.doneEmoji` | string | `"white_check_mark"` | — | `reconnect` |
| `channels.slack.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.slack.groupPolicy` | string | `"mention"` | — | `reconnect` |
| `channels.slack.groupAllowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.slack.dm` | object | 见子字段 | Slack DM policy configuration. | `reconnect` |
| `channels.slack.dm.enabled` | boolean | `true` | — | `reconnect` |
| `channels.slack.dm.policy` | string | `"open"` | — | `reconnect` |
| `channels.slack.dm.allowFrom` | array<string> | `[]` | — | `reconnect` |

### channels.telegram

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.telegram.enabled` | boolean | `false` | — | `reconnect` |
| `channels.telegram.token` | string | `""` | — | `reconnect` |
| `channels.telegram.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.telegram.proxy` | string / null | `null` | — | `reconnect` |
| `channels.telegram.replyToMessage` | boolean | `false` | — | `reconnect` |
| `channels.telegram.groupPolicy` | string | `"mention"` | 可选值：`"open"`, `"mention"` | `reconnect` |
| `channels.telegram.markdownMethod` | string | `"convert"` | 可选值：`"telegramify"`, `"convert"` | `reconnect` |

### channels.wecom

| 配置路径 | 类型 | 默认值 | 可选值、约束与说明 | Web 修改生效方式 |
| --- | --- | --- | --- | --- |
| `channels.wecom.enabled` | boolean | `false` | — | `reconnect` |
| `channels.wecom.botId` | string | `""` | — | `reconnect` |
| `channels.wecom.secret` | string | `""` | — | `reconnect` |
| `channels.wecom.allowFrom` | array<string> | `[]` | — | `reconnect` |
| `channels.wecom.welcomeMessage` | string | `""` | — | `reconnect` |
