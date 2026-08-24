import { computed, signal } from "@preact/signals";
import { api, ApiError } from "./api/client";
import { deleteDraftFiles, pruneDraftFiles } from "./api/drafts";
import { mergeTimelineEvent } from "./api/reducer";
import { navigate, route } from "./router";
import type {
  ApprovalRequest,
  ConnectionState,
  LogEntry,
  ModelInfo,
  RuntimeModelState,
  RuntimeSnapshot,
  SessionDetail,
  SessionSummary,
  SettingSection,
  TimelineEvent,
} from "./types";

export const sessions = signal<SessionSummary[]>([]);
export const currentSession = signal<SessionDetail | null>(null);
export const runtime = signal<RuntimeSnapshot | null>(null);
export const approvals = signal<ApprovalRequest[]>([]);
export const models = signal<ModelInfo[]>([]);
export const modelSettings = signal<RuntimeModelState | null>(null);
export const settings = signal<SettingSection[]>([]);
export const settingsWritable = signal(false);
export const settingsDegradedReason = signal<string | null>(null);
export const settingsRevision = signal<number | undefined>(undefined);
export const logs = signal<LogEntry[]>([]);
export const connection = signal<ConnectionState>("connecting");
export const booting = signal(true);
export const loadingSession = signal(false);
export const appError = signal<string | null>(null);
export const operationError = signal<string | null>(null);
export const creatingSession = signal(false);
function initialNavigatorOpen(): boolean {
  try {
    return !window.matchMedia("(max-width: 1279px)").matches
      && localStorage.getItem("nanocat.navigator.open") !== "false";
  } catch {
    return false;
  }
}

export const navigatorOpen = signal(initialNavigatorOpen());
export const inspectorOpen = signal(false);
export const selectedNodeId = signal<string | null>(null);
const selectedEventOverride = signal<TimelineEvent | null>(null);
export const commandPaletteOpen = signal(false);
export const theme = signal<"system" | "light" | "dark">(
  (localStorage.getItem("nanocat.theme") as "system" | "light" | "dark" | null) ?? "system",
);

export const pendingAttention = computed(() => {
  const approvalSessions = new Set(approvals.value.map((item) => item.sessionId));
  return sessions.value.filter((item) => approvalSessions.has(item.id)
    || item.status === "waiting_approval" || item.status === "waiting_user" || item.status === "failed");
});
export const runningSessions = computed(() => sessions.value.filter((item) => item.status === "running" || item.status === "queued"));
export const selectedEvent = computed(() => selectedEventOverride.value ?? currentSession.value?.events.find((item) =>
  item.nodeId === selectedNodeId.value || item.eventId === selectedNodeId.value,
) ?? null);

let sessionLoadGeneration = 0;

export function sortSessions(items: SessionSummary[]): SessionSummary[] {
  return [...items].sort((left, right) => {
    const time = Date.parse(right.updatedAt) - Date.parse(left.updatedAt);
    return Number.isFinite(time) && time !== 0 ? time : left.id.localeCompare(right.id);
  });
}

function message(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "An unexpected error occurred";
}

function mergeSessionState(next: SessionSummary[]): SessionSummary[] {
  const approvalSessions = new Set(approvals.value.map((item) => item.sessionId));
  return sortSessions(next.map((item) => {
    const status = approvalSessions.has(item.id)
      ? "waiting_approval" as const
      : item.status;
    return { ...item, status };
  }));
}

function applyWorkspaceSnapshot(runtimeResult: RuntimeSnapshot, sessionItems: SessionSummary[], approvalItems: ApprovalRequest[]): void {
  runtime.value = { ...runtimeResult, unprotected: runtime.value?.unprotected };
  approvals.value = approvalItems;
  sessions.value = sortSessions(mergeSessionState(sessionItems).map((item) => {
    if (approvalItems.some((approval) => approval.sessionId === item.id)) return { ...item, status: "waiting_approval" };
    if (item.id === runtimeResult.sessionId && runtimeResult.busy) return { ...item, status: "running" };
    return item;
  }));
}

export function reportOperationError(error: unknown, fallback = "The operation could not be completed"): void {
  operationError.value = error instanceof Error ? message(error) : fallback;
}

export function clearOperationError(): void {
  operationError.value = null;
}

export function applyTheme(next: "system" | "light" | "dark"): void {
  theme.value = next;
  localStorage.setItem("nanocat.theme", next);
  document.documentElement.dataset.theme = next;
}

