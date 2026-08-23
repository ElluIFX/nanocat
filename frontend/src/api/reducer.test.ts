import { describe, expect, it } from "vitest";
import type { TimelineEvent } from "../types";
import { mergeTimelineEvent } from "./reducer";

function event(eventId: string, sequence: number, summary = eventId): TimelineEvent {
  return { eventId, sequence, summary, timestamp: "2026-08-23T00:00:00Z", type: "tool.call" };
}

describe("mergeTimelineEvent", () => {
  it("preserves snapshot order when live sequence domains restart", () => {
    expect(mergeTimelineEvent([event("historical", 20)], event("live", 1)).map((item) => item.eventId)).toEqual(["historical", "live"]);
  });

  it("reconciles a repeated event without duplicating it", () => {
    const merged = mergeTimelineEvent([event("call", 4, "Running")], { ...event("call", 4, "Complete"), status: "completed" });
    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({ summary: "Complete", status: "completed" });
  });

  it("reconciles projection and SSE nodes without erasing retained detail", () => {
    const previous = { ...event("projection", 4, "Running"), nodeId: "tool-node", redactedInput: { path: "fixture.txt" } };
    const incoming = { ...event("sse", 5, "Complete"), nodeId: "tool-node", status: "completed" as const, redactedInput: undefined };
    const merged = mergeTimelineEvent([previous], incoming);

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({ eventId: "sse", summary: "Complete", status: "completed", redactedInput: { path: "fixture.txt" } });
  });
});
