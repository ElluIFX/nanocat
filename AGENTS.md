# NanoCat Development Notebook

本文件是 NanoCat 源码开发索引和长期规约，也是后续 agent 的唯一项目上下文来源，不是需求说明、发布说明或操作手册。开始工作前先读本文件和相关源码；架构、接口、风险或验证结论变化时直接更新本文件，删除已失效的过程结论。

`nanocat/templates/**` 是随包分发的模板/Skill 载荷。其内部 `AGENTS.md` 只约束模板载荷自身，不属于本文件，不参与 NanoCat 源码架构推导；除非任务明确涉及模板载荷，否则不要修改或解释它。

## 工程事实

- 依赖、可选依赖、构建包内容、入口点、Ruff 规则和版本约束只以 `pyproject.toml` / `uv.lock` 为准，不在本文件复制一份易漂移的版本表。
- 主入口是 `nanocat.runtime.launcher:main`；兼容入口包括 `python -m nanocat`、`nanocat gateway`、`nanocat tui`、`build_runtime(...)`、`run_gateway_async(...)` 和 `run_local_tui(...)`。
- `workdir` 是运行时锚点：其父目录承载 `config.json`，并决定 workspace、sessions、cron、logs、media 和 restart notification 路径。
- `data/config.json` 是本地实例数据，不是 schema；凭证、cookie、token、SSH 信息和完整敏感参数不能进入代码、日志、文档或提交。
- 用户提示文本由 `nanocat/application/text_catalog.py` 的 `USER_TEXT` 提供；配置只承载行为参数，不能把用户文案重新做成配置字段。

## 架构索引

| 层/模块 | 唯一职责 | 主要入口/契约 |
| --- | --- | --- |
| `nanocat/core/` | 无 SDK 的稳定类型、事件、错误、能力和 port | `ConversationRef`、`Principal`、`InboundEvent`、`OutboundEvent`、`TurnRef`、`InterventionRequest`、`CapabilityDescriptor`、`MessagePort` |
| `nanocat/runtime/` | composition root、生命周期、资源 owner、健康和重启 | `RuntimeContext`、`RuntimeSupervisor.start/wait/run/stop`、`ShutdownCoordinator` |
| `nanocat/bus/` | 进程内有界、带优先级的入站/出站传输 | `MessageBus.publish_*`、`consume_*`/`receive_*`、`close`、`join`、`drain` |
| `nanocat/application/` | 控制面和应用编排，不依赖具体 SDK | `AgentService`、`TurnRequest`、`CommandRouter`、`CommandService`、`ToolExecutor`、`InterventionBroker`、`OutboundDispatcher`、`SystemTurnGateway` |
| `nanocat/agent/` | LLM 上下文、记忆、子代理和内置工具实现 | `AgentLoop` 目前仍是兼容 turn engine；新业务不得继续向其堆职责 |
| `nanocat/security/` | 统一命令、路径、网络和工具能力策略 | `SecurityPolicy`；结果只能是 `ALLOW`、`HARD_DENY`、`REQUIRE_INTERVENTION` |
| `nanocat/session/` | 会话模型、索引、策略和原子 JSON 持久化 | `Session`、`SessionManager` 兼容 facade；迁移不得覆盖失败源文件 |
| `nanocat/providers/` | Provider registry、model matching、客户端和 resolver | `ProviderSpec`/registry、runtime-scoped resolver；具体 adapter 不读取全局配置 |
| `nanocat/channels/` | SDK 适配、ACL、归一化、生命周期和发送 | `BaseChannel`、`ChannelDescriptor`、`ChannelCapabilities`、`ChannelManager` |
| `nanocat/cron/`、`heartbeat/` | 调度和 tick；不直接实现 LLM/tool/channel 业务 | 通过 `SystemTurnGateway` 提交 system turn |
| `nanocat/templates/` | 分发给 workspace 的模板、Skill、脚本和资源 | 仅在模板/打包任务中处理；不与本文件的源码规约混用 |