export async function bootstrap(): Promise<void> {
  booting.value = true;
  appError.value = null;
  try {
    void pruneDraftFiles().catch(() => undefined);
    const authState = await api.authStatus();
    if (authState.protected && !authState.authenticated) {
      window.location.assign("/login");
      return;
    }
    const runtimeResult = await api.runtime();
    const sessionsResult = await api.sessions();
    const approvalItems = await api.approvals();
    runtime.value = { ...runtimeResult, unprotected: !authState.protected };
    approvals.value = approvalItems;
    sessions.value = sortSessions(mergeSessionState(sessionsResult).map((item) => {
      if (approvalItems.some((approval) => approval.sessionId === item.id)) return { ...item, status: "waiting_approval" };
      if (item.id === runtimeResult.sessionId && runtimeResult.busy) return { ...item, status: "running" };
      return item;
    }));
    connection.value = "online";
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && route.value.name === "session") {
      void deleteDraftFiles(route.value.sessionId).catch(() => undefined);
    }
    if (error instanceof ApiError && error.status === 401 && window.location.pathname !== "/login") {
      window.location.assign("/login");
      return;
    }
    appError.value = message(error);
    connection.value = "offline";
  } finally {
    booting.value = false;
  }
}

export async function refreshSessions(): Promise<void> {
  sessions.value = mergeSessionState(await api.sessions());
}

export async function recoverGlobalState(): Promise<void> {
  const runtimeResult = await api.runtime();
  const sessionItems = await api.sessions();
  const approvalItems = await api.approvals();
  applyWorkspaceSnapshot(runtimeResult, sessionItems, approvalItems);
}

export async function recoverSessionState(id: string): Promise<void> {
  await recoverGlobalState();
  const authoritative = sessions.value.find((item) => item.id === id);
  const detail = await loadSession(id, true);
  if (!detail || currentSession.value?.id !== id || authoritative === undefined) return;
  currentSession.value = { ...currentSession.value, status: authoritative.status };
  sessions.value = sessions.value.map((item) => item.id === id
    ? { ...item, status: authoritative.status }
    : item);
}

export function invalidateSessionLoad(): void {
  sessionLoadGeneration += 1;
  loadingSession.value = false;
}

export async function loadSession(id: string, force = false): Promise<SessionDetail | null> {
  if (!force && currentSession.value?.id === id) return currentSession.value;
  const generation = ++sessionLoadGeneration;
  loadingSession.value = true;
  appError.value = null;
  try {
    const detail = await api.session(id);
    if (generation !== sessionLoadGeneration) return null;
    currentSession.value = detail;
    sessions.value = sortSessions(sessions.value.map((item) => item.id === id ? { ...item, status: detail.status, updatedAt: detail.updatedAt || item.updatedAt } : item));
    selectedEventOverride.value = null;
    selectedNodeId.value = null;
    inspectorOpen.value = false;
    void refreshApprovals(id);
    return detail;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      void deleteDraftFiles(id).catch(() => undefined);
    }
    if (generation === sessionLoadGeneration) appError.value = message(error);
    return null;
  } finally {
    if (generation === sessionLoadGeneration) loadingSession.value = false;
  }
}

export async function reloadCurrentSession(): Promise<void> {
  const id = currentSession.value?.id;
  if (!id) return;
  const generation = ++sessionLoadGeneration;
  try {
    const [detail, sessionItems] = await Promise.all([api.session(id), api.sessions()]);
    if (generation !== sessionLoadGeneration || currentSession.value?.id !== id) return;
    currentSession.value = detail;
    sessions.value = sortSessions(mergeSessionState(sessionItems).map((item) => item.id === id ? { ...item, status: detail.status, updatedAt: detail.updatedAt || item.updatedAt } : item));
  } catch (error) {
    if (generation === sessionLoadGeneration) reportOperationError(error, "Session state could not be refreshed");
  }
}

export async function loadEarlierConversation(): Promise<void> {
  const session = currentSession.value;
  const before = session?.conversationCursor ?? 0;
  if (!session || before <= 0) return;
  try {
    const page = await api.earlierConversation(session.id, before);
    if (currentSession.value?.id !== session.id) return;
    const known = new Set(currentSession.value.turns.map((turn) => turn.id));
    currentSession.value = {
      ...currentSession.value,
      conversationCursor: page.cursor,
      turns: [...page.turns.filter((turn) => !known.has(turn.id)), ...currentSession.value.turns],
    };
  } catch (error) {
    reportOperationError(error, "Earlier conversation could not be loaded");
  }
}

export async function loadEarlierTrajectory(): Promise<void> {
  const session = currentSession.value;
  if (!session) return;
  const activityBefore = session.trajectoryActivityCursor ?? 0;
  const activityOldest = session.trajectoryOldestCursor ?? 0;
  const historyBefore = session.trajectoryHistoryCursor ?? 0;
  if (activityBefore <= activityOldest && historyBefore <= 0) return;
  try {
    const page = await api.earlierTrajectory(
      session.id,
      activityBefore,
      activityOldest,
      historyBefore,
      session.trajectoryCursorKind ?? "sequence",
    );
    if (currentSession.value?.id !== session.id) return;
    const known = new Set(currentSession.value.events.map((event) => event.eventId));
    currentSession.value = {
      ...currentSession.value,
      trajectoryActivityCursor: page.activityCursor,
      trajectoryHistoryCursor: page.historyCursor,
      events: [
        ...page.events.filter((event) => !known.has(event.eventId)),
        ...currentSession.value.events,
      ],
    };
  } catch (error) {
    reportOperationError(error, "Earlier activity could not be loaded");
  }
}

