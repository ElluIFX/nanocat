import {
  AlertCircle,
  Bot,
  ChevronRight,
  ChevronsDownUp,
  ChevronsUpDown,
  GitBranch,
  Search,
  Sparkles,
  Terminal,
  User,
} from "lucide-preact";
import type { ComponentChildren } from "preact";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { selectedNodeId, setSelectedEvent } from "../store";
import type { TimelineEvent } from "../types";
import { EmptyState } from "./Primitives";

type TrajectoryKind = "user" | "assistant" | "tool" | "subagent" | "approval" | "system" | "event";

interface TrajectoryRecord {
  event: TimelineEvent;
  kind: TrajectoryKind;
  depth: number;
  heading: string;
  preview: string;
}

interface TrajectoryTurn {
  key: string;
  label: string;
  records: TrajectoryRecord[];
}

interface ToolEventFacts {
  ids: string[];
  names: string[];
  phase?: string;
  failed: boolean;
}

const TRANSIENT_LABEL = /\s*\((?:running|queued|cancelling|pending)\)\s*$/i;
const MAX_PREVIEW_CHARS = 420;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function cleanText(value: string): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > MAX_PREVIEW_CHARS ? `${compact.slice(0, MAX_PREVIEW_CHARS - 1).trimEnd()}…` : compact;
}

function compactValue(value: unknown): string {
  if (typeof value === "string") return cleanText(value);
  if (value === undefined || value === null) return "";
  const item = asRecord(value);
  if (typeof item.content === "string") return cleanText(item.content);
  if (typeof item.command === "string") return cleanText(item.command);
  if (typeof item.path === "string") return cleanText(item.path);
  const keys = Object.keys(item);
  if (keys.length && keys.every((key) => ["contentChars", "artifactCount", "toolEvent"].includes(key))) return "";
  try {
    return cleanText(JSON.stringify(value));
  } catch {
    return "";
  }
}

function toolEventFacts(event: TimelineEvent): ToolEventFacts {
  const ids: string[] = [];
  const names: string[] = [];
  let phase = event.phase;
  let failed = event.status === "failed" || event.status === "cancelled";
  for (const candidate of [event.redactedInput, event.redactedOutput]) {
    const record = asRecord(candidate);
    const nested = asRecord(record.toolEvent);
    const toolEvent = Object.keys(nested).length ? nested : record;
    if (!phase && typeof toolEvent.phase === "string") phase = toolEvent.phase;
    const calls = Array.isArray(toolEvent.calls) ? toolEvent.calls : [];
    for (const value of calls) {
      const call = asRecord(value);
      if (typeof call.id === "string" && !ids.includes(call.id)) ids.push(call.id);
      if (typeof call.name === "string" && !names.includes(call.name)) names.push(call.name);
      if (["error", "failed", "failure", "cancelled", "canceled"].includes(String(call.status ?? "").toLowerCase())) failed = true;
    }
  }
  if (event.toolCallId && !ids.includes(event.toolCallId)) ids.push(event.toolCallId);
  if (event.toolName && !names.includes(event.toolName)) names.push(event.toolName);
  return { ids, names, phase, failed };
}

function mergeToolEvents(events: TimelineEvent[]): TimelineEvent[] {
  const merged: TimelineEvent[] = [];
  const openCalls = new Map<string, number>();
  for (const event of events) {
    if (!(event.type.includes("tool") || event.source === "tool")) {
      merged.push(event);
      continue;
    }
    const facts = toolEventFacts(event);
    const key = facts.ids.length ? facts.ids.join("\u0000") : "";
    const phase = facts.phase?.toLowerCase();
    const isResult = phase === "end" || phase === "result" || phase === "complete" || phase === "completed";
    if (isResult && key && openCalls.has(key)) {
      const index = openCalls.get(key)!;
      const start = merged[index];
      const startedAt = start.startedAt ?? start.timestamp;
      const endedAt = event.endedAt ?? event.timestamp;
      const startMs = Date.parse(startedAt);
      const endMs = Date.parse(endedAt);
      merged[index] = {
        ...start,
        ...event,
        eventId: event.eventId,
        nodeId: event.nodeId ?? event.eventId,
        sequence: start.sequence,
        timestamp: start.timestamp,
        startedAt,
        endedAt,
        durationMs: event.durationMs ?? (Number.isFinite(startMs) && Number.isFinite(endMs) ? Math.max(0, endMs - startMs) : undefined),
        status: facts.failed ? "failed" : "completed",
        summary: facts.names.length ? facts.names.join(", ") : event.summary ?? start.summary,
        redactedInput: start.redactedInput ?? event.redactedInput,
        redactedOutput: event.redactedOutput ?? start.redactedOutput,
        toolCallId: facts.ids[0] ?? start.toolCallId,
        toolName: facts.names[0] ?? start.toolName,
      };
      openCalls.delete(key);
      continue;
    }
    merged.push(facts.failed ? { ...event, status: "failed" } : event);
    if (key && (phase === "start" || phase === "call")) openCalls.set(key, merged.length - 1);
  }
  return merged;
}

