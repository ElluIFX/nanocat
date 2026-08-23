import { Activity, CirclePause, Download, ExternalLink, Filter, RefreshCw, Search, Wifi } from "lucide-preact";
import { useEffect, useMemo, useState } from "preact/hooks";
import { PageHeader } from "../components/AppShell";
import { connection, loadLogs, logs, reportOperationError, runtime } from "../store";

export function Diagnostics() {
  const [query, setQuery] = useState("");
  const [level, setLevel] = useState("all");
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    const refresh = () => void loadLogs().catch((error) => reportOperationError(error, "Diagnostics could not be refreshed"));
    refresh();
    if (paused) return;
    const timer = window.setInterval(refresh, 5000);
    return () => window.clearInterval(timer);
  }, [paused]);
  const filtered = useMemo(() => logs.value.filter((entry) =>
    (level === "all" || entry.level === level) && `${entry.message} ${entry.source} ${entry.turnId ?? ""}`.toLowerCase().includes(query.toLowerCase()),
  ), [logs.value, level, query]);

  const download = () => {
    const blob = new Blob([filtered.map((entry) => `${entry.timestamp} ${entry.level.toUpperCase()} ${entry.source} ${entry.message}`).join("\n")], { type: "text/plain" });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = "nanocat-diagnostics.log";
    anchor.click();
    URL.revokeObjectURL(anchor.href);
  };

  return <div class="page diagnostics-page">
    <PageHeader title="Diagnostics" eyebrow="Runtime observability" actions={<>
      <button class="secondary-button" onClick={() => setPaused(!paused)}><CirclePause size={15} /> {paused ? "Resume" : "Pause"}</button>
      <button class="secondary-button" onClick={() => void loadLogs().catch((error) => reportOperationError(error, "Diagnostics could not be refreshed"))}><RefreshCw size={15} /> Refresh</button>
    </>} />
    <section class="runtime-metrics" aria-label="Runtime status">
      <article><span><Wifi size={15} /> Connection</span><strong class={`metric-${connection.value}`}>{connection.value}</strong></article>
      <article><span><Activity size={15} /> Runtime</span><strong>{runtime.value?.healthy ? "Healthy" : "Unavailable"}</strong></article>
      <article><span>Model</span><strong>{runtime.value?.model ?? "—"}</strong></article>
      <article><span>Context</span><strong>{runtime.value?.contextUsed !== undefined ? `${runtime.value.contextUsed.toLocaleString()} / ${runtime.value.contextLimit?.toLocaleString() ?? "—"}` : "—"}</strong></article>
    </section>
    <section class="log-panel">
      <header class="log-toolbar">
        <label class="search-box"><Search size={15} /><span class="sr-only">Search logs</span><input value={query} onInput={(event) => setQuery(event.currentTarget.value)} placeholder="Search logs and correlation IDs" /></label>
        <label class="select-control"><Filter size={15} /><span class="sr-only">Log level</span><select value={level} onChange={(event) => setLevel(event.currentTarget.value)}><option value="all">All levels</option><option value="info">Info</option><option value="warning">Warning</option><option value="error">Error</option><option value="debug">Debug</option></select></label>
        <button class="secondary-button" onClick={download}><Download size={15} /> Export</button>
      </header>
      <div class="log-table" role="log" aria-live={paused ? "off" : "polite"}>
        {filtered.length ? filtered.map((entry) => <div class={`log-row level-${entry.level}`} key={entry.id}>
          <time>{entry.timestamp && Number.isFinite(new Date(entry.timestamp).getTime()) ? new Date(entry.timestamp).toLocaleTimeString([], { hour12: false }) : "--:--:--"}</time>
          <span class="log-level">{entry.level}</span>
          <span class="log-source">{entry.source}</span>
          <code>{entry.message}</code>
          {entry.sessionId && <a data-router href={`/sessions/${encodeURIComponent(entry.sessionId)}/trajectory${entry.turnId ? `?turn=${encodeURIComponent(entry.turnId)}` : ""}`} aria-label="Open related activity"><ExternalLink size={14} /></a>}
        </div>) : <div class="log-empty">No matching log entries</div>}
      </div>
    </section>
  </div>;
}
