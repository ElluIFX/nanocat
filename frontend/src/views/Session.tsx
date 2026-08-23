import {
  Activity,
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Bot,
  Check,
  FileText,
  ImagePlus,
  LoaderCircle,
  MessageSquareText,
  MoreHorizontal,
  Pencil,
  Paperclip,
  Send,
  Sparkles,
  Trash2,
  User,
  X,
} from "lucide-preact";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { api } from "../api/client";
import { deleteDraftFiles, loadDraftFiles, saveDraftFiles, validateDraftFiles } from "../api/drafts";
import { PageHeader } from "../components/AppShell";
import { Inspector } from "../components/Inspector";
import { TrajectoryLedger } from "../components/TrajectoryLedger";
import { useDialogFocus } from "../components/focus";
import { Markdown } from "../components/Markdown";
import { EmptyState, IconButton, Skeleton, StatusBadge } from "../components/Primitives";
import { WorkingFeed } from "../components/WorkingFeed";
import { hrefFor, navigate, route } from "../router";
import {
  approvals,
  connection,
  currentSession,
  inspectorOpen,
  invalidateSessionLoad,
  loadSession,
  loadEarlierConversation,
  loadEarlierTrajectory,
  loadModels,
  loadingSession,
  models,
  recoverGlobalState,
  reloadCurrentSession,
  refreshApprovals,
  reportOperationError,
  selectedNodeId,
  setSelectedNode,
  sessions,
  runtime,
} from "../store";
import type { ApprovalRequest, ConversationTurn, TimelineEvent } from "../types";

function dateValue(value: string): Date | null {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}

function timeLabel(value: string, seconds = false): string | null {
  const date = dateValue(value);
  const options: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  if (seconds) options.second = "2-digit";
  return date?.toLocaleTimeString([], options) ?? null;
}

function Turn({ turn, active, events, onStop }: { turn: ConversationTurn; active?: boolean; events?: TimelineEvent[]; onStop?: () => void }) {
  const timestamp = timeLabel(turn.timestamp);
  const work = events ?? turn.activity ?? [];
  return <article class={`conversation-turn role-${turn.role}`}>
    <div class="turn-avatar" aria-hidden="true">{turn.role === "user" ? <User size={16} /> : <Bot size={17} />}</div>
    <div class="turn-content">
      <header><strong>{turn.role === "user" ? "You" : turn.role === "assistant" ? "NanoCat" : "System"}</strong>{timestamp && <time dateTime={turn.timestamp}>{timestamp}</time>}{turn.status && <StatusBadge status={turn.status} />}</header>
      {turn.role === "assistant" && (active || work.length > 0) && <WorkingFeed events={work} active={active} status={turn.status} onStop={onStop} />}
      {turn.content && <Markdown content={turn.content} />}
      {turn.artifactRefs?.length ? <div class="turn-artifacts">{turn.artifactRefs.map((artifact) => <a href={api.artifactUrl(artifact)} target="_blank" rel="noreferrer"><FileText size={15} />{artifact.name}</a>)}</div> : null}
    </div>
  </article>;
}

function ApprovalDock({ approval, onDone }: { approval: ApprovalRequest; onDone: () => void }) {
  const [submitting, setSubmitting] = useState<string | null>(null);
  const decide = async (action: ApprovalRequest["allowedActions"][number]) => {
    setSubmitting(action);
    try {
      await api.decideApproval(approval.requestId, action, approval.sessionId);
      onDone();
    } catch (error) {
      reportOperationError(error, "The approval response could not be submitted");
    } finally {
      setSubmitting(null);
    }
  };
  return <section class="approval-dock" aria-labelledby="approval-title">
    <header><span><AlertCircle size={17} /></span><div><p class="eyebrow">Approval required</p><h3 id="approval-title">{approval.toolName}</h3></div></header>
    <p>{approval.summary}</p>
    {approval.parameters !== undefined && <details><summary>Review parameters</summary><pre><code>{JSON.stringify(approval.parameters, null, 2)}</code></pre></details>}
    {approval.review && <div class="approval-review"><Sparkles size={14} /><span>{approval.review}</span></div>}
    <footer>
      {approval.allowedActions.includes("deny") && <button class="danger-button" disabled={Boolean(submitting)} onClick={() => void decide("deny")}>{submitting === "deny" ? <LoaderCircle class="spin" size={15} /> : <X size={15} />} Deny</button>}
      <span />
      {approval.allowedActions.filter((item) => item !== "deny").map((action) => <button class={action === "once" ? "primary-button" : "secondary-button"} disabled={Boolean(submitting)} onClick={() => void decide(action)}>{submitting === action ? <LoaderCircle class="spin" size={15} /> : <Check size={15} />} {action === "once" ? "Allow once" : action === "turn" ? "Allow for turn" : "Allow for session"}</button>)}
    </footer>
  </section>;
}