function trajectoryKind(event: TimelineEvent): TrajectoryKind {
  if (event.type.includes("subagent")) return "subagent";
  if (event.type.includes("tool") || event.source === "tool") return "tool";
  if (event.type.includes("approval")) return "approval";
  if (event.type.includes("assistant") || event.type.includes("provider")) return "assistant";
  if (event.type.includes("user") || event.type.includes("steer")) return "user";
  if (event.type.includes("system") || event.type.includes("compact") || event.type.includes("turn")) return "system";
  return "event";
}

function trajectoryHeading(event: TimelineEvent, kind: TrajectoryKind): string {
  const tool = toolEventFacts(event);
  if (kind === "tool" && tool.names.length) return tool.names.join(", ");
  if (event.toolName) return event.toolName;
  if (event.type === "assistant.progress" || event.type === "assistant.thinking" || event.type === "assistant.work") return "Thinking";
  if (event.type === "assistant.final") return "Assistant response";
  if (event.type === "user.message") return "User message";
  const summary = event.summary?.replace(TRANSIENT_LABEL, "").replace(/^Tools?:\s*/i, "").trim();
  if (summary) return summary;
  return event.type.split(".").map((part) => part.replaceAll("_", " ")).join(" · ");
}

function trajectoryPreview(event: TimelineEvent, kind: TrajectoryKind): string {
  const values = kind === "tool"
    ? [event.redactedInput, event.redactedOutput, event.content]
    : [event.content, event.redactedOutput, event.redactedInput];
  for (const value of values) {
    const preview = compactValue(value);
    if (preview) return preview;
  }
  return "";
}

function recordDepth(event: TimelineEvent, byId: Map<string, TimelineEvent>): number {
  let depth = 0;
  let parentId = event.parentId;
  const seen = new Set<string>();
  while (parentId && depth < 3 && !seen.has(parentId)) {
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (!parent) break;
    depth += 1;
    parentId = parent.parentId;
  }
  return depth;
}

function buildTurns(events: TimelineEvent[]): TrajectoryTurn[] {
  const ordered = [...events].sort((left, right) => left.sequence - right.sequence);
  const byId = new Map(ordered.flatMap((event) => [event.nodeId, event.eventId].filter(Boolean).map((id) => [id!, event] as const)));
  const turns: TrajectoryTurn[] = [];
  const turnNumbers = new Map<string, number>();
  for (const event of ordered) {
    const key = event.turnId ? `turn:${event.turnId}` : "between-turns";
    let turn = turns.at(-1);
    if (!turn || turn.key !== key) {
      if (event.turnId && !turnNumbers.has(event.turnId)) turnNumbers.set(event.turnId, turnNumbers.size + 1);
      turn = {
        key,
        label: event.turnId ? `Turn ${turnNumbers.get(event.turnId)}` : "Between turns",
        records: [],
      };
      turns.push(turn);
    }
    const kind = trajectoryKind(event);
    turn.records.push({
      event,
      kind,
      depth: recordDepth(event, byId),
      heading: trajectoryHeading(event, kind),
      preview: trajectoryPreview(event, kind),
    });
  }
  return turns;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
}

function kindIcon(kind: TrajectoryKind): ComponentChildren {
  if (kind === "tool") return <Terminal size={12} />;
  if (kind === "subagent") return <GitBranch size={12} />;
  if (kind === "assistant") return <Sparkles size={12} />;
  if (kind === "user") return <User size={12} />;
  if (kind === "approval") return <AlertCircle size={12} />;
  return <Bot size={12} />;
}

function turnSummary(turn: TrajectoryTurn): string {
  const tools = turn.records.filter((record) => record.kind === "tool").length;
  const first = Date.parse(turn.records[0]?.event.timestamp ?? "");
  const last = Date.parse(turn.records.at(-1)?.event.endedAt ?? turn.records.at(-1)?.event.timestamp ?? "");
  const elapsed = Number.isFinite(first) && Number.isFinite(last) && last >= first ? formatDuration(last - first) : undefined;
  return [`${turn.records.length} records`, tools ? `${tools} tools` : undefined, elapsed].filter(Boolean).join(" · ");
}

