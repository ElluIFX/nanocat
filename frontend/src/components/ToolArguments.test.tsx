import { render, screen } from "@testing-library/preact";
import { describe, expect, it } from "vitest";
import { toolArgumentEntries } from "./ToolArguments";
import { WorkingFeed } from "./WorkingFeed";

describe("tool argument subtitles", () => {
  it.each([
    ["read_file", { path: "/src/main.py", offset: 1, column_offset: 0, limit: 80, encoding: "utf-8", max_chars: 4000 }],
    ["edit_file", { path: "/src/main.py", old_text: "old", new_text: "new" }],
    ["grep_file", { path: "/src", pattern: "hello", case_sensitive: false }],
    ["ssh_send", { session_id: "ssh-1", text: "pwd", keys: ["CTRL+C"], enter: true, wait: 0, immediate_return: false }],
    ["ssh_upload", { host: "remote", local_path: "/tmp/a", remote_path: "/srv/a", port: 2222, timeout: 60 }],
    ["proc_start", { command: "python app.py", cwd: "/src", cols: 120, rows: 30 }],
    ["http_request", { url: "https://example.com", method: "POST", json_body: { count: 2 }, stream: false }],
    ["subagent_spawn", { tasks: [{ label: "Review", task: "Inspect source" }], model: "test" }],
    ["todo", { action: "update", id: "todo-1", index: 2, status: "INPROGRESS" }],
    ["cron", { action: "add", cron_expr: "0 9 * * *", tz: "Asia/Shanghai", notify: false }],
    ["mcp_custom", { unknown_field: "value", nested: { enabled: false }, empty: "", nullable: null }],
  ])("renders every supplied parameter for %s", (name, input) => {
    render(<WorkingFeed active events={[{
      eventId: "call", sequence: 1, timestamp: "2026-09-21T00:00:00Z",
      type: "tool.call", toolCallId: "call-1", toolName: name as string,
      status: "running", redactedInput: input,
    }]} />);
    const subtitle = screen.getByLabelText("Call parameters");
    for (const key of Object.keys(input)) expect(subtitle).toHaveTextContent(key);
  });

  it("preserves zero, false, empty strings and null and parses serialized arguments", () => {
    expect(toolArgumentEntries('{"wait":0,"enter":false,"text":"","mode":null}')).toEqual([
      ["text", '""'], ["enter", "false"], ["mode", "null"], ["wait", "0"],
    ]);
  });

  it("bounds large values without changing the input and masks nested credentials", () => {
    const input = { content: "a".repeat(10_000), headers: { Authorization: "private-value" }, tasks: [1, 2, 3, 4, 5] };
    const summary = toolArgumentEntries(input).map((entry) => entry.join(": ")).join("\n");
    expect(summary.length).toBeLessThan(400);
    expect(summary).toContain("chars");
    expect(summary).toContain("+1 items");
    expect(summary).toContain("[REDACTED]");
    expect(summary).not.toContain("private-value");
    expect(input.content).toHaveLength(10_000);
    expect(input.headers.Authorization).toBe("private-value");
  });
});
