import type {
  ApprovalRequest,
  ArtifactRef,
  CommandInfo,
  LogEntry,
  ModelInfo,
  RuntimeModelState,
  RuntimeSnapshot,
  SessionDetail,
  SessionSummary,
  SettingSection,
  SettingsUpdateResult,
  TimelineEvent,
} from "../types";

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryAfter?: number;

  constructor(message: string, status: number, code?: string, retryAfter?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

function csrfToken(): string | undefined {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
  if (meta) return meta;
  const prefix = "nanocat_web_csrf=";
  const cookie = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(prefix));
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : undefined;
}

function randomId(): string {
  return crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD"].includes(init.method.toUpperCase())) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }

  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) {
    if (response.status === 401 && path.startsWith("/api/")) window.location.assign("/login");
    let body: Record<string, unknown> = {};
    try {
      body = (await response.json()) as Record<string, unknown>;
    } catch {
      // The status text remains the fallback for non-JSON proxy errors.
    }
    const retryHeader = Number(response.headers.get("Retry-After"));
    const retryBody = Number(body.retryAfter);
    const retryAfter = Number.isFinite(retryHeader) && retryHeader > 0
      ? retryHeader
      : Number.isFinite(retryBody) && retryBody > 0
        ? retryBody
        : undefined;
    throw new ApiError(
      String(body.detail ?? body.message ?? body.title ?? response.statusText ?? "Request failed"),
      response.status,
      typeof body.code === "string" ? body.code : undefined,
      retryAfter,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function unwrapList<T>(value: T[] | { items?: T[]; sessions?: T[]; results?: T[] }): T[] {
  if (Array.isArray(value)) return value;
  return value.items ?? value.sessions ?? value.results ?? [];
}

type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
}

function textContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => {
    const block = record(item);
    return typeof block.text === "string" ? block.text : typeof item === "string" ? item : JSON.stringify(item);
  }).filter(Boolean).join("\n");
  if (value === undefined || value === null) return "";
  return JSON.stringify(value, null, 2);
}

function normalizeStatus(value: unknown): SessionSummary["status"] {
  const status = String(value ?? "idle").toLowerCase().replaceAll("-", "_");
  if (["idle", "queued", "running", "cancelling", "waiting_approval", "waiting_user", "completed", "failed", "cancelled"].includes(status)) return status as SessionSummary["status"];
  if (status === "waitingforuser" || status === "waiting_for_user") return "waiting_user";
  return "idle";
}

function eventStatus(type: string, explicit?: unknown): TimelineEvent["status"] {
  if (explicit) return normalizeStatus(explicit);
  if (type === "approval.pending") return "waiting_approval";
  if (type === "turn.cancelling") return "cancelling";
  if (type === "assistant.final" || type === "turn.completed") return "completed";
  if (type === "turn.failed" || type === "turn.rejected") return "failed";
  if (type === "turn.cancelled") return "cancelled";
  if (["turn.queued", "turn.steer_queued", "assistant.progress", "tool.event"].includes(type)) return "running";
  return undefined;
}

function sessionDetailStatus(events: TimelineEvent[], busy: boolean): SessionSummary["status"] {
  if (busy) {
    const intervention = [...events].reverse().find((event) => (
      event.type === "approval.pending"
      || event.type === "approval.updated"
      || event.status === "waiting_user"
    ));
    if (intervention?.type === "approval.pending") return "waiting_approval";
    if (intervention?.status === "waiting_user") return "waiting_user";
    return "running";
  }
  const terminal = [...events].reverse().find((event) => (
    ["assistant.final", "turn.completed", "turn.failed", "turn.rejected", "turn.cancelled"].includes(event.type)
  ));
  return terminal?.status ?? "idle";
}

function normalizeSessionSummary(value: unknown, fallbackUpdatedAt = ""): SessionSummary {
  const item = record(value);
  return {
    id: String(item.id ?? item.sessionId ?? item.session_id ?? ""),
    title: String(item.title ?? item.name ?? item.sessionName ?? item.session_name ?? "") || "Untitled session",
    status: normalizeStatus(item.status ?? (item.busy ? "running" : "idle")),
    updatedAt: String(item.updatedAt ?? item.lastActive ?? item.last_active ?? item.createdAt ?? item.created_at ?? fallbackUpdatedAt),
    preview: typeof item.preview === "string" ? item.preview : undefined,
    turnCount: Number(item.turnCount ?? item.turn_count ?? 0),
    model: typeof item.model === "string" ? item.model : undefined,
    revision: typeof item.revision === "number" ? item.revision : undefined,
  };
}

