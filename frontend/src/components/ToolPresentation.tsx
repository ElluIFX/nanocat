import {
  CheckCircle2,
  Circle,
  Download,
  FileCode2,
  FolderSearch2,
  GitBranch,
  Globe2,
  ListChecks,
  LoaderCircle,
  MonitorDot,
  Upload,
} from "lucide-preact";
import type { RunStatus } from "../types";

type JsonRecord = Record<string, unknown>;

export interface TodoTaskPresentation {
  index: number;
  task: string;
  status: "PENDING" | "INPROGRESS" | "COMPLETED";
}

export interface TodoPresentation {
  id: string;
  name: string;
  action: string;
  completed: boolean;
  tasks: TodoTaskPresentation[];
}

export interface ToolPresentationValue {
  name: string;
  input?: unknown;
  output?: unknown;
  status?: RunStatus;
  todo?: TodoPresentation;
}

function record(value: unknown): JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : {};
}

function stringValue(...values: unknown[]): string {
  return String(values.find((value) => typeof value === "string" && value.trim()) ?? "");
}

function numberValue(...values: unknown[]): number | undefined {
  const value = values.find((item) => typeof item === "number" && Number.isFinite(item));
  return typeof value === "number" ? value : undefined;
}

function shortText(value: unknown, max = 220): string {
  const source = typeof value === "string" ? value.trim() : "";
  return source.length > max ? `${source.slice(0, max)}…` : source;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}

function ToolCardState({ status }: { status?: RunStatus }) {
  if (!status || status === "idle" || status === "completed") return null;
  const pending = ["queued", "running", "cancelling"].includes(status);
  return <span class={`tool-card-state status-${status}`}>
    {pending ? <LoaderCircle class="spin" size={11} /> : null}
    {status.replaceAll("_", " ")}
  </span>;
}

