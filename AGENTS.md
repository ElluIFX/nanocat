# NanoCat Development Notebook

本文件是 NanoCat 源码开发索引和长期规约。开始工作前读取本文件和相关源码；架构、接口或风险发生变化时更新对应条目，删除过程记录和失效结论。

`nanocat/templates/**` 是随包分发的模板与 Skill 载荷。其内部 `AGENTS.md` 只约束载荷自身，源码任务无需读取或修改。

## 工程事实

- 依赖、构建内容、入口点、Ruff 规则和版本约束以 `pyproject.toml`、`uv.lock` 为准。
- 主入口是 `nanocat.runtime.launcher:main`；`nanocat` 与 `python -m nanocat` 始终启动渠道服务。TUI、启动模式选择及其兼容入口已移除。
- `build_runtime(...)` 是 composition root 构造入口。`workdir` 是运行时锚点，直接包含 `config.json`，并派生 workspace、sessions、activity、cron、logs、media 和 restart notification 路径。
- `data/config.json` 属于本地实例数据。凭证、cookie、token、SSH 信息和完整敏感参数禁止进入代码、日志、文档或提交。
- 用户可见固定文案统一放在 `nanocat/application/text_catalog.py` 的 `USER_TEXT`；配置只承载行为参数。

## 架构索引

| 层/模块 | 职责 | 主要入口 |
| --- | --- | --- |
| `nanocat/core/` | 稳定类型、事件、错误、能力和 port | `ConversationRef`、`Principal`、`InboundEvent`、`OutboundEvent`、`TurnRef`、`InterventionRequest` |
| `nanocat/runtime/` | composition root、生命周期、资源 owner、健康与重启 | `RuntimeContext`、`RuntimeSupervisor`、`ShutdownCoordinator` |
| `nanocat/bus/` | 有界且带优先级的进程内消息传输 | `MessageBus.publish_*`、`consume_*`、`close`、`join`、`drain` |
| `nanocat/application/` | 控制面与应用编排 | `AgentService`、`CommandDispatcher`、`ToolExecutor`、`InterventionBroker`、`OutboundDispatcher`、`SystemTurnGateway` |
| `nanocat/agent/` | LLM 上下文、记忆、子代理和内置工具实现 | `AgentLoop` 仍是兼容 turn engine |
| `nanocat/security/` | 工具、命令、路径和网络能力策略 | `SecurityPolicy` |
| `nanocat/session/` | 会话模型、索引、策略和原子持久化 | `Session`、`SessionManager` |
| `nanocat/providers/` | provider registry、model matching、客户端和 resolver | `ProviderSpec`、runtime-scoped resolver |
| `nanocat/channels/` | SDK 适配、ACL、归一化、能力和生命周期 | `BaseChannel`、`ChannelDescriptor`、`ChannelManager` |
| `nanocat/api/`、`web/` | 通用 HTTP API、Web BFF、鉴权、SSE、artifact 和静态前端 | `ApiRuntime`、`SseBroker`、`ArtifactRegistry` |
| `nanocat/observability/` | 脱敏、健康和有界 activity journal | `ActivityJournal`、`ActivityEvent`、`redact_value()` |
| `nanocat/cron/`、`heartbeat/` | 调度与 tick | 通过 `SystemTurnGateway` 提交 system turn |

依赖方向是 `core contracts ← application ← adapters/infrastructure`；`runtime` 负责组装具体实现。`core` 保持无 SDK、无异步框架、无文件实现；application 依赖 core 与 ports；adapter 隔离外部错误并归一化类型。

## 运行链路与 owner

```text
channel callback
  -> principal / ACL / normalization / event_id / dedup
  -> MessageBus
  ├─ command lane -> CommandDispatcher -> control outbound
  └─ normal lane -> intervention response -> TurnRequest
                    -> Provider -> ToolExecutor -> OutboundDispatcher
```

