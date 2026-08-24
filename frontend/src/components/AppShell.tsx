import { useComputed } from "@preact/signals";
import {
  Bot,
  Command,
  Menu,
  MessageSquareText,
  Moon,
  PanelLeftClose,
  Pencil,
  Plus,
  Search,
  Settings,
  Sun,
  TerminalSquare,
  Trash2,
  LoaderCircle,
  X,
} from "lucide-preact";
import type { ComponentChildren } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { api } from "../api/client";
import { deleteDraftFiles } from "../api/drafts";
import { connectGlobalEvents } from "../api/sse";
import { hrefFor, navigate, route, type Route } from "../router";
import {
  appendGlobalEvent,
  applyTheme,
  booting,
  clearOperationError,
  commandPaletteOpen,
  connection,
  createSessionAndOpen,
  creatingSession,
  currentSession,
  navigatorOpen,
  operationError,
  recoverGlobalState,
  reportOperationError,
  runtime,
  sessions,
  theme,
} from "../store";
import type { CommandInfo, SessionSummary } from "../types";
import { IconButton, StatusBadge } from "./Primitives";
import { useDialogFocus } from "./focus";

let navigatorReturnFocus: HTMLElement | null = null;

function openNavigator(trigger: HTMLElement): void {
  navigatorReturnFocus = trigger;
  const drawer = window.matchMedia("(max-width: 1279px)").matches;
  navigatorOpen.value = drawer ? true : !navigatorOpen.value;
  if (!drawer) localStorage.setItem("nanocat.navigator.open", String(navigatorOpen.value));
}

function Link({ to, label, children, class: className = "", current = false }: {
  to: Route;
  label: string;
  children: ComponentChildren;
  class?: string;
  current?: boolean;
}) {
  return <a data-router href={hrefFor(to)} aria-label={label} aria-current={current ? "page" : undefined} title={label} class={`${className} ${current ? "active" : ""}`.trim()}>{children}</a>;
}

function SessionItem({ session }: { session: SessionSummary }) {
  const active = route.value.name === "session" && route.value.sessionId === session.id;
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(session.title);
  const [working, setWorking] = useState<"rename" | "delete" | null>(null);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const deleteTimer = useRef<number | undefined>(undefined);
  const itemRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!editing) setTitle(session.title);
  }, [editing, session.title]);
  useEffect(() => () => {
    if (deleteTimer.current !== undefined) window.clearTimeout(deleteTimer.current);
  }, []);
  useEffect(() => {
    if (!deleteArmed) return;
    const dismiss = (event: PointerEvent) => {
      if (!itemRef.current?.contains(event.target as Node)) setDeleteArmed(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDeleteArmed(false);
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [deleteArmed]);

  const rename = async () => {
    const next = title.trim();
    if (working || !editing) return;
    if (!next || next === session.title) {
      setTitle(session.title);
      setEditing(false);
      return;
    }
    setWorking("rename");
    try {
      const updated = await api.renameSession(session.id, next, session.revision);
      sessions.value = sessions.value.map((item) => item.id === session.id ? { ...item, title: next, revision: updated.revision ?? item.revision } : item);
      if (currentSession.value?.id === session.id) currentSession.value = { ...currentSession.value, title: next, revision: updated.revision ?? currentSession.value.revision };
      setEditing(false);
    } catch (error) {
      reportOperationError(error, "The session could not be renamed");
    } finally {
      setWorking(null);
    }
  };

  const remove = async () => {
    setWorking("delete");
    try {
      await api.deleteSession(session.id, session.revision);
      await deleteDraftFiles(session.id).catch((error) => reportOperationError(error, "Session deleted, but its attachment draft needs cleanup"));
      sessions.value = sessions.value.filter((item) => item.id !== session.id);
      if (currentSession.value?.id === session.id) currentSession.value = null;
      if (active) navigate({ name: "home" });
    } catch (error) {
      setDeleteArmed(false);
      reportOperationError(error, "The session could not be deleted");
    } finally {
      setWorking(null);
    }
  };

  const requestDelete = () => {
    if (working) return;
    if (deleteArmed) {
      if (deleteTimer.current !== undefined) window.clearTimeout(deleteTimer.current);
      void remove();
      return;
    }
    setDeleteArmed(true);
    deleteTimer.current = window.setTimeout(() => setDeleteArmed(false), 3_000);
  };

  return <div ref={itemRef} class={`session-item ${active ? "active" : ""} ${deleteArmed ? "delete-armed" : ""}`}>
    <div class="session-item-main">
      {editing ? <input
        autoFocus
        class="session-item-rename"
        value={title}
        maxLength={200}
        aria-label={`Rename ${session.title || "session"}`}
        onFocus={(event) => event.currentTarget.select()}
        onInput={(event) => setTitle(event.currentTarget.value)}
        onBlur={() => void rename()}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
          if (event.key === "Escape") {
            setTitle(session.title);
            setEditing(false);
          }
        }}
      /> : <a
        data-router
        href={hrefFor({ name: "session", sessionId: session.id, view: "conversation" })}
        class="session-item-link"
        aria-current={active ? "page" : undefined}
        onClick={() => { navigatorOpen.value = false; }}
      >
        <span class="session-item-title">{session.title || "Untitled session"}</span>
        <span class="session-item-meta"><StatusBadge status={session.status} /><time dateTime={session.updatedAt}>{relativeTime(session.updatedAt)}</time></span>
        {session.preview && <span class="session-preview">{session.preview}</span>}
      </a>}
    </div>
    <div class="session-item-actions">
      <button aria-label={`Rename ${session.title || "session"}`} title="Rename session" disabled={Boolean(working)} onClick={() => { setDeleteArmed(false); setEditing(true); }}>{working === "rename" ? <LoaderCircle class="spin" size={13} /> : <Pencil size={13} />}</button>
      <button class={deleteArmed ? "armed" : ""} aria-label={deleteArmed ? `Confirm deletion of ${session.title || "session"}` : `Delete ${session.title || "session"}`} title={deleteArmed ? "Click again to delete" : "Delete session"} disabled={Boolean(working)} onClick={requestDelete}>{working === "delete" ? <LoaderCircle class="spin" size={13} /> : <Trash2 size={13} />}</button>
    </div>
    {deleteArmed && <span class="session-delete-prompt" role="status">Click again</span>}
  </div>;
}