依赖方向：`core contracts ← application ← adapters/infrastructure`；`runtime` 是唯一 composition root，可以组装所有具体实现。`core` 不导入 asyncio、httpx、Provider SDK、channel SDK 或具体文件实现；application 只依赖 core 和 ports；adapter 负责外部错误隔离和类型归一化。

## 运行链路与 owner

### 消息面/控制面

```text
channel callback
  -> principal/ACL/字段归一化/event_id/dedup
  -> MessageBus
  -> Intervention response
  -> Command candidate
  -> TurnRequest / per-session ordering
  -> Provider
  -> ToolExecutor
  -> OutboundDispatcher
  -> channel adapter
```

- 介入响应和命令属于控制面；普通 turn、tool call、cron、heartbeat、subagent 属于数据面。控制面不能被 LLM prompt 解释。
- 以单个 `session_key` 为顺序边界；不同 session 可并发。任何新增等待、缓存、task、timer、client、stream、process 或临时文件都必须有上限、deadline、owner 和 close path。
- `RuntimeSupervisor` 负责组件启动、健康状态、部分失败隔离和逆序关闭；`stop/close/restart` 必须幂等。不得创建第二个 outbound dispatcher 或 MCP owner。
- 当前兼容边界：`AgentService` 已是 runtime 对外 facade，但 `AgentLoop` 仍拥有实际 turn/tool/MCP/process/HTTP engine；后续拆分只能通过现有 facade、`TurnRequest`、`ToolExecutionContext` 和 ports 进行。
- `OutboundDispatcher` 独占 outbound 消费、能力过滤、有限退避重试和 request-scoped `DeliveryResult`；发送失败不得回写 inbound 或递归触发 turn。

### 核心身份和事件

使用不可变契约传递身份和关联关系，不用 metadata 隐式传递核心控制状态：

- `ConversationRef(channel, chat_id, session_key)`：会话路由键。
- `Principal(id, display_name, roles, attributes)`：ACL、命令和审批主体；`sender_id` 不等同于规范化后的 `principal_id`。
- `InboundEvent`：`event_id`、时间、sender、conversation、content、media、metadata、principal、source、correlation、deadline、priority、request_id、turn_id。
- `OutboundEvent`：conversation、content、reply/media、metadata、correlation、deadline、priority、request_id、turn_id、principal_id。
- `TurnRequest`：`content`、`conversation`、`principal_id`、`source`、`transient`、`metadata`、`deadline_at`；interactive、direct、cron、heartbeat、subagent 共用这一入口。
- `ToolExecutionContext`：`turn_id`、`session_key`、`conversation`、`principal_id`、`state_hook`、`message_id`、`session`、`model`、`user_input`；不得从 AgentLoop 私有字段或全局 context 推断授权。

`metadata` 只能承载 adapter 扩展或事件标志（如 `_control`、`_progress`、`_tool_event`、`_intervention`），不能承载安全许可、session 所有权、turn 状态或业务结果。

## 命令控制面

命令入口在普通 turn/Provider 之前。`CommandClassifier`/`CommandParser`/`CommandRouter`/`CommandService` 是唯一命令路径；渠道只归一化输入，不维护本地业务命令表。

### 识别和执行

- 去除前导空白后，首字符为单个 `/` 的文本是命令候选；普通文本中间的 `/` 不拦截。
- `//text` 还原为普通文本 `/text`；Telegram 等 `/command@bot` 在 adapter 归一化，错误 bot 后缀按未知命令处理。
- 已知命令缺参、错参、未知子命令、未知命令均直接返回 `CommandResult`，不调用 Provider、不写 history、不进入 steer。
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

`InterventionRequest` 必须绑定内部 `request_id`、`kind`、`turn_id`、`session_key`、`conversation`、`principal_id`、`capability`、脱敏后的工具名与参数、`summary`、`call_fingerprint`、允许 action、`resume_mode`、审批流程、可选自动审批意见、过期时间和可选 tool_call_id。`request_id` 只供 runtime 调度、送达回执、去重和 TUI 内部关联使用，用户不需要也不应该提供任何 ID 或随机参数。状态是单向且幂等的：pending → approve_once/approve_turn/approve_forever/reject/expired/cancelled/delivery_failed/consumed。

