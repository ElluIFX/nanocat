import { fireEvent, render, screen } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import type { TimelineEvent } from "../types";
import { WorkingFeed } from "./WorkingFeed";

describe("WorkingFeed", () => {
  it("pairs tool input and output into one compact node", () => {
    const events: TimelineEvent[] = [
      {
        eventId: "assistant-work",
        sequence: 1,
        timestamp: "2026-08-23T00:00:00Z",
        type: "assistant.work",
        status: "running",
        content: "Checking the remote workspace.",
        redactedInput: {
          toolCalls: [{ id: "call-1", name: "ssh_send", arguments: { command: "pwd" } }],
        },
      },
      {
        eventId: "tool-result",
        sequence: 2,
        timestamp: "2026-08-23T00:00:01Z",
        type: "tool.result",
        status: "completed",
        toolCallId: "call-1",
        toolName: "ssh_send",
        redactedOutput: { preview: { content: "/srv/workspace" } },
      },
    ];

    render(<WorkingFeed events={events} active />);

    expect(screen.getAllByText("ssh_send")).toHaveLength(1);
    expect(screen.getByText("Checking the remote workspace.")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Details"));
    expect(screen.getByText(/"command": "pwd"/)).toBeInTheDocument();
    expect(screen.getByText(/"content": "\/srv\/workspace"/)).toBeInTheDocument();
  });

  it("keeps failed terminal work expanded", () => {
    render(<WorkingFeed events={[{
      eventId: "failed",
      sequence: 1,
      timestamp: "2026-08-23T00:00:00Z",
      type: "turn.failed",
      status: "failed",
      summary: "Provider request failed",
    }]} status="failed" />);

    expect(screen.getByText("Provider request failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Worked for/ })).toHaveAttribute("aria-expanded", "true");
  });

  it("settles superseded running nodes while the current step remains active", () => {
    render(<WorkingFeed active events={[
      {
        eventId: "thinking-1",
        sequence: 1,
        timestamp: "2026-08-23T00:00:00Z",
        type: "assistant.work",
        status: "running",
        content: "Inspecting the service.",
      },
      {
        eventId: "tool-1",
        sequence: 2,
        timestamp: "2026-08-23T00:00:01Z",
        type: "tool.result",
        status: "completed",
        toolCallId: "call-1",
        toolName: "ssh_send",
        redactedOutput: { preview: "done" },
      },
      {
        eventId: "thinking-2",
        sequence: 3,
        timestamp: "2026-08-23T00:00:02Z",
        type: "assistant.work",
        status: "running",
        content: "Choosing the next operation.",
      },
    ]} />);

    expect(screen.getAllByText("running")).toHaveLength(1);
    expect(screen.getByText("Choosing the next operation.")).toBeInTheDocument();
  });

  it("resumes tail following after the user returns to the bottom", () => {
    const first: TimelineEvent = {
      eventId: "thinking-1",
      sequence: 1,
      timestamp: "2026-08-23T00:00:00Z",
      type: "assistant.work",
      status: "running",
      content: "First step.",
    };
    const second: TimelineEvent = {
      ...first,
      eventId: "thinking-2",
      sequence: 2,
      timestamp: "2026-08-23T00:00:01Z",
      content: "Second step.",
    };
    const { container, rerender } = render(<WorkingFeed active events={[first]} />);
    const list = container.querySelector<HTMLElement>(".work-log-events");
    expect(list).not.toBeNull();
    Object.defineProperties(list!, {
      scrollHeight: { configurable: true, value: 500 },
      clientHeight: { configurable: true, value: 100 },
      scrollTop: { configurable: true, writable: true, value: 100 },
    });
    fireEvent.scroll(list!);
    rerender(<WorkingFeed active events={[first, second]} />);
    expect(list!.scrollTop).toBe(100);

    list!.scrollTop = 400;
    fireEvent.scroll(list!);
    rerender(<WorkingFeed active events={[first, second, { ...second, eventId: "thinking-3", sequence: 3, content: "Third step." }]} />);
    expect(list!.scrollTop).toBe(500);
  });

  it("does not render internal activity counters as thinking content", () => {
    render(<WorkingFeed active events={[{
      eventId: "internal-progress",
      sequence: 1,
      timestamp: "2026-08-23T00:00:00Z",
      type: "assistant.thinking",
      status: "running",
      content: JSON.stringify({ contentChars: 75, artifactCount: 0, toolEvent: null }),
      redactedOutput: { contentChars: 75, artifactCount: 0, toolEvent: null },
    }]} />);

    expect(screen.queryByText(/contentChars/)).not.toBeInTheDocument();
    expect(screen.getByText("Thinking")).toBeInTheDocument();
  });

  it("keeps queued user guidance outside the working ledger", () => {
    render(<WorkingFeed active events={[{
      eventId: "queued-guidance",
      sequence: 1,
      timestamp: "2026-08-23T00:00:00Z",
      type: "turn.steer_queued",
      status: "running",
      summary: "Guidance queued",
    }]} />);

    expect(screen.queryByText("Guidance queued")).not.toBeInTheDocument();
    expect(screen.getByText("Preparing run")).toBeInTheDocument();
  });
});
