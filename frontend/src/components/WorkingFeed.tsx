import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleStop,
  CornerDownRight,
  GitBranch,
  LoaderCircle,
  Sparkles,
  Terminal,
  Zap,
} from "lucide-preact";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { api } from "../api/client";
import { setSelectedNode } from "../store";
import type { RunStatus, TimelineEvent } from "../types";
import { Markdown } from "./Markdown";
import { supportsToolPresentation, ToolPresentation, type TodoPresentation } from "./ToolPresentation";

type JsonRecord = Record<string, unknown>;

interface WorkingNode {
  key: string;
  kind: "thinking" | "tool" | "subagent" | "approval" | "progress" | "control" | "error";
  label: string;
  toolName?: string;
  source?: string;
  status?: RunStatus;
  startedAt: string;
  endedAt?: string;
  durationMs?: number;
  content?: string;
  description?: string;
  hint?: string;
  input?: unknown;
  output?: unknown;
  artifactRefs?: TimelineEvent["artifactRefs"];
  eventId: string;
  nodeId?: string;
  todo?: TodoPresentation;
}

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function values(value: unknown): JsonRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is JsonRecord => item !== null && typeof item === "object" && !Array.isArray(item))
    : [];
}

function text(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (!Array.isArray(value)) return "";
  return value.map((item) => {
    if (typeof item === "string") return item;
    const block = record(item);
    return typeof block.text === "string" ? block.text : "";
  }).filter(Boolean).join("\n\n").trim();
}

function cleanOutput(value: unknown): unknown {
  const output = record(value);
  if (Object.prototype.hasOwnProperty.call(output, "preview") && output.truncated !== true && Object.keys(output).length === 1) return output.preview;
  if (Object.prototype.hasOwnProperty.call(output, "resultPreview")) return output.resultPreview;
  if (output.summary === "Tool result retained in private session history.") return undefined;
  return value;
}

function normalizeToolStatus(value: unknown, fallback?: RunStatus): RunStatus | undefined {
  const status = String(value ?? "").toLowerCase();
  if (["ok", "success", "succeeded", "complete", "completed"].includes(status)) return "completed";
  if (["error", "failed", "failure"].includes(status)) return "failed";
  if (["cancelled", "canceled"].includes(status)) return "cancelled";
  if (["start", "started", "running", "pending"].includes(status)) return "running";
  return fallback;
}

function callsFor(event: TimelineEvent): JsonRecord[] {
  const input = record(event.redactedInput);
  const output = record(event.redactedOutput);
  const toolEvent = record(output.toolEvent);
  return [
    ...values(input.toolCalls),
    ...values(input.calls),
    ...values(output.calls),
    ...values(toolEvent.calls),
  ];
}

