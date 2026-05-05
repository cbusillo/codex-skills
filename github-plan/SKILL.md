---
name: github-plan
description: Use when the user asks for a plan, durable work tracking, roadmap, project/workstream planning, stale or duplicate plan cleanup, GitHub issue-backed planning, cross-repo blockers, milestones, labels, Projects, or replacing local Codex plan files with GitHub issues. Think locally first, then promote durable plans to GitHub through scripted issue relationships and compact lookups.
metadata:
  short-description: Plan durable work with GitHub issues
---

# GitHub Plan

## Purpose

Use GitHub issues as the durable planning database and keep local/chat planning
ephemeral. The model should reason with the user first, then create or update
GitHub issues only when the work should survive the current conversation.

This skill supersedes the local file-backed `plan` skill for normal planning.
Use local plan files only when the user explicitly requests an offline/local
plan or the work must not be written to GitHub.

## Operating Model

- Think in chat first; do not immediately create GitHub issues for fuzzy ideas.
- Search before create; update an existing issue when the intent overlaps.
- Promote durable work to one canonical issue with the `plan` label.
- Optimize for the user finishing work, not for cataloging every possible idea.
- Keep issue bodies structured and current; `Current Status` is the recovery
  point for future sessions.
- Use native GitHub dependencies and sub-issues for relationships, including
  cross-repo relationships.
- Use Projects as a view layer, not the source of truth.
- Use milestones for release/phase/date buckets only.
- Avoid dynamic labels. Use the fixed configured label set; ask before adding a
  new taxonomy.
- Prefer `Refs #123` from PRs unless the user explicitly wants auto-close or the
  issue is an internal task that can be conclusively closed.

## Human Workflow

If `.local/github-plan.md` exists, read it before creating, routing, or updating
durable plan issues and follow its private local conventions.

Use the `Manager` Project field for ownership by default when configured. Assign
or mention a manager only when their attention is actually needed. Private repo
items require repo access; Project access alone may not reveal private issue
contents.

Manager routing should live in workspace planning config such as
`~/.code/github-planning.json` or `~/.codex/github-planning.json` under
`workflow.default_manager` and `workflow.repo_managers`; update that JSON when
manager routing changes.

Default session ritual:

1. Orient from the active issue: finish line, current status, next action,
   blockers.
2. Pick one next action and start work.
3. When a new idea appears, classify it as do now, acceptance criterion, related
   issue, or later. Do not pivot without an explicit decision.
4. Before pausing, update `Current Status` so the user can resume quickly.
5. Let Project fields handle management state; keep the user in maker mode.

Use `Focus` in the Project as a simple attention lane:

- `Now`: one thing the user and Code are actively trying to finish.
- `Next`: ready after Now or after the manager chooses it.
- `Waiting`: blocked or awaiting an external decision/event.
- `Later`: real but intentionally out of focus.

Prefer at most one `Now` item unless the user or manager explicitly chooses a
parallel track.

## Token Discipline

Always use `scripts/gh-plan.py` instead of ad hoc `gh` calls for planning state.
It returns compact JSON and avoids loading issue bodies unless needed.

Default read path:

```sh
~/.code/skills/github-plan/scripts/gh-plan.py --repo OWNER/REPO index
~/.code/skills/github-plan/scripts/gh-plan.py --repo OWNER/REPO show 123
```

Only use `show --full` when editing broad context or when the requested answer
depends on full prose. Prefer `update-section` over rewriting the whole body.

## Issue Shape

Durable planning issues should use these headings:

```markdown
## Objective
## Finish Line
## Current Status
## Scope
## Acceptance Criteria
## Relationships
## Validation
## Decisions
## Open Questions
```

Keep `Current Status` short and concrete:

```text
State:
Next action:
Blocked by:
Last verified:
```

Keep `Finish Line` observable. If the finish line is vague, narrow it before
creating sub-issues or adding Project fields.

## Relationship Semantics

