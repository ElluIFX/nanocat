# NanoCat Development Notebook

本文件是 NanoCat 源码开发索引和长期规约，也是后续 agent 的唯一项目上下文来源，不是需求说明、发布说明或操作手册。开始工作前先读本文件和相关源码；架构、接口、风险或验证结论变化时直接更新本文件，删除已失效的过程结论。

`nanocat/templates/**` 是随包分发的模板/Skill 载荷。其内部 `AGENTS.md` 只约束模板载荷自身，不属于本文件，不参与 NanoCat 源码架构推导；除非任务明确涉及模板载荷，否则不要修改或解释它。

## 工程事实

- 依赖、可选依赖、构建包内容、入口点、Ruff 规则和版本约束只以 `pyproject.toml` / `uv.lock` 为准，不在本文件复制一份易漂移的版本表。
- 主入口是 `nanocat.runtime.launcher:main`；`nanocat` 与 `python -m nanocat` 始终启动渠道服务，`build_runtime(...)` 保留为 composition root 构造入口。
- `workdir` 是运行时锚点：`config.json` 直接位于其中，并由此派生 workspace、sessions、cron、logs、media 和 restart notification 路径。
- `data/config.json` 是本地实例数据，不是 schema；凭证、cookie、token、SSH 信息和完整敏感参数不能进入代码、日志、文档或提交。
- 用户提示文本由 `nanocat/application/text_catalog.py` 的 `USER_TEXT` 提供；配置只承载行为参数，不能把用户文案重新做成配置字段。

## 架构索引

| 层/模块 | 唯一职责 | 主要入口/契约 |
| --- | --- | --- |
| `nanocat/core/` | 无 SDK 的稳定类型、事件、错误、能力和 port | `ConversationRef`、`Principal`、`InboundEvent`、`OutboundEvent`、`TurnRef`、`InterventionRequest`、`CapabilityDescriptor`、`MessagePort` |
| `nanocat/runtime/` | composition root、生命周期、资源 owner、健康和重启 | `RuntimeContext`、`RuntimeSupervisor.start/wait/run/stop`、`ShutdownCoordinator` |
| `nanocat/bus/` | 进程内有界、带优先级的入站/出站传输 | `MessageBus.publish_*`、`consume_*`/`receive_*`、`close`、`join`、`drain` |
| `nanocat/application/` | 控制面和应用编排，不依赖具体 SDK | `AgentService`、`TurnRequest`、`CommandRouter`、`CommandService`、`CommandDispatcher`、`ToolExecutor`、`InterventionBroker`、`OutboundDispatcher`、`SystemTurnGateway`、`VisionFallbackService` |
| `nanocat/agent/` | LLM 上下文、记忆、子代理和内置工具实现 | `AgentLoop` 目前仍是兼容 turn engine；新业务不得继续向其堆职责 |
| `nanocat/security/` | 统一命令、路径、网络和工具能力策略 | `SecurityPolicy`；结果只能是 `ALLOW`、`HARD_DENY`、`REQUIRE_INTERVENTION` |
| `nanocat/session/` | 会话模型、索引、策略和原子 JSON 持久化 | `Session`、`SessionManager` 兼容 facade；迁移不得覆盖失败源文件 |
| `nanocat/providers/` | Provider registry、model matching、客户端和 resolver | `ProviderSpec`/registry、runtime-scoped resolver；具体 adapter 不读取全局配置 |
| `nanocat/channels/` | SDK 适配、ACL、归一化、生命周期和发送 | `BaseChannel`、`ChannelDescriptor`、`ChannelCapabilities`、`ChannelManager` |
| `nanocat/api/`、`web/` | 通用 HTTP API、Web BFF、鉴权、SSE、artifact 和静态前端 | `ApiRuntime`、`create_api_app()`、`create_web_app()`、`SseBroker`、`ArtifactRegistry` |
| `nanocat/observability/` | 脱敏、健康和有界 activity journal | `ActivityJournal`、`ActivityEvent`、`redact_value()` |
| `nanocat/cron/`、`heartbeat/` | 调度和 tick；不直接实现 LLM/tool/channel 业务 | 通过 `SystemTurnGateway` 提交 system turn |
| `nanocat/templates/` | 分发给 workspace 的模板、Skill、脚本和资源 | 仅在模板/打包任务中处理；不与本文件的源码规约混用 |

依赖方向：`core contracts ← application ← adapters/infrastructure`；`runtime` 是唯一 composition root，可以组装所有具体实现。`core` 不导入 asyncio、httpx、Provider SDK、channel SDK 或具体文件实现；application 只依赖 core 和 ports；adapter 负责外部错误隔离和类型归一化。

## 运行链路与 owner

### 消息面/控制面

```text
channel callback
  -> principal/ACL/字段归一化/event_id/dedup
  -> MessageBus
  ├─ command lane -> CommandDispatcher -> control outbound -> channel adapter
  └─ normal lane -> Intervention response -> TurnRequest / per-session ordering
                    -> Provider -> ToolExecutor -> OutboundDispatcher -> channel adapter
```

- 介入响应和命令属于控制面；普通 turn、tool call、cron、heartbeat、subagent 属于数据面。控制面不能被 LLM prompt 解释。
- 以单个 `session_key` 为顺序边界；不同 session 可并发。任何新增等待、缓存、task、timer、client、stream、process 或临时文件都必须有上限、deadline、owner 和 close path。
- `RuntimeSupervisor` 负责组件启动、健康状态、部分失败隔离和逆序关闭；`stop/close/restart` 必须幂等。重复 stop 等待同一 shutdown 完成，不能取消正在执行的 restart task。不得创建第二个 outbound dispatcher 或 MCP owner。
- 当前兼容边界：`AgentService` 已是 runtime 对外 facade，但 `AgentLoop` 仍拥有实际 turn/tool/MCP/process/HTTP engine；后续拆分只能通过现有 facade、`TurnRequest`、`ToolExecutionContext` 和 ports 进行。
- `CommandDispatcher` 独占 slash command 消费、命令策略、busy 检查和 control response；`AgentLoop` 只消费普通消息与 system turn。
- `OutboundDispatcher` 独占 outbound 消费、能力过滤、有限退避重试和 request-scoped `DeliveryResult`；control outbound 使用保留容量，发送失败不得回写 inbound 或递归触发 turn。

