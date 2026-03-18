---
name: todo
description: Drives complex, multi-step engineering tasks using an independent Markdown todo file as a state machine. Use when users request a complex task that requires a step-by-step plan.
---

## Core Principles

1. **Plan Before Execution:** Always create a tracking file at `todos/<task-name_timestamp>.md` before making any codebase changes. The filename must be contextually distinct.
2. **Single Focus (Baby Steps):** Focus on *one and only one* substantive step at a time. Never scatter partial implementations across multiple concurrent steps.
3. **Incremental Validation:** A step is not strictly completed until it is implemented *and* verified (e.g., via compiler checks, linting, or unit tests).
4. **State Synchronization:** Always read the todo file before acting to ensure context alignment. Update the file immediately after any step transitions state.
5. **Persistent Execution:** Do not autonomously halt the process unless all tasks are completed, a hard blocker is encountered, or human-in-the-loop (HITL) input is strictly required.

## Execution Lifecycle

### 1. Initialization

- Evaluate task complexity.
- Create the `todos/*.md` file using the Standard Template.
- Decompose the global goal into granular, actionable, and verifiable checklist items.

### 2. Execution Loop

For every iteration, adhere to this strict control flow:

1. **Read:** Parse the current `todos/*.md` file to map the current state.
2. **Select:** Identify the *first* pending (`[ ]`) item. Mark it as In Progress (`[-]`).
3. **Execute:** Perform the necessary actions (code generation, file I/O, shell commands).
4. **Validate:** Run appropriate structural checks or tests to verify the outcome.
5. **Update:**
   - If verified: Mark as Completed (`[x]`).
   - If partially successful/complex: Decompose the current step into nested sub-tasks within the markdown file.
   - If failed/blocked: Mark as Blocked (`[!]`), document the exact error in the Notes, and halt to prompt the user.
6. **Iterate:** Proceed to the next pending item.

IMPORTANT: Rewrite your latest state back to the mark item `[ ]` in `todos/*.md` before executing the next pending step.

### 3. Context & Memory Management

- **Context Offloading:** If the task spans excessive iterations or the context window approaches its limits, proactively summarize architectural decisions and progress in the `Execution Notes` section.
- **Dynamic Adaptation:** Any newly discovered requirements or dependencies must be explicitly appended to the Checklist. Do not rely on implicit model memory.

### 4. Finalization & Cleanup

When all checklist items equal `[x]`:

- Perform a holistic review of the deliverables against the original `Goal` and `Constraints`.
- Delete the `todos/*.md` file to maintain workspace cleanliness.
- Output a concise telemetry report to the user (actions taken, final state, and follow-up engineering recommendations).

## Standard Todo Template

```md
# Task: <Task Name>

## Goal
<Concise, one-sentence objective>

## Context & Constraints
- <Architectural constraints, required libraries, or specific rules>
- <e.g., "Must conform to MISRA C standards", "Use async/await strictly">

## Checklist
*State Indicators: `[ ]` Pending | `[-]` In Progress | `[x]` Completed | `[!]` Blocked*

- [ ] **Phase 1: <Phase Definition>**
  - [ ] <Specific, verifiable step 1>
  - [ ] <Specific, verifiable step 2>
- [ ] **Phase 2: <Phase Definition>**
  - [ ] <Specific, verifiable step 3>

## Execution Notes & Discoveries
- *<Date/Time>: <Record critical design decisions, unexpected API behaviors, or context required for future steps>*
