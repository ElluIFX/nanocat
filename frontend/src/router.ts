import { signal } from "@preact/signals";

export type Route =
  | { name: "home" }
  | { name: "login" }
  | { name: "session"; sessionId: string; view: "conversation" | "trajectory"; nodeId?: string; turnId?: string }
  | { name: "settings"; section?: string }
  | { name: "diagnostics" };

function parseLocation(): Route {
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "login") return { name: "login" };
  if (parts[0] === "settings") return { name: "settings", section: parts[1] };
  if (parts[0] === "diagnostics") return { name: "diagnostics" };
  if (parts[0] === "sessions" && parts[1]) {
    const query = new URLSearchParams(window.location.search);
    return {
      name: "session",
      sessionId: decodeURIComponent(parts[1]),
      view: parts[2] === "trajectory" ? "trajectory" : "conversation",
      nodeId: query.get("node") ?? undefined,
      turnId: query.get("turn") ?? undefined,
    };
  }
  return { name: "home" };
}

export const route = signal<Route>(parseLocation());

export function hrefFor(next: Route): string {
  if (next.name === "home") return "/";
  if (next.name === "login") return "/login";
  if (next.name === "settings") return `/settings/${next.section ?? "general"}`;
  if (next.name === "diagnostics") return "/diagnostics";
  const base = `/sessions/${encodeURIComponent(next.sessionId)}/${next.view}`;
  if (next.nodeId) return `${base}?node=${encodeURIComponent(next.nodeId)}`;
  return next.turnId ? `${base}?turn=${encodeURIComponent(next.turnId)}` : base;
}

export function navigate(next: Route, replace = false, scroll = true): void {
  const href = hrefFor(next);
  if (replace) window.history.replaceState(null, "", href);
  else window.history.pushState(null, "", href);
  route.value = next;
  if (scroll) window.scrollTo({ top: 0, behavior: "auto" });
}

export function installRouter(): () => void {
  const onPop = () => { route.value = parseLocation(); };
  const onClick = (event: MouseEvent) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = event.target as Element | null;
    const anchor = target?.closest<HTMLAnchorElement>("a[data-router]");
    if (!anchor || anchor.target || anchor.origin !== window.location.origin) return;
    event.preventDefault();
    window.history.pushState(null, "", anchor.href);
    route.value = parseLocation();
    window.scrollTo({ top: 0, behavior: "auto" });
  };
  window.addEventListener("popstate", onPop);
  document.addEventListener("click", onClick);
  return () => {
    window.removeEventListener("popstate", onPop);
    document.removeEventListener("click", onClick);
  };
}