### 核心身份和事件

使用不可变契约传递身份和关联关系，不用 metadata 隐式传递核心控制状态：

- `ConversationRef(channel, chat_id, session_key)`：会话路由键。
- `Principal(id, display_name, roles, attributes)`：ACL、命令和审批主体；`sender_id` 不等同于规范化后的 `principal_id`。
- `InboundEvent`：`event_id`、时间、sender、conversation、content、media、metadata、principal、source、correlation、deadline、priority、request_id、turn_id。
- `OutboundEvent`：conversation、content、reply/media、metadata、correlation、deadline、priority、request_id、turn_id、principal_id。
- `TurnRequest`：`content`、`conversation`、`principal_id`、`source`、`transient`、`metadata`、`deadline_at`；interactive、direct、cron、heartbeat、subagent 共用这一入口。
- `ToolExecutionContext`：`turn_id`、`session_key`、`storage_scope`、`conversation`、`principal_id`、`state_hook`、`message_id`、`session`、`model`、`user_input`；`storage_scope` 绑定实际 session 文件生命周期，授权仍由 principal/conversation/turn/capability 决定。

`metadata` 只能承载 adapter 扩展或事件标志（如 `_control`、`_progress`、`_tool_event`、`_intervention`），不能承载安全许可、session 所有权、turn 状态或业务结果。

## 命令控制面

命令入口在普通 turn/Provider 之前。`CommandClassifier`/`CommandParser`/`CommandRouter`/`CommandService`/`CommandDispatcher` 是唯一命令路径；渠道只归一化输入，不维护本地业务命令表。

### 识别和执行

- 去除前导空白后，首字符为单个 `/` 的文本是命令候选；普通文本中间的 `/` 不拦截。
- `//text` 还原为普通文本 `/text`；Telegram 等 `/command@bot` 在 adapter 归一化，错误 bot 后缀按未知命令处理。
- `MessageBus.publish_inbound()` 将命令候选送入有界 command queue，将普通消息送入 normal queue；两条队列拥有独立容量和消费者，命令不会等待普通消息或 LLM turn。
- `CommandDispatcher` 为每条命令生成独立 control outbound，保留 event/correlation/request/principal 关联；HTTP control action、`process_direct()` 和渠道 slash 输入复用同一执行器。
- 已知命令缺参、错参、未知子命令、未知命令均直接返回 `CommandResult`，不调用 Provider、不写 history、不进入 steer。
- `/help`、`/logs`、`/whoami`、`/model`、审批响应、`/stop`、`/restart` 和 `/compact status` 实时处理；`/cron`、`/memory` 独立运行；`/compact`、`/new`、session 变更操作在 session busy 时返回 idle-only 结果。
- 介入响应先于普通命令；`/approve` 等效 `/approve once`，并支持 `once|turn|forever|cancel`；`/deny` 和 `/reject` 只提交 scheduler-owned action。`/restart` 是 runtime control，由 supervisor 处理，不由 handler 直接替换进程。
- `CommandSpec` 是单一事实来源，至少声明 `name`、`aliases`、`group`、`summary`、`usage`、`examples`、`subcommands`、`permission`、`execution_policy`、`handler`、`enabled` 和参数接受策略。
- 当前 `CommandResult` 字段：`ok`、`code`、`title`、`message`、`usage`、`examples`、`suggestions`、`data`、`persist_history`、`execution_effect`、`audit`。handler 返回结构化结果，不直接操作 channel。

### 反馈

- `CommandFeedback.render()` 是旧 outbound 边界的统一渲染器；`CommandRouter.command_list()` 生成分组帮助。
- Markdown 必须使用标题、命令反引号、列表项和必要空行；多条示例之间用空行，不输出依赖单换行的裸命令列表。
- 用户可见的固定文案只放 `application/text_catalog.py`；动态值用明确 `.format()` 占位符，命令/渠道不得各自复制文案。
- `CommandResult.ok` 属于控制面结果，不是 tool result，不进入 LLM。

## 人机介入和安全

安全 gate 不向 LLM 返回“请请求用户批准”的 `tool_err`。工具实现不读取安全配置，也不执行旁路路径、命令或 URL 安全检查；`SecurityPolicy` 是唯一策略入口，在工具边界产生三种结果：

- `ALLOW`：直接执行。
- `HARD_DENY`：直接终止，审批不能覆盖策略明确拒绝的命令、路径或本地网络目标。
- `REQUIRE_INTERVENTION`：`ToolExecutor` 将当前 turn 置为 `WAITING_FOR_USER`，由 `InterventionBroker` 通过原始 channel/chat 直接询问用户；等待期间不发起新的 LLM request。

`InterventionRequest` 必须绑定内部 `request_id`、`kind`、`turn_id`、`session_key`、`conversation`、`principal_id`、`capability`、脱敏后的工具名与参数、`summary`、`call_fingerprint`、允许 action、`resume_mode`、审批流程、可选自动审批意见、过期时间和可选 tool_call_id。`request_id` 只供 runtime 调度、送达回执、去重和 Web/API 关联使用，用户无需提供任何 ID 或随机参数。状态是单向且幂等的：pending → approve_once/approve_turn/approve_forever/reject/expired/cancelled/delivery_failed/consumed。

