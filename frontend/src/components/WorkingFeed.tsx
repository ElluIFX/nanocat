import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronRight,
  CircleStop,
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

type JsonRecord = Record<string, unknown>;

interface WorkingNode {
  key: string;
  kind: "thinking" | "tool" | "subagent" | "approval" | "progress";
  label: string;
  source?: string;
  status?: RunStatus;
  startedAt: string;
  endedAt?: string;
  durationMs?: number;
  content?: string;
  input?: unknown;
  output?: unknown;
  artifactRefs?: TimelineEvent["artifactRefs"];
  eventId: string;
  nodeId?: string;
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
  if (Object.prototype.hasOwnProperty.call(output, "preview")) return output.preview;
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
  if (event.type.includes("thinking") || event.type.includes("reasoning")) return "Thinking";
  if (event.type.includes("subagent")) return event.summary || "Subagent";
  if (event.type.includes("approval")) return event.summary || "Approval";
  if (event.type === "assistant.progress") return event.summary === "Assistant Progress" ? "Progress update" : event.summary || "Progress update";
  return event.summary || event.type.split(".").map((part) => part.replaceAll("_", " ")).join(" · ");
}

function normalizeWorkingEvents(events: TimelineEvent[]): WorkingNode[] {
  const nodes: WorkingNode[] = [];
  const toolNodes = new Map<string, WorkingNode>();
  const sorted = [...events].sort((left, right) => left.sequence - right.sequence);

  const upsertTool = (event: TimelineEvent, call: JsonRecord, ordinal: number) => {
    const callId = String(call.id ?? event.toolCallId ?? "");
    const name = String(call.name ?? event.toolName ?? "tool");
    const key = callId || `${event.eventId}:${name}:${ordinal}`;
    const existing = toolNodes.get(key);
    const input = call.arguments ?? call.args ?? event.redactedInput;
    const output = cleanOutput(call.resultPreview ?? call.output ?? event.redactedOutput);
    const status = normalizeToolStatus(call.status, event.status);
    if (existing) {
      existing.status = status ?? existing.status;
      existing.endedAt = event.endedAt ?? event.timestamp ?? existing.endedAt;
      existing.durationMs = typeof call.durationMs === "number" ? call.durationMs : existing.durationMs;
      if (input !== undefined) existing.input = input;
      if (output !== undefined) existing.output = output;
      if (event.artifactRefs?.length) existing.artifactRefs = event.artifactRefs;
      return;
    }
    const node: WorkingNode = {
      key: `tool:${key}`,
      kind: "tool",
      label: name,
      source: event.source,
      status,
      startedAt: event.startedAt ?? event.timestamp,
      endedAt: event.endedAt,
      durationMs: typeof call.durationMs === "number" ? call.durationMs : undefined,
      input,
      output,
      artifactRefs: event.artifactRefs,
      eventId: event.eventId,
      nodeId: event.nodeId,
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
    const kind: WorkingNode["kind"] = event.type.includes("thinking") || event.type.includes("reasoning") || event.type === "assistant.work"
      ? "thinking"
      : event.type.includes("subagent")
        ? "subagent"
        : event.type.includes("approval")
          ? "approval"
          : "progress";
    nodes.push({
      key: event.eventId,
      kind,
      label: defaultLabel(event),
      source: event.source,
      status: event.status,
      startedAt: event.startedAt ?? event.timestamp,
      endedAt: event.endedAt,
      durationMs: kind === "subagent" ? event.durationMs : undefined,
      content: meaningfulContent(event),
      input: kind === "approval" ? event.redactedInput : undefined,
      output: kind === "thinking" || kind === "progress" ? undefined : cleanOutput(event.redactedOutput),
      artifactRefs: event.artifactRefs,
      eventId: event.eventId,
      nodeId: event.nodeId,
    });
  }
  return nodes;
}

function settleWorkingNodes(nodes: WorkingNode[], active: boolean, terminalStatus?: string): WorkingNode[] {
  const settled = nodes.map((node) => ({ ...node }));
  for (const [index, node] of settled.entries()) {
    if (node.status !== "running") continue;
    const successor = settled.slice(index + 1).find((candidate) => (
      node.kind !== "tool" || candidate.kind === "thinking" || candidate.kind === "progress" || candidate.kind === "approval"
    ));
    if (successor) {
      node.status = "completed";
      node.endedAt = successor.startedAt || node.endedAt;
      const startedAt = Date.parse(node.startedAt);
      const endedAt = Date.parse(node.endedAt ?? "");
      if (Number.isFinite(startedAt) && Number.isFinite(endedAt)) node.durationMs = Math.max(0, endedAt - startedAt);
      continue;
    }
    if (!active) {
      node.status = terminalStatus === "failed" ? "failed" : terminalStatus === "cancelled" ? "cancelled" : "completed";
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
  const failed = node.status === "failed" || node.status === "cancelled";
  return <article class={`working-entry kind-${node.kind} ${failed ? "is-failed" : ""}`}>
    <div class="working-entry-line">
      <span class="working-entry-icon">{nodeIcon(node)}</span>
      <button class="working-entry-title" onClick={() => setSelectedNode(node.nodeId ?? node.eventId)}>
        <strong>{node.label}</strong>
        <small>{[node.source && node.source !== "runtime" ? node.source : undefined, node.durationMs !== undefined ? formatDuration(node.durationMs) : undefined].filter(Boolean).join(" · ")}</small>
      </button>
      {node.status && node.status !== "completed" && <span class={`working-entry-status status-${node.status}`}>{node.status === "running" ? <LoaderCircle class="spin" size={12} /> : null}{node.status.replaceAll("_", " ")}</span>}
    </div>
    {node.content && <div class="working-entry-content"><Markdown content={node.content} /></div>}
    {hasDetails && <details class="working-entry-details"><summary>Details</summary><div>
      <DetailValue label="Input" value={node.input} />
      <DetailValue label="Output" value={node.output} />
      {node.artifactRefs?.length ? <section class="working-detail-section"><h5>Artifacts</h5><div class="turn-artifacts">{node.artifactRefs.map((artifact) => <a key={artifact.id} href={api.artifactUrl(artifact)} target="_blank" rel="noreferrer">{artifact.name}</a>)}</div></section> : null}
    </div></details>}
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
  const workEvents = events.filter((event) => event.type !== "turn.queued" && event.type !== "turn.steer_queued");
  const visibleEvents = workEvents.length ? workEvents : active ? [{ eventId: "working-pending", sequence: 0, timestamp: mountedAt.current, type: "assistant.progress", status: "running" as const, summary: "Preparing run" }] : [];
  const terminalFailure = status === "failed" || status === "cancelled" || events.some((event) => event.status === "failed" || event.status === "cancelled");
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
  const subagentCount = nodes.filter((node) => node.kind === "subagent").length;
  return <section class={`work-log working-feed ${open ? "open" : ""} ${active ? "active" : "terminal"}`}>
    <div class="work-log-heading">
      <button class="work-log-summary" onClick={() => {
        if (!open) followTail.current = true;
        setOpen(!open);
      }} aria-expanded={open}>
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <span class="work-log-icon">{active ? <LoaderCircle class="spin" size={13} /> : <Zap size={13} />}</span>
        <span><strong>{active ? "Working…" : `Worked for ${formatDuration(duration)}`}</strong><small>{nodes.length} steps{toolCount ? ` · ${toolCount} tools` : ""}{subagentCount ? ` · ${subagentCount} subagents` : ""}{active ? ` · ${formatDuration(duration)}` : ""}</small></span>
      </button>
      {active && onStop && <button class="working-stop" onClick={onStop}><CircleStop size={13} /> Stop</button>}
    </div>
    {open && <div ref={eventList} class="work-log-events" onScroll={(event) => {
      const list = event.currentTarget;
      followTail.current = list.scrollHeight - list.scrollTop - list.clientHeight <= 24;
    }}>{nodes.map((node) => <WorkingEntry key={node.key} node={node} />)}</div>}
  </section>;
}
