import { afterEach, describe, expect, it, vi } from "vitest";
import { connectGlobalEvents, parseEvent } from "./sse";

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function responseWithFrames(frames: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(`${frame}\n\n`));
      controller.close();
    },
  }), { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("SSE projection", () => {
  it("drops unrecognized and raw tool fields", () => {
    const event = parseEvent(JSON.stringify({
      eventId: "1",
      sequence: 2,
      timestamp: "2026-08-23T00:00:00Z",
      type: "tool.event",
      secret: "top-secret",
      payload: {
        summary: "Tool completed",
        input: { token: "top-secret" },
        output: { token: "top-secret" },
        metadata: {
          _tool_event: {
            phase: "complete",
            calls: [{ id: "call-1", name: "read_file", status: "completed", arguments: { token: "top-secret" } }],
            arguments: { token: "top-secret" },
          },
        },
      },
    }));

    expect(event?.phase).toBe("complete");
    expect(event?.summary).toBe("Tool completed");
    expect(event?.redactedInput).toEqual({ calls: [{ id: "call-1", name: "read_file", status: "completed" }] });
    expect(event?.redactedOutput).toBeUndefined();
    expect(JSON.stringify(event)).not.toContain("top-secret");
  });

  it("accepts server-redacted tool input and output", () => {
    const event = parseEvent(JSON.stringify({
      eventId: "safe-tool",
      sequence: 3,
      timestamp: "2026-08-23T00:00:00Z",
      type: "tool.event",
      payload: {
        redactedInput: { calls: [{ id: "call-1", name: "read_file", arguments: { path: "workspace/file.txt" } }] },
        redactedOutput: { calls: [{ id: "call-1", name: "read_file", resultPreview: { content: "bounded result" } }] },
      },
    }));

    expect(event?.redactedInput).toEqual({ calls: [{ id: "call-1", name: "read_file", arguments: { path: "workspace/file.txt" } }] });
    expect(event?.redactedOutput).toEqual({ calls: [{ id: "call-1", name: "read_file", resultPreview: { content: "bounded result" } }] });
  });

  it("keeps low-frequency overflow recovery alive after repeated failures", async () => {
    vi.useFakeTimers();
    const resetFrame = `id: 1\nevent: stream.reset\ndata: ${JSON.stringify({
      eventId: "1",
      sequence: 1,
      timestamp: "2026-08-23T00:00:00Z",
      type: "stream.reset",
      payload: { reason: "subscriber_overflow" },
    })}`;
    let fetchCount = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      fetchCount += 1;
      return responseWithFrames(fetchCount === 1 ? [resetFrame] : []);
    }));
    let recoveryAttempts = 0;
    let close: () => void = () => undefined;
    close = connectGlobalEvents({
      onEvent: () => undefined,
      onState: () => undefined,
      onReset: async () => {
        recoveryAttempts += 1;
        if (recoveryAttempts < 5) throw new Error("temporary recovery failure");
        close();
      },
    });

    await vi.advanceTimersByTimeAsync(90_000);
    expect(recoveryAttempts).toBeGreaterThanOrEqual(5);
    expect(fetchCount).toBe(1);
    close();
  });

  it("keeps replay-window recovery pending until the projection refresh succeeds", async () => {
    vi.useFakeTimers();
    const resetFrame = `id: 9\nevent: stream.reset\ndata: ${JSON.stringify({
      eventId: "9",
      sequence: 9,
      timestamp: "2026-08-23T00:00:00Z",
      type: "stream.reset",
      payload: { reason: "replay_window_expired" },
    })}`;
    let fetchCount = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      fetchCount += 1;
      return responseWithFrames(fetchCount === 1 ? [resetFrame] : []);
    }));
    let recoveryAttempts = 0;
    let close: () => void = () => undefined;
    close = connectGlobalEvents({
      onEvent: () => undefined,
      onState: () => undefined,
      onReset: async (reason) => {
        expect(reason).toBe("replay_window_expired");
        recoveryAttempts += 1;
        if (recoveryAttempts === 1) throw new Error("temporary recovery failure");
        close();
      },
    });

    await vi.advanceTimersByTimeAsync(10_000);
    expect(recoveryAttempts).toBe(2);
    expect(fetchCount).toBe(1);
    close();
  });

  it("closes the reset stream and does not open another subscriber before recovery", async () => {
    vi.useFakeTimers();
    const encoder = new TextEncoder();
    let streamCancelled = 0;
    const resetFrame = `id: 10\nevent: stream.reset\ndata: ${JSON.stringify({
      eventId: "10",
      sequence: 10,
      timestamp: "2026-08-23T00:00:00Z",
      type: "stream.reset",
      payload: { reason: "replay_window_expired" },
    })}\n\n`;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream({
      start(controller) {
        controller.enqueue(encoder.encode(resetFrame));
      },
      cancel() {
        streamCancelled += 1;
      },
    }), { status: 200, headers: { "Content-Type": "text/event-stream" } })));
    const close = connectGlobalEvents({
      onEvent: () => undefined,
      onState: () => undefined,
      onReset: async () => { throw new Error("recovery remains unavailable"); },
    });

    await vi.advanceTimersByTimeAsync(900);
    expect(streamCancelled).toBe(1);
    expect(fetch).toHaveBeenCalledTimes(1);
    close();
  });

  it("pagehide closes the global stream without scheduling reconnects", async () => {
    vi.useFakeTimers();
    let aborted = false;
    vi.stubGlobal("fetch", vi.fn((_path: string, init?: RequestInit) => new Promise<Response>(
      (_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          aborted = true;
          reject(new DOMException("aborted", "AbortError"));
        });
      },
    )));
    const close = connectGlobalEvents({
      onEvent: () => undefined,
      onState: () => undefined,
      onReset: () => undefined,
    });
    await Promise.resolve();
    window.dispatchEvent(new PageTransitionEvent("pagehide"));
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(aborted).toBe(true);
    expect(fetch).toHaveBeenCalledTimes(1);
    close();
  });

  it("resumes the global stream after a bfcache pageshow", async () => {
    vi.useFakeTimers();
    let aborts = 0;
    vi.stubGlobal("fetch", vi.fn((_path: string, init?: RequestInit) => new Promise<Response>(
      (_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          aborts += 1;
          reject(new DOMException("aborted", "AbortError"));
        });
      },
    )));
    const close = connectGlobalEvents({
      onEvent: () => undefined,
      onState: () => undefined,
      onReset: () => undefined,
    });
    await Promise.resolve();
    window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
    await vi.advanceTimersByTimeAsync(0);
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
    await vi.advanceTimersByTimeAsync(0);

    expect(aborts).toBe(1);
    expect(fetch).toHaveBeenCalledTimes(2);
    close();
  });
});