- `approve_once` 只匹配原始 call fingerprint；`approve_turn` 只对当前 principal、conversation、turn 和 capability 生效。
- `approve_forever` 只对当前 principal、conversation/session 和当前 capability 生效，保存在 runtime 内存中，直到用户发送 `/approve cancel`；它不是全局永久授权，也不覆盖其他 capability。
- `/approve cancel` 撤销当前 principal 在当前 conversation/session 的全部 session 级审批；没有 session 级审批时返回明确的无操作反馈。挂起中的单次申请使用 `/deny`，不把 `/approve cancel` 当作普通文本送入 LLM。
- 批准后必须重新 evaluate policy，并再次比较 fingerprint；不能因为批准期间参数、路径、策略或配置变化而执行另一调用。
- grant、pending request 和 WAITING_FOR_USER 只存在 runtime 内存，不写 session JSON；once 不跨调用，turn 不跨 turn，session grant 不跨 session、principal 或 runtime 复用。
- delivery 失败必须进入终态并终止原调用；不能把“询问未送达”当作已批准。
- TUI 的 ApprovalCard 展示工具名和脱敏后的完整参数；普通审批提供 once/turn/deny，自动审批复核只提供 `/approve` 和 `/deny`，并回到普通 ingress。YOLO/AUTO 工具栏切换负责 `/approve forever` 与 `/approve cancel`，不与 `autoApproveMode` 混用，TUI 不直接调用 Broker。内部 request_id 仅用于卡片去重和状态回写，不显示为用户输入项；卡片处理过期和脱敏，但授权判断仍由 Broker 完成。
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
- 不新增依赖共享可变 `set_context` 跨 session 传播状态；兼容入口仍存在时，只能由 Registry 在调用边界绑定 turn-local context。

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
- transient/system turn、ephemeral block、intervention grant 和 pending request 不写入普通 session history；assistant/tool content list、image placeholder、compaction cursor 和 metadata 语义保持。
- `Config` 由 Pydantic schema 校验 camelCase；当前 `memory` 直接承载 Nowledge 配置，session compaction 配置位于 `agents.defaults`；不保留 `memory.nowledge` 和 `autoExtractMemories`。`memoryTools` 只控制 LLM memory tool 注册，不关闭自动注入、Thread、Working Memory 或服务端蒸馏；各功能有独立开关和边界参数。loader 在规范化保存前报告 schema 外字段，动态 `channels` 配置除外。
- `get_runtime_config()`、`get_config_path()` 是单 runtime legacy facade；新 application/adapter 构造函数显式接收 `RuntimeContext`、`RuntimePaths` 或配置快照，不新增对全局可变配置的依赖。
- Provider 由 registry/spec 按 model 解析；resolver/cache 属于 runtime owner。DeepSeek、LiteLLM、Custom、OpenAI Codex、OAuth/local、vision/reasoning、retry/fallback 的行为属于 provider adapter，不能复制到 TurnCoordinator。`ProvidersConfig` 字段必须与 registry provider 集合一致。
- provider/client/cache 失败分类为 timeout、dependency、policy、validation、state、internal 或 cancelled；取消不能包装成模型错误。API key、base headers、cookie 和原始 provider 响应必须经过脱敏。
- Nowledge Thread 捕获、distill、auto naming、evaluation 和 compaction 是受限后台后处理，不得无界阻塞最终回复；Nowledge 不可用时基本聊天路径仍可降级运行。

## Channel、后台服务和 TUI

