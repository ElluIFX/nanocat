import type { TimelineEvent } from "../types";

interface StreamOptions {
  onEvent: (event: TimelineEvent) => void;
  onState: (state: "connecting" | "online" | "reconnecting" | "offline") => void;
  onReset: (reason?: string) => Promise<void> | void;
}

interface ParsedTimelineEvent extends TimelineEvent {
  resetReason?: string;
}

export function parseEvent(raw: string): ParsedTimelineEvent | undefined {
  try {
    const event = JSON.parse(raw) as Record<string, unknown>;
    const payload = event.payload && typeof event.payload === "object"
      ? event.payload as Record<string, unknown>
      : {};
    const metadata = payload.metadata && typeof payload.metadata === "object" ? payload.metadata as Record<string, unknown> : {};
    const toolEvent = metadata._tool_event && typeof metadata._tool_event === "object"
      ? metadata._tool_event as Record<string, unknown>
      : {};
    const toolCalls = Array.isArray(toolEvent.calls)
      ? toolEvent.calls.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object").map((call) => ({
        id: typeof call.id === "string" ? call.id : undefined,
        name: typeof call.name === "string" ? call.name : "tool",
        status: typeof call.status === "string" ? call.status : "updated",
      }))
      : [];
    const toolSummary = toolCalls.length
      ? `Tools: ${toolCalls.map((call) => `${call.name} (${call.status})`).join(", ")}`
      : undefined;
    const type = typeof event.type === "string" ? event.type : "unknown";
    const explicitStatus = typeof payload.status === "string" ? payload.status : typeof event.status === "string" ? event.status : undefined;
    const inferredStatus = type === "approval.pending" ? "waiting_approval"
      : type === "turn.cancelling" ? "cancelling"
      : type === "assistant.final" || type === "turn.completed" ? "completed"
        : type === "turn.failed" ? "failed"
          : type === "turn.cancelled" ? "cancelled"
            : ["turn.queued", "turn.steer_queued", "assistant.progress", "tool.event"].includes(type) ? "running"
              : undefined;
    return {
      eventId: typeof event.eventId === "string" ? event.eventId : "",
      sequence: typeof event.sequence === "number" ? event.sequence : 0,
      timestamp: typeof event.timestamp === "string" ? event.timestamp : "",
      type,
      sessionId: typeof event.sessionId === "string" ? event.sessionId : undefined,
      turnId: typeof event.turnId === "string" ? event.turnId : undefined,
      requestId: typeof event.requestId === "string" ? event.requestId : undefined,
      nodeId: typeof payload.nodeId === "string" ? payload.nodeId : typeof event.nodeId === "string" ? event.nodeId : undefined,
      parentId: typeof payload.parentId === "string" ? payload.parentId : typeof event.parentId === "string" ? event.parentId : undefined,
      source: typeof payload.source === "string" ? payload.source : typeof event.source === "string" ? event.source : undefined,
      phase: typeof payload.phase === "string"
        ? payload.phase
        : typeof toolEvent.phase === "string"
          ? toolEvent.phase
          : typeof event.phase === "string" ? event.phase : undefined,
      status: (explicitStatus ?? inferredStatus) as TimelineEvent["status"],
      summary: typeof payload.summary === "string" ? payload.summary : toolSummary ?? (typeof payload.content === "string" ? payload.content.slice(0, 160) : undefined),
      content: typeof payload.content === "string" ? payload.content : undefined,
      redactedInput: payload.redactedInput ?? (toolCalls.length ? { calls: toolCalls } : undefined),
      redactedOutput: payload.redactedOutput,
      startedAt: typeof payload.startedAt === "string" ? payload.startedAt : undefined,
      endedAt: typeof payload.endedAt === "string" ? payload.endedAt : undefined,
      durationMs: typeof payload.durationMs === "number" ? payload.durationMs : undefined,
      model: typeof payload.model === "string" ? payload.model : undefined,
      artifactRefs: payload.artifactRefs as TimelineEvent["artifactRefs"],
      resetReason: typeof payload.reason === "string" ? payload.reason : undefined,
    };
  } catch {
    return undefined;
  }
}

function retryAfterMs(response: Response): number | undefined {
  const value = response.headers.get("Retry-After");
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds > 0) return seconds * 1000;
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : undefined;
}