interface ProjectionItem {
  id: string;
  type: string;
  source?: string;
  sequence?: number;
  timestamp?: string;
  status?: string;
  turnId?: string;
  requestId?: string;
  parentId?: string;
  role?: string;
  summary?: string;
  content?: unknown;
  detailAvailable?: boolean;
  metadata?: JsonRecord;
}

interface ProjectionPage {
  items?: ProjectionItem[];
  historicalItems?: ProjectionItem[];
  nextOffset?: number | null;
  nextCursor?: number | null;
  nextHistoricalCursor?: number | null;
  hasMore?: boolean;
  historicalHasMore?: boolean;
  cursorReset?: boolean;
  totalCount?: number;
  previousOffset?: number | null;
  previousCursor?: number | null;
  pageOffset?: number | null;
  historicalPageOffset?: number | null;
  historicalTotalCount?: number;
  oldestSequence?: number | null;
  latestSequence?: number | null;
  source?: string;
  cursorKind?: "sequence" | "offset";
}

const PROJECTION_PAGE_SIZE = 1000;
const INITIAL_CONVERSATION_ITEMS = 500;

async function conversationProjection(sessionId: string): Promise<{ items: ProjectionItem[]; cursor: number }> {
  const page = await request<ProjectionPage>(`/api/v1/sessions/${sessionId}/conversation?latest=true&limit=${INITIAL_CONVERSATION_ITEMS}`);
  const total = Math.max(0, Number(page.totalCount ?? page.items?.length ?? 0));
  const cursor = Math.max(0, Number(page.pageOffset ?? total - (page.items?.length ?? 0)));
  return { items: page.items ?? [], cursor };
}

async function earlierConversationProjection(sessionId: string, before: number): Promise<{ items: ProjectionItem[]; cursor: number }> {
  const cursor = Math.max(0, before - INITIAL_CONVERSATION_ITEMS);
  const limit = Math.max(1, before - cursor);
  const page = await request<ProjectionPage>(`/api/v1/sessions/${sessionId}/conversation?cursor=${cursor}&limit=${limit}`);
  return { items: page.items ?? [], cursor };
}

async function trajectoryProjection(sessionId: string): Promise<{
  historical: ProjectionItem[];
  activity: ProjectionItem[];
  activityCursor: number;
  oldestCursor: number;
  historyCursor: number;
  cursorKind: "sequence" | "offset";
}> {
  const latest = await request<ProjectionPage>(`/api/v1/sessions/${sessionId}/trajectory?latest=true&limit=${PROJECTION_PAGE_SIZE}&historyLimit=${PROJECTION_PAGE_SIZE}`);
  if (latest.cursorKind === "offset" || latest.source === "session") {
    const total = Math.max(0, Number(latest.totalCount ?? latest.items?.length ?? 0));
    const activityCursor = Math.max(0, Number(latest.pageOffset ?? total - (latest.items?.length ?? 0)));
    return {
      historical: [],
      activity: latest.items ?? [],
      activityCursor,
      oldestCursor: 0,
      historyCursor: 0,
      cursorKind: "offset",
    };
  }
  const oldestCursor = Math.max(0, Number(latest.oldestSequence ?? 1) - 1);
  const activityCursor = Math.max(oldestCursor, Number(latest.previousCursor ?? oldestCursor));
  const historyCursor = Math.max(0, Number(latest.historicalPageOffset ?? 0));
  return {
    historical: latest.historicalItems ?? [],
    activity: latest.items ?? [],
    activityCursor,
    oldestCursor,
    historyCursor,
    cursorKind: "sequence",
  };
}

async function earlierTrajectoryProjection(
  sessionId: string,
  activityBefore: number,
  activityOldest: number,
  historyBefore: number,
  cursorKind: "sequence" | "offset",
): Promise<{ historical: ProjectionItem[]; activity: ProjectionItem[]; activityCursor: number; historyCursor: number }> {
  const activityCursor = Math.max(activityOldest, activityBefore - PROJECTION_PAGE_SIZE);
  const historyCursor = Math.max(0, historyBefore - PROJECTION_PAGE_SIZE);
  const historyLimit = Math.max(1, historyBefore - historyCursor);
  const activityLimit = cursorKind === "offset"
    ? Math.max(1, activityBefore - activityCursor)
    : PROJECTION_PAGE_SIZE;
  const page = await request<ProjectionPage>(`/api/v1/sessions/${sessionId}/trajectory?cursor=${activityCursor}&limit=${activityLimit}&historyCursor=${historyCursor}&historyLimit=${historyLimit}`);
  return {
    historical: page.historicalItems ?? [],
    activity: cursorKind === "offset"
      ? page.items ?? []
      : (page.items ?? []).filter((item) => Number(item.sequence ?? 0) <= activityBefore),
    activityCursor,
    historyCursor,
  };
}