- `BaseChannel` 是外部 plugin 兼容 facade；channel callback 只做 ACL、字段归一化、media/reply target、principal 和 event 发布，不调用 AgentService、不解析业务命令、不读 session/turn 状态。
- `ChannelDescriptor` 提供 `name`、`factory`、`source`、`version`、capabilities 和 config schema；`ChannelCapabilities` 至少区分 progress、tool_events、media、reply_threads、interactive_reply。能力只能按实现证据声明，未知能力保持 false。
- `ChannelManager` 负责 discovery、构造、启动握手、隔离失败和 stop；`OutboundDispatcher` 负责队列消费、能力过滤、有限指数退避和 terminal delivery state。
- channel send 失败不能生成 inbound；重复 callback、重连、typing/reaction/polling task 必须有 dedup、owner 和 close path。一个 channel 启动失败不应阻塞其他 channel，核心 runtime 失败才触发整体清理。
- `CronService` 只负责 jobs.json、schedule、timer 和通知策略；`HeartbeatService` 只负责 tick、HEARTBEAT.md 和 single-flight。二者通过 `SystemTurnGateway.submit()` 生成 `TurnRequest`，不直接构造 AgentLoop 私有参数或调用 Provider。
- `run_local_tui()` 将 runtime loop 放入后台线程、Textual UI 留在主线程；关闭由 supervisor 统一收敛。TUI approval card 的视觉状态不能成为授权状态来源。
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
- 先做静态验证，再做被明确授权的运行验证。默认不新增测试脚本、不调用在线 Provider/channel/MCP/Nowledge；用户授权后优先本地 TUI、无网络渠道和离线组件回归，真实 API 验证单独标注。
- 当前基础检查：`uv run ruff check nanocat`、`uv run python -m compileall -q nanocat`、`git diff --check`。涉及打包时增加 `uv build`、`uv export --frozen --no-dev --extra tui --no-emit-project`；涉及入口时检查 `python -m nanocat --help`、gateway、tui 和 `build_runtime`。
- 验证报告区分：静态/AST/import、配置/JSON 只读迁移、构建/打包、本地运行、实际 Provider、在线渠道、Docker。未执行的外部依赖验证不能写成通过。
- 资源类修改至少审查：start/stop/close 幂等、部分启动失败、取消传播、task owner、timeout、队列上限、临时文件和 socket/client 清理。
- 安全类修改至少审查：HARD_DENY 不可批准、fingerprint 重检、principal/conversation/turn 边界、审批送达失败、日志脱敏和没有 LLM approval path。

## 当前已知边界

以下是当前实现事实，不是待恢复的旧架构：