function recordSearchText(record: TrajectoryRecord): string {
  const event = record.event;
  return [record.heading, record.preview, record.kind, event.type, event.source, event.toolName].filter(Boolean).join(" ").toLowerCase();
}

function recordFailed(record: TrajectoryRecord): boolean {
  return record.event.status === "failed" || record.event.status === "cancelled" || toolEventFacts(record.event).failed;
}

function TrajectoryOverview({ records }: { records: TrajectoryRecord[] }) {
  const lanes = ["user", "assistant", "tool"] as const;
  if (!records.length) return null;
  return <div class="trajectory-overview" aria-label="Trajectory overview">
    {lanes.map((lane) => <div class="trajectory-overview-lane" key={lane}>
      <span>{lane === "user" ? "Input" : lane === "assistant" ? "Agent" : "Tools"}</span>
      <div class="trajectory-overview-track">
        {records.map((record, index) => {
          const recordLane = record.kind === "user" ? "user" : record.kind === "tool" || record.kind === "subagent" ? "tool" : "assistant";
          if (recordLane !== lane) return null;
          const selected = selectedNodeId.value === (record.event.nodeId ?? record.event.eventId);
          return <button
            key={record.event.eventId}
            class={`trajectory-overview-mark kind-${record.kind} ${recordFailed(record) ? "is-failed" : ""} ${selected ? "selected" : ""}`}
            style={`--trajectory-mark-left:${records.length === 1 ? 50 : index / (records.length - 1) * 100}%`}
            aria-label={`Inspect ${record.heading}`}
            title={`${record.heading} · ${timeLabel(record.event.timestamp)}`}
            onClick={() => setSelectedEvent(record.event)}
          />;
        })}
      </div>
    </div>)}
  </div>;
}

function TrajectoryRow({ record, turn, first, last, collapsed, onToggleTurn }: {
  record: TrajectoryRecord;
  turn: TrajectoryTurn;
  first: boolean;
  last: boolean;
  collapsed: boolean;
  onToggleTurn: () => void;
}) {
  const event = record.event;
  const id = event.nodeId ?? event.eventId;
  const selected = selectedNodeId.value === id;
  const failed = recordFailed(record);
  const attention = event.status === "waiting_approval" || event.status === "waiting_user";
  const inspect = () => setSelectedEvent(event);
  return <tr
    class={`${selected ? "selected" : ""} ${failed ? "is-failed" : ""} ${attention ? "needs-attention" : ""}`}
    data-turn-start={first || undefined}
    data-turn-end={last || undefined}
    data-kind={record.kind}
    onClick={inspect}
  >
    <td class="trajectory-sequence">
      {first && <button class="trajectory-turn-label" aria-label={`${collapsed ? "Expand" : "Collapse"} ${turn.label}`} onClick={(click) => { click.stopPropagation(); onToggleTurn(); }}>{turn.label}</button>}
      <span>{event.sequence}</span>
      <i aria-hidden="true" />
    </td>
    <td class="trajectory-kind"><span class={`trajectory-kind-tag kind-${record.kind}`}>{kindIcon(record.kind)}<span>{record.kind}</span></span></td>
    <td class="trajectory-record-content"><div class="trajectory-record-content-inner">
      <button class="trajectory-record-open" aria-label={`Inspect ${record.heading}`} onClick={(click) => { click.stopPropagation(); inspect(); }} style={`--trajectory-depth:${record.depth}`}>
        <strong>{record.heading}</strong>
        {record.preview && <span>{record.preview}</span>}
      </button>
      {event.durationMs !== undefined && <small>{formatDuration(event.durationMs)}</small>}
    </div></td>
    <td class="trajectory-time"><time dateTime={event.timestamp}>{timeLabel(event.timestamp)}</time></td>
  </tr>;
}