function projectionEvent(item: ProjectionItem, index: number): TimelineEvent {
  const metadata = record(item.metadata);
  const output = record(metadata.output);
  const artifactValues = Array.isArray(metadata.artifactRefs)
    ? metadata.artifactRefs
    : Array.isArray(output.artifacts)
      ? output.artifacts
      : [];
  const refs = artifactValues.length ? artifactValues.map((value, artifactIndex) => {
    if (typeof value === "string") return { id: value, name: value.split(/[\\/]/).at(-1) || `Artifact ${artifactIndex + 1}`, kind: "text" as const };
    const artifact = record(value);
    return {
      id: String(artifact.id ?? artifact.path ?? artifactIndex),
      name: String(artifact.name ?? artifact.path ?? `Artifact ${artifactIndex + 1}`),
      kind: String(artifact.kind ?? "text") as ArtifactRef["kind"],
      size: typeof artifact.size === "number" ? artifact.size : undefined,
      mediaType: typeof artifact.mediaType === "string" ? artifact.mediaType : undefined,
      expired: Boolean(artifact.expired),
    };
  }) : undefined;
  const toolCalls = Array.isArray(metadata.toolCalls) ? metadata.toolCalls.map(record) : [];
  const toolNames = toolCalls.map((call) => String(call.name ?? "tool")).filter(Boolean);
  const reasoning = textContent(metadata.reasoningContent);
  const activityOutput = record(metadata.output);
  const visibleContent = item.type === "assistant.thinking"
    ? textContent(activityOutput.content)
    : item.type === "assistant.progress"
      ? ""
      : textContent(item.content);
  const workContent = item.type === "assistant.work"
    ? [reasoning, visibleContent].filter(Boolean).join("\n\n")
    : visibleContent;
  return {
    eventId: item.id,
    nodeId: item.id,
    sequence: item.sequence ?? index,
    timestamp: item.timestamp ?? "",
    type: item.type,
    source: item.source,
    status: eventStatus(item.type, item.status),
    turnId: item.turnId,
    requestId: item.requestId,
    parentId: item.parentId,
    summary: item.summary ?? (toolNames.length ? `Requested ${toolNames.join(", ")}` : undefined),
    content: workContent,
    redactedInput: metadata.input ?? (toolCalls.length ? { toolCalls } : undefined),
    redactedOutput: metadata.output ?? item.content,
    toolCallId: typeof metadata.toolCallId === "string" ? metadata.toolCallId : undefined,
    toolName: typeof metadata.toolName === "string" ? metadata.toolName : undefined,
    artifactRefs: refs,
    phase: typeof metadata.phase === "string" ? metadata.phase : undefined,
    startedAt: typeof metadata.startedAt === "string" ? metadata.startedAt : undefined,
    endedAt: typeof metadata.endedAt === "string" ? metadata.endedAt : undefined,
    durationMs: typeof metadata.durationMs === "number" ? metadata.durationMs : undefined,
    model: typeof metadata.model === "string" ? metadata.model : undefined,
    usage: record(metadata.usage),
  };
}

function conversationTurns(conversation: ProjectionItem[]): SessionDetail["turns"] {
  const turns: SessionDetail["turns"] = [];
  let pendingActivity: TimelineEvent[] = [];
  const flushActivity = () => {
    if (!pendingActivity.length) return;
    const terminal = pendingActivity.at(-1);
    const status = terminal?.type === "turn.failed" || terminal?.type === "turn.rejected"
      ? "failed"
      : terminal?.type === "turn.cancelled"
        ? "cancelled"
        : "completed";
    turns.push({
      id: `work-${terminal?.turnId ?? terminal?.eventId ?? turns.length}`,
      turnId: terminal?.turnId,
      role: "assistant",
      content: "",
      timestamp: terminal?.endedAt ?? terminal?.timestamp ?? "",
      status,
      activity: pendingActivity,
      artifactRefs: terminal?.artifactRefs,
    });
    pendingActivity = [];
  };
  for (const [index, item] of conversation.entries()) {
    const projected = projectionEvent(item, index);
    if (item.type === "assistant.work") {
      pendingActivity.push(projected);
      continue;
    }
    const role = item.role ?? (item.type.startsWith("user.") ? "user" : item.type.startsWith("assistant.") ? "assistant" : undefined);
    if (role === "user" || role === "assistant") {
      if (role === "user" && pendingActivity.length && pendingActivity.some((event) => event.turnId !== item.turnId)) flushActivity();
      turns.push({
        id: item.id,
        turnId: item.turnId,
        role,
        content: textContent(item.content),
        timestamp: item.timestamp ?? "",
        status: item.status ? normalizeStatus(item.status) : undefined,
        activity: role === "assistant" ? pendingActivity : [],
        artifactRefs: projected.artifactRefs,
      });
      if (role === "assistant") pendingActivity = [];
    } else {
      pendingActivity.push(projected);
    }
  }
  flushActivity();
  return turns;
}

