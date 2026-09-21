const PARAMETER_ORDER = [
  "action", "path", "paths", "local_path", "remote_path", "host", "port", "identity",
  "session_id", "proc_id", "subagent_id", "subagent_ids", "id", "job_id",
  "command", "text", "keys", "enter", "working_dir", "cwd", "shell",
  "line_start", "line_end", "line", "after", "offset", "column_offset", "length", "limit",
  "pattern", "query", "regex", "case_sensitive", "recursive", "mode", "encoding",
  "url", "method", "extract_mode", "count", "max_chars", "stream", "session",
  "name", "task", "tasks", "index", "status", "model", "reasoning_effort",
  "channel", "chat_id", "question", "message", "content", "attachments",
  "every_seconds", "cron_expr", "tz", "at", "notify",
  "region", "cols", "rows", "wait", "wait_s", "timeout", "timeout_s", "immediate_return",
];
const ORDER = new Map(PARAMETER_ORDER.map((key, index) => [key, index]));
const SENSITIVE_KEY = /password|passwd|secret|token$|credential|authorization|cookie|api[-_]?key|private[-_]?key/i;
const MAX_STRING = 180;

function summarize(value: unknown, key = "", depth = 0): string {
  if (SENSITIVE_KEY.test(key)) return "[REDACTED]";
  if (value === null) return "null";
  if (typeof value === "string") {
    const preview = JSON.stringify(value.slice(0, MAX_STRING));
    return value.length > MAX_STRING ? `${preview}… (${value.length.toLocaleString()} chars)` : preview;
  }
  if (Array.isArray(value)) {
    if (depth >= 2) return `[${value.length} items]`;
    const items = value.slice(0, 4).map((item) => summarize(item, "", depth + 1));
    if (value.length > 4) items.push(`… +${value.length - 4} items`);
    return `[${items.join(", ")}]`;
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value);
    if (depth >= 2) return `{${entries.length} fields}`;
    const fields = entries.slice(0, 4).map(([name, item]) => `${name}: ${summarize(item, name, depth + 1)}`);
    if (entries.length > 4) fields.push(`… +${entries.length - 4} fields`);
    return `{${fields.join(", ")}}`;
  }
  return String(value);
}

export function toolArgumentEntries(input: unknown): [string, string][] {
  if (typeof input === "string") {
    try { input = JSON.parse(input); } catch { return [["input", summarize(input)]]; }
  }
  if (input === undefined) return [];
  if (input === null || typeof input !== "object" || Array.isArray(input)) {
    return [["input", summarize(input)]];
  }
  return Object.entries(input)
    .filter(([, value]) => value !== undefined)
    .sort(([left], [right]) => (ORDER.get(left) ?? ORDER.size) - (ORDER.get(right) ?? ORDER.size))
    .map(([key, value]) => [key, summarize(value, key)]);
}

export function ToolArguments({ input }: { input: unknown }) {
  const entries = toolArgumentEntries(input);
  if (!entries.length) return null;
  return <small class="tool-arguments" aria-label="Call parameters">
    {entries.map(([key, value]) => <span class="tool-argument" key={key}>
      <span class="tool-argument-name">{key}</span><code>{value}</code>
    </span>)}
  </small>;
}
