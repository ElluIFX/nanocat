import { Bot, Eye, EyeOff, KeyRound, LoaderCircle, ShieldAlert } from "lucide-preact";
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "../api/client";

export function Login() {
  const [checking, setChecking] = useState(true);
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const retrySeconds = Math.max(0, Math.ceil((retryAt - now) / 1000));

  useEffect(() => {
    let active = true;
    void api.authStatus().then((status) => {
      if (!active) return;
      if (!status.protected || status.authenticated) {
        window.location.assign("/");
        return;
      }
      setChecking(false);
    }).catch(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!retrySeconds) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [retrySeconds]);

  const submit = async (event: Event) => {
    event.preventDefault();
    if (!password || retrySeconds > 0) return;
    setSubmitting(true);
    setError("");
    try {
      const result = await api.login(password);
      if (result.csrfToken) {
        let meta = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]');
        if (!meta) {
          meta = document.createElement("meta");
          meta.name = "csrf-token";
          document.head.append(meta);
        }
        meta.content = result.csrfToken;
      }
      window.location.assign("/");
    } catch (cause) {
      if (cause instanceof ApiError && cause.retryAfter) {
        setRetryAt(Date.now() + cause.retryAfter * 1000);
        setNow(Date.now());
      }
      setError("The password could not be verified. Check it and try again.");
      setPassword("");
    } finally {
      setSubmitting(false);
    }
  };

  if (checking) return <main class="login-page"><section class="login-card login-checking" aria-label="Checking authentication"><LoaderCircle class="spin" size={24} /><span>Checking workspace access…</span></section></main>;

  return <main class="login-page">
    <section class="login-card">
      <div class="login-brand"><span><Bot size={22} /></span><strong>NanoCat</strong></div>
      <div class="login-copy"><p class="eyebrow">Private workspace</p><h1>Welcome back</h1><p>Enter the password configured for this NanoCat instance.</p></div>
      <form onSubmit={(event) => void submit(event)}>
        <label htmlFor="password">Password</label>
        <div class="password-field">
          <KeyRound size={17} />
          <input id="password" name="password" autoFocus autoComplete="current-password" type={visible ? "text" : "password"} value={password} onInput={(event) => setPassword(event.currentTarget.value)} disabled={submitting || retrySeconds > 0} />
          <button type="button" aria-label={visible ? "Hide password" : "Show password"} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button>
        </div>
        {error && <div class="login-error" role="alert"><ShieldAlert size={16} /><span>{error}{retrySeconds > 0 && <> Try again in <strong>{retrySeconds}s</strong>.</>}</span></div>}
        <button class="primary-button login-submit" disabled={!password || submitting || retrySeconds > 0}>
          {submitting ? <><LoaderCircle class="spin" size={16} /> Verifying</> : retrySeconds > 0 ? `Try again in ${retrySeconds}s` : "Continue"}
        </button>
      </form>
      <footer>Protected by server-side rate limiting and an HttpOnly session.</footer>
    </section>
  </main>;
}