- `approve_once` 只匹配原始 call fingerprint；`approve_turn` 只对当前 principal、conversation、turn 和 capability 生效。
- `approve_forever` 只对当前 principal、conversation/session 和当前 capability 生效，保存在 runtime 内存中，直到用户发送 `/approve cancel`；它不是全局永久授权，也不覆盖其他 capability。
- `/approve cancel` 撤销当前 principal 在当前 conversation/session 的全部 session 级审批；没有 session 级审批时返回明确的无操作反馈。挂起中的单次申请使用 `/deny`，不把 `/approve cancel` 当作普通文本送入 LLM。
- 批准后必须重新 evaluate policy，并再次比较 fingerprint；不能因为批准期间参数、路径、策略或配置变化而执行另一调用。
- grant、pending request 和 WAITING_FOR_USER 只存在 runtime 内存，不写 session JSON；once 不跨调用，turn 不跨 turn，session grant 不跨 session、principal 或 runtime 复用。
- delivery 失败必须进入终态并终止原调用；不能把“询问未送达”当作已批准。
- Web 审批卡展示工具名和脱敏参数，决策通过通用 HTTP control action 回到 `CommandService`；卡片状态来自 SSE 与 pending snapshot，授权判断仍由 Broker 完成。内部 request_id 只用于关联、去重和状态提交。
- 非交互渠道提供 `/approve once|turn|forever|cancel`、`/deny` 和隐藏等效的 `/reject` 行为，adapter 只归一化 action，不判断用户是否同意。

策略配置位于 `tools.policy`：`saftyCheck` 是总开关，关闭时 `SecurityPolicy` 立即返回 `ALLOW`；`saftySafeTool` 使用全工具名匹配并支持 `*`，优先级最高。随后按 deny 优先、allow 次之匹配 `tool_name(compact-json-parameters)`；再执行命令分析、工作区路径限制和本地 URL 限制。工作区内文件读写和公网普通访问默认 `ALLOW`；`restrictPathToWorkspace` 与 `restrictUrlOutsideLocal` 默认关闭，开启后分别将工作区外路径和内网目标直接 `HARD_DENY`，拒绝理由说明 agent 工作范围受限。关闭时不因边界本身拦截，只有明显高危操作进入人工审批；无法可靠确认的路径或 URL 按无罪假定放行，SSH 远程路径不受本地工作区限制。`autoApproveMode` 只对 `REQUIRE_INTERVENTION` 启用 assistantModel 审查，工作区外的无害读写不因路径位置单独拒绝，不改变上述优先级；自动批准只作用于当前调用，自动拒绝或审查失败转人工复核。

用户拒绝或策略硬拒绝返回给 LLM 的结构化错误必须附带 `guidance`：禁止绕过审批/安全策略；必要操作应停止，向用户解释原因并询问是否修改命令。

## 工具契约与返回风格

### 注册和执行

新工具实现 `Tool` 的 `name`、`description`、`parameters`、`capabilities` 和异步 `execute(**kwargs) -> str`，通过 `ToolRegistry`/未来 `ToolHost` 注册；工具定义来自 `to_schema()`，名称和参数 schema 是兼容 API。

调用路径固定为：

```text
ToolExecutor
  -> SecurityPolicy.evaluate
  -> approval/preflight for the whole batch
  -> ToolRegistry parameter cast/validate
  -> Tool.execute
  -> result normalization/truncation
  -> AgentLoop.add_tool_result
```

- `ToolExecutor.execute_batch()` 先完成整批安全预检，再按输入顺序返回结果；同一 turn 内可并行，但受 runtime `maxConcurrentToolCalls` 限制。失败时显式取消未完成兄弟任务。
- tool、MCP、subagent、message/ask/wait 不得直接发送 channel、写 session 或修改全局授权；通过 context、event sink、service port 或 application owner 完成。
- 工具必须声明/遵守参数校验、输出类别、超时、取消域、并发类别、资源 owner 和是否需要安全 gate。副作用工具不隐式重试；部分成功必须可区分并可恢复。
- 安全授权元数据只在 `ToolExecutor`/`ToolRegistry` 内部校验，不能写入工具参数；MCP、HTTP、进程和其他外部适配器只接收经过工具 schema 校验的业务参数。
- 不新增依赖共享可变 `set_context` 跨 session 传播状态；兼容入口仍存在时，只能由 Registry 在调用边界绑定 turn-local context。
- `load_image` 是唯一公开图片工具，只负责读取并返回原始图片块；图片能力探测和描述回退由 application 层统一处理。

### JSON 返回格式

所有新工具使用 `nanocat.agent.tools.base` 的 helper，不手写包络：

```python
tool_ok(content=text, path=path, total=total)
# 内部：{"ok": true, "content": ..., "path": ..., "total": ...}

tool_err("operation failed", hint="retry with ...", detail=detail)
# Internal envelope: {"ok": false, "error": "operation failed", "hint": ..., "detail": ...}
```

具体约定：