- `blocked-by`: current issue cannot move until the target changes.
- `blocks`: current issue is holding up the target.
- `subissue`: target is part of the current workstream and can be tracked
  independently.
- `related`: useful context without execution dependency.

Use native relationships first. Body references are explanatory, not canonical.

Examples:

```sh
~/.code/skills/github-plan/scripts/gh-plan.py --repo OWNER/APP_REPO \
  link 42 blocked-by OWNER/PLATFORM_REPO#17

~/.code/skills/github-plan/scripts/gh-plan.py --repo OWNER/APP_REPO \
  link 42 subissue OWNER/PLATFORM_REPO#17
```

## Projects

Projects are optional views. Add plans to Projects when the repo/workspace config
defines a default Project or the user asks for Project tracking.

Use only a few human-facing fields:

- `Focus`: Now, Next, Waiting, or Later.
- `Manager`: configured human owner or reviewer.
- `Finish Line`: compact observable done state.
- `Roadmap Start`: coarse planning anchor for when work is or becomes active.
- `Roadmap Target`: realistic target window when one is useful and honest.

Do not duplicate the whole issue body into Project fields.

### Roadmap Dates

When maintaining planning Projects, keep roadmap dates useful for LLM-assisted
coding without turning them into fake promises. Coding slices can often be dated
in days rather than weeks, but integration, validation, external feedback, and
UI/product judgment still need calendar space.

- `Now`: set `Roadmap Start` to today or the actual start date; set
  `Roadmap Target` to the plausible finish window when one exists.
- `Next`: set near-term dates only when the item is truly pickable soon.
- `Waiting`: date only when the blocker has a realistic response, retry, or
  revisit window.
- `Later`: usually leave dates blank unless intentionally scheduled.
- Prefer week or month anchors when exact dates would be artificial.

Treat roadmap dates as planning anchors, not commitments. Move or clear stale
dates when reality changes.

```sh
~/.code/skills/github-plan/scripts/gh-plan.py project-list --owner OWNER
~/.code/skills/github-plan/scripts/gh-plan.py \
  --repo OWNER/REPO project-add 123 \
  --owner OWNER --project "Roadmap"
~/.code/skills/github-plan/scripts/gh-plan.py \
  --repo OWNER/REPO project-set 123 \
  --focus Now --manager @manager-login --finish-line "Observable done state"
```

If GitHub reports missing `project` or `read:project` scope, say so and continue
with issue-backed planning.

## Workflow

1. Decide whether the work is ephemeral or durable. Keep ephemeral planning in
   chat; promote durable work to GitHub.
2. Resolve the repo and run `index` or `search` before creating anything.
3. Draft or revise the plan with the user in chat when the shape is still
   unclear.
4. Use `create` for a new canonical issue only after dedupe. Include a finish
   line and set Project fields when the work enters the board.
5. Use `update-section` to keep `Current Status`, acceptance criteria,
   decisions, and validation current.
6. Use `link`, `unlink`, and `deps` for blockers and cross-repo relationships.
7. Add to Projects only as a view/tracking layer.
8. When work completes, update status and close or relabel the issue; do not
   leave stale local plan files behind.

## Script Commands

- `index`: compact plan issue list, no bodies.
- `search`: compact issue search, no bodies.
- `show`: selected sections by default; `--full` for full body.
- `create`: creates a deduped durable plan issue and fixed labels when missing;
  use `--plan-status none|active|blocked|stale|done` so migration/import work
  does not mark parked plans active by accident.
- `update-section`: patches one markdown section.
- `link` / `unlink`: manages native dependencies and sub-issues.
- `deps`: compact dependency and sub-issue view.
- `close`: closes a completed plan, relabels it `plan:done`, sets Project
  status to Done, and clears Focus. Before closing, update `Current Status` so
  the issue records why it is done or no longer active.
- `project-list` / `project-add` / `project-set`: Project view integration.
- `ensure-labels`: creates only the fixed configured planning labels.

Read `references/config-schema.md` only when changing repo/workspace planning
configuration.
