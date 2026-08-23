import { AlertTriangle, Command, RefreshCw } from "lucide-preact";
import { useEffect } from "preact/hooks";
import { AppShell } from "./components/AppShell";
import { EmptyState, Skeleton } from "./components/Primitives";
import { installRouter, route } from "./router";
import { appError, applyTheme, bootstrap, booting, commandPaletteOpen, createSessionAndOpen, navigatorOpen, setSelectedNode, theme } from "./store";
import { Diagnostics } from "./views/Diagnostics";
import { Home } from "./views/Home";
import { Login } from "./views/Login";
import { Session } from "./views/Session";
import { Settings } from "./views/Settings";

function CurrentPage() {
  if (route.value.name === "home") return <Home />;
  if (route.value.name === "session") return <Session />;
  if (route.value.name === "settings") return <Settings />;
  if (route.value.name === "diagnostics") return <Diagnostics />;
  return null;
}

export function App() {
  useEffect(() => {
    applyTheme(theme.value);
    const uninstall = installRouter();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        commandPaletteOpen.value = false;
        navigatorOpen.value = false;
        setSelectedNode(null);
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        commandPaletteOpen.value = !commandPaletteOpen.value;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") {
        if (route.value.name === "login") return;
        const target = event.target as HTMLElement | null;
        if (event.repeat || target?.closest("input, textarea, select, [contenteditable='true']")) return;
        event.preventDefault();
        void createSessionAndOpen();
      }
    };
    window.addEventListener("keydown", onKey);
    if (route.value.name !== "login") void bootstrap();
    return () => {
      uninstall();
      window.removeEventListener("keydown", onKey);
    };
  }, []);

  if (route.value.name === "login") return <Login />;

  return <AppShell>
    {booting.value
      ? <main class="boot-screen"><div class="boot-mark"><Command size={22} /></div><Skeleton lines={4} /></main>
      : appError.value
        ? <EmptyState icon={<AlertTriangle size={28} />} title="NanoCat is unavailable" description={appError.value} action={<button class="primary-button" onClick={() => void bootstrap()}><RefreshCw size={16} /> Retry connection</button>} />
        : <CurrentPage />}
  </AppShell>;
}
