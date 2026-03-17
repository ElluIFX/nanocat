# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## spawn — Background Subagent

- Starts a subagent in the background and returns immediately; the main agent continues its work.
- When the subagent finishes, the result is injected as a new message that re-invokes the main agent.
- Use when: the result is not needed right away and the task can run independently.

## delegate — Inline Subagents

- Launches a set of subagents concurrently and blocks until all finish, returning their results directly as the tool call response.
- The subagents' full execution context (tool call history) never enters the main agent's context — only the final results are returned.
- Use when: you need the results before deciding the next step, want to parallelize independent subtasks, and want to keep the main context lean.
- Key difference from spawn: spawn is async fire-and-forget (result arrives later as a new message); delegate is synchronous aggregation (results returned in the current turn).

## Subagent Task Prompt Best Practices

Subagents have no conversation history — they rely entirely on the `task` field to understand their assignment.

- **Self-contained**: include all necessary context in the task string; do not rely on "you already know…" or prior conversation memory.
- **Explicit output format**: specify the expected result format (e.g. "return a JSON list", "output the file path"), so the subagent does not invent its own.
- **Scoped**: if the task only needs a specific file or URL, provide it directly rather than asking the subagent to locate it.
- **Independent subtasks for delegate**: each task in a delegate call should have no data dependency on the others; steps with dependencies should be sequenced across multiple delegate calls or handled by the main agent.
- **Avoid open-ended instructions**: directives like "do whatever seems appropriate" leave the subagent without a clear goal and risk drift or unintended side effects.