function attachTrajectoryActivity(
  turns: SessionDetail["turns"],
  events: TimelineEvent[],
): SessionDetail["turns"] {
  const hidden = new Set(["user.message", "user.steer", "assistant.final", "turn.completed"]);
  const byTurn = new Map<string, TimelineEvent[]>();
  for (const event of events) {
    if (!event.turnId || hidden.has(event.type)) continue;
    const retained = byTurn.get(event.turnId) ?? [];
    retained.push(event);
    byTurn.set(event.turnId, retained);
  }
  const lastAssistant = new Map<string, number>();
  turns.forEach((turn, index) => {
    if (turn.role === "assistant" && turn.turnId) lastAssistant.set(turn.turnId, index);
  });
  return turns.map((turn, index) => {
    if (turn.role !== "assistant" || !turn.turnId) return turn;
    if (lastAssistant.get(turn.turnId) !== index) return turn;
    const trajectory = byTurn.get(turn.turnId) ?? [];
    if (!trajectory.length) return turn;
    const trajectoryTypes = new Set(trajectory.map((event) => event.type));
    const historical = (turn.activity ?? []).filter((event) => (
      !trajectoryTypes.has(event.type)
      || !["turn.failed", "turn.rejected", "turn.cancelled", "turn.cancelling", "turn.steer_queued", "turn.queued"].includes(event.type)
    ));
    const activity = [...historical, ...trajectory].sort((left, right) => {
      const time = Date.parse(left.timestamp) - Date.parse(right.timestamp);
      return Number.isFinite(time) && time !== 0 ? time : left.sequence - right.sequence;
    });
    const terminal = [...activity].reverse().find((event) => event.type === "turn.failed" || event.type === "turn.rejected" || event.type === "turn.cancelled");
    return {
      ...turn,
      status: terminal?.status ?? turn.status,
      activity,
    };
  });
}