- 命令与介入响应属于控制面；普通 turn、tool、cron、heartbeat 和 subagent 属于数据面。控制面数据禁止交给 LLM prompt 解释。
- 单个 `session_key` 是顺序边界，不同 session 可以并发。等待、缓存、task、timer、client、stream、process 和临时文件必须声明上限、deadline、owner 与 close path。
- `RuntimeSupervisor` 管理启动、健康、部分失败隔离和逆序关闭。`stop/close/restart` 必须幂等；重复 stop 等待同一 shutdown，restart task 保持独立所有权。
- `CommandDispatcher` 独占 slash command 消费、策略、busy 检查和 control response。`AgentLoop` 只消费普通消息与 system turn。
- `OutboundDispatcher` 独占 outbound 消费、能力过滤、有限退避和 `DeliveryResult`；control outbound 使用保留容量，发送失败禁止回写 inbound。
- `AgentService` 是 runtime 对外 facade；新的 application 能力通过 `TurnRequest`、`ToolExecutionContext` 和 ports 接入，职责禁止继续堆入 `AgentLoop`。

### 核心契约

- `ConversationRef(channel, chat_id, session_key)` 定义会话路由。
- `Principal(id, display_name, roles, attributes)` 定义 ACL、命令和审批主体；adapter 的 sender id 需要规范化。
- `InboundEvent` 与 `OutboundEvent` 显式携带 conversation、principal、correlation、deadline、priority、request_id 和 turn_id。
- `TurnRequest` 统一 interactive、direct、cron、heartbeat 与 subagent 输入。
- `ToolExecutionContext` 携带 turn/session/storage/conversation/principal/state hook，以及 admission 时捕获的 model、subagent model、reasoning effort 和 pulse 快照。
- `metadata` 只承载 adapter 扩展与事件标志；安全授权、session 所有权、turn 状态和业务结果使用显式契约。

## 命令控制面

- 去除前导空白后，以单个 `/` 开头的文本是命令候选；`//text` 还原为普通文本 `/text`。Telegram 等渠道在 adapter 层归一化 `/command@bot`。
- command queue 与 normal queue 独立有界。命令在 Provider 前处理，已知命令参数错误和未知命令直接返回 `CommandResult`，不写普通 history。
- HTTP control action、`process_direct()` 和渠道 slash 输入复用同一命令执行器。
- `/help`、`/logs`、`/whoami`、`/model`、审批、`/stop`、`/restart` 和 `/compact status` 实时处理；修改 session 的操作遵守 busy/idle policy。
- `CommandSpec` 是命令名称、别名、帮助、权限、执行策略和 handler 的单一事实来源。`CommandResult` 保持结构化，handler 禁止直接操作 channel。
- Markdown 反馈通过 `CommandFeedback` 渲染；`CommandResult.ok` 属于控制面结果，禁止伪装为 tool result。

## 人机介入与安全

`SecurityPolicy` 是工具边界唯一策略入口，结果限定为：

- `ALLOW`：执行调用。
- `HARD_DENY`：终止调用，审批无法覆盖。
- `REQUIRE_INTERVENTION`：turn 进入 `WAITING_FOR_USER`，由 `InterventionBroker` 向原始 conversation 请求决策，等待期间暂停 LLM 请求。

长期契约：

- `InterventionRequest` 绑定 request、turn、session、conversation、principal、capability、脱敏参数、fingerprint、允许 action、resume mode、过期时间和可选 tool call id。
- 状态单向且幂等：pending → approved/rejected/expired/cancelled/delivery_failed/consumed。请求送达失败进入终态。
- once grant 绑定 fingerprint；turn grant 绑定 principal/conversation/turn/capability；session grant 绑定 principal/conversation/session/capability，并只存在于当前 runtime。
- 批准后重新执行 policy evaluation 并校验 fingerprint。参数、路径、策略或配置发生变化时重新请求决策。
- Web 审批卡通过通用 HTTP control action 提交；渠道只负责展示和 action 归一化，授权判断归 `InterventionBroker`。
- `tools.policy` 按 safe tool、deny、allow、命令分析、路径和 URL 限制的顺序求值。`HARD_DENY` 与 restrict 规则优先于自动审批。
- `autoApproveMode` 只审查 `REQUIRE_INTERVENTION`，自动批准只作用于当前 fingerprint；审查失败转人工处理。
- 策略拒绝返回结构化 `guidance`，要求 agent 停止相关操作并向用户说明原因。