const TERMINAL_EVENT_TYPES = new Set(["assistant.final", "turn.completed", "turn.failed", "turn.rejected", "turn.cancelled"]);
const TRANSIENT_EVENT_STATUSES = new Set(["queued", "running", "cancelling"]);
const SESSION_STATUS_EVENT_TYPES = new Set([
  "turn.queued",
  "turn.cancelling",
  "assistant.progress",
  "assistant.final",
  "turn.completed",
  "turn.failed",
  "turn.rejected",
  "turn.cancelled",
  "approval.pending",
  "approval.updated",
]);

function followsTerminalEvent(events: TimelineEvent[], incoming: TimelineEvent): boolean {
  if (!incoming.turnId || !incoming.status || !TRANSIENT_EVENT_STATUSES.has(incoming.status)) return false;
  return events.some((event) => event.turnId === incoming.turnId && TERMINAL_EVENT_TYPES.has(event.type));
}

export function appendEvent(event: TimelineEvent): boolean {
  const session = currentSession.value;
  if (!session || (event.sessionId && event.sessionId !== session.id)) return false;
  if (followsTerminalEvent(session.events, event)) return false;
  sessionLoadGeneration += 1;
  const events = mergeTimelineEvent(session.events, event);
  currentSession.value = { ...session, events };
  if (event.status && SESSION_STATUS_EVENT_TYPES.has(event.type)) {
    currentSession.value = { ...currentSession.value, status: event.status };
    sessions.value = sessions.value.map((item) => item.id === session.id ? { ...item, status: event.status! } : item);
  }
  if (event.type === "approval.pending" || event.type === "approval.updated") void refreshApprovals(session.id);
  if (["assistant.final", "turn.completed", "turn.failed", "turn.rejected", "turn.cancelled"].includes(event.type)) {
    void reloadCurrentSession();
  }
  return true;
}

export function appendGlobalEvent(event: TimelineEvent): void {
  if (!event.sessionId) return;
  if (currentSession.value?.id === event.sessionId && !appendEvent(event)) return;
  let found = false;
  sessions.value = sortSessions(sessions.value.map((item) => {
    if (item.id !== event.sessionId) return item;
    found = true;
    return {
      ...item,
      status: event.status && SESSION_STATUS_EVENT_TYPES.has(event.type) ? event.status : item.status,
      updatedAt: event.timestamp || item.updatedAt,
      preview: event.type === "turn.queued" && event.content ? event.content : item.preview,
    };
  }));
  if (!found) void refreshSessions().catch((error) => reportOperationError(error, "Sessions could not be refreshed"));
  if (event.type === "approval.pending" || event.type === "approval.updated") void refreshApprovals();
}

export async function refreshApprovals(sessionId?: string): Promise<void> {
  try {
    const next = await api.approvals(sessionId);
    approvals.value = sessionId
      ? [...approvals.value.filter((item) => item.sessionId !== sessionId), ...next]
      : next;
  } catch {
    // Approval polling is best-effort; SSE can still update the current turn.
  }
}

export async function loadModels(force = false): Promise<void> {
  if (!force && models.value.length && modelSettings.value) return;
  const state = await api.modelState();
  models.value = state.catalog;
  modelSettings.value = state;
  if (runtime.value) {
    runtime.value = {
      ...runtime.value,
      model: state.effective.agent,
      effort: state.reasoningEffort,
      pulseEnabled: state.pulseEnabled,
    };
  }
}

export async function loadSettings(): Promise<void> {
  const result = await api.settings();
  settings.value = result.sections;
  settingsWritable.value = result.writable;
  settingsDegradedReason.value = result.degradedReason ?? null;
  settingsRevision.value = result.revision;
}

export async function loadLogs(query = ""): Promise<void> {
  logs.value = await api.logs(query);
}

export async function createSessionAndOpen(): Promise<void> {
  if (creatingSession.value) return;
  creatingSession.value = true;
  clearOperationError();
  try {
    const session = await api.createSession();
    sessions.value = sortSessions([session, ...sessions.value.filter((item) => item.id !== session.id)]);
    navigatorOpen.value = false;
    navigate({ name: "session", sessionId: session.id, view: "conversation" });
  } catch (error) {
    reportOperationError(error, "A new session could not be created");
  } finally {
    creatingSession.value = false;
  }
}

export function setSelectedNode(id: string | null, syncRoute = true): void {
  selectedEventOverride.value = null;
  selectedNodeId.value = id;
  inspectorOpen.value = Boolean(id);
  if (syncRoute && route.value.name === "session") {
    navigate({ ...route.value, nodeId: id ?? undefined, turnId: undefined }, id === null, false);
  }
}

export function setSelectedEvent(event: TimelineEvent, syncRoute = true): void {
  const id = event.nodeId ?? event.eventId;
  selectedEventOverride.value = event;
  selectedNodeId.value = id;
  inspectorOpen.value = true;
  if (syncRoute && route.value.name === "session") {
    navigate({ ...route.value, nodeId: id, turnId: undefined }, false, false);
  }
}