function normalizeSettings(payload: unknown): SettingSection[] {
  const envelope = record(payload);
  const root = record(envelope.settings ?? payload);
  const readOnlyPaths = new Set(
    (Array.isArray(envelope.readOnlyPaths) ? envelope.readOnlyPaths : []).map(String),
  );
  const effectiveOverrides = record(envelope.effectiveOverrides);
  const fieldSchema = record(envelope.fieldSchema);
  const restartRequiredPaths = new Set(
    (Array.isArray(envelope.restartRequiredPaths) ? envelope.restartRequiredPaths : []).map(String),
  );
  const titles: Record<string, string> = {
    general: "General",
    agents: "Agents",
    runtime: "Runtime",
    runtimeFiles: "Runtime Files",
    api: "API",
    channels: "Channels",
    providers: "Providers",
    heartbeat: "Heartbeat",
    tools: "Tools",
    transcription: "Transcription",
    memory: "Memory",
    schemaVersion: "General",
  };
  type FlatSetting = { key: string; value: unknown; configured?: boolean; structured?: boolean; secret?: boolean };
  const flatten = (value: unknown, prefix = ""): FlatSetting[] => {
    const schema = record(fieldSchema[prefix]);
    if (prefix && ["json", "object", "array"].includes(String(schema.type ?? ""))) {
      return [{ key: prefix, value: JSON.stringify(value, null, 2), structured: true }];
    }
    if (Array.isArray(value)) return [{ key: prefix, value: JSON.stringify(value, null, 2), structured: true }];
    const object = record(value);
    if (prefix && Object.keys(object).length > 0 && Object.keys(object).every((key) => ["configured", "fingerprint", "keys"].includes(key))) {
      const schema = record(fieldSchema[prefix]);
      return [{ key: prefix, value: "", configured: Boolean(object.configured), secret: true, structured: schema.type === "json" }];
    }
    return Object.entries(object).flatMap(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      if (child !== null && typeof child === "object" && !Array.isArray(child)) return flatten(child, path);
      return Array.isArray(child)
        ? [{ key: path, value: JSON.stringify(child, null, 2), structured: true }]
        : [{ key: path, value: child }];
    });
  };

  const sectionOrder = ["general", "agents", "runtime", "runtimeFiles", "api", "channels", "providers", "heartbeat", "tools", "transcription", "memory"];
  const grouped = new Map<string, FlatSetting[]>();
  for (const [rootKey, value] of Object.entries(root)) {
    const sectionId = rootKey === "schemaVersion" ? "general" : rootKey;
    grouped.set(sectionId, [...(grouped.get(sectionId) ?? []), ...flatten(value, rootKey)]);
  }

  return [...grouped.entries()].sort(([left], [right]) => {
    const a = sectionOrder.indexOf(left);
    const b = sectionOrder.indexOf(right);
    return (a < 0 ? sectionOrder.length : a) - (b < 0 ? sectionOrder.length : b);
  }).map(([sectionId, values]) => ({
    id: sectionId,
    title: titles[sectionId] ?? sectionId.replaceAll("_", " "),
    fields: values.map((item) => ({
      ...(() => {
        const key = item.key;
        const schema = record(fieldSchema[key]);
        const secret = Boolean(schema.secret) || item.secret || /password|token|api.?key|secret|credential|authorization|cookie/i.test(key);
        const structured = schema.type === "json" || schema.type === "array" || schema.type === "object" || item.structured;
        const options = Array.isArray(schema.options) ? schema.options : Array.isArray(schema.enum) ? schema.enum : [];
        const applyMode = String(schema.applyMode ?? (restartRequiredPaths.has("*") || restartRequiredPaths.has(key) ? "restart" : "live"));
        return {
          key,
          label: String(schema.label ?? key.split(".").at(-1) ?? key).replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " "),
          type: structured ? "json" as const : options.length ? "select" as const : secret ? "password" as const : schema.type === "boolean" || typeof item.value === "boolean" ? "boolean" as const : schema.type === "number" || schema.type === "integer" || typeof item.value === "number" ? "number" as const : "text" as const,
          value: secret ? "" : item.value,
          options: options.map((option) => typeof option === "object" && option !== null
            ? { label: String(record(option).label ?? record(option).value ?? ""), value: String(record(option).value ?? "") }
            : { label: String(option), value: String(option) }),
          secret,
          configured: item.configured,
          readOnly: readOnlyPaths.has(key),
          overriddenValue: Object.prototype.hasOwnProperty.call(effectiveOverrides, key) ? effectiveOverrides[key] : undefined,
          restartRequired: restartRequiredPaths.has("*") || restartRequiredPaths.has(key),
          applyMode: (["live", "next_turn", "reconnect", "restart"].includes(applyMode) ? applyMode : "live") as "live" | "next_turn" | "reconnect" | "restart",
          minimum: typeof schema.minimum === "number" ? schema.minimum : undefined,
          maximum: typeof schema.maximum === "number" ? schema.maximum : undefined,
          description: typeof schema.description === "string" ? schema.description : Object.prototype.hasOwnProperty.call(effectiveOverrides, key)
            ? `Runtime override currently applies: ${String(effectiveOverrides[key])}`
            : undefined,
        };
      })(),
    })),
  }));
}

function normalizeLogLevel(value: unknown): LogEntry["level"] {
  const level = String(value ?? "info").trim().toLowerCase();
  if (["error", "critical", "fatal"].includes(level)) return "error";
  if (["warning", "warn"].includes(level)) return "warning";
  if (["debug", "trace"].includes(level)) return "debug";
  return "info";
}

function normalizeLogTimestamp(value: unknown): string {
  const text = String(value ?? "").trim();
  const time = text.match(/^(\d{2}):(\d{2}):(\d{2})$/);
  if (!time) return text;
  const date = new Date();
  date.setHours(Number(time[1]), Number(time[2]), Number(time[3]), 0);
  return date.toISOString();
}