- `tool_ok(**fields)` 生成 JSON 字符串，过滤值为 `None` 的字段，`content` 承载正文，其他字段只放结构化 metadata；编码保持 UTF-8/非 ASCII 可读。
- `tool_err(error, hint=None, **fields)` 的 `error` 必须是可理解的一句话；`hint` 只提供下一步恢复建议；异常信息使用 `exc_message()`，不得留下空错误或泄露凭证、cookie、完整命令参数。
- 工具返回必须是 JSON 字符串；旧工具若在私有 helper 返回 dict，公共 `execute()` 边界必须在修改时归一化。顶层不得是 Markdown、Python repr、裸异常或混合 stdout/stderr 文本；自然语言/Markdown 只能作为 JSON 的 `content` 或专用字段。
- 字段按语义保持稳定：正文用 `content`，集合沿用工具已有的 `items`/`entries`/`results`，资源使用 `id`/`session_id`/`stream_id`，进程结果使用 `stdout`/`stderr`/`returncode`；不要为了形式统一丢失工具语义，也不要新增同义字段。
- 成功包络中的 `ok: true` 只用于工具内部/控制判断；`ToolExecutor` 在返回 LLM 前递归移除所有布尔真值 `ok`，保留 `ok: false` 和错误字段。工具不得依赖成功结果中 `ok` 仍存在。
- 工具不得把审批语法或授权细节作为错误反馈给 LLM；需要用户决策必须触发 `REQUIRE_INTERVENTION`。
- 大正文由 application 统一截断或落盘引用，工具不要各自实现不同的上下文截断协议；路径、临时文件、媒体和进程句柄必须有 owner 和清理路径。
- `ToolRegistry` 负责工具不存在、参数非法和未捕获异常的统一 `tool_err` fallback；工具自身仍应在可预期失败处返回结构化错误。

## 状态、配置和 Provider

- Session JSON 按 schema version 兼容读取；读失败或迁移失败不得覆盖源文件。写入流程是 parse → validate → temp write/flush/fsync → atomic replace。
- Session history view、completed-turn boundary 与写入校验共用同一 user-rooted 状态机；tool call ID、tool result、terminal assistant 和连续完整 turn 使用同一定义。损坏片段按 turn 隔离，已完成 turn 继续可见；一次 turn 的内存 append 在持久化失败时回滚 messages、revision 和 updated_at。
- transient/system turn、ephemeral block、intervention grant 和 pending request 不写入普通 session history；assistant/tool content list、image placeholder、compaction cursor 和 metadata 语义保持。
- `Config` 由 Pydantic schema 校验 camelCase；当前 `memory` 直接承载 Nowledge 配置，session compaction 配置位于 `agents.defaults`；不保留 `memory.nowledge` 和 `autoExtractMemories`。`memoryTools` 只控制 LLM memory tool 注册，不关闭自动注入、Thread、Working Memory 或服务端蒸馏；各功能有独立开关和边界参数。loader 在规范化保存前报告 schema 外字段，动态 `channels` 配置除外。
- `get_runtime_config()`、`get_config_path()` 是单 runtime legacy facade；新 application/adapter 构造函数显式接收 `RuntimeContext`、`RuntimePaths` 或配置快照，不新增对全局可变配置的依赖。
- Provider 由 registry/spec 按 model 解析；resolver/cache 属于 runtime owner。DeepSeek、LiteLLM、Custom、OpenAI Codex 和 OAuth/local adapter 负责协议格式、响应归一化及通用瞬时错误重试。图片能力以实际请求结果为准；`VisionFallbackService` 识别明确的视觉拒绝，使用全局 `visionModel` 或 `assistantModel` 提取描述，并对原模型重试一次。`ProvidersConfig` 字段必须与 registry provider 集合一致。
- provider/client/cache 失败分类为 timeout、dependency、policy、validation、state、internal 或 cancelled；取消不能包装成模型错误。API key、base headers、cookie 和原始 provider 响应必须经过脱敏。
- Nowledge Thread 捕获、distill、auto naming、evaluation 和 compaction 是受限后台后处理，不得无界阻塞最终回复；Nowledge 不可用时基本聊天路径仍可降级运行。

## Channel、后台服务和 HTTP/Web

- `BaseChannel` 是外部 plugin 兼容 facade；channel callback 只做 ACL、字段归一化、media/reply target、principal 和 event 发布，不调用 AgentService、不解析业务命令、不读 session/turn 状态。
- `ChannelDescriptor` 提供 `name`、`factory`、`source`、`version`、capabilities 和 config schema；`ChannelCapabilities` 至少区分 progress、tool_events、media、reply_threads、interactive_reply。能力只能按实现证据声明，未知能力保持 false。
- `ChannelManager` 负责 discovery、构造、启动握手、隔离失败和 stop；`OutboundDispatcher` 负责队列消费、能力过滤、有限指数退避和 terminal delivery state。
- channel send 失败不能生成 inbound；重复 callback、重连、typing/reaction/polling task 必须有 dedup、owner 和 close path。一个 channel 启动失败不应阻塞其他 channel，核心 runtime 失败才触发整体清理。
- `CronService` 只负责 jobs.json、schedule、timer 和通知策略；`HeartbeatService` 只负责 tick、HEARTBEAT.md 和 single-flight。二者通过 `SystemTurnGateway.submit()` 生成 `TurnRequest`，不直接构造 AgentLoop 私有参数或调用 Provider。
- `ApiRuntime` 由 supervisor 管理 core API 与 Web BFF listener；WebChannel 只负责 HTTP ingress/outbound 适配。SSE、artifact、HTTP client、socket 和静态资源均由明确 owner 关闭。
- core API 使用 Bearer token；配置为 `null` 时每次启动生成随机 token。Web BFF 在进程内读取 token，并使用 HttpOnly session cookie、Origin/CSRF 校验和有界登录限流，浏览器侧不持有 core bearer。
- `api.enabled=false` 且 Web 开启时，runtime 在 `127.0.0.1` 随机端口启动私有 core API；Web 关闭时只保留显式启用的 core API。
- 语音转写是可选 channel capability；失败只影响语音输入，不破坏同一渠道的文本消息。

## 扩展方式

### 新增命令

