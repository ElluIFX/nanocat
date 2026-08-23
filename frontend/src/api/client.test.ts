import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

function json(value: unknown): Response {
  return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("API projections", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads bounded latest conversation and trajectory projections", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/sessions/session")) return json({ sessionName: "Paged", busy: false });
      if (url.includes("/conversation?latest=true&limit=500")) return json({
        items: [
          { id: "user-1", type: "user.message", role: "user", content: "Hello" },
          { id: "assistant-1", type: "assistant.final", role: "assistant", content: "Done" },
        ],
        hasMore: false,
        totalCount: 2,
        pageOffset: 0,
      });
      if (url.includes("/trajectory?latest=true&limit=1000&historyLimit=1000")) return json({
        items: [
          { id: "activity-1", type: "tool.event", sequence: 1, summary: "updated" },
          { id: "activity-thinking", type: "assistant.thinking", sequence: 2, content: { contentChars: 75, artifactCount: 0 }, metadata: { output: { contentChars: 75, artifactCount: 0, content: "Safe reasoning" } } },
          { id: "activity-progress", type: "assistant.progress", sequence: 3, content: { contentChars: 0, artifactCount: 0 }, metadata: { output: { contentChars: 0, artifactCount: 0 } } },
          { id: "activity-2", type: "assistant.final", sequence: 4 },
          { id: "activity-late", type: "assistant.thinking", sequence: 5, status: "running", turnId: "turn-1" },
        ],
        historicalItems: [
          { id: "history-1", type: "turn.historical", summary: "updated" },
          { id: "history-2", type: "tool.historical" },
        ],
        historicalPageOffset: 0,
        previousCursor: 0,
        oldestSequence: 1,
        latestSequence: 5,
        historicalHasMore: false,
        hasMore: false,
      });
      throw new Error(`Unexpected request: ${url}`);
    }));

    const session = await api.session("session");

    expect(session.turns.map((turn) => turn.id)).toEqual(["user-1", "assistant-1"]);
    expect(session.events.map((event) => event.eventId)).toEqual(["history-1", "history-2", "activity-1", "activity-thinking", "activity-progress", "activity-2", "activity-late"]);
    expect(session.status).toBe("completed");
    expect(session.events.find((event) => event.eventId === "activity-1")?.summary).toBe("updated");
    expect(session.events.find((event) => event.eventId === "activity-thinking")?.content).toBe("Safe reasoning");
    expect(session.events.find((event) => event.eventId === "activity-progress")?.content).toBe("");
  });

  it("normalizes bracket log timestamps and preserves correlation fields", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({
      lines: [
        "[07:12:33] [WARNING] bounded message",
        { timestamp: "2026-08-23T00:00:00Z", level: "info", message: "linked", sessionId: "s1", turnId: "t1", requestId: "r1" },
      ],
    })));

    const logs = await api.logs();

    expect(Number.isFinite(new Date(logs[0].timestamp).getTime())).toBe(true);
    expect(logs[1]).toMatchObject({ sessionId: "s1", turnId: "t1", requestId: "r1" });
  });

  it("loads and paginates the latest offset trajectory fallback", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/sessions/offset")) return json({ sessionName: "Offset history" });
      if (url.includes("/conversation?latest=true&limit=500")) return json({ items: [], totalCount: 0, pageOffset: 0 });
      if (url.includes("/trajectory?latest=true&limit=1000&historyLimit=1000")) return json({
        items: [{ id: "offset-1500", type: "assistant.final" }],
        totalCount: 1501,
        pageOffset: 501,
        source: "session",
        cursorKind: "offset",
      });
      if (url.includes("/trajectory?cursor=0&limit=501&historyCursor=0&historyLimit=1")) return json({
        items: [{ id: "offset-500", type: "tool.event" }],
        totalCount: 1501,
        source: "session",
        cursorKind: "offset",
      });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const session = await api.session("offset");
    const earlier = await api.earlierTrajectory(
      "offset",
      session.trajectoryActivityCursor ?? 0,
      session.trajectoryOldestCursor ?? 0,
      session.trajectoryHistoryCursor ?? 0,
      session.trajectoryCursorKind ?? "sequence",
    );

    expect(session.trajectoryCursorKind).toBe("offset");
    expect(session.trajectoryActivityCursor).toBe(501);
    expect(session.events.map((event) => event.eventId)).toEqual(["offset-1500"]);
    expect(earlier.activityCursor).toBe(0);
    expect(earlier.events.map((event) => event.eventId)).toEqual(["offset-500"]);
  });
});