function SessionActions({ session, onClose }: { session: NonNullable<typeof currentSession.value>; onClose: () => void }) {
  const [title, setTitle] = useState(session.title);
  const [effort, setEffort] = useState(runtime.value?.effort ?? "auto");
  const [working, setWorking] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const dialog = useDialogFocus<HTMLElement>(true, onClose);

  const rename = async () => {
    const next = title.trim();
    if (!next || next === session.title) return;
    setWorking("rename");
    try {
      const updated = await api.renameSession(session.id, next, session.revision);
      if (currentSession.value?.id === session.id) currentSession.value = { ...currentSession.value, title: next, revision: updated.revision ?? currentSession.value.revision };
      sessions.value = sessions.value.map((item) => item.id === session.id ? { ...item, title: next, revision: updated.revision ?? item.revision } : item);
      onClose();
    } catch (error) {
      reportOperationError(error, "The session could not be renamed");
    } finally {
      setWorking("");
    }
  };

  const compact = async () => {
    setWorking("compact");
    try {
      await api.compact(session.id);
      await reloadCurrentSession();
      onClose();
    } catch (error) {
      reportOperationError(error, "The session could not be compacted");
    } finally {
      setWorking("");
    }
  };

  const updateEffort = async (value: string) => {
    setEffort(value);
    setWorking("effort");
    try {
      await api.setEffort(value);
      if (runtime.value) runtime.value = { ...runtime.value, effort: value };
    } catch (error) {
      reportOperationError(error, "Reasoning effort could not be changed");
    } finally {
      setWorking("");
    }
  };

  const remove = async () => {
    setWorking("delete");
    try {
      await api.deleteSession(session.id, session.revision);
      await deleteDraftFiles(session.id).catch((error) => {
        reportOperationError(error, "Session deleted, but its local attachment draft needs cleanup");
      });
      sessions.value = sessions.value.filter((item) => item.id !== session.id);
      currentSession.value = null;
      navigate({ name: "home" });
      onClose();
    } catch (error) {
      reportOperationError(error, "The session could not be deleted");
    } finally {
      setWorking("");
    }
  };

  return <section ref={dialog} tabIndex={-1} class="session-action-menu" role="dialog" aria-modal="true" aria-label="Session actions" onKeyDown={(event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    onClose();
  }}>
    <header><strong>Session actions</strong><IconButton label="Close session actions" onClick={onClose}><X size={15} /></IconButton></header>
    <label><span>Title</span><span class="session-rename-row"><input autoFocus value={title} maxLength={200} onInput={(event) => setTitle(event.currentTarget.value)} onKeyDown={(event) => { if (event.key === "Enter") void rename(); }} /><button class="secondary-button" disabled={!title.trim() || title.trim() === session.title || Boolean(working)} onClick={() => void rename()}>{working === "rename" ? <LoaderCircle class="spin" size={14} /> : <Pencil size={14} />} Rename</button></span></label>
    <label><span>Reasoning effort</span><select value={effort} disabled={Boolean(working)} onChange={(event) => void updateEffort(event.currentTarget.value)}><option value="auto">Auto</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option><option value="xhigh">Extra high</option><option value="max">Maximum</option></select></label>
    <button class="menu-action" disabled={Boolean(working)} onClick={() => void compact()}>{working === "compact" ? <LoaderCircle class="spin" size={15} /> : <Sparkles size={15} />} Compact context</button>
    {confirmDelete ? <div class="delete-confirm"><p>Permanently delete this session and its local conversation history?</p><button class="secondary-button" onClick={() => setConfirmDelete(false)}>Cancel</button><button class="danger-button" disabled={Boolean(working)} onClick={() => void remove()}>{working === "delete" ? <LoaderCircle class="spin" size={14} /> : <Trash2 size={14} />} Delete</button></div> : <button class="menu-action danger" disabled={Boolean(working)} onClick={() => setConfirmDelete(true)}><Trash2 size={15} /> Delete session</button>}
  </section>;
}