## 工具契约

```text
ToolExecutor
  -> SecurityPolicy.evaluate
  -> batch preflight
  -> ToolRegistry cast/validate
  -> Tool.execute
  -> normalize/truncate
  -> AgentLoop.add_tool_result
```

- 工具实现 `name`、`description`、`parameters`、`capabilities` 和异步 `execute(**kwargs) -> str`，通过 Registry/ToolHost 注册。
- batch 先完成全量安全预检，再按输入顺序返回结果；并发受 `maxConcurrentToolCalls` 限制，失败时取消未完成兄弟任务。
- tool、MCP、subagent、message/ask/wait 禁止直接发送 channel、写 session 或修改全局授权。
- 工具声明参数校验、超时、取消域、并发类别、输出上限、资源 owner、失败和部分成功语义。副作用工具不进行隐式重试。
- Registry 在调用边界绑定 turn-local context；禁止使用共享可变 `set_context` 跨 session 传播状态。
- 安全授权对象只存在于 `ToolExecutor`/`ToolRegistry` 内部，禁止注入工具参数或远程 MCP arguments。

### JSON 返回

- 使用 `nanocat.agent.tools.base.tool_ok/tool_err` 生成 JSON 字符串。
- 正文使用 `content`；集合沿用 `items/entries/results`；资源使用 `id/session_id/stream_id`；进程使用 `stdout/stderr/returncode`。
- `tool_err` 的 `error` 是明确的一句话，`hint` 只提供恢复建议，异常文本通过 `exc_message()` 脱敏。
- `ToolExecutor` 在返回 LLM 前递归移除成功结果中的布尔真值 `ok`，保留 `ok: false` 与错误字段。
- 大正文由 application 统一截断或写入 `RuntimeFileStore`；工具本身禁止定义独立的上下文截断协议。
- `ToolRegistry` 统一处理工具缺失、参数非法和未捕获异常。

## Session、配置与 Provider

### Session

- Session JSON 按 schema version 兼容读取；解析或迁移失败时保留源文件。写入采用 validate → temp write/flush/fsync → atomic replace。
- history view、completed-turn boundary 与写入校验共享 user-rooted 状态机。损坏片段按 turn 隔离，已完成 turn 继续可见。
- 一次内存 append 的持久化失败回滚 messages、revision 和 updated_at。聊天消息与 metadata 更新使用 revision CAS。
- transient/system turn、ephemeral block、intervention grant 和 pending request 不写入普通 history。
- terminal assistant 保存结构化 error/control；tool call、tool result、terminal assistant 与连续完整 turn 使用同一边界定义。
- 删除先将 session 文件移动为同目录 tombstone，再提交 metadata；提交失败恢复源文件。首个 session 先创建 payload，再原子发布 metadata。

### 配置

- `Config` 使用 camelCase Pydantic schema v2。schema 外字段静默忽略，规范化保存只写当前字段；`channels` 保留动态插件配置。
- JSON 解析或已知字段校验失败抛出 `ConfigLoadError`，源文件保持原字节。文件缺失时使用默认配置。
- `memory` 直接承载 Nowledge 配置；session compaction 位于 `agents.defaults`。
- `get_runtime_config()` 与 `get_config_path()` 是单 runtime facade。新的 application/adapter 构造函数显式接收 runtime context、paths 或配置快照。
- settings PATCH 使用 revision/ETag CAS、原子保存和 runtime applier。只有 listener 地址与启用状态等无法原地替换的字段要求 restart；其他字段声明 live、next_turn 或 reconnect。
- 配置服务合并 `ChannelDescriptor.config_schema` 默认值与持久值，保证 QQ、Telegram 和插件渠道进入统一设置树。