1. 在 `CommandSpec` registry 注册 canonical name、aliases、group、usage/examples、权限、执行策略和 handler。
2. handler 只接收显式 `CommandContext`，返回 `CommandResult`；不要在 handler 里访问 channel SDK、直接发消息或调用 Provider。
3. 若影响 session、runtime、approval 或持久状态，先声明 `execution_effect`、history policy 和 cancellation 行为。
4. 通过 `CommandFeedback`、`command_list()` 和所有启用渠道检查未知/错参/成功反馈；不另写 `/help` 文案。

### 新增工具

1. 实现 `Tool` schema 与 `capabilities`，用 `tool_ok/tool_err` 定义返回；在 Registry/ToolHost 注册来源和 descriptor。
2. 为文件、命令、网络、进程、SSH、MCP、消息或持久化副作用接入 `SecurityPolicy`；工具层只负责参数校验、I/O 和结构化错误，不读取策略或绕过策略。
3. 明确 timeout、取消、并发类别、输出上限、资源 owner、失败/部分成功语义；至少验证不同 session 的 context 不串扰。

### 新增 Provider/Channel/后台服务

- Provider：新增 registry descriptor、settings/schema 和 adapter；由 resolver 构造，不能改 TurnCoordinator 的 provider 分支。
- Channel：新增 descriptor/factory、normalized inbound/outbound、capabilities、ACL、生命周期和 intervention renderer；不得增加 AgentLoop 分支。
- Cron/Heartbeat/Webhook/维护任务：新增 system-turn request 和 `SystemTurnGateway` 调用，不复制一条 LLM/tool pipeline。

## 施工与验证

- 修改前检查 `git status --short`、当前 diff、本文件的“当前已知边界”和相关调用链；工作区已有改动视为用户内容，不能覆盖或借机重构无关模块。
- 多阶段工作用 TODO/计划实时记录 owner、已完成项、风险和下一批；每个批次结束更新本文件的“当前已知边界”，而不是只在任务末尾总结。
- 先做静态验证，再做被明确授权的运行验证。默认不新增测试脚本、不调用在线 Provider/channel/MCP/Nowledge；用户授权后优先隔离 workdir、本地 HTTP API、浏览器和离线组件回归，真实 Provider/API 验证单独标注。
- 当前基础检查：`uv run ruff check nanocat hatch_build.py`、`uv run python -m compileall -q nanocat`、`git diff --check`。涉及前端或打包时增加 `npm test`、`npm run build`、`uv build` 与 `uv export --frozen --no-dev --no-emit-project`；涉及入口时检查 `python -m nanocat --help`、`nanocat --help`、服务启动和 `build_runtime`。
- 验证报告区分：静态/AST/import、配置/JSON 只读迁移、构建/打包、本地运行、实际 Provider、在线渠道、Docker。未执行的外部依赖验证不能写成通过。
- 资源类修改至少审查：start/stop/close 幂等、部分启动失败、取消传播、task owner、timeout、队列上限、临时文件和 socket/client 清理。
- 安全类修改至少审查：HARD_DENY 不可批准、fingerprint 重检、principal/conversation/turn 边界、审批送达失败、日志脱敏和没有 LLM approval path。

## 当前已知边界

以下是当前实现事实，不是待恢复的旧架构：

