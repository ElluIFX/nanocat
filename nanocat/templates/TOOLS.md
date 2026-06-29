# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## Subagent Task Prompt Best Practices

Subagents have no conversation history — they rely entirely on the `task` field to understand their assignment.

- **Self-contained**: include all necessary context in the task string; do not rely on "you already know…" or prior conversation memory.
- **Explicit output format**: specify the expected result format (e.g. "return a JSON list", "output the file path"), so the subagent does not invent its own.
- **Scoped**: if the task needs specific files or URLs, provide it directly rather than asking the subagent to locate it.
- **Independent subtasks for gather**: each task in a gather call should have no data dependency on the others; steps with dependencies should be sequenced across multiple gather calls or handled by the main agent.
- **Avoid open-ended instructions**: directives like "do whatever seems appropriate" leave the subagent without a clear goal and risk drift or unintended side effects.