- P0–P8 施工批次已完成，但 `AgentLoop` 仍是兼容性 turn engine；其内部 turn、工具、MCP、process/HTTP owner 尚未完全拆为独立 application service。
- `RuntimeSupervisor`、`AgentService`、`ToolExecutor`、`ToolHost`、`MCPHost`、`OutboundDispatcher` 和 `SystemTurnGateway` 已形成主要 owner 边界；legacy plugin 自行创建的资源仍属于兼容风险。
- global config path/config 仍是单 runtime 兼容状态，不能宣称多 runtime 并行隔离；provider/background/process/HTTP 的完整统一配额仍由各 adapter 负责。
- 动态 channel plugin 能力声明、非 TUI 在线渠道的完整原生菜单/交互组件和所有 SDK 内部 task 仍是保守兼容边界。
- `tools.policy.autoApproveMode` 已接入 `AutoApprovalReviewer`：所有非硬拒绝的待审批调用并发进行 assistantModel 审查；工作区外的无害读写可以被自动批准，路径位置本身不是拒绝理由。自动批准只作用于当前 fingerprint，自动拒绝或审查失败进入单请求人工复核。自动审查显式不携带 `reasoning_effort` 或 token 上限，`HARD_DENY`、restrict 规则和 session/turn grant 保持更高优先级。
- `/approve` 无参数等效 `/approve once`；自动复核的审批消息和 TUI 卡片只提供 `/approve` 与 `/deny`，普通审批与 AUTO/YOLO session 授权保持独立。
- `ContextArtifactStore`、`ContextLookupTool` 和 `ContextBudget` 已接入 AgentLoop/subagent：大 tool observation 完整归档到 session 隔离目录，LLM 只收到脱敏预览并可按 artifact/行号/模式检索；provider 前执行快速 token 预算、保留完整 tool-call 对、超限时只做一次降历史重试。
- `CompactionCheckpoint` 已接入 Session JSON：压缩结果保存结构化 goal/state、source range/revision/hash、模型名和 token 前后值；压缩只在完整 turn 边界推进，revision/CAS 失败或 provider 失败均不推进 cursor，失败不丢原始 history。
- `/compact` 是手动压缩入口，`/compact status` 同时返回上下文预算、压缩游标、checkpoint、失败次数和可压缩状态；上下文状态查询已并入 compact，不再注册独立的 `/context` 命令。非法 compact 参数在 Provider 前由 CommandRouter 拒绝；手动压缩返回完成范围、token 前后值或明确失败原因。
- `agents.defaults.compactionEnabled` 只控制自动 token 触发；`/compact` 手动触发仍可显式执行。`/compact status` 同时显示该开关，避免把“可压缩”和“会自动压缩”混为一谈。
- `/session list` 使用完整 user→assistant turn 边界计算对话轮数，并在每个列表项中显示 `N turns`；现有最少轮数筛选、按最近活跃排序和数量限制保持不变。
- compaction model 可由 `agents.defaults.compactionModel` 独立指定；DeepSeek `deepseek/deepseek-v4-flash` 已完成 8/12/16 turn、约 9k/29k/58k token 虚拟 session 的真实 API 回归，压缩后约 3.9k/7.8k/11.9k token，最大 case 保留 artifact 引用。compaction、Nowledge、vision、话题命名、heartbeat 和 evaluator 调用均显式不携带 `reasoning_effort`。
- 本地 TUI 生命周期、approval card 的去重/过期/脱敏和非在线组件已做运行验证；真实 DeepSeek provider 与上述 compaction 回归已验证；Gemini 在线网关、其他在线渠道 API、Docker 镜像构建、外部 MCP/Nowledge 行为只有在明确执行后才能更新为已验证。
- 文案配置已移除：新增用户文案改 `application/text_catalog.py`。
- 本轮 Nowledge 配置已改为 `memory` 直层；本地 `autoExtractMemories` 和 session extraction 标记已删除，自动蒸馏由 Nowledge 服务负责。
- `nanocat/agent/nowledge_client.py` 已提供共享连接、结构化错误、有限重试、健康缓存、Space 默认传播、Thread、Memory、distill 和后台状态 API；`agent/memory.py` 只保留 `MemoryCompactor`、`MemoryStore` 和 Thread capture facade。
- Nowledge 连接超时、健康探测、重试次数/间隔、连接池大小、Thread 单消息上限、蒸馏阈值/级别/语言、Working Memory 超时/长度和 autoInject 检索参数均从 `config.json` 进入对应 owner；`/memory status` 返回脱敏后的有效运行配置，不返回 API key。
- Thread v2 捕获已保留 user/assistant/tool 顺序，执行凭证脱敏，大型 tool 结果使用 artifact 引用；session metadata 保存 ack 游标，append 失败时按游标重放并使用幂等键。
- `memory_thread_search`/`memory_thread_get` 为 LLM 提供原始 Thread 的按需检索，结果保留来源元数据、消息顺序和有界正文；Memory 工具支持 `unit_type`、labels 与 Space guard，`/memory spaces` 读取 Nowledge Space roster。
- autoInject 已改为完整问题检索、历史追问 deep 路由、Space 传播、摘要/来源注入和 session 去重；Nowledge 实测相关性分数约 0.34–0.38，因此默认 `minScore` 为低限保护线 0.25，不能恢复为固定 0.7。
- `memory status` 即使 Nowledge 当前关闭或不可用也可查询本地有效配置；只有实际需要服务的 memory 子命令在客户端不存在时返回不可用，避免把“配置关闭”和“状态查询失败”混成同一错误。
- 已使用 `data/config.json` 的 Nowledge 服务验证 health、fast/deep search、Working Memory、agent status、processing status、Space roster、Thread Search/Get、`/memory status` 和 `/memory spaces`；临时 Thread 的真实 create/append/get/delete、triage 与 distill apply 已验证并清理。官方文档列出的 `distill/preview` 在当前服务部署返回 HTTP 404，NanoCat 保留明确失败，不伪造 preview 或回退为 apply。

后续 agent 不应重新推导本文件已经明确的架构和契约；先看本节，再根据实际代码和用户目标选择工作范围。完成后把实际差异、验证边界和剩余风险写回本文件。