function connectEvents(path: string, options: StreamOptions): () => void {
  let closed = false;
  let suspended = false;
  let opening = false;
  let retry = 750;
  let lastEventId = "";
  let controller: AbortController | undefined;
  let retryTimer: number | undefined;
  let resolveRetry: (() => void) | undefined;
  let lastCapacityRecoveryAt = 0;
  let recoveryTimer: number | undefined;
  let recoveryDirty = false;
  let recoveryGeneration = 0;
  let recoveryAttempts = 0;
  let recoveryReason: string | undefined;
  let recoveryPromise: Promise<boolean> | undefined;

  const wait = (ms: number) => new Promise<void>((resolve) => {
    resolveRetry = resolve;
    retryTimer = window.setTimeout(() => {
      retryTimer = undefined;
      resolveRetry = undefined;
      resolve();
    }, ms);
  });
  const attemptRecovery = (): Promise<boolean> => {
    if (recoveryPromise) return recoveryPromise;
    const generation = recoveryGeneration;
    const reason = recoveryReason;
    recoveryPromise = Promise.resolve(options.onReset(reason))
      .then(() => {
        if (reason === "subscriber_overflow") lastCapacityRecoveryAt = Date.now();
        if (recoveryDirty && generation === recoveryGeneration) {
          recoveryDirty = false;
          recoveryReason = undefined;
        }
        const recovered = !recoveryDirty;
        if (recovered) recoveryAttempts = 0;
        return recovered;
      })
      .catch(() => {
        options.onState("reconnecting");
        return false;
      })
      .finally(() => {
        recoveryPromise = undefined;
        if (recoveryDirty && !closed && recoveryTimer === undefined) {
          const remaining = recoveryReason === "subscriber_overflow"
            ? Math.max(0, 5000 - (Date.now() - lastCapacityRecoveryAt))
            : Math.min(15_000, 1000 * (2 ** Math.min(recoveryAttempts, 4)));
          scheduleRecovery(remaining);
        }
      });
    return recoveryPromise;
  };
  const scheduleRecovery = (delay: number) => {
    if (closed || recoveryTimer !== undefined) return;
    const retryDelay = recoveryAttempts >= 3 ? Math.max(delay, 15_000) : delay;
    recoveryTimer = window.setTimeout(() => {
      recoveryTimer = undefined;
      if (!recoveryDirty || closed) return;
      recoveryAttempts += 1;
      void attemptRecovery().then((recovered) => {
        if (!recovered) {
          scheduleRecovery(
            Math.min(15_000, 1000 * (2 ** Math.min(recoveryAttempts, 4))),
          );
        }
      });
    }, retryDelay);
  };
  const recoverReset = async (reason?: string) => {
    if (recoveryTimer !== undefined) window.clearTimeout(recoveryTimer);
    recoveryTimer = undefined;
    recoveryDirty = true;
    recoveryReason = reason;
    recoveryGeneration += 1;
    recoveryAttempts = 0;
    const remaining = reason === "subscriber_overflow"
      ? 5000 - (Date.now() - lastCapacityRecoveryAt)
      : 0;
    if (remaining <= 0) {
      if (!await attemptRecovery()) scheduleRecovery(1000);
      return;
    }
    scheduleRecovery(remaining);
  };
  const open = async () => {
    if (closed || suspended || opening) return;
    opening = true;
    try {
      while (!closed && !suspended) {
        controller = new AbortController();
        options.onState(lastEventId ? "reconnecting" : "connecting");
        let serverDelay: number | undefined;
        try {
          if (recoveryDirty && (recoveryTimer !== undefined || !await attemptRecovery())) {
            throw new Error("SSE state recovery failed");
          }
          const headers = new Headers({ Accept: "text/event-stream" });
          if (lastEventId) headers.set("Last-Event-ID", lastEventId);
          const response = await fetch(path, { headers, credentials: "same-origin", signal: controller.signal });
          if (response.status === 401) {
            window.location.assign("/login");
            return;
          }
          if (!response.ok || !response.body) {
            serverDelay = retryAfterMs(response);
            throw new Error(`SSE connection failed (${response.status})`);
          }
          options.onState("online");
          const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
          let buffer = "";
          let reset = false;
          while (!closed && !reset) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += value;
            const frames = buffer.split(/\r?\n\r?\n/);
            buffer = frames.pop() ?? "";
            for (const frame of frames) {
              let data = "";
              for (const line of frame.split(/\r?\n/)) {
                if (line.startsWith("id:")) lastEventId = line.slice(3).trim();
                if (line.startsWith("data:")) data += `${line.slice(5).trimStart()}\n`;
              }
              if (!data) continue;
              const event = parseEvent(data.trimEnd());
              if (!event) continue;
              if (event.type === "stream.reset") {
                reset = true;
                await recoverReset(event.resetReason);
                await reader.cancel();
                break;
              }
              retry = 750;
              options.onEvent(event);
            }
          }
        } catch (error) {
          if (closed || suspended || (error instanceof DOMException && error.name === "AbortError")) break;
        }
        if (closed || suspended) break;
        options.onState(navigator.onLine ? "reconnecting" : "offline");
        await wait(Math.max(retry, serverDelay ?? 0));
        retry = Math.min(Math.round(retry * 1.7), 15_000);
      }
    } finally {
      opening = false;
    }
  };

  const offline = () => options.onState("offline");
  const suspend = () => {
    if (closed || suspended) return;
    suspended = true;
    controller?.abort();
    if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    if (recoveryTimer !== undefined) window.clearTimeout(recoveryTimer);
    retryTimer = undefined;
    recoveryTimer = undefined;
    resolveRetry?.();
    resolveRetry = undefined;
  };
  function pageHide(event: PageTransitionEvent): void {
    if (event.persisted) {
      suspend();
      return;
    }
    close();
  }
  function pageShow(event: PageTransitionEvent): void {
    if (!event.persisted || closed || !suspended) return;
    suspended = false;
    if (recoveryDirty) scheduleRecovery(0);
    void open();
  }
  function close(): void {
    if (closed) return;
    closed = true;
    controller?.abort();
    if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    if (recoveryTimer !== undefined) window.clearTimeout(recoveryTimer);
    resolveRetry?.();
    window.removeEventListener("offline", offline);
    window.removeEventListener("pagehide", pageHide);
    window.removeEventListener("pageshow", pageShow);
  }
  window.addEventListener("offline", offline);
  window.addEventListener("pagehide", pageHide);
  window.addEventListener("pageshow", pageShow);
  void open();

  return close;
}

export function connectSessionEvents(sessionId: string, options: StreamOptions): () => void {
  return connectEvents(`/api/v1/sessions/${encodeURIComponent(sessionId)}/events`, options);
}

export function connectGlobalEvents(options: StreamOptions): () => void {
  return connectEvents("/api/v1/events", options);
}