function meaningfulContent(event: TimelineEvent): string {
  if (
    event.type === "turn.failed"
    || event.type === "turn.rejected"
    || event.type === "turn.steer_rejected"
    || event.type === "control.stop_failed"
    || event.type === "turn.cancelling"
    || event.type === "turn.cancelled"
    || event.type === "turn.queued"
    || event.type === "turn.steer_queued"
    || event.type === "user.steer"
  ) return "";
  const content = event.content?.trim() ?? "";
  if (!content) return "";
  if (/^[\[{]/.test(content)) {
    try {
      const telemetry = record(JSON.parse(content));
      if ("contentChars" in telemetry || "artifactCount" in telemetry || "toolEvent" in telemetry) return "";
    } catch {
      // Preserve ordinary prose that happens to begin with JSON punctuation.
    }
  }
  if (event.type === "assistant.progress" && /^[\[{]/.test(content)) return "";
  return content;
}

function defaultLabel(event: TimelineEvent): string {
  if (event.type === "turn.failed") return event.summary || "Turn failed";
  if (event.type === "turn.rejected") return event.summary || "Request rejected";
  if (event.type === "turn.steer_rejected") return event.summary || "Guidance rejected";
  if (event.type === "turn.steer_applied") return event.summary || "Guidance applied";
  if (event.type === "control.stop_failed") return event.summary || "Stop failed";
  if (event.type === "turn.cancelling") return event.summary || "Stop requested";
  if (event.type === "turn.cancelled") return event.summary || "Turn stopped";
  if (event.type === "turn.steer_queued") return event.summary || "Guidance queued";
  if (event.type === "turn.queued") return event.summary || "Turn queued";
  if (event.type === "user.steer") return event.summary || "Guidance submitted";
  if (event.type.includes("thinking") || event.type.includes("reasoning")) return "Thinking";
  if (event.type.includes("subagent")) return event.summary || "Subagent";
  if (event.type.includes("approval")) return event.summary || "Approval";
  if (event.type === "assistant.progress") return event.summary === "Assistant Progress" ? "Progress update" : event.summary || "Progress update";
  return event.summary || event.type.split(".").map((part) => part.replaceAll("_", " ")).join(" · ");
}

function normalizeWorkingEvents(events: TimelineEvent[]): WorkingNode[] {
  const nodes: WorkingNode[] = [];
  const toolNodes = new Map<string, WorkingNode>();
  const eventNodes = new Map<string, WorkingNode>();
  const sorted = [...events].sort((left, right) => left.sequence - right.sequence);

  const upsertTool = (event: TimelineEvent, call: JsonRecord, ordinal: number) => {
    const callId = String(call.id ?? event.toolCallId ?? "");
    const name = String(call.name ?? event.toolName ?? "tool");
    const key = callId || `${event.eventId}:${name}:${ordinal}`;
    const existing = toolNodes.get(key);
    const input = call.arguments ?? call.args ?? event.redactedInput;
    const output = cleanOutput(call.resultPreview ?? call.output ?? event.redactedOutput);
    const status = normalizeToolStatus(call.status, event.status);
    const outputRecord = record(output);
    const outputError = record(outputRecord.error);
    const description = status === "failed"
      ? String(outputError.message ?? outputRecord.error ?? "Tool execution failed.")
      : undefined;
    const hint = status === "failed" && typeof outputRecord.hint === "string"
      ? outputRecord.hint
      : status === "failed" && typeof outputError.hint === "string"
        ? outputError.hint
        : undefined;
    if (existing) {
      existing.status = status ?? existing.status;
      existing.endedAt = event.endedAt ?? event.timestamp ?? existing.endedAt;
      existing.durationMs = typeof call.durationMs === "number" ? call.durationMs : existing.durationMs;
      if (input !== undefined) existing.input = input;
      if (output !== undefined) existing.output = output;
      if (description) existing.description = description;
      if (hint) existing.hint = hint;
      if (event.artifactRefs?.length) existing.artifactRefs = event.artifactRefs;
      existing.eventId = event.eventId;
      existing.nodeId = callId || event.nodeId || existing.nodeId;
      return;
    }
    const node: WorkingNode = {
      key: `tool:${key}`,
      kind: "tool",
      label: name,
      toolName: name,
      source: event.source,
      status,
      startedAt: event.startedAt ?? event.timestamp,
      endedAt: event.endedAt,
      durationMs: typeof call.durationMs === "number" ? call.durationMs : undefined,
      input,
      output,
      description,
      hint,
      artifactRefs: event.artifactRefs,
      eventId: event.eventId,
      nodeId: callId || event.nodeId,
    };
    toolNodes.set(key, node);
    nodes.push(node);
  };

  for (const event of sorted) {
    const calls = callsFor(event);
    if (calls.length) {
      const content = meaningfulContent(event);
      if (content && (event.type === "assistant.work" || event.type.includes("thinking"))) {
        nodes.push({
          key: `${event.eventId}:thinking`,
          kind: "thinking",
          label: "Thinking",
          source: event.source,
          status: event.status,
          startedAt: event.startedAt ?? event.timestamp,
          endedAt: event.endedAt,
          durationMs: undefined,
          content,
          eventId: event.eventId,
          nodeId: event.nodeId,
        });
      }
      calls.forEach((call, index) => upsertTool(event, call, index));
      continue;
    }
    if (event.type === "tool.result" || event.type === "tool.call" || event.type === "tool.event") {
      upsertTool(event, {}, 0);
      continue;
    }
    const kind: WorkingNode["kind"] = event.type === "turn.failed" || event.type === "turn.rejected" || event.type === "turn.steer_rejected" || event.type === "control.stop_failed"
      ? "error"
      : event.type === "turn.cancelling" || event.type === "turn.cancelled" || event.type === "turn.queued" || event.type === "turn.steer_queued" || event.type === "turn.steer_applied" || event.type === "user.steer"
        ? "control"
        : event.type.includes("thinking") || event.type.includes("reasoning") || event.type === "assistant.work"
      ? "thinking"
      : event.type.includes("subagent")
        ? "subagent"
        : event.type.includes("approval")
          ? "approval"
          : "progress";
    const output = cleanOutput(event.redactedOutput);
    const error = record(record(output).error);
    const semanticKey = event.nodeId || event.eventId;
    const node: WorkingNode = {
      key: semanticKey,
      kind,
      label: defaultLabel(event),
      source: event.source,
      status: event.status,
      startedAt: event.startedAt ?? event.timestamp,
      endedAt: event.endedAt,
      durationMs: kind === "subagent" || kind === "control" || kind === "error" ? event.durationMs : undefined,
      content: meaningfulContent(event),
      description: kind === "error"
        ? String(error.message ?? "The operation ended without a valid result.")
        : kind === "control"
          ? String(record(output).message ?? "") || undefined
          : undefined,
      hint: kind === "error" && typeof error.hint === "string" ? error.hint : undefined,
      input: kind === "approval" ? event.redactedInput : undefined,
      output: kind === "thinking" || kind === "progress" || kind === "control" || kind === "error" ? undefined : output,
      artifactRefs: event.artifactRefs,
      eventId: event.eventId,
      nodeId: event.nodeId,
    };
    const existing = eventNodes.get(semanticKey);
    if (existing) {
      Object.assign(existing, node, { startedAt: existing.startedAt });
    } else {
      eventNodes.set(semanticKey, node);
      nodes.push(node);
    }
  }
  const todoStates = new Map<string, TodoPresentation>();
  const todoNodes = new Map<string, WorkingNode>();
  const mergedTodoNodes = new Set<WorkingNode>();
  for (const node of nodes) {
    if (node.kind !== "tool" || node.label !== "todo") continue;
    const input = record(node.input);
    const output = record(node.output);
    const action = String(input.action ?? "check");
    const snapshot = record(output.todo);
    const id = String(snapshot.id ?? output.id ?? input.id ?? "");
    if (!id) continue;
    let current = todoStates.get(id);
    if (Array.isArray(snapshot.tasks)) {
      current = {
        id,
        name: String(snapshot.name ?? output.name ?? input.name ?? current?.name ?? "Task list"),
        action: String(snapshot.action ?? action),
        completed: snapshot.completed === true,
        tasks: snapshot.tasks.map((value, index) => {
          const task = record(value);
          const rawStatus = String(task.status ?? "PENDING");
          const status = rawStatus === "COMPLETED" || rawStatus === "INPROGRESS" ? rawStatus : "PENDING";
          return { index: Number(task.index ?? index + 1), task: String(task.task ?? ""), status };
        }),
      };
    } else if (action === "create") {
      current = {
        id,
        name: String(output.name ?? input.name ?? "Task list"),
        action,
        completed: false,
        tasks: (Array.isArray(input.tasks) ? input.tasks : []).map((task, index) => ({ index: index + 1, task: String(task), status: "PENDING" as const })),
      };
    } else if (Array.isArray(output.tasks)) {
      current = {
        id,
        name: String(output.name ?? current?.name ?? "Task list"),
        action,
        completed: false,
        tasks: output.tasks.map((value, index) => {
          const task = record(value);
          const rawStatus = String(task.status ?? "PENDING");
          const status = rawStatus === "COMPLETED" || rawStatus === "INPROGRESS" ? rawStatus : "PENDING";
          return { index: Number(task.index ?? index + 1), task: String(task.task ?? ""), status };
        }),
      };
    } else if (current) {
      current = { ...current, action, tasks: current.tasks.map((task) => ({ ...task })) };
      if (action === "update") {
        const index = Number(output.index ?? input.index);
        const rawStatus = String(output.status ?? input.status);
        const status = rawStatus === "COMPLETED" || rawStatus === "INPROGRESS" ? rawStatus : "PENDING";
        current.tasks = current.tasks.map((task) => task.index === index ? { ...task, status } : task);
      } else if (action === "append") {
        current.tasks.push({ index: Number(output.index ?? current.tasks.length + 1), task: String(input.task ?? ""), status: "PENDING" });
      } else if (action === "complete") {
        current.completed = true;
      }
    }
    if (!current) continue;
    todoStates.set(id, current);
    node.todo = { ...current, tasks: current.tasks.map((task) => ({ ...task })) };
    node.label = current.name;
    const aggregate = todoNodes.get(id);
    if (aggregate) {
      aggregate.status = node.status;
      aggregate.endedAt = node.endedAt;
      aggregate.durationMs = node.durationMs;
      aggregate.input = node.input;
      aggregate.output = node.output;
      aggregate.artifactRefs = node.artifactRefs;
      aggregate.eventId = node.eventId;
      aggregate.nodeId = node.nodeId;
      aggregate.todo = node.todo;
      aggregate.label = node.label;
      mergedTodoNodes.add(node);
    } else {
      todoNodes.set(id, node);
    }
  }
  return nodes.filter((node) => !mergedTodoNodes.has(node));
}

function settleWorkingNodes(nodes: WorkingNode[], active: boolean, terminalStatus?: string): WorkingNode[] {
  const settled = nodes.map((node) => ({ ...node }));
  const completeControl = (node: WorkingNode) => {
    if (node.kind !== "control") return;
    if (node.label === "Turn queued") node.label = "Turn started";
  };
  for (const [index, node] of settled.entries()) {
    if (!node.status || !["queued", "running", "cancelling"].includes(node.status)) continue;
    const successor = settled.slice(index + 1).find((candidate) => (
      node.kind !== "tool" || candidate.kind === "thinking" || candidate.kind === "progress" || candidate.kind === "approval"
    ));
    if (successor) {
      node.status = "completed";
      completeControl(node);
      node.endedAt = successor.startedAt || node.endedAt;
      const startedAt = Date.parse(node.startedAt);
      const endedAt = Date.parse(node.endedAt ?? "");
      if (Number.isFinite(startedAt) && Number.isFinite(endedAt)) node.durationMs = Math.max(0, endedAt - startedAt);
      continue;
    }
    if (!active) {
      node.status = terminalStatus === "failed" ? "failed" : terminalStatus === "cancelled" ? "cancelled" : "completed";
      if (node.status === "completed") completeControl(node);
    }
  }
  return settled;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`;
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function nodeIcon(node: WorkingNode) {
  if (node.kind === "error") return <AlertCircle size={14} />;
  if (node.kind === "control") return node.status === "cancelling" || node.status === "cancelled" ? <CircleStop size={14} /> : <CornerDownRight size={14} />;
  if (node.kind === "tool") return <Terminal size={14} />;
  if (node.kind === "subagent") return <GitBranch size={14} />;
  if (node.kind === "approval") return <AlertCircle size={14} />;
  if (node.kind === "thinking") return <Sparkles size={14} />;
  return <Check size={14} />;
}

function DetailValue({ label, value }: { label: string; value: unknown }) {
  if (value === undefined) return null;
  return <section class="working-detail-section"><h5>{label}</h5><pre><code>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</code></pre></section>;
}

function WorkingEntry({ node }: { node: WorkingNode }) {
  const hasDetails = node.input !== undefined || node.output !== undefined || Boolean(node.artifactRefs?.length);
  const failed = node.status === "failed" || node.kind === "error";
  const cancelled = node.status === "cancelled";
  const tool = { name: node.toolName ?? node.label, input: node.input, output: node.output, status: node.status, todo: node.todo };
  const specializedTool = node.kind === "tool" && supportsToolPresentation(tool);
  const control = node.kind === "control";
  return <article class={`working-entry kind-${node.kind} ${specializedTool ? "has-tool-card" : ""} ${control ? "has-control-card" : ""} ${failed ? "is-failed" : ""} ${cancelled ? "is-cancelled" : ""}`}>
    {control ? <div class={`control-card status-${node.status ?? "completed"}`}>
      <span class="control-card-mark">{nodeIcon(node)}</span>
      <span class="control-card-copy"><strong>{node.label}</strong>{node.description ? <small>{node.description}</small> : null}</span>
      {node.status && node.status !== "completed" && <span class="control-card-status">{node.status === "running" || node.status === "cancelling" ? <LoaderCircle class="spin" size={11} /> : null}{node.status.replaceAll("_", " ")}</span>}
    </div> : specializedTool ? <ToolPresentation tool={tool} /> : <>
      <div class="working-entry-line">
        <span class="working-entry-icon">{nodeIcon(node)}</span>
        <button class="working-entry-title" onClick={() => setSelectedNode(node.nodeId ?? node.eventId)}>
          <strong>{node.label}</strong>
          <small>{[node.source && node.source !== "runtime" ? node.source : undefined, node.durationMs !== undefined ? formatDuration(node.durationMs) : undefined].filter(Boolean).join(" · ")}</small>
        </button>
        {node.status && node.status !== "completed" && <span class={`working-entry-status status-${node.status}`}>{node.status === "running" || node.status === "cancelling" ? <LoaderCircle class="spin" size={12} /> : null}{node.status.replaceAll("_", " ")}</span>}
      </div>
      {node.description && <p class="working-entry-description">{node.description}</p>}
      {node.content && <div class="working-entry-content"><Markdown content={node.content} /></div>}
    </>}
    {specializedTool && node.description && <p class="working-entry-description">{node.description}</p>}
    {hasDetails && <details class="working-entry-details"><summary>Details</summary><div>
      <DetailValue label="Input" value={node.input} />
      <DetailValue label="Output" value={node.output} />
      {node.artifactRefs?.length ? <section class="working-detail-section"><h5>Artifacts</h5><div class="turn-artifacts">{node.artifactRefs.map((artifact) => <a key={artifact.id} href={api.artifactUrl(artifact)} target="_blank" rel="noreferrer">{artifact.name}</a>)}</div></section> : null}
    </div></details>}
    {node.hint && <p class="working-entry-hint">{node.hint}</p>}
  </article>;
}

function eventStart(event: TimelineEvent): number {
  const value = Date.parse(event.startedAt || event.timestamp);
  return Number.isFinite(value) ? value : Date.now();
}

export function WorkingFeed({ events, active = false, status, onStop }: { events: TimelineEvent[]; active?: boolean; status?: string; onStop?: () => void }) {
  const mountedAt = useRef(new Date().toISOString());
  const eventList = useRef<HTMLDivElement>(null);
  const followTail = useRef(true);
  const workEvents = events.filter((event) => event.type !== "user.steer");
  const visibleEvents = workEvents.length ? workEvents : active ? [{ eventId: "working-pending", sequence: 0, timestamp: mountedAt.current, type: "assistant.progress", status: "running" as const, summary: "Preparing run" }] : [];
  const terminalFailure = status === "failed" || status === "cancelled";
  const stopping = status === "cancelling";
  const nodes = useMemo(() => settleWorkingNodes(normalizeWorkingEvents(visibleEvents), active, status), [visibleEvents, active, status]);
  const [open, setOpen] = useState(active || terminalFailure);
  const [now, setNow] = useState(Date.now());
  const wasActive = useRef(active);
  useEffect(() => {
    if (active) setOpen(true);
    else if (wasActive.current && !terminalFailure) setOpen(false);
    wasActive.current = active;
  }, [active, terminalFailure]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  useLayoutEffect(() => {
    const list = eventList.current;
    if (!open || !list || !followTail.current) return;
    list.scrollTop = list.scrollHeight;
  }, [nodes, open]);
  if (!visibleEvents.length) return null;
  const started = Math.min(...visibleEvents.map(eventStart));
  const ended = Math.max(...visibleEvents.map((event) => Date.parse(event.endedAt || event.timestamp)).filter(Number.isFinite), started);
  const serverDuration = visibleEvents.reduce((maximum, event) => Math.max(maximum, event.durationMs ?? 0), 0);
  const duration = active ? Math.max(0, now - started) : Math.max(serverDuration, ended - started);
  const toolCount = nodes.filter((node) => node.kind === "tool").length;
  const subagentCount = nodes.filter((node) => node.kind === "subagent" || node.toolName?.startsWith("subagent_")).length;
  const panelId = `working-events-${String(visibleEvents[0].eventId).replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const terminalTitle = status === "failed"
    ? `Failed after ${formatDuration(duration)}`
    : status === "cancelled"
      ? `Stopped after ${formatDuration(duration)}`
      : `Worked for ${formatDuration(duration)}`;
  return <section class={`work-log working-feed ${open ? "open" : ""} ${active ? "active" : "terminal"}`}>
    <div class="work-log-heading">
      <button class="work-log-summary" onClick={() => {
        if (!open) followTail.current = true;
        setOpen(!open);
      }} aria-expanded={open} aria-controls={panelId}>
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <span class="work-log-icon">{active ? <LoaderCircle class="spin" size={13} /> : status === "failed" ? <AlertCircle size={13} /> : <Zap size={13} />}</span>
        <span><strong>{active ? stopping ? "Stopping…" : "Working…" : terminalTitle}</strong><small>{nodes.length} steps{toolCount ? ` · ${toolCount} tools` : ""}{subagentCount ? ` · ${subagentCount} subagents` : ""}{active ? ` · ${formatDuration(duration)}` : ""}</small></span>
      </button>
      {active && onStop && <button class="working-stop" onClick={onStop}><CircleStop size={13} /> Stop</button>}
    </div>
    {open && <div id={panelId} ref={eventList} class="work-log-events" role="log" aria-live={active ? "polite" : "off"} onScroll={(event) => {
      const list = event.currentTarget;
      followTail.current = list.scrollHeight - list.scrollTop - list.clientHeight <= 24;
    }}>{nodes.map((node) => <WorkingEntry key={node.key} node={node} />)}</div>}
  </section>;
}