function Composer({ sessionId, running }: { sessionId: string; running: boolean }) {
  const draftKey = `nanocat.draft.${sessionId}`;
  const [text, setText] = useState(() => sessionStorage.getItem(draftKey) ?? "");
  const [submitting, setSubmitting] = useState(false);
  const [commandResult, setCommandResult] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [filesLoaded, setFilesLoaded] = useState(false);
  const [filesSaving, setFilesSaving] = useState(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => { sessionStorage.setItem(draftKey, text); }, [draftKey, text]);
  useEffect(() => {
    let cancelled = false;
    setFilesLoaded(false);
    void loadDraftFiles(sessionId).then((stored) => {
      if (!cancelled) setFiles(stored);
    }).catch((error) => {
      if (!cancelled) reportOperationError(error, "Attachment drafts could not be restored");
    }).finally(() => {
      if (!cancelled) setFilesLoaded(true);
    });
    return () => { cancelled = true; };
  }, [sessionId]);
  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 220)}px`;
  }, [text]);

  const persistFiles = async (next: File[]) => {
    if (filesSaving) return;
    setFilesSaving(true);
    try {
      await saveDraftFiles(sessionId, next);
      setFiles(next);
    } catch (error) {
      reportOperationError(error, "Attachment drafts could not be saved");
    } finally {
      setFilesSaving(false);
    }
  };

  const submit = async () => {
    const content = text.trim();
    if ((!content && (running || files.length === 0)) || submitting || !filesLoaded || filesSaving) return;
    setSubmitting(true);
    try {
      const trimmedStart = content.trimStart();
      const commandInput = trimmedStart.startsWith("/") && !trimmedStart.startsWith("//");
      if (commandInput) {
        const response = await api.executeCommand(content, sessionId);
        setCommandResult(response.content?.trim() || "Command completed.");
        setText("");
        sessionStorage.removeItem(draftKey);
        await recoverGlobalState();
        if (sessions.value.some((item) => item.id === sessionId)) {
          await reloadCurrentSession();
        } else {
          navigate({ name: "home" }, true);
        }
        return;
      }
      const uploaded = running ? [] : await Promise.all(files.map((file) => api.uploadMedia(file)));
      const attachmentIds = uploaded.map((item) => item.id);
      const submitted = running ? await api.steer(sessionId, content) : await api.submitTurn(sessionId, content, attachmentIds);
      const active = currentSession.value;
      if (active?.id === sessionId) {
        currentSession.value = {
          ...active,
          status: "running",
          events: [...active.events, {
            eventId: `local-${submitted.turnId ?? crypto.randomUUID?.() ?? Date.now()}`,
            sequence: active.events.length ? Math.max(...active.events.map((event) => event.sequence)) + 1 : 1,
            timestamp: new Date().toISOString(),
            sessionId,
            turnId: submitted.turnId,
            type: running ? "turn.steer_queued" : "turn.queued",
            status: "running",
            summary: running ? "Guidance queued" : "Turn queued",
          }],
          turns: [...active.turns, {
            id: `local-${crypto.randomUUID?.() ?? Date.now()}`,
            role: "user",
            content,
            timestamp: new Date().toISOString(),
            status: "queued",
            artifactRefs: uploaded,
          }],
        };
        sessions.value = sessions.value.map((item) => item.id === sessionId ? { ...item, status: "running", preview: content || `${uploaded.length} attachment${uploaded.length === 1 ? "" : "s"}`, updatedAt: new Date().toISOString() } : item);
      }
      setText("");
      if (!running) {
        try {
          await deleteDraftFiles(sessionId);
          setFiles([]);
        } catch (error) {
          reportOperationError(error, "The message was sent, but its attachment draft could not be cleared");
        }
      }
      sessionStorage.removeItem(draftKey);
    } catch (error) {
      reportOperationError(error, running ? "The steer request could not be queued" : "The message could not be sent");
    } finally {
      setSubmitting(false);
    }
  };

  return <div class="composer-wrap">
    {commandResult && <output class="composer-command-result"><span>{commandResult}</span><button aria-label="Dismiss command result" onClick={() => setCommandResult(null)}><X size={13} /></button></output>}
    {files.length > 0 && <div class="attachment-tray">{files.map((file, index) => <span><FileText size={14} /><strong>{file.name}</strong><small>{formatSize(file.size)}</small><button disabled={filesSaving} aria-label={`Remove ${file.name}`} onClick={() => void persistFiles(files.filter((_, itemIndex) => itemIndex !== index))}><X size={13} /></button></span>)}</div>}
    <div class={`composer ${running ? "steer-mode" : ""}`}>
      <textarea ref={textarea} value={text} rows={1} placeholder={running ? "Guide the running agent…" : "Ask NanoCat anything…"} aria-label={running ? "Steer the running agent" : "Message NanoCat"} onInput={(event) => { setText(event.currentTarget.value); setCommandResult(null); }} onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          void submit();
        }
      }} />
      <div class="composer-toolbar">
        <div>
          {!running && <IconButton label="Attach files" disabled={!filesLoaded || filesSaving || submitting} onClick={() => fileInput.current?.click()}><Paperclip size={17} /></IconButton>}
          <input ref={fileInput} hidden disabled={!filesLoaded || filesSaving || submitting} type="file" multiple accept="image/*,.txt,.md,.markdown,.json,.csv,.diff,.patch" onChange={(event) => {
            const selected = Array.from(event.currentTarget.files ?? []);
            event.currentTarget.value = "";
            try {
              validateDraftFiles(selected);
              void persistFiles(selected);
            } catch (error) {
              reportOperationError(error, "Attachments exceed the draft limit");
            }
          }} />
          {!running && <IconButton label="Attach image" disabled={!filesLoaded || filesSaving || submitting} onClick={() => fileInput.current?.click()}><ImagePlus size={17} /></IconButton>}
        </div>
        <span class="composer-hint">{running ? "Queued as steer" : "Enter to send · Shift+Enter for new line"}</span>
        <button class="send-button" aria-label={running ? "Steer agent" : "Send message"} disabled={!filesLoaded || filesSaving || (!text.trim() && (running || files.length === 0)) || submitting} onClick={() => void submit()}>{submitting ? <LoaderCircle class="spin" size={17} /> : <><span>{running ? "Steer" : "Send"}</span><Send size={16} /></>}</button>
      </div>
    </div>
  </div>;
}

function ConversationView() {
  const session = currentSession.value;
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const [following, setFollowing] = useState(true);
  const [newUpdates, setNewUpdates] = useState(0);
  const [visibleCount, setVisibleCount] = useState(250);
  const lastCount = useRef(0);
  const lastEventCount = useRef(0);
  const lastTailId = useRef<string | undefined>();

  useLayoutEffect(() => {
    setVisibleCount(250);
    lastCount.current = session?.turns.length ?? 0;
    lastEventCount.current = session?.events.length ?? 0;
    lastTailId.current = session?.turns.at(-1)?.id;
    setFollowing(true);
    followingRef.current = true;
    setNewUpdates(0);
    const scroll = () => {
      const target = scrollRef.current;
      if (target) target.scrollTop = target.scrollHeight;
    };
    scroll();
    const frame = requestAnimationFrame(scroll);
    return () => cancelAnimationFrame(frame);
  }, [session?.id]);
  useLayoutEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      if (!followingRef.current) return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const target = scrollRef.current;
        if (target) target.scrollTop = target.scrollHeight;
      });
    });
    observer.observe(content);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [session?.id]);
  useEffect(() => {
    const count = session?.turns.length ?? 0;
    if (count > lastCount.current) {
      const added = count - lastCount.current;
      setVisibleCount((value) => Math.min(count, value + added));
      const appended = lastTailId.current !== session?.turns.at(-1)?.id;
      if (appended && following) requestAnimationFrame(() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }));
      else if (appended) setNewUpdates((value) => value + count - lastCount.current);
    }
    lastCount.current = count;
    lastTailId.current = session?.turns.at(-1)?.id;
  }, [session?.turns.length, following]);
  useEffect(() => {
    const count = session?.events.length ?? 0;
    if (count > lastEventCount.current) {
      if (following) requestAnimationFrame(() => scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }));
      else setNewUpdates((value) => value + count - lastEventCount.current);
    }
    lastEventCount.current = count;
  }, [session?.events.length, following]);

  const preservePrepend = async (action: () => void | Promise<void>) => {
    const node = scrollRef.current;
    const height = node?.scrollHeight ?? 0;
    const top = node?.scrollTop ?? 0;
    await action();
    requestAnimationFrame(() => {
      if (node) node.scrollTop = top + node.scrollHeight - height;
    });
  };

  if (!session) return null;
  const visibleTurns = session.turns.slice(-visibleCount);
  const running = ["running", "queued", "cancelling", "waiting_approval", "waiting_user"].includes(session.status);
  const liveTurnId = [...session.events].reverse().find((event) => event.turnId)?.turnId;
  const activeEvents = liveTurnId ? session.events.filter((event) => {
    if (event.turnId !== liveTurnId) return false;
    return !["assistant.final", "turn.completed", "turn.failed", "turn.cancelled", "user.message"].includes(event.type);
  }) : [];
  const stop = () => void api.cancel(session.id).catch((error) => reportOperationError(error, "The turn could not be stopped"));
  return <div ref={scrollRef} class="conversation-scroll" onScroll={(event) => {
    const target = event.currentTarget;
    const next = target.scrollHeight - target.scrollTop - target.clientHeight < 140;
    followingRef.current = next;
    setFollowing(next);
    if (next) setNewUpdates(0);
  }}>
    <div ref={contentRef} class={`conversation-content ${session.turns.length || running ? "has-messages" : ""}`}>
      {session.turns.length > visibleTurns.length && <button class="load-earlier" onClick={() => void preservePrepend(() => setVisibleCount((value) => Math.min(session.turns.length, value + 250)))}><ArrowUp size={15} /> Show {Math.min(250, session.turns.length - visibleTurns.length)} earlier messages</button>}
      {session.turns.length === visibleTurns.length && (session.conversationCursor ?? 0) > 0 && <button class="load-earlier" onClick={() => void preservePrepend(loadEarlierConversation)}><ArrowUp size={15} /> Load earlier history</button>}
      {session.turns.length
        ? visibleTurns.map((turn) => <Turn key={turn.id} turn={turn} />)
        : <EmptyState icon={<MessageSquareText size={27} />} title="Start a new conversation" description="Describe a task, ask a question, or attach context. NanoCat will expose each important action as it works." />}
      {running && <Turn turn={{ id: `active-${liveTurnId ?? session.id}`, role: "assistant", content: "", timestamp: activeEvents[0]?.timestamp ?? "", status: session.status }} active events={activeEvents} onStop={stop} />}
    </div>
    {newUpdates > 0 && <button class="new-updates" onClick={() => { followingRef.current = true; setFollowing(true); scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); setNewUpdates(0); }}><ArrowDown size={15} /> {newUpdates} new updates</button>}
  </div>;
}

function TrajectoryView() {
  const session = currentSession.value;
  if (!session) return null;
  const hasEarlier = (
    (session.trajectoryActivityCursor ?? 0) > (session.trajectoryOldestCursor ?? 0)
    || (session.trajectoryHistoryCursor ?? 0) > 0
  );
  return <div class="trajectory-view"><TrajectoryLedger sessionKey={session.id} events={session.events} hasEarlier={hasEarlier} onLoadEarlier={loadEarlierTrajectory} /></div>;
}

export function Session() {
  if (route.value.name !== "session") return null;
  const { sessionId, view, nodeId, turnId } = route.value;
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsAnchor = useRef<HTMLSpanElement>(null);
  const closeActions = () => {
    setActionsOpen(false);
    requestAnimationFrame(() => actionsAnchor.current?.querySelector("button")?.focus());
  };

  useEffect(() => {
    let disposed = false;
    void loadSession(sessionId).then((detail) => {
      if (disposed || detail?.id !== sessionId) return;
    });
    return () => {
      disposed = true;
      invalidateSessionLoad();
    };
  }, [sessionId]);
  useEffect(() => { void loadModels().catch((error) => reportOperationError(error, "Models could not be loaded")); }, []);
  useEffect(() => setActionsOpen(false), [sessionId]);

  const session = currentSession.value?.id === sessionId ? currentSession.value : null;
  useEffect(() => {
    const turnEvent = turnId ? session?.events.find((event) => event.turnId === turnId) : undefined;
    const selected = nodeId ?? turnEvent?.nodeId ?? turnEvent?.eventId;
    const exists = selected && session?.events.some((event) => event.nodeId === selected || event.eventId === selected);
    const next = exists ? selected : null;
    if (selectedNodeId.value !== next) setSelectedNode(next, false);
  }, [sessionId, nodeId, turnId, session?.events.length]);
  const running = session ? ["running", "queued", "cancelling", "waiting_approval", "waiting_user"].includes(session.status) : false;
  const approval = approvals.value.find((item) => item.sessionId === sessionId);
  const onTabKeyDown = (event: KeyboardEvent) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const nextView = view === "conversation" ? "trajectory" : "conversation";
    navigate({ name: "session", sessionId, view: nextView }, false, false);
    requestAnimationFrame(() => document.getElementById(`${nextView}-tab`)?.focus());
  };

  return <div class={`session-page ${inspectorOpen.value ? "with-inspector" : ""}`}>
    <PageHeader title={session?.title || "Session"} eyebrow={session?.model ?? "Agent workspace"} actions={<>
      <label class="header-model-select">
        <span class="sr-only">Agent model</span>
        <select value={session?.model ?? runtime.value?.model ?? ""} onChange={(event) => {
          const model = event.currentTarget.value;
          void api.selectModel(model).then(() => {
            if (runtime.value) runtime.value = { ...runtime.value, model };
            if (currentSession.value) currentSession.value = { ...currentSession.value, model };
            sessions.value = sessions.value.map((item) => item.id === sessionId ? { ...item, model } : item);
          }).catch((error) => reportOperationError(error, "The model could not be changed"));
        }}>
          {!models.value.some((item) => item.id === (session?.model ?? runtime.value?.model)) && <option value={session?.model ?? runtime.value?.model ?? ""}>{session?.model ?? runtime.value?.model ?? "Select model"}</option>}
          {[...new Set(models.value.map((model) => model.providerLabel || model.provider || "Other"))].map((provider) => <optgroup key={provider} label={provider}>{models.value.filter((model) => (model.providerLabel || model.provider || "Other") === provider).map((model) => <option key={model.id} value={model.id}>{model.name ?? model.id}</option>)}</optgroup>)}
        </select>
      </label>
      <span class={`header-connection ${connection.value}`}><span />{connection.value}</span>
      <span ref={actionsAnchor} class="session-actions-anchor">
        <IconButton label="Session actions" onClick={() => actionsOpen ? closeActions() : setActionsOpen(true)}><MoreHorizontal size={18} /></IconButton>
        {session && actionsOpen && <SessionActions session={session} onClose={closeActions} />}
      </span>
    </>}>
      {session && <div class="session-tabs" role="tablist">
        <a id="conversation-tab" data-router role="tab" aria-controls="session-panel" aria-selected={view === "conversation"} tabIndex={view === "conversation" ? 0 : -1} onKeyDown={onTabKeyDown} href={hrefFor({ name: "session", sessionId, view: "conversation" })}><MessageSquareText size={15} /> Conversation</a>
        <a id="trajectory-tab" data-router role="tab" aria-controls="session-panel" aria-selected={view === "trajectory"} tabIndex={view === "trajectory" ? 0 : -1} onKeyDown={onTabKeyDown} href={hrefFor({ name: "session", sessionId, view: "trajectory" })}><Activity size={15} /> Trajectory</a>
      </div>}
    </PageHeader>
    <main id="session-panel" class="session-main" role="tabpanel" aria-labelledby={`${view}-tab`}>
      {loadingSession.value ? <div class="session-loading"><Skeleton lines={5} /><Skeleton lines={4} /></div> : session ? view === "conversation" ? <ConversationView /> : <TrajectoryView /> : <EmptyState icon={<AlertCircle size={27} />} title="Session unavailable" description="This session may have been removed or could not be loaded." />}
      {session && <div class="session-input-layer">
        {approval && <ApprovalDock approval={approval} onDone={() => { void Promise.all([reloadCurrentSession(), refreshApprovals(sessionId)]); }} />}
        {view === "conversation" && <Composer key={sessionId} sessionId={sessionId} running={running} />}
      </div>}
    </main>
    <Inspector />
  </div>;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
}
