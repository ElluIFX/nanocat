---
name: skill-creator
description: "Create, edit, validate, and package NanoCat skills. Use whenever the user wants to build a new skill from scratch, capture a workflow as a reusable skill, fix or improve an existing skill, tune a skill's description so it triggers reliably, or package a skill for sharing. Triggers on phrases like create skill, new skill, make a skill, improve skill, fix skill, package skill, 创建技能, 新技能, 改进技能, 打包技能."
compatibility: "Pure-Python helper scripts (validate/package) need PyYAML. No browser, CLI, or network dependency — designed for NanoCat's headless runtime."
---

# Skill Creator

Build and refine NanoCat skills. A skill is a folder under `workspace/skills/<name>/`
containing a `SKILL.md`; optional `scripts/`, `references/`, and `assets/` hold code,
docs, and templates the skill points to.

The loop: **draft → test by spawning a subagent → read the transcript → improve → repeat**
until it works. Figure out where the user is and jump in. They may have nothing ("make a
skill for X"), or an existing draft (go straight to test/improve), or just want to vibe —
stay flexible.

## How NanoCat loads skills (read this before writing frontmatter)

NanoCat's loader is stricter than generic YAML. Get these wrong and the skill loads with a
broken description or doesn't gate as intended:

- **The skill name is the directory name**, not the frontmatter `name:` field. Name the
  folder in kebab-case (`pdf-extract`, not `PDF Extract`). Keep `name:` matching it.
- **`description` must be a single physical line.** The parser reads frontmatter line by
  line as `key: value` — it does *not* understand YAML block scalars (`>`, `|`) or values
  wrapped across multiple lines. A multi-line description silently truncates to the first
  line or to a literal `>`. Put everything on one line; wrapping quotes are stripped.
- The description is the **only** trigger signal — it sits in context permanently while the
  body loads only when the skill fires. Pack both *what it does* and *when to use it* here.
- **Optional gating** goes in a `metadata:` field whose value is **single-line JSON**
  (parsed with `json.loads`, not YAML):

  ```
  metadata: {"nanocat": {"requires": {"bins": ["ssh", "rsync"]}, "env": ["API_KEY"], "always": false}}
  ```

  - `requires.bins` — CLI names checked on `PATH`; if any is missing the skill is hidden.
  - `requires.env` — env vars that must be set, same effect.
  - `always: true` — keep the skill body loaded every turn (use sparingly; costs context).

  Nested YAML under `metadata:` (indented `key: value`) is **not** read — it must be JSON.

- Built-in skills are copied into `workspace/skills/` on startup and never overwritten.
  Edit the **workspace copy** to change behavior; the bundled template only seeds new
  workspaces.

## Writing the SKILL.md

After understanding intent (what should it enable, when should it trigger, what's the
output), fill in:

- **name** — matches the directory.
- **description** — single line, what + when. NanoCat tends to *under*-trigger skills, so be
  a little pushy: instead of "Builds a dashboard", write "Builds a dashboard. Use this
  whenever the user mentions dashboards, metrics, or wants to display data, even if they
  don't say 'dashboard'." List concrete trigger phrases.
- **the body** — imperative instructions. Explain *why* a step matters rather than barking
  `ALWAYS`/`NEVER`; a modern model with good theory of mind follows reasoning better than
  rigid rules. If you catch yourself writing all-caps musts, reframe.

### Anatomy

```
skill-name/
├── SKILL.md          (required: frontmatter + instructions)
├── scripts/          (executable code for deterministic/repetitive work)
├── references/       (docs the body points to, loaded on demand)
└── assets/           (templates, icons, files used in output)
```

### Progressive disclosure

Three loading levels — exploit them to keep things lean:
1. **name + description** — always in context. Keep tight.
2. **SKILL.md body** — loaded when the skill triggers. Aim under ~500 lines.
3. **Bundled files** — read only when needed; scripts run via `exec` without loading.

If the body grows past ~500 lines, split detail into `references/*.md` and point to them
from SKILL.md ("for the full schema see `references/schema.md`"). For multi-variant skills,
one reference file per variant so only the relevant one gets read:

```
cloud-deploy/
├── SKILL.md          (workflow + which-variant selection)
└── references/{aws,gcp,azure}.md
```

### Patterns

Define a fixed output shape inline when the skill needs one:

```markdown
ALWAYS use this template:
# [Title]
## Summary
## Findings
```

Show input→output examples for transforms:

```markdown
Input: Added user auth with JWT
Output: feat(auth): implement JWT-based authentication
```

### Safety

Skills must not contain malware, exploit code, or anything that would surprise the user
versus what the skill claims to do. Decline requests to build deceptive skills or ones
meant for unauthorized access or data exfiltration. (Benign roleplay skills are fine.)

## Testing a skill in NanoCat

NanoCat is headless — no browser, no eval-viewer. Test by execution:

1. Write 2-3 realistic prompts a real user would actually type (concrete details: file
   paths, names, values), and confirm them with the user.
2. For each, `subagent_spawn` a task that points at the skill and runs the prompt; for a
   baseline, spawn the same prompt *without* the skill path. Launch them together so they
   finish around the same time, then `subagent_gather`.
3. Read the **transcripts**, not just the final output. Watch for the skill making the model
   waste steps, write throwaway helper scripts (a recurring helper across runs should be
   bundled into `scripts/`), or ignore instructions.
4. Present outputs to the user inline (save any produced files and give the path). Ask what
   to change.

If subagents aren't available, just follow the skill's own instructions yourself on each
prompt, one at a time — less rigorous, but the user's review compensates.

## Improving

- **Generalize from feedback.** You're tuning on a few examples to serve a skill used many
  times. Don't overfit with fiddly per-example rules; if an issue is stubborn, try a
  different framing or metaphor rather than another rigid MUST.
- **Keep it lean.** Cut instructions that don't pull their weight. If the transcript shows
  the skill causing unproductive work, remove the offending part and re-test.
- **Bundle repeated work.** If every test run independently writes the same `create_docx.py`
  or `build_chart.py`, write it once into `scripts/` and tell the skill to call it.

Re-run the test prompts after each change. Stop when the user is happy, feedback is empty,
or you've stopped making meaningful progress.

## Tuning the description for triggering

The description decides whether NanoCat consults the skill at all. NanoCat only reaches for
a skill on tasks it can't trivially do itself — "read this file" won't trigger anything
regardless of wording, so test with substantive, multi-step prompts. To tune manually:
write ~10 should-trigger and ~10 should-not-trigger prompts (the negatives are most useful
as near-misses that share keywords but need something else), check which the current
description would catch, and adjust wording. Keep the result a single line.

## Validate and package

Helper scripts live in `scripts/` (run from the skill-creator directory; they need PyYAML):

```bash
python scripts/quick_validate.py <path/to/skill>     # checks frontmatter + structure
python -m scripts.package_skill <path/to/skill>       # writes a distributable .skill zip
```

Note: `quick_validate.py` parses frontmatter with full YAML (`yaml.safe_load`), which is
*more* permissive than NanoCat's runtime loader. Passing validation does **not** guarantee
the description is single-line — enforce that rule yourself per the loader notes above.

## Reference files

- `references/schemas.md` — JSON structures used by the optional eval scripts.