export function TrajectoryLedger({ sessionKey, events, hasEarlier, onLoadEarlier }: {
  sessionKey: string;
  events: TimelineEvent[];
  hasEarlier: boolean;
  onLoadEarlier: () => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [collapsedTurns, setCollapsedTurns] = useState<Set<string>>(new Set());
  const [visibleCount, setVisibleCount] = useState(1000);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const followsTail = useRef(true);
  const mergedEvents = useMemo(() => mergeToolEvents(events), [events]);
  const visibleEvents = mergedEvents.slice(-visibleCount);
  const allTurns = useMemo(() => buildTurns(visibleEvents), [visibleEvents]);
  const needle = query.trim().toLowerCase();
  const turns = useMemo(() => needle
    ? allTurns.map((turn) => ({ ...turn, records: turn.records.filter((record) => recordSearchText(record).includes(needle)) })).filter((turn) => turn.records.length)
    : allTurns, [allTurns, needle]);
  const records = turns.flatMap((turn) => turn.records);
  const collapsible = allTurns.filter((turn) => turn.records.length > 1);
  const allCollapsed = collapsible.length > 0 && collapsible.every((turn) => collapsedTurns.has(turn.key));

  useEffect(() => {
    setQuery("");
    setCollapsedTurns(new Set());
    setVisibleCount(1000);
    followsTail.current = true;
  }, [sessionKey]);
  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    if (scroll && followsTail.current) scroll.scrollTop = scroll.scrollHeight;
  }, [records.length, collapsedTurns]);

  const toggleTurn = (key: string) => setCollapsedTurns((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  });
  const loadEarlier = async () => {
    if (loadingEarlier) return;
    setLoadingEarlier(true);
    const scroll = scrollRef.current;
    const height = scroll?.scrollHeight ?? 0;
    const top = scroll?.scrollTop ?? 0;
    followsTail.current = false;
    try {
      if (mergedEvents.length > visibleEvents.length) setVisibleCount((count) => Math.min(mergedEvents.length, count + 1000));
      else await onLoadEarlier();
      requestAnimationFrame(() => {
        if (scroll) scroll.scrollTop = top + scroll.scrollHeight - height;
      });
    } finally {
      setLoadingEarlier(false);
    }
  };

  return <section class="trajectory-ledger">
    <div class="trajectory-toolbar" role="toolbar" aria-label="Trajectory controls">
      <span class="trajectory-count"><strong>{records.length}</strong> records <i /> {turns.length} turns</span>
      <button
        class="trajectory-fold-all"
        disabled={!collapsible.length || Boolean(needle)}
        aria-pressed={allCollapsed}
        onClick={() => setCollapsedTurns(allCollapsed ? new Set() : new Set(collapsible.map((turn) => turn.key)))}
      >{allCollapsed ? <ChevronsUpDown size={13} /> : <ChevronsDownUp size={13} />}{allCollapsed ? "Expand turns" : "Collapse turns"}</button>
      <label class="trajectory-search"><Search size={13} /><span class="sr-only">Search trajectory</span><input type="search" value={query} onInput={(event) => setQuery(event.currentTarget.value)} placeholder="Search" /></label>
    </div>
    <TrajectoryOverview records={records} />
    <div ref={scrollRef} class="trajectory-table-scroll" onScroll={(event) => {
      const target = event.currentTarget;
      followsTail.current = target.scrollHeight - target.scrollTop - target.clientHeight < 48;
    }}>
      {(hasEarlier || mergedEvents.length > visibleEvents.length) && <button class="trajectory-load-earlier" disabled={loadingEarlier} onClick={() => void loadEarlier()}>{loadingEarlier ? "Loading earlier history…" : "Load earlier history"}</button>}
      {turns.length ? <table class="trajectory-table">
        <colgroup><col class="trajectory-sequence-column" /><col class="trajectory-kind-column" /><col /><col class="trajectory-time-column" /></colgroup>
        <thead><tr><th>#</th><th>Event</th><th>Content</th><th>Time</th></tr></thead>
        <tbody>{turns.flatMap((turn) => {
          const collapsed = !needle && collapsedTurns.has(turn.key) && turn.records.length > 1;
          const rows = collapsed ? turn.records.slice(0, 1) : turn.records;
          return [
            ...rows.map((record, index) => <TrajectoryRow key={record.event.eventId} record={record} turn={turn} first={index === 0} last={!collapsed && index === rows.length - 1} collapsed={collapsed} onToggleTurn={() => toggleTurn(turn.key)} />),
            collapsed && <tr key={`${turn.key}:summary`} class="trajectory-collapsed-row" data-turn-end="true"><td /><td /><td colSpan={2}><button onClick={() => toggleTurn(turn.key)}><ChevronRight size={13} /><span>{turnSummary(turn)}</span></button></td></tr>,
          ];
        })}</tbody>
      </table> : <EmptyState icon={<Sparkles size={25} />} title="No matching activity" description="Change the search query, or start a turn to record its execution trajectory." />}
    </div>
  </section>;
}