### Provider、Vision 与 Memory

- Provider 由 registry/spec 按 model 解析；resolver 与 cache 属于 runtime owner。adapter 负责协议格式、响应归一化、有限瞬时错误重试和脱敏。
- provider/client/cache 错误分类为 timeout、dependency、policy、validation、state、internal 或 cancelled；取消保持取消语义。
- `VisionFallbackService` 是 application 级单例。provider 先接收原始图片；明确视觉拒绝后，服务使用 `visionModel` 或 `assistantModel` 生成描述并重试一次。`load_image` 是唯一公开图片工具。
- `RuntimeFileStore` 在 `workspace/_runtime_temp/sessions/<session-hash>/` 管理 tool、compaction、context、image 和 HTTP stream 副本。LLM 通过 `read_file`/`grep_file` 分页读取；副本失败只影响追溯能力。
- Nowledge Thread、Working Memory、auto inject、distill 和 memory tools 使用同一 runtime client；后台后处理有界，服务不可用时基本聊天继续运行。
- Thread capture 保持 user/assistant/tool 顺序并使用 ack cursor 与幂等键；超长内容写入 runtime file。Memory 与 Thread 搜索保留 Space guard 和来源信息。
- compaction 只在完整 turn 边界推进 checkpoint；provider/CAS 失败保持 cursor。`compactionEnabled` 只控制自动触发，`/compact` 仍可手动执行。

## Channel、HTTP 与 Web

- channel callback 只做 ACL、字段归一化、media/reply target、principal 和事件发布。业务命令、session 与 turn 状态归 application。
- `ChannelDescriptor` 提供 factory、source、version、capabilities 和 config schema；未知能力保持 false。
- `ChannelManager` 负责 discovery、构造、启动握手、失败隔离和 stop；单个 channel 启动失败保持其他渠道运行。
- channel send 失败禁止生成 inbound。callback、重连、typing、reaction 与 polling task 需要 dedup、owner 和 close path。
- `CronService` 与 `HeartbeatService` 只负责调度，通过 `SystemTurnGateway.submit()` 进入统一 turn pipeline。
- `ApiRuntime` 由 supervisor 管理 core API 与 Web BFF listener；WebChannel 只负责 HTTP ingress/outbound 适配。
- core API 使用 Bearer token；配置为 null 时每次启动生成随机 token。Web BFF 在进程内读取 token，浏览器使用 HttpOnly session cookie、Origin/CSRF 校验和有界登录限流。
- `api.enabled=false` 且 Web 开启时，runtime 在 `127.0.0.1` 随机端口启动私有 core API。Web 关闭时只启动显式启用的 core API。
- `ApplicationControlService.routing_guard()` 串行化同一 Web conversation 的 turn admission 和 active mapping 变更；session、cancel、compact、approval 与 command 操作在 guard 内重检目标。
- SSE、artifact、HTTP client、socket 和静态资源必须由明确 owner 关闭。SSE reset 经过 authoritative snapshot 后恢复订阅；ArtifactRegistry 使用 lease/pin 保护活动文件。

### Web 前端

- Preact/Vite 前端位于 `frontend/`，生产资源由 Hatch build hook 打入 wheel。源码 checkout 通过源指纹按需安装并重建前端；wheel 运行无需 Node。
- Conversation 正文来自 completed-turn projection；activity journal 与 SSE 提供脱敏后的 thinking、tool、control、error 和 artifact 增量。
- `WorkingFeed` 按稳定 node/tool-call id 合并事件。活动 turn 展开并跟随尾部；用户上滚时暂停；成功终态自动折叠，失败和取消保持展开。
- stop、steer、interrupt、runtime shutdown 与 error 使用结构化 feed 组件。TODO、文件、terminal/SSH、network 和 subagent 使用统一 tool card，完整 input/output 保留在 Details。
- Trajectory 按 turn 分组，主表显示摘要、耗时和时间，完整数据进入 Inspector；Conversation 与 Trajectory 由 session 顶栏切换。
- session 列表支持就地 rename 与二次确认 delete；顶栏菜单提供 agent/subagent model、effort、pulse、compact 和 delete。模型选择按 provider 分组，运行中 turn 使用 admission 快照。
- settings 按 JSON 层级折叠，并依据 live、next_turn、reconnect、restart 展示应用方式。
- Web 密码为空时跳过登录页并输出终端告警；启用密码后执行按来源和全局限流、指数锁定与 `Retry-After`。