- P0–P8 施工批次已完成，实时命令控制面已形成独立 command lane；`AgentLoop` 仍是兼容性 turn engine，其内部 turn、工具、MCP、process/HTTP owner 尚未完全拆为独立 application service。
- `RuntimeSupervisor`、`AgentService`、`CommandDispatcher`、`ToolExecutor`、`ToolHost`、`MCPHost`、`OutboundDispatcher` 和 `SystemTurnGateway` 已形成主要 owner 边界；legacy plugin 自行创建的资源仍属于兼容风险。
- global config path/config 仍是单 runtime 兼容状态，不能宣称多 runtime 并行隔离；provider/background/process/HTTP 的完整统一配额仍由各 adapter 负责。
- 动态 channel plugin 能力声明、在线渠道的完整原生菜单/交互组件和所有 SDK 内部 task 仍是保守兼容边界。
- `tools.policy.autoApproveMode` 已接入 `AutoApprovalReviewer`：所有非硬拒绝的待审批调用并发进行 assistantModel 审查；工作区外的无害读写可以被自动批准，路径位置本身不是拒绝理由。自动批准只作用于当前 fingerprint，自动拒绝或审查失败进入单请求人工复核。自动审查显式不携带 `reasoning_effort` 或 token 上限，`HARD_DENY`、restrict 规则和 session/turn grant 保持更高优先级。
- `/approve` 无参数等效 `/approve once`；自动复核的渠道消息只提供 `/approve` 与 `/deny`，Web 卡片按 broker 的 `allowed_actions` 渲染，普通审批与 AUTO/YOLO session 授权保持独立。
- `ContextLookupTool` 和 `ContextArtifactStore` 已移除。runtime composition root 创建单个 `RuntimeFileStore`，在 `workspace/_runtime_temp/sessions/<session-hash>/` 下统一管理 tools、compaction、context、images 和 HTTP stream 副本；主 agent、subagent、Nowledge Thread、压缩源快照、context budget 裁剪和图片桥接共用该 owner。LLM 通过 `read_file`/`grep_file` 按路径分页或检索，`fileTools=false` 时保持文件工具关闭。默认配额为单文件 64 MiB、单 session 256 MiB、全局 2 GiB，完成文件按 LRU 回收，活动 stream 预留最大文件容量；副本写入、回收或缺失只影响按需追溯，session 主数据链路继续执行。旧 `data/sessions/_artifacts` 保持原状，无迁移和清理行为。
- `read_file` 和 `grep_file` 的正文返回受 `maxChars<=5000` 约束；`read_file` 通过 line offset 与 column offset 续读超长单行，`grep_file` 通过 line offset 继续匹配。读取 runtime 文件会刷新 LRU，session 删除成功后由 control 层按实际 session scope 尝试清理对应目录。
- runtime composition root 创建单个 `VisionFallbackService` 并注入 AgentLoop 与 SubagentManager；二者共享 128 项图片描述缓存和有界 HTTP client。provider 原样发送图片，明确视觉拒绝才触发描述替换与单次重试；视觉描述调用省略工具定义和 token 参数，并显式使用 `reasoning_effort=None`。`parse_image` 已移除，`load_image` 是唯一公开图片工具。离线伪 provider 已覆盖直接视觉成功、DeepSeek 风格拒绝、多图顺序与缓存、误触发保护、回退失败和损坏图片；真实 DeepSeek 多模态与全局视觉模型 API 行为仍需独立在线验证。
- `CompactionCheckpoint` 已接入 Session JSON：压缩结果保存结构化 goal/state、source range/revision/hash、模型名和 token 前后值；压缩只在完整 turn 边界推进，revision/CAS 失败或 provider 失败均不推进 cursor，失败不丢原始 history。
- `/compact` 是手动压缩入口，`/compact status` 同时返回上下文预算、压缩游标、checkpoint、失败次数和可压缩状态；上下文状态查询已并入 compact，不再注册独立的 `/context` 命令。非法 compact 参数在 Provider 前由 CommandRouter 拒绝；手动压缩返回完成范围、token 前后值或明确失败原因。
- `agents.defaults.compactionEnabled` 只控制自动 token 触发；`/compact` 手动触发仍可显式执行。`/compact status` 同时显示该开关，避免把“可压缩”和“会自动压缩”混为一谈。
- `/session list` 使用完整 user→assistant turn 边界计算对话轮数，并在每个列表项中显示 `N turns`；现有最少轮数筛选、按最近活跃排序和数量限制保持不变。
- compaction model 可由 `agents.defaults.compactionModel` 独立指定；DeepSeek `deepseek/deepseek-v4-flash` 已完成 8/12/16 turn、约 9k/29k/58k token 虚拟 session 的真实 API 回归，压缩后约 3.9k/7.8k/11.9k token，最大 case 保留大输出引用。compaction、Nowledge、vision、话题命名、heartbeat 和 evaluator 调用均显式不携带 `reasoning_effort`。
- 真实 DeepSeek provider 与上述 compaction 回归已验证；本轮 HTTP/Web、浏览器、Docker 和外部渠道的验证状态以后续条目为准。
- 文案配置已移除：新增用户文案改 `application/text_catalog.py`。
- 本轮 Nowledge 配置已改为 `memory` 直层；本地 `autoExtractMemories` 和 session extraction 标记已删除，自动蒸馏由 Nowledge 服务负责。
- `nanocat/agent/nowledge_client.py` 已提供共享连接、结构化错误、有限重试、健康缓存、Space 默认传播、Thread、Memory、distill 和后台状态 API；`agent/memory.py` 只保留 `MemoryCompactor`、`MemoryStore` 和 Thread capture facade。
- Nowledge 连接超时、健康探测、重试次数/间隔、连接池大小、Thread 单消息上限、蒸馏阈值/级别/语言、Working Memory 超时/长度和 autoInject 检索参数均从 `config.json` 进入对应 owner；`/memory status` 返回脱敏后的有效运行配置，不返回 API key。
- Thread v2 捕获已保留 user/assistant/tool 顺序，执行凭证脱敏，超长消息使用 workspace runtime 文件路径、预览和大小信息；session metadata 保存 ack 游标，append 失败时按游标重放并使用幂等键。
- `memory_thread_search`/`memory_thread_get` 为 LLM 提供原始 Thread 的按需检索，结果保留来源元数据、消息顺序和有界正文；Memory 工具支持 `unit_type`、labels 与 Space guard，`/memory spaces` 读取 Nowledge Space roster。
- autoInject 已改为完整问题检索、历史追问 deep 路由、Space 传播、摘要/来源注入和 session 去重；Nowledge 实测相关性分数约 0.34–0.38，因此默认 `minScore` 为低限保护线 0.25，不能恢复为固定 0.7。
- `memory status` 即使 Nowledge 当前关闭或不可用也可查询本地有效配置；只有实际需要服务的 memory 子命令在客户端不存在时返回不可用，避免把“配置关闭”和“状态查询失败”混成同一错误。
- 已使用 `data/config.json` 的 Nowledge 服务验证 health、fast/deep search、Working Memory、agent status、processing status、Space roster、Thread Search/Get、`/memory status` 和 `/memory spaces`；临时 Thread 的真实 create/append/get/delete、triage 与 distill apply 已验证并清理。官方文档列出的 `distill/preview` 在当前服务部署返回 HTTP 404，NanoCat 保留明确失败，不伪造 preview 或回退为 apply。
- TUI channel、Textual 源码、可选依赖、启动脚本和入口模式已移除；NanoCat 只保留渠道服务启动模式。`nanocat` 与 `python -m nanocat` 接受 workdir/verbose 参数并进入相同 runtime。
- `ApplicationControlService` 为 HTTP API 提供 session、model、compact、turn cancel、approval、runtime snapshot、logs 和 command action；HTTP handler 返回稳定 JSON。`POST /sessions/{id}/turns|steer` 只接收普通 agent 输入，在任何 turn/join/steer/attachment reservation 前以 422 拒绝 slash command candidate；Web Composer 和外部调用方通过 `/commands/execute` 进入 `CommandDispatcher`。
- `ApplicationControlService.routing_guard()` 以 `web:<chat_id>` 串行化 turn admission 与 active mapping 变更，并支持当前 asyncio task 的有界重入。session create/new/switch/delete、cancel、compact、approval 和 command 的复合 HTTP 操作在同一 guard 内重新校验目标；compact status 按显式 session id 只读目标 Session，查询 inactive session 保持 active mapping 和 intervention 状态。
- `MessageBus` 分离 normal/command/control outbound 队列，并分别跟踪每个 session 的 pending normal 与 command ingress；普通 outbound 在同一 turn 内保持 progress/tool/final FIFO，control outbound 保留优先通道。session 删除的 idle reservation 同时覆盖 queued/active command、normal turn、direct turn、steer 和后台 scope task。
- `ApiConfig` 与 `WebChannelConfig` 使用 schema v2；loader 直接验证当前 schema。旧 gateway 与 TUI 配置迁移、备份和 session 导入路径已删除，现有 Web session 数据继续按普通 session 读取。
- 已有 `config.json` 的解析或 schema 校验失败会抛出 `ConfigLoadError` 并终止启动，源文件保持原字节；文件缺失时使用默认配置。`NANOCAT_WEB_HOST`/`NANOCAT_WEB_PORT` 只形成运行时覆盖，供容器绑定使用。
- `ApiRuntime` 始终使用同一 core API；公开 API 与内部 Web BFF 共用 control、WebChannel、SSE broker、artifact registry 和 activity journal。公开 API token 只在显式 API 启用且配置为空时输出一次，私有 core token 保持进程内可见。
- Preact/Vite 前端位于 `frontend/`，生产静态资源由 Hatch custom build hook 编译并打入 wheel；源码 checkout 的 `uv run nanocat` 通过源指纹按需执行锁文件安装与前端重建。
- 安全授权参数污染已修复：`ToolRegistry` 保留授权对象作为内部校验输入，不再注入 `_security_authorization`；Shell/proc 不再通过删除该字段掩盖边界错误，MCP wrapper 的远程 `arguments` 保持原始工具参数。在线 MCP 服务仍需在获得明确运行验证授权后检查。
- `/restart` 的 shutdown race 已修复：CommandDispatcher 被关闭导致 supervisor wait 收到取消时，等待当前 shutdown 完成，外层重复 stop 复用同一 shutdown，不取消继续执行 `os.execv` 的 restart task。当前仅完成静态验证，实际重启行为仍需明确运行验证。
- runtime 逆序关闭顺序为 heartbeat/cron/commands/channels → agent final drain → intervention/message bus；所有 normal ingress producer 收敛后，agent 保存 active、direct、deferred 和 queued 输入的 terminal record，message bus 最后关闭。supersede 等待窗口会在 stop/shutdown 后重新检查 owner 状态，禁止创建脱离 snapshot 的迟到 task。
- Web command palette 由 HTTP command catalog 驱动，导航项执行打开动作，命令项执行 command action；Composer 将 slash 输入交给 command API 并显示结构化反馈。session workspace、trajectory、inspector、diagnostics、settings、审批卡和响应式移动导航共享同一 API client/store。
- `SessionManager.delete_session()` 先在同目录移动 session 文件到 tombstone，再提交 metadata；提交失败恢复原文件。Web 删除 active session 时传入 `create_replacement=false`，并在 routing guard 内完成 ingress/outbound 排他、后台任务 drain、runtime/activity scope 清理和 durable delete。
- 首个 Session 先原子创建 JSONL，再以单次 metadata commit 发布名称、owner 和 active index；commit 失败时回滚新文件。读取现有 Session 会校验 active、metadata owner、唯一 header、payload key 与文件身份，任何不一致都 fail-closed。`set_active` 在更新索引前实际加载并验证目标 payload。
- Web 对话正文来自 Session completed-turn projection；activity journal 持久化脱敏后的结构化状态、工具输入输出摘要、bounded thinking 正文和 artifact 引用，SSE 提供 live 增量。conversation、activity 与 historical trace 分别分页，客户端按稳定 node/tool-call ID 合并。
- `WorkingFeed` 是对话内 thinking、tool、subagent、approval 和 progress 的统一组件；tool input/result 按 call ID 合并为单节点。活动时展开并贴底跟随，用户上滚后暂停跟随，回到底部恢复；终态成功自动折叠并显示总耗时，失败与取消保持展开。节点状态按执行顺序收敛，只有当前活动尾节点显示 Running，内部 `contentChars`/`artifactCount` telemetry 仅作为结构数据处理；`turn.queued` 与 `turn.steer_queued` 属于 ingress 状态，从 Working 明细省略。
- Web `TrajectoryLedger` 使用按 turn 分组的紧凑 ledger；tool start/end 按 call ID 合并，主表呈现事件类型、单行摘要、耗时和时间，完整 input/output 进入 Inspector。三轨 overview、搜索、turn 折叠、历史向前加载和尾部跟随共用同一事件投影；trajectory 省略 running/queued/cancelling 临时状态。桌面 rail 与移动 tabbar 仅保留全局导航，Conversation/Trajectory 统一由 session 顶栏切换。
- Conversation 在 session 首次加载与页面刷新后即时定位最新消息；短对话从 composer 上方贴底生长。内容高度变化仅在用户处于尾部时推动外层滚动。
- Web 密码为空时跳过登录页并输出终端告警；配置密码后使用有界 session、每来源与全局 token bucket、指数锁定和 `Retry-After`。所有浏览器 mutation 都需要同源 Origin；受保护模式同时校验 CSRF。
- session 自动命名按 `session.key` single-flight；达到三轮后的重复 save 共用同一后台命名任务，手动 rename 会取消待完成的自动任务并覆盖缓存，自动任务提交前重新读取 metadata，保证已有名称优先。
- `/stop` 在任何 await 前建立 session stop 域、截取同 session queued 输入并取消 active/direct turn；全局 durability scheduler 跳过活动 stop 域，并在取得 session lock 后再次检查，域内新 ingress 由 stop owner 分类后继续入队或形成一次 terminal record。append-only turn ledger 保存已形成的 user/assistant/tool 消息，为尚未记录结果的 tool call 在下一 user/assistant 前补充结构化 cancelled result，并把实际 stop 反馈作为 terminal assistant。未 drain steer 独立归属其原始 normal session；工具轮数上限、Provider/application error、runtime interruption和空/pulse 输出均形成可见 terminal record；空闲 `/stop` 仅返回 control outbound，history 保持原状。
- Session/stop/delete 链路经过多轮独立复审；聊天消息通过 revision CAS 防止旧实例覆盖。Nowledge Thread ack 在 CAS 冲突后重新加载并单调合并游标，只回滚其拥有的 metadata 字段；compaction checkpoint 仍依赖 Session revision/CAS。`_deleted_keys`、session write lock、RuntimeFileStore/ActivityJournal blocked scope 在大量创建删除下存在常驻集合增长边界。
- `sshTools` 已包含单文件 `ssh_upload`/`ssh_download`：二者复用系统 `scp` 与 SSH config/key/agent，具有显式 deadline、进程组/进程树取消、有界 stderr 和结构化部分成功语义；下载写入目标同目录临时文件后原子替换。`ssh_open` 与 transfer 共用严格 host 和 workspace-relative identity 解析，identity 进入 `restrictPathToWorkspace`，公开 schema 已移除可注入本地命令或路径选项的 raw `extra_args`。远端路径拒绝 shell-sensitive 字符并保持 SSH 语义；transfer capability 均进入 `REQUIRE_INTERVENTION`。SSH 工具日志只记录参数字段名和结果完成事件，Docker runtime 安装 `openssh-client` 提供 `scp`。
- Web BFF 的 loopback proxy client 禁用环境代理；嵌入式 Uvicorn listener 不接管进程 signal，SIGINT 由 supervisor 收敛。`RuntimeSupervisor.stop()` 的所有调用者共享完整 stop task。
- intervention deferred 输入由单一有界 worker 持有；审批调用只负责将消息移交 worker。`/stop` 与 exact cancel 等待活动 sink 得到确定结果后再次 drain，已提交的 bus 消息和未提交的 broker 消息各保留一个 owner。sink 失败进入有界 durability owner；重复 stop、caller cancellation 与 critical close timeout 均保留同一清理 task。
- ArtifactRegistry 将 manifest/file I/O 与 lease/pin 锁分离；上传支持短写循环、物理临时文件配额和事务化回收。stream、SSE finalizer、turn terminal retention race 与 caller cancellation 均通过显式 lease/close owner 收敛；脱离 asyncio awaiter 的 write/flush/close 在底层 I/O 结算后重试临时文件回收，覆盖 Windows 句柄占用窗口。
- SSE client 将 overflow/replay-window reset 保持为 pending recovery，authoritative snapshot 成功后再恢复事件订阅；失败按有界退避重试，reset stream 与 bfcache suspend/resume 均显式释放原 subscriber。
- settings API 按 JSON schema 层级输出字段并标注 `live`、`next_turn`、`reconnect`、`restart`；仅 core API/Web listener 的 enabled/host/port 六项要求重启。其余配置通过 runtime applier 定向更新或换代 provider cache、channel、MCP、Memory/Nowledge、鉴权、并发、runtime file、compaction、heartbeat、transcription、tool 与 policy owner，应用失败回滚持久与有效配置。配置服务在读取时合并 `ChannelDescriptor.config_schema` 的缺省字段，现有持久值优先；runtime 与 `ChannelManager` 共用同一 descriptor 集合，因此未写入 `config.json` 的 QQ、Telegram 等内置渠道仍会出现在设置树并可保存、热重连。模型目录按 provider 分组，模型字段使用目录下拉，modelChoice 通过稳定模型 ID 增删。
- session 列表与详情 API 由 turn owner 的 `busy` 状态决定运行态；activity/conversation projection 和 SSE store 会丢弃同一 turn 终态之后的迟到 transient 记录，历史 journal 中的 `final → late thinking` 不再使页面恢复为 Running。
- 2026-08-23 本地隔离实例已验证密码登录、错误密码 401、第四次失败 429、`Retry-After`、锁定期间正确密码回退、CSRF、session CRUD、conversation/trajectory projection、command、上传、artifact metadata/Range、SSE `turn.queued`、exact turn cancel、删除后 404 和统一关闭。后端离线回归 100/100、Vitest 20/20、Playwright Chromium 桌面/平板/移动端真实 API 与视觉回归 27/27、TypeScript/Vite build 通过；Working、刷新贴底、session header、navigator current state、Trajectory ledger、Inspector、终态后迟到 progress、Guidance 过滤和动态渠道设置已覆盖响应式基线与人工截图检查。
- wheel 运行无需 Node；源码 checkout 和由 sdist 构建 wheel 需要 Node/npm。配置 settings PATCH 使用 revision/ETag CAS、原子保存与 runtime applier。shutdown 按 runtime owner 图停止 ingress producer、完成 agent durability drain、收敛 outbound 与 adapter；critical closer 超时后由持久 continuation 完成依赖关闭。
- Docker 镜像构建、其他在线渠道、外部 MCP/Nowledge、真实 Provider 对话与真实 SSH 传输保持独立验证边界。

后续 agent 不应重新推导本文件已经明确的架构和契约；先看本节，再根据实际代码和用户目标选择工作范围。完成后把实际差异、验证边界和剩余风险写回本文件。