function parseLogLine(value: unknown, index: number): LogEntry {
  const item = record(value);
  if (Object.keys(item).length) return {
    id: String(item.id ?? index),
    timestamp: normalizeLogTimestamp(item.timestamp),
    level: normalizeLogLevel(item.level),
    source: String(item.source ?? "runtime"),
    message: String(item.message ?? item.content ?? ""),
    sessionId: typeof item.sessionId === "string" ? item.sessionId : typeof item.session_id === "string" ? item.session_id : undefined,
    turnId: typeof item.turnId === "string" ? item.turnId : typeof item.turn_id === "string" ? item.turn_id : undefined,
    requestId: typeof item.requestId === "string" ? item.requestId : typeof item.request_id === "string" ? item.request_id : undefined,
  };
  const raw = String(value);
  const pipe = raw.match(/^(.*?)\s+\|\s+(TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL)\s+\|\s+(.*?)\s+-\s+(.*)$/i);
  if (pipe) return { id: String(index), timestamp: normalizeLogTimestamp(pipe[1]), level: normalizeLogLevel(pipe[2]), source: pipe[3], message: pipe[4] };
  const bracket = raw.match(/^\[([^\]]+)]\s*\[([^\]]+)]\s*(.*)$/);
  if (bracket) return { id: String(index), timestamp: normalizeLogTimestamp(bracket[1]), level: normalizeLogLevel(bracket[2]), source: "runtime", message: bracket[3] };
  return { id: String(index), timestamp: "", level: "info", source: "runtime", message: raw };
}

async function requestModelState(): Promise<RuntimeModelState> {
  const payload = record(await request<unknown>("/api/v1/models"));
  const state = record(payload.models);
  const providerItems = Array.isArray(state.providers) ? state.providers.map(record) : [];
  const providers = new Map(providerItems.map((item) => [String(item.name ?? ""), item]));
  const catalogById = new Map((Array.isArray(state.catalog) ? state.catalog : []).map((value) => { const item = record(value); return [String(item.id ?? ""), item]; }));
  const references = record(payload.references ?? state.references);
  const catalog = (Array.isArray(state.choices) ? state.choices : []).map((value) => {
    const rawItem = record(value);
    const id = typeof value === "string" ? value : String(rawItem.id ?? rawItem.model ?? "");
    const item = catalogById.get(id) ?? rawItem;
    const provider = String(item.provider ?? id.split("/", 1)[0] ?? "unknown");
    const providerMeta = providers.get(provider) ?? {};
    const refs = Array.isArray(references[id]) ? references[id].map(String) : Array.isArray(item.references) ? item.references.map(String) : [];
    return {
      id,
      name: typeof item.name === "string" ? item.name : id.includes("/") ? id.slice(id.indexOf("/") + 1) : id,
      provider,
      providerLabel: String(item.providerLabel ?? providerMeta.label ?? providerMeta.displayName ?? provider),
      configured: item.configured === undefined ? Boolean(providerMeta.configured ?? true) : Boolean(item.configured),
      removable: item.removable === undefined ? refs.length === 0 : Boolean(item.removable),
      references: refs,
    } satisfies ModelInfo;
  }).filter((item) => item.id);
  const slots = record(state.slots);
  const effective = record(state.effective);
  return {
    catalog,
    slots: {
      agent: String(slots.agent ?? effective.agent ?? ""),
      subagent: String(slots.subagent ?? "") || undefined,
      assistant: String(slots.assistant ?? "") || undefined,
      vision: String(slots.vision ?? "") || undefined,
      compaction: String(slots.compaction ?? "") || undefined,
    },
    effective: {
      agent: String(effective.agent ?? slots.agent ?? ""),
      subagent: String(effective.subagent ?? ""),
      assistant: String(effective.assistant ?? "") || undefined,
      vision: String(effective.vision ?? "") || undefined,
      compaction: String(effective.compaction ?? "") || undefined,
    },
    reasoningEffort: String(state.reasoningEffort ?? state.reasoning_effort ?? "auto"),
    pulseEnabled: Boolean(state.pulseEnabled ?? state.pulse_enabled),
  };
}