## 关键运行边界

- `/stop` 与 exact cancel 建立 session stop 域，收敛 active/direct/queued/deferred 输入，并为已形成的 user/tool 记录补充唯一 terminal record。
- 临时 stop requeue 与失败后的 durability queue 使用独立 owner。durability journal 保留 terminal error/control 和 tool status，重启 replay 保持原语义。
- terminal activity 到达后，projection 与客户端丢弃同 turn 的迟到 transient 事件，避免 session 回到 Running。
- runtime 关闭顺序是 heartbeat/cron/commands/channels → agent final drain → intervention/message bus；critical closer 的延续任务保持 owner。
- `ssh_upload`/`ssh_download` 复用系统 `scp` 与 SSH config/key/agent，执行 deadline、进程树取消、有界错误和原子下载替换；transfer capability 进入安全 gate。
- session 删除后清理对应 RuntimeFileStore 与 activity scope；清理失败不能反向破坏已提交的 session 状态。

## 扩展规则

- 新命令注册 `CommandSpec`，handler 只接收 `CommandContext` 并返回 `CommandResult`。
- 新工具实现 schema/capabilities，接入 `SecurityPolicy`，声明 timeout、取消、并发、输出与 owner。
- 新 Provider 增加 registry descriptor、settings schema 和 adapter，由 resolver 构造。
- 新 Channel 增加 descriptor/factory、normalized events、capabilities、ACL、生命周期和 intervention renderer。
- 新后台服务通过 `SystemTurnGateway` 提交 turn，复用现有 LLM/tool pipeline。

## 施工与验证

- 修改前检查 `git status --short`、当前 diff、本文件与相关调用链。工作区已有改动视为用户内容。
- 多阶段任务实时记录 owner、风险和下一批；只把稳定架构、契约与未解决风险写回本文件。
- 只有用户明确要求时才创建或执行测试。验证需要区分静态检查、构建、本地运行、真实 Provider、在线渠道、Docker 和浏览器。
- 用户授权验证时，基础命令为 `uv run ruff check nanocat hatch_build.py`、`uv run python -m compileall -q nanocat` 和 `git diff --check`；前端或打包任务按需增加 `npm test`、`npm run build`、`uv build` 与 frozen export。
- 资源类修改审查 start/stop/close 幂等、部分启动失败、取消传播、task owner、timeout、队列上限、临时文件与 socket/client 清理。
- 安全类修改审查 HARD_DENY、fingerprint 重检、principal/conversation/turn 边界、审批送达失败和日志脱敏。

## 当前边界与风险

- `AgentLoop` 仍拥有部分 turn、tool、MCP、process 和 HTTP engine；后续拆分必须经现有 facade 与 ports。
- global config path/config 仍是单 runtime 状态，多 runtime 并行隔离尚未成立。
- legacy plugin 自建资源、动态 channel plugin 的能力声明和部分 SDK 内部 task 仍属于保守兼容边界。
- provider、background、process 与 HTTP 的完整统一配额仍由各 adapter 分别负责。
- `_deleted_keys`、session write lock、RuntimeFileStore 与 ActivityJournal 的 blocked scope 在大量创建删除场景存在常驻集合增长风险。

后续 agent 先依据本文件确定 owner 与契约，再读取目标源码。完成工作后只记录新的稳定事实与仍然存在的风险。
