export type RunStatus =
  | "idle"
  | "queued"
  | "running"
  | "cancelling"
  | "waiting_approval"
  | "waiting_user"
  | "completed"
  | "failed"
  | "cancelled";

export type ConnectionState = "connecting" | "online" | "reconnecting" | "offline";

export interface SessionSummary {
  id: string;
  title: string;
  status: RunStatus;
  updatedAt: string;
  preview?: string;
  unread?: number;
  turnCount?: number;
  model?: string;
  revision?: number;
}

export interface ArtifactRef {
  id: string;
  name: string;
  kind: "text" | "markdown" | "json" | "code" | "diff" | "image" | "binary";
  size?: number;
  mediaType?: string;
  expired?: boolean;
}

export interface TimelineEvent {
  eventId: string;
  sequence: number;
  timestamp: string;
  sessionId?: string;
  turnId?: string;
  requestId?: string;
  nodeId?: string;
  parentId?: string;
  type: string;
  source?: string;
  phase?: string;
  toolCallId?: string;
  toolName?: string;
  status?: RunStatus;
  startedAt?: string;
  endedAt?: string;
  durationMs?: number;
  summary?: string;
  content?: string;
  redactedInput?: unknown;
  redactedOutput?: unknown;
  artifactRefs?: ArtifactRef[];
  model?: string;
  usage?: { inputTokens?: number; outputTokens?: number; totalTokens?: number };
}

export interface ConversationTurn {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  status?: RunStatus;
  activity?: TimelineEvent[];
  artifactRefs?: ArtifactRef[];
}

export interface SessionDetail extends SessionSummary {
  revision?: number;
  conversationCursor?: number;
  trajectoryActivityCursor?: number;
  trajectoryOldestCursor?: number;
  trajectoryHistoryCursor?: number;
  trajectoryCursorKind?: "sequence" | "offset";
  turns: ConversationTurn[];
  events: TimelineEvent[];
}

export interface ApprovalRequest {
  requestId: string;
  sessionId: string;
  turnId?: string;
  toolName: string;
  summary: string;
  parameters?: unknown;
  review?: string;
  expiresAt?: string;
  allowedActions: Array<"once" | "turn" | "forever" | "deny">;
}

export interface RuntimeSnapshot {
  healthy: boolean;
  busy?: boolean;
  sessionId?: string;
  version?: string;
  model?: string;
  effort?: string;
  contextUsed?: number;
  contextLimit?: number;
  unprotected?: boolean;
}

export interface ModelInfo {
  id: string;
  name?: string;
  provider?: string;
  providerLabel?: string;
  configured?: boolean;
  removable?: boolean;
  references?: string[];
}

export interface CommandInfo {
  name: string;
  aliases: string[];
  group: string;
  summary: string;
  usage: string;
  examples: string[];
  enabled: boolean;
  acceptsArguments: boolean;
  idleOnly: boolean;
}

export interface SettingSection {
  id: string;
  title: string;
  description?: string;
  fields: SettingField[];
  groups?: SettingGroup[];
}

export interface SettingGroup {
  id: string;
  title: string;
  path: string;
  fields: SettingField[];
  groups: SettingGroup[];
}

export interface SettingField {
  key: string;
  label: string;
  description?: string;
  type: "text" | "password" | "number" | "boolean" | "select" | "json";
  value: unknown;
  options?: Array<{ label: string; value: string }>;
  secret?: boolean;
  configured?: boolean;
  readOnly?: boolean;
  overriddenValue?: unknown;
  restartRequired?: boolean;
  applyMode?: "live" | "next_turn" | "reconnect" | "restart";
  minimum?: number;
  maximum?: number;
}

export interface SettingsUpdateResult {
  revision?: number;
  appliedPaths?: string[];
  nextTurnPaths?: string[];
  reconnectedPaths?: string[];
  restartRequiredPaths?: string[];
}

export interface LogEntry {
  id: string;
  timestamp: string;
  level: "debug" | "info" | "warning" | "error";
  source: string;
  message: string;
  sessionId?: string;
  turnId?: string;
  requestId?: string;
}

export interface ApiErrorBody {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code?: string;
  retryAfter?: number;
}
