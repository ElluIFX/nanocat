import { Activity, AlertTriangle, ArrowRight, Bot, Clock3, Plus, Sparkles } from "lucide-preact";
import { hrefFor } from "../router";
import { createSessionAndOpen, creatingSession, pendingAttention, runningSessions, runtime, sessions } from "../store";
import type { SessionSummary } from "../types";
import { PageHeader } from "../components/AppShell";
import { EmptyState, StatusBadge } from "../components/Primitives";

function SessionCard({ session, attention = false }: { session: SessionSummary; attention?: boolean }) {
  return <a data-router href={hrefFor({ name: "session", sessionId: session.id, view: "conversation" })} class={`run-card ${attention ? "attention" : ""}`}>
    <div class="run-card-top"><StatusBadge status={session.status} />{session.model && <span class="model-label">{session.model}</span>}</div>
    <h3>{session.title || "Untitled session"}</h3>
    <p>{session.preview || "Open this session to inspect its latest activity."}</p>
    <footer>
      {session.turnCount !== undefined && <span>{session.turnCount} turns</span>}
      <span class="open-label">Open <ArrowRight size={14} /></span>
    </footer>
  </a>;
}

export function Home() {
  const featured = new Set([
    ...pendingAttention.value.slice(0, 3).map((session) => session.id),
    ...runningSessions.value.slice(0, 3).map((session) => session.id),
  ]);
  const recent = sessions.value.filter((session) => !featured.has(session.id)).slice(0, 6);
  const create = createSessionAndOpen;

  return <div class="page command-center">
    <PageHeader title="Home" eyebrow="NanoCat workspace" actions={
      <button class="primary-button" disabled={creatingSession.value} onClick={() => void create()}><Plus size={16} /> {creatingSession.value ? "Creating…" : "New session"}</button>
    }>
      <p>Run, inspect, and guide your agents from one quiet workspace.</p>
    </PageHeader>

    {runtime.value?.unprotected && <div class="security-banner" role="status">
      <AlertTriangle size={17} />
      <span><strong>Web access is unprotected.</strong> Set a password before exposing this listener beyond a trusted device.</span>
      <a data-router href="/settings/web">Configure</a>
    </div>}

    {pendingAttention.value.length > 0 && <section class="dashboard-section">
      <div class="section-heading"><div><span class="section-kicker amber"><AlertTriangle size={14} /> Needs attention</span><h2>Action required</h2></div><span>{pendingAttention.value.length}</span></div>
      <div class="card-grid attention-grid">{pendingAttention.value.slice(0, 3).map((session) => <SessionCard key={session.id} session={session} attention />)}</div>
    </section>}

    {runningSessions.value.length > 0 && <section class="dashboard-section">
      <div class="section-heading"><div><span class="section-kicker blue"><Activity size={14} /> Live</span><h2>Running now</h2></div><span>{runningSessions.value.length}</span></div>
      <div class="card-grid">{runningSessions.value.slice(0, 3).map((session) => <SessionCard key={session.id} session={session} />)}</div>
    </section>}

    <section class="dashboard-section">
      <div class="section-heading"><div><span class="section-kicker"><Clock3 size={14} /> Workspace</span><h2>Recent sessions</h2></div>{recent.length > 0 && <span>{recent.length}</span>}</div>
      {recent.length
        ? <div class="card-grid">{recent.map((session) => <SessionCard key={session.id} session={session} />)}</div>
        : <EmptyState icon={<Bot size={28} />} title="Your workspace is ready" description="Start a session to ask a question, run a tool, or delegate a larger task." action={<button class="primary-button" disabled={creatingSession.value} onClick={() => void create()}><Sparkles size={16} /> Start a session</button>} />}
    </section>
  </div>;
}