function TodoCard({ tool }: { tool: ToolPresentationValue }) {
  const todo = tool.todo!;
  const completed = todo.tasks.filter((task) => task.status === "COMPLETED").length;
  const progress = todo.tasks.length ? Math.round((completed / todo.tasks.length) * 100) : 0;
  return <div class="tool-card todo-card">
    <header>
      <span class="tool-card-mark"><ListChecks size={15} /></span>
      <span><strong>{todo.name || "Task list"}</strong><small>#{todo.id} · {completed}/{todo.tasks.length} complete</small></span>
      <ToolCardState status={tool.status} />
      {(!tool.status || tool.status === "completed") && <span class="todo-progress-value">{progress}%</span>}
    </header>
    <div class="todo-progress" role="progressbar" aria-label={`${todo.name} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
    <ol>
      {todo.tasks.map((task) => <li class={`todo-${task.status.toLowerCase()}`} key={task.index}>
        <span>{task.status === "COMPLETED" ? <CheckCircle2 size={14} /> : task.status === "INPROGRESS" ? <LoaderCircle class="spin" size={14} /> : <Circle size={14} />}</span>
        <span><small>{task.index}</small>{task.task}</span>
      </li>)}
    </ol>
  </div>;
}

const FILE_TOOLS = new Set([
  "read_file", "write_file", "edit_file", "list_dir", "grep_file", "insert_lines",
  "delete_lines", "delete", "file_hex", "load_image", "screenshot",
]);

function FileToolCard({ tool }: { tool: ToolPresentationValue }) {
  const input = record(tool.input);
  const output = record(tool.output);
  const path = stringValue(input.path, output.path, output.saved_to);
  const entries = Array.isArray(output.entries) ? output.entries.map(String) : [];
  const matches = Array.isArray(output.results) ? output.results.map(record) : [];
  const metrics = [
    numberValue(output.total_lines) !== undefined ? `${numberValue(output.total_lines)} lines` : "",
    numberValue(output.matches) !== undefined ? `${numberValue(output.matches)} matches` : "",
    numberValue(output.bytes_written) !== undefined ? `${formatBytes(numberValue(output.bytes_written) ?? 0)} written` : "",
    output.truncated === true ? "truncated" : "",
  ].filter(Boolean);
  const preview = shortText(output.content ?? output.preview, 520);
  return <div class="tool-card file-tool-card">
    <header><span class="tool-card-mark">{tool.name === "grep_file" || tool.name === "list_dir" ? <FolderSearch2 size={15} /> : <FileCode2 size={15} />}</span><span><strong>{path || tool.name.replaceAll("_", " ")}</strong>{metrics.length ? <small>{metrics.join(" · ")}</small> : null}</span><ToolCardState status={tool.status} /></header>
    {entries.length ? <ul class="tool-result-list">{entries.slice(0, 5).map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
    {matches.length ? <ul class="tool-result-list code-list">{matches.slice(0, 5).map((match, index) => <li key={`${match.line ?? index}`}><code>{String(match.line ?? "")}</code><span>{shortText(match.content)}</span></li>)}</ul> : null}
    {preview ? <pre class="file-preview"><code>{preview}</code></pre> : null}
  </div>;
}

const TERMINAL_TOOLS = new Set(["exec", "proc_start", "proc_send", "proc_read", "proc_stop", "proc_list", "ssh_open", "ssh_send", "ssh_read", "ssh_close", "ssh_list", "ssh_upload", "ssh_download"]);

function TerminalToolCard({ tool }: { tool: ToolPresentationValue }) {
  const input = record(tool.input);
  const output = record(tool.output);
  const transfer = tool.name === "ssh_upload" || tool.name === "ssh_download";
  const command = stringValue(input.command, output.command);
  const host = stringValue(input.host, output.host);
  const id = stringValue(input.session_id, input.proc_id, output.session_id, output.proc_id, output.id);
  const stdout = shortText(output.stdout ?? output.output ?? output.screen ?? output.content, 520);
  const stderr = shortText(output.stderr ?? output.error ?? output.detail, 280);
  const returnCode = numberValue(output.returncode);
  if (transfer) {
    const upload = tool.name === "ssh_upload";
    return <div class="tool-card transfer-card">
      <header><span class="tool-card-mark">{upload ? <Upload size={15} /> : <Download size={15} />}</span><span><strong>{upload ? "Uploaded file" : "Downloaded file"}</strong><small>{host || "SSH transfer"}{numberValue(output.bytes) !== undefined ? ` · ${formatBytes(numberValue(output.bytes) ?? 0)}` : ""}</small></span><ToolCardState status={tool.status} /></header>
      <div class="transfer-route"><code>{upload ? stringValue(output.local_path, input.local_path) || "local" : stringValue(output.remote_path, input.remote_path) || "remote"}</code><span>→</span><code>{upload ? stringValue(output.remote_path, input.remote_path) || "remote" : stringValue(output.local_path, input.local_path) || "local"}</code></div>
    </div>;
  }
  return <div class="tool-card terminal-tool-card">
    <header><span class="tool-card-mark"><MonitorDot size={15} /></span><span><strong>{command || host || id || tool.name.replaceAll("_", " ")}</strong><small>{[id, returnCode !== undefined ? `exit ${returnCode}` : ""].filter(Boolean).join(" · ")}</small></span><ToolCardState status={tool.status} /></header>
    {(stdout || stderr) && <pre class="terminal-preview"><code>{stdout}{stdout && stderr ? "\n" : ""}{stderr}</code></pre>}
  </div>;
}

const NETWORK_TOOLS = new Set(["web_search", "web_fetch", "http_request"]);
const SUBAGENT_TOOLS = new Set(["subagent_spawn", "subagent_gather", "subagent_list", "subagent_steer", "subagent_kill"]);

function NetworkToolCard({ tool }: { tool: ToolPresentationValue }) {
  const input = record(tool.input);
  const output = record(tool.output);
  const target = stringValue(input.query, output.url, input.url);
  const status = numberValue(output.status);
  const count = numberValue(output.count);
  const body = shortText(output.content ?? output.body, 420);
  return <div class="tool-card network-tool-card">
    <header><span class="tool-card-mark"><Globe2 size={15} /></span><span><strong>{target || tool.name.replaceAll("_", " ")}</strong><small>{[status !== undefined ? `HTTP ${status}` : "", count !== undefined ? `${count} results` : "", output.extractor ? String(output.extractor) : ""].filter(Boolean).join(" · ")}</small></span><ToolCardState status={tool.status} /></header>
    {body && <p>{body}</p>}
  </div>;
}

function SubagentToolCard({ tool }: { tool: ToolPresentationValue }) {
  const input = record(tool.input);
  const output = record(tool.output);
  const inputTasks = Array.isArray(input.tasks) ? input.tasks.map(record) : [];
  const agents = Array.isArray(output.spawned)
    ? output.spawned.map(record)
    : Array.isArray(output.subagents)
      ? output.subagents.map(record)
      : [];
  const resultCount = Array.isArray(output.results)
    ? output.results.length
    : numberValue(output.total);
  const target = stringValue(input.subagent_id, output.steer_to, output.stopped);
  const count = agents.length || inputTasks.length || resultCount || 0;
  return <div class="tool-card subagent-tool-card">
    <header><span class="tool-card-mark"><GitBranch size={15} /></span><span><strong>{tool.name.replace("subagent_", "Subagent ").replaceAll("_", " ")}</strong><small>{target ? `Agent ${target}` : count ? `${count} agent${count === 1 ? "" : "s"}` : "Agent orchestration"}</small></span><ToolCardState status={tool.status} /></header>
    {(agents.length || inputTasks.length) ? <ul class="tool-result-list">{(agents.length ? agents : inputTasks).slice(0, 5).map((agent, index) => <li key={String(agent.id ?? index)}>{stringValue(agent.label, agent.task, agent.id) || `Agent ${index + 1}`}</li>)}</ul> : null}
  </div>;
}

export function ToolPresentation({ tool }: { tool: ToolPresentationValue }) {
  if (tool.todo) return <TodoCard tool={tool} />;
  if (FILE_TOOLS.has(tool.name)) return <FileToolCard tool={tool} />;
  if (TERMINAL_TOOLS.has(tool.name)) return <TerminalToolCard tool={tool} />;
  if (NETWORK_TOOLS.has(tool.name)) return <NetworkToolCard tool={tool} />;
  if (SUBAGENT_TOOLS.has(tool.name)) return <SubagentToolCard tool={tool} />;
  return null;
}

export function supportsToolPresentation(tool: Pick<ToolPresentationValue, "name" | "todo">): boolean {
  return Boolean(tool.todo)
    || FILE_TOOLS.has(tool.name)
    || TERMINAL_TOOLS.has(tool.name)
    || NETWORK_TOOLS.has(tool.name)
    || SUBAGENT_TOOLS.has(tool.name);
}
