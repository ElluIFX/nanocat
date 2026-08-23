import type { TimelineEvent } from "../types";

export function mergeTimelineEvent(events: TimelineEvent[], incoming: TimelineEvent): TimelineEvent[] {
  const index = events.findIndex((event) => (
    incoming.nodeId && event.nodeId
      ? event.nodeId === incoming.nodeId
      : event.eventId === incoming.eventId
  ));
  if (index >= 0) {
    const next = [...events];
    const defined = Object.fromEntries(
      Object.entries(incoming).filter(([, value]) => value !== undefined),
    ) as Partial<TimelineEvent>;
    next[index] = { ...events[index], ...defined };
    return next;
  }
  return [...events, incoming];
}