function relativeTime(value: string): string {
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta)) return "";
  const minutes = Math.max(0, Math.round(delta / 60_000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

function Navigator() {
  const [query, setQuery] = useState("");
  const [drawerMode, setDrawerMode] = useState(() => window.matchMedia("(max-width: 1279px)").matches);
  const filtered = useComputed(() => {
    const needle = query.trim().toLowerCase();
    return sessions.value.filter((session) => !needle || `${session.title} ${session.preview ?? ""}`.toLowerCase().includes(needle));
  });

  const create = async () => {
    await createSessionAndOpen();
  };
  useEffect(() => {
    const media = window.matchMedia("(max-width: 1279px)");
    const update = () => setDrawerMode(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const drawerOpen = navigatorOpen.value && drawerMode;
  const drawerHidden = drawerMode && !navigatorOpen.value;
  const drawer = useDialogFocus<HTMLElement>(
    drawerOpen,
    () => { navigatorOpen.value = false; },
    () => navigatorReturnFocus,
  );

  return <aside id="session-navigator" ref={drawer} tabIndex={drawerOpen ? -1 : undefined} inert={drawerHidden ? true : undefined} class={`navigator ${navigatorOpen.value ? "open" : ""}`} aria-label="Sessions" aria-hidden={drawerHidden ? "true" : undefined} role={drawerOpen ? "dialog" : undefined} aria-modal={drawerOpen ? "true" : undefined}>
    <header class="navigator-header">
      <div class="wordmark"><span class="wordmark-mark"><Bot size={18} /></span><strong>NanoCat</strong></div>
      <IconButton class="mobile-only" label="Close sessions" onClick={() => { navigatorOpen.value = false; }}><X size={18} /></IconButton>
    </header>
    <button class="new-session" disabled={creatingSession.value} onClick={() => void create()}><Plus size={16} /> {creatingSession.value ? "Creating…" : "New session"} <kbd>⌘N</kbd></button>
    <label class="search-box">
      <Search size={15} aria-hidden="true" />
      <span class="sr-only">Search sessions</span>
      <input value={query} onInput={(event) => setQuery(event.currentTarget.value)} placeholder="Search sessions" />
    </label>
    <nav class="session-list" aria-label="Recent sessions">
      <p class="section-label">Recent</p>
      {filtered.value.length
        ? filtered.value.map((session) => <SessionItem key={session.id} session={session} />)
        : <p class="muted navigator-empty">{sessions.value.length && query.trim() ? "No matching sessions" : "No sessions yet"}</p>}
    </nav>
    <footer class="navigator-footer">
      <span class={`connection-dot ${connection.value}`} aria-hidden="true" />
      <span>{connection.value}</span>
      {runtime.value?.version && <span class="version">v{runtime.value.version}</span>}
    </footer>
  </aside>;
}

function GlobalRail() {
  const sessionRoute = route.value.name === "session" ? route.value : null;
  return <nav class="global-rail" aria-label="Primary navigation">
    <Link to={{ name: "home" }} label="Home" current={route.value.name === "home"}><Bot size={19} /></Link>
    <IconButton label={navigatorOpen.value ? "Hide sessions" : "Show sessions"} aria-expanded={navigatorOpen.value} aria-controls="session-navigator" class={sessionRoute?.view === "conversation" ? "active" : ""} aria-current={sessionRoute?.view === "conversation" ? "page" : undefined} onClick={(event) => openNavigator(event.currentTarget)}><MessageSquareText size={19} /></IconButton>
    <span class="rail-spacer" />
    <button aria-label="Command palette" title="Command palette" onClick={() => { commandPaletteOpen.value = true; }}><Command size={19} /></button>
    <Link to={{ name: "diagnostics" }} label="Diagnostics" current={route.value.name === "diagnostics"}><TerminalSquare size={19} /></Link>
    <Link to={{ name: "settings", section: "general" }} label="Settings" current={route.value.name === "settings"}><Settings size={19} /></Link>
  </nav>;
}

function CommandPalette() {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [commands, setCommands] = useState<CommandInfo[]>([]);
  const [working, setWorking] = useState(false);
  const [result, setResult] = useState("");
  const dialog = useDialogFocus<HTMLElement>(true, () => { commandPaletteOpen.value = false; });
  const sessionId = route.value.name === "session" ? route.value.sessionId : undefined;
  const actions = useMemo(() => [
    { title: "Open Home", keywords: "home", kind: "navigate" as const, icon: <Bot size={16} />, run: () => navigate({ name: "home" }) },
    { title: "Open diagnostics", keywords: "logs runtime", kind: "navigate" as const, icon: <TerminalSquare size={16} />, run: () => navigate({ name: "diagnostics" }) },
    { title: "Open settings", keywords: "config preferences", kind: "navigate" as const, icon: <Settings size={16} />, run: () => navigate({ name: "settings", section: "general" }) },
    ...sessions.value.slice(0, 30).map((session) => ({
      title: session.title,
      keywords: `session ${session.preview ?? ""}`,
      kind: "navigate" as const,
      icon: <MessageSquareText size={16} />,
      run: () => navigate({ name: "session", sessionId: session.id, view: "conversation" }),
    })),
    ...commands.filter((command) => command.enabled && !["restart", "stop"].includes(command.name)).map((command) => ({
      title: `/${command.name}`,
      keywords: `command ${command.group} ${command.summary} ${command.aliases.join(" ")} ${command.usage}`,
      kind: "command" as const,
      icon: <Command size={16} />,
      run: () => api.executeCommand(`/${command.name}`, sessionId),
    })),
  ], [commands, sessionId, sessions.value]);
  const filtered = actions.filter((item) => `${item.title} ${item.keywords}`.toLowerCase().includes(query.toLowerCase()));
  const rawCommand = query.trim().startsWith("/")
    ? { title: `Run ${query.trim()}`, keywords: "direct command", kind: "command" as const, icon: <Command size={16} />, run: () => api.executeCommand(query.trim(), sessionId) }
    : null;
  const visible = [...(rawCommand ? [rawCommand] : []), ...filtered].slice(0, 12);
  useEffect(() => {
    void api.commands().then(setCommands).catch((error) => reportOperationError(error, "Commands could not be loaded"));
  }, []);
  useEffect(() => setActive(0), [query]);
  useEffect(() => setActive((value) => visible.length ? Math.min(value, visible.length - 1) : 0), [visible.length]);
  useEffect(() => {
    document.getElementById(`command-option-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const run = async (index: number) => {
    const item = visible[index];
    if (!item || working) return;
    if (item.kind === "navigate") {
      item.run();
      commandPaletteOpen.value = false;
      return;
    }
    setWorking(true);
    setResult("");
    try {
      const response = await item.run();
      setResult(response.content?.trim() || "Command completed.");
      await recoverGlobalState();
    } catch (error) {
      reportOperationError(error, "The command could not be executed");
    } finally {
      setWorking(false);
    }
  };

  return <div class="palette-backdrop" role="presentation" onMouseDown={(event) => {
    if (event.currentTarget === event.target) commandPaletteOpen.value = false;
  }}>
    <section ref={dialog} class="command-palette" role="dialog" aria-modal="true" aria-label="Command palette" aria-busy={working}>
      <label><Search size={18} /><input autoFocus value={query} role="combobox" aria-expanded="true" aria-controls="command-results" aria-activedescendant={visible[active] ? `command-option-${active}` : undefined} onInput={(event) => setQuery(event.currentTarget.value)} onKeyDown={(event) => {
        if (event.key === "ArrowDown") { event.preventDefault(); setActive((value) => visible.length ? Math.min(value + 1, visible.length - 1) : 0); }
        if (event.key === "ArrowUp") { event.preventDefault(); setActive((value) => Math.max(value - 1, 0)); }
        if (event.key === "Enter") { event.preventDefault(); void run(active); }
        if (event.key === "Escape") commandPaletteOpen.value = false;
      }} placeholder="Search sessions, actions, and commands" /></label>
      <div id="command-results" role="listbox">
        {visible.length ? visible.map((item, index) => <button id={`command-option-${index}`} key={`${item.title}-${item.keywords}`} role="option" tabIndex={-1} aria-selected={active === index} disabled={working} onMouseEnter={() => setActive(index)} onClick={() => void run(index)}>{item.icon}<span>{item.title}</span>{item.kind === "command" && <small>Run</small>}</button>) : <p class="muted">No matches</p>}
      </div>
      {result && <output class="palette-result">{result}</output>}
      <footer>{working ? "Executing command…" : <><kbd>↑↓</kbd> Navigate <kbd>↵</kbd> Open or run <kbd>Esc</kbd> Close</>}</footer>
    </section>
  </div>;
}

export function PageHeader({ title, eyebrow, actions, children }: {
  title: string;
  eyebrow?: string;
  actions?: ComponentChildren;
  children?: ComponentChildren;
}) {
  return <header class="page-header">
    <IconButton class="compact-nav-toggle" label="Open sessions" aria-expanded={navigatorOpen.value} aria-controls="session-navigator" onClick={(event) => openNavigator(event.currentTarget)}><Menu size={19} /></IconButton>
    <div class="page-heading">{eyebrow && <span>{eyebrow}</span>}<h1>{title}</h1>{children}</div>
    <div class="page-actions">{actions}</div>
  </header>;
}

export function AppShell({ children }: { children: ComponentChildren }) {
  useEffect(() => {
    if (booting.value) return;
    return connectGlobalEvents({
      onEvent: appendGlobalEvent,
      onReset: async () => {
        try {
          await recoverGlobalState();
        } catch (error) {
          reportOperationError(error, "Workspace state could not be recovered");
          throw error;
        }
      },
      onState: (state) => { connection.value = state; },
    });
  }, [booting.value]);

  return <div class={`app-shell ${navigatorOpen.value ? "" : "navigator-collapsed"}`}>
    <GlobalRail />
    <Navigator />
    {navigatorOpen.value && <button class="drawer-scrim" aria-label="Close sessions" onClick={() => { navigatorOpen.value = false; }} />}
    <div class="main-surface">{children}</div>
    <nav class="mobile-tabbar" aria-label="Mobile navigation">
      <Link to={{ name: "home" }} label="Home" current={route.value.name === "home"}><Bot size={19} /><span>Home</span></Link>
      <button class={route.value.name === "session" && route.value.view === "conversation" ? "active" : ""} aria-current={route.value.name === "session" && route.value.view === "conversation" ? "page" : undefined} aria-expanded={navigatorOpen.value} aria-controls="session-navigator" onClick={(event) => openNavigator(event.currentTarget)}><MessageSquareText size={19} /><span>Sessions</span></button>
      <Link to={{ name: "settings", section: "general" }} label="Settings" current={route.value.name === "settings"}><Settings size={19} /><span>Settings</span></Link>
    </nav>
    {commandPaletteOpen.value && <CommandPalette />}
    {operationError.value && <div class="operation-toast" role="alert"><span>{operationError.value}</span><button aria-label="Dismiss error" onClick={clearOperationError}><X size={15} /></button></div>}
    <button class="theme-quick-toggle" aria-label="Toggle theme" onClick={() => applyTheme(theme.value === "dark" ? "light" : "dark")}>
      {theme.value === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  </div>;
}

export function PaneToggle({ onClick }: { onClick: () => void }) {
  return <IconButton label="Toggle panel" onClick={onClick}><PanelLeftClose size={18} /></IconButton>;
}