export const api = {
  authStatus: () => request<{ protected: boolean; authenticated: boolean }>("/auth/status"),
  runtime: async () => {
    const data = record(await request<unknown>("/api/v1/runtime"));
    const models = record(data.models);
    const effective = record(models.effective);
    const compact = record(data.compact);
    const identity = record(data.identity);
    return {
      healthy: true,
      busy: Boolean(data.busy),
      sessionId: String(identity.sessionId ?? identity.session_id ?? "") || undefined,
      model: String(effective.agent ?? "") || undefined,
      effort: String(models.reasoningEffort ?? models.reasoning_effort ?? "") || undefined,
      pulseEnabled: Boolean(models.pulseEnabled ?? models.pulse_enabled),
      contextUsed: Number(compact.estimatedPromptTokens ?? compact.estimated_prompt_tokens ?? 0),
      contextLimit: Number(compact.contextWindowTokens ?? compact.context_window_tokens ?? 0),
    } satisfies RuntimeSnapshot;
  },
  sessions: async () => unwrapList(await request<unknown[] | { sessions?: unknown[] }>("/api/v1/sessions?limit=1000")).map((item) => normalizeSessionSummary(item)),
  session: async (id: string) => {
    const escaped = encodeURIComponent(id);
    const [history, conversation, trajectory] = await Promise.all([
      request<unknown>(`/api/v1/sessions/${escaped}`),
      conversationProjection(escaped),
      trajectoryProjection(escaped),
    ]);
    const historyData = record(history);
    const events = [
      ...trajectory.historical.map((item, index) => ({ ...projectionEvent(item, index), sequence: index - trajectory.historical.length })),
      ...trajectory.activity.map(projectionEvent),
    ];
    const turns = attachTrajectoryActivity(conversationTurns(conversation.items), events);
    if (!turns.length && Array.isArray(historyData.events)) {
      for (const [index, raw] of historyData.events.entries()) {
        const item = record(raw);
        if (item.kind !== "user" && item.kind !== "bot") continue;
        turns.push({ id: `history-${index}`, role: item.kind === "user" ? "user" : "assistant", content: String(item.text ?? ""), timestamp: "" });
      }
    }
    return {
      id,
      title: String(historyData.sessionName ?? historyData.session_name ?? "") || "Untitled session",
      status: sessionDetailStatus(events, Boolean(historyData.busy)),
      updatedAt: events.at(-1)?.timestamp ?? "",
      revision: typeof historyData.revision === "number" ? historyData.revision : undefined,
      conversationCursor: conversation.cursor,
      trajectoryActivityCursor: trajectory.activityCursor,
      trajectoryOldestCursor: trajectory.oldestCursor,
      trajectoryHistoryCursor: trajectory.historyCursor,
      trajectoryCursorKind: trajectory.cursorKind,
      turns,
      events,
    } satisfies SessionDetail;
  },
  earlierConversation: async (id: string, before: number) => {
    const page = await earlierConversationProjection(encodeURIComponent(id), before);
    return { turns: conversationTurns(page.items), cursor: page.cursor };
  },
  earlierTrajectory: async (id: string, activityBefore: number, activityOldest: number, historyBefore: number, cursorKind: "sequence" | "offset") => {
    const page = await earlierTrajectoryProjection(encodeURIComponent(id), activityBefore, activityOldest, historyBefore, cursorKind);
    return {
      events: [
        ...page.historical.map((item, index) => ({ ...projectionEvent(item, index), sequence: index - page.historical.length })),
        ...page.activity.map(projectionEvent),
      ],
      activityCursor: page.activityCursor,
      historyCursor: page.historyCursor,
    };
  },
  createSession: async (title?: string) => normalizeSessionSummary(await request<unknown>("/api/v1/sessions", {
      method: "POST",
      headers: { "Idempotency-Key": randomId() },
      body: title ? JSON.stringify({ title }) : undefined,
    }), new Date().toISOString()),
  renameSession: (id: string, title: string, revision?: number) => request<SessionSummary>(`/api/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: revision === undefined ? {} : { "If-Match": String(revision) },
    body: JSON.stringify({ title }),
  }),
  deleteSession: (id: string, revision?: number) => request<void>(`/api/v1/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
    headers: revision === undefined ? {} : { "If-Match": String(revision) },
  }),
  submitTurn: (sessionId: string, content: string, attachmentIds: string[] = []) => request<{ turnId: string; requestId?: string }>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/turns`, {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
    body: JSON.stringify({ content, attachmentIds }),
  }),
  uploadMedia: async (file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<{ id: string; name: string; size?: number; mediaType?: string; kind: ArtifactRef["kind"] }>("/api/v1/media", { method: "POST", body: form });
  },
  steer: (sessionId: string, content: string) => request<{ turnId?: string; requestId?: string }>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/steer`, {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
    body: JSON.stringify({ content }),
  }),
  cancel: (sessionId: string) => request<void>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/cancel`, { method: "POST" }),
  approvals: async (sessionId?: string) => {
    const response = await request<{ approvals?: JsonRecord[] }>(sessionId
      ? `/api/v1/sessions/${encodeURIComponent(sessionId)}/approvals`
      : "/api/v1/approvals");
    return (response.approvals ?? []).map((item) => ({
      requestId: String(item.requestId ?? ""),
      sessionId: String(item.sessionId ?? sessionId ?? ""),
      turnId: typeof item.turnId === "string" ? item.turnId : undefined,
      toolName: String(item.toolName ?? item.capability ?? "Approval"),
      summary: String(item.summary ?? "Review this request before the agent continues."),
      parameters: item.toolParams,
      review: [item.reviewDecision, item.reviewReason].filter(Boolean).join(" · ") || undefined,
      expiresAt: typeof item.expiresAt === "string" ? item.expiresAt : undefined,
      allowedActions: (Array.isArray(item.allowedActions) ? item.allowedActions : []).map((value) => ({ approve_once: "once", approve_turn: "turn", approve_forever: "forever", reject: "deny" })[String(value)] ?? String(value)).filter((value): value is ApprovalRequest["allowedActions"][number] => ["once", "turn", "forever", "deny"].includes(value)),
    }));
  },
  decideApproval: (requestId: string, action: ApprovalRequest["allowedActions"][number], sessionId?: string) => request<void>(sessionId
    ? `/api/v1/sessions/${encodeURIComponent(sessionId)}/approvals/respond`
    : `/api/v1/approvals/${encodeURIComponent(requestId)}`, {
      method: "POST",
      headers: { "Idempotency-Key": randomId() },
      body: JSON.stringify(sessionId ? { requestId, action } : { action }),
    }),
  modelState: requestModelState,
  models: async () => (await requestModelState()).catalog,
  addModel: (provider: string, model: string) => request<unknown>("/api/v1/models/catalog", { method: "POST", body: JSON.stringify({ provider, model }) }),
  deleteModel: (modelId: string) => request<void>(`/api/v1/models/catalog/${encodeURIComponent(modelId)}`, { method: "DELETE" }),
  selectModel: (model: string | null, slot = "agent") => request<void>("/api/v1/models/select", { method: "POST", body: JSON.stringify({ slot, model }) }),
  setEffort: (value: string) => request<void>("/api/v1/models/effort", { method: "POST", body: JSON.stringify({ value }) }),
  setPulse: (enabled: boolean) => request<void>("/api/v1/agent/pulse", { method: "POST", body: JSON.stringify({ enabled }) }),
  compact: (sessionId: string) => request<Record<string, unknown>>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/compact`, { method: "POST" }),
  commands: async () => {
    const payload = record(await request<unknown>("/api/v1/commands"));
    return (Array.isArray(payload.commands) ? payload.commands : []).map((value) => {
      const item = record(value);
      return {
        name: String(item.name ?? ""),
        aliases: Array.isArray(item.aliases) ? item.aliases.map(String) : [],
        group: String(item.group ?? "general"),
        summary: String(item.summary ?? ""),
        usage: String(item.usage ?? ""),
        examples: Array.isArray(item.examples) ? item.examples.map(String) : [],
        enabled: item.enabled !== false,
        acceptsArguments: Boolean(item.acceptsArguments),
        idleOnly: Boolean(item.idleOnly),
      } satisfies CommandInfo;
    }).filter((item) => item.name);
  },
  executeCommand: (text: string, sessionId?: string) => request<{ content?: string; routed?: string }>("/api/v1/commands/execute", {
    method: "POST",
    body: JSON.stringify({ text, sessionId }),
  }),
  settings: async () => {
    const payload = await request<unknown>("/api/v1/settings");
    const data = record(payload);
    return {
      sections: normalizeSettings(payload),
      writable: data.writable !== false,
      degradedReason: typeof data.degradedReason === "string" ? data.degradedReason : undefined,
      revision: typeof data.revision === "number" ? data.revision : undefined,
    };
  },
  updateSettings: (values: Record<string, unknown>, revision?: number) => request<SettingsUpdateResult>("/api/v1/settings", {
    method: "PATCH",
    headers: revision === undefined ? {} : { "If-Match": String(revision) },
    body: JSON.stringify({ values }),
  }),
  logs: async (_query = "") => {
    const payload = record(await request<unknown>("/api/v1/logs?count=200"));
    return (Array.isArray(payload.lines) ? payload.lines : []).map(parseLogLine);
  },
  login: (password: string) => request<{ csrfToken?: string }>("/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  artifactUrl: (artifact: ArtifactRef) => `/api/v1/artifacts/${encodeURIComponent(artifact.id)}`,
};
