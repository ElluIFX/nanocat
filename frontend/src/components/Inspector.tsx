import { Clipboard, Download, ExternalLink, FileCode2, Image as ImageIcon, Info, X } from "lucide-preact";
import { useState } from "preact/hooks";
import { api } from "../api/client";
import { inspectorOpen, selectedEvent, setSelectedNode } from "../store";
import type { ArtifactRef } from "../types";
import { IconButton } from "./Primitives";
import { useDialogFocus } from "./focus";

function JsonBlock({ value }: { value: unknown }) {
  if (value === undefined || value === null) return <p class="muted">No data recorded</p>;
  return <pre class="json-block"><code>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</code></pre>;
}

function ArtifactLink({ artifact }: { artifact: ArtifactRef }) {
  const Icon = artifact.kind === "image" ? ImageIcon : FileCode2;
  return <a class={`artifact-row ${artifact.expired ? "expired" : ""}`} href={artifact.expired ? undefined : api.artifactUrl(artifact)} target="_blank" rel="noreferrer">
    <Icon size={17} />
    <span><strong>{artifact.name}</strong><small>{artifact.expired ? "Output expired" : [artifact.kind, artifact.size !== undefined ? formatSize(artifact.size) : ""].filter(Boolean).join(" · ")}</small></span>
    {!artifact.expired && <ExternalLink size={14} />}
  </a>;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
}

export function Inspector() {
  const event = selectedEvent.value;
  const [tab, setTab] = useState<"summary" | "input" | "output" | "artifacts" | "metadata">("summary");
  const visible = Boolean(inspectorOpen.value && event);
  const close = () => setSelectedNode(null);
  const panel = useDialogFocus<HTMLElement>(visible, close);
  if (!visible || !event) return null;

  const tabs = [
    ["summary", "Summary"],
    ["input", "Input"],
    ["output", "Output"],
    ["artifacts", `Artifacts${event.artifactRefs?.length ? ` (${event.artifactRefs.length})` : ""}`],
    ["metadata", "Metadata"],
  ] as const;
  const timestamp = new Date(event.timestamp);
  const timestampLabel = Number.isFinite(timestamp.getTime()) ? timestamp.toLocaleString() : "—";

  return <>
    <button class="inspector-scrim" aria-label="Close inspector" onClick={close} />
    <aside ref={panel} tabIndex={-1} class="inspector" role="dialog" aria-modal="true" aria-label="Activity inspector">
      <header>
        <div><span class="eyebrow">{event.source ?? "Runtime"}</span><h2>{event.summary || event.type}</h2></div>
        <IconButton label="Close inspector" onClick={close}><X size={18} /></IconButton>
      </header>
      <nav class="inspector-tabs" role="tablist" aria-label="Inspector sections">{tabs.map(([id, label], index) => <button id={`inspector-${id}-tab`} role="tab" aria-selected={tab === id} aria-controls="inspector-panel" tabIndex={tab === id ? 0 : -1} class={tab === id ? "active" : ""} onClick={() => setTab(id)} onKeyDown={(event) => {
        const offset = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        const target = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : offset ? (index + offset + tabs.length) % tabs.length : -1;
        if (target < 0) return;
        event.preventDefault();
        setTab(tabs[target][0]);
        document.getElementById(`inspector-${tabs[target][0]}-tab`)?.focus();
      }}>{label}</button>)}</nav>
      <div id="inspector-panel" class="inspector-body" role="tabpanel" aria-labelledby={`inspector-${tab}-tab`}>
        {tab === "summary" && <div class="inspector-summary">
          <dl>
            <div><dt>Type</dt><dd>{event.type}</dd></div>
            {event.status && !["running", "queued", "cancelling"].includes(event.status) && <div><dt>Status</dt><dd>{event.status.replaceAll("_", " ")}</dd></div>}
            <div><dt>Time</dt><dd>{timestampLabel}</dd></div>
            {event.durationMs !== undefined && <div><dt>Duration</dt><dd>{formatDuration(event.durationMs)}</dd></div>}
            {event.model && <div><dt>Model</dt><dd>{event.model}</dd></div>}
            {event.usage?.totalTokens !== undefined && <div><dt>Tokens</dt><dd>{event.usage.totalTokens.toLocaleString()}</dd></div>}
          </dl>
          {event.content && <section><h3>Content</h3><JsonBlock value={event.content} /></section>}
        </div>}
        {tab === "input" && <JsonBlock value={event.redactedInput} />}
        {tab === "output" && <JsonBlock value={event.redactedOutput ?? event.content} />}
        {tab === "artifacts" && <div class="artifact-list">{event.artifactRefs?.length ? event.artifactRefs.map((artifact) => <ArtifactLink key={artifact.id} artifact={artifact} />) : <p class="muted">No artifacts attached to this activity</p>}</div>}
        {tab === "metadata" && <JsonBlock value={{ eventId: event.eventId, sequence: event.sequence, nodeId: event.nodeId, parentId: event.parentId, sessionId: event.sessionId, turnId: event.turnId, requestId: event.requestId, phase: event.phase }} />}
      </div>
      <footer>
        <button class="secondary-button" onClick={() => void navigator.clipboard.writeText(JSON.stringify(event, null, 2))}><Clipboard size={15} /> Copy event</button>
        {event.artifactRefs?.[0] && !event.artifactRefs[0].expired && <a class="secondary-button" href={api.artifactUrl(event.artifactRefs[0])} download><Download size={15} /> Download</a>}
        {!event.content && !event.redactedOutput && <span><Info size={14} /> Detailed output was not retained.</span>}
      </footer>
    </aside>
  </>;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}
