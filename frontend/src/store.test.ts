import { waitFor } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api/client";
import { appendGlobalEvent, currentSession, reloadCurrentSession, sessions } from "./store";
import type { TimelineEvent } from "./types";

type SessionResponse = Awaited<ReturnType<typeof api.session>>;
const original: SessionResponse = {
  id: "session-1", title: "Untitled session", status: "completed",
  updatedAt: "2026-09-21T00:00:00Z", turns: [], events: [],
  revision: 2, conversationCursor: 0, trajectoryActivityCursor: 0,
  trajectoryOldestCursor: 0, trajectoryHistoryCursor: 0, trajectoryCursorKind: "sequence",
};
const named: SessionResponse = { ...original, title: "Inspect startup performance" };
const update: TimelineEvent = {
  eventId: "name-1", sequence: 2, timestamp: original.updatedAt,
  type: "session.updated", sessionId: original.id,
};

afterEach(() => {
  vi.restoreAllMocks();
  currentSession.value = null;
  sessions.value = [];
});

describe("automatic session title updates", () => {
  it("refreshes the sidebar and current title without adding a feed event", async () => {
    currentSession.value = original;
    sessions.value = [original];
    vi.spyOn(api, "session").mockResolvedValue(named);
    vi.spyOn(api, "sessions").mockResolvedValue([named]);
    appendGlobalEvent(update);
    await waitFor(() => expect(currentSession.value?.title).toBe(named.title));
    expect(sessions.value[0].title).toBe(named.title);
    expect(currentSession.value?.events).toEqual([]);
  });

  it("updates an inactive session without navigating away", async () => {
    currentSession.value = { ...original, id: "other" };
    vi.spyOn(api, "sessions").mockResolvedValue([named]);
    const detail = vi.spyOn(api, "session");
    appendGlobalEvent(update);
    await waitFor(() => expect(sessions.value[0]?.title).toBe(named.title));
    expect(currentSession.value.id).toBe("other");
    expect(detail).not.toHaveBeenCalled();
  });

  it("discards a stale terminal refresh after the title update", async () => {
    currentSession.value = original;
    sessions.value = [original];
    let finishOld!: (value: SessionResponse) => void;
    vi.spyOn(api, "session")
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }))
      .mockResolvedValue(named);
    vi.spyOn(api, "sessions").mockResolvedValue([named]);
    const oldRefresh = reloadCurrentSession();
    appendGlobalEvent(update);
    await waitFor(() => expect(currentSession.value?.title).toBe(named.title));
    finishOld(original);
    await oldRefresh;
    expect(currentSession.value?.title).toBe(named.title);
    expect(sessions.value[0].title).toBe(named.title);
  });
});
