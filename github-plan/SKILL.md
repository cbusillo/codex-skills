---
name: github-plan
description: Use when the user asks for a plan, durable work tracking, roadmap, workstream planning, GitHub issue-backed planning, cross-repo blockers, milestones, Projects, or replacing local plans with GitHub issues. Think in chat first, then promote durable plans to GitHub with parent issues, sub-issues, blockers, and compact scripted lookups.
metadata:
  short-description: Plan durable work in GitHub issues
---

# GitHub Plan

## Purpose

Use GitHub issues as the durable planning database. Keep chat planning
ephemeral until the work should survive the current conversation.

Optional surfaces such as GitHub Projects, LaunchPlane, or other local planning
views may make work easier to scan, prioritize, or recover, but they are not
separate planning backends. GitHub issues remain canonical for plan prose,
relationships, blockers, labels, validation, and completion state.

This skill supersedes local file-backed plans for normal GitHub-backed planning.
Use local plan files only when the user explicitly asks for an offline/local
plan or the work must not be written to GitHub.

## Operating Model

- Think in chat first; do not immediately create issues for fuzzy ideas.
- Search before creating; update an existing issue when intent overlaps.
- Promote durable work to one canonical issue with the configured planning
  label, usually `plan`.
- Keep issue bodies structured and current; `Current Status` is the recovery
  point for future sessions and the preferred durable handoff surface for
  GitHub-backed planning work.
- Use native GitHub dependencies and sub-issues for relationships, including
  cross-repo relationships.
- Use Projects and other configured surfaces as view layers, not sources of
  truth.
- Use milestones for release, phase, or date buckets only.
- Avoid ad hoc label taxonomies; ask before creating new labels.
- Prefer `Refs #123` from PRs unless the user explicitly wants auto-close or the
  issue is an internal task that can be conclusively closed. `Refs` is
  deliberately non-closing; after merge, sweep referenced issues and close only
  the ones whose finish line was actually satisfied.
- Optimize for the user finishing work, not for cataloging every possible idea.

## Local Conventions

If `.local/github-plan.md` exists, read it before creating, routing, or updating
durable plan issues and follow its private local conventions.

Use configured owner or manager routing when available. Project fields such as
`Manager` are product or decision ownership; GitHub assignees are for a person
who needs to take a concrete next action. Mention a person only when their
attention is needed now.

## Tooling

Reuse the sibling `github` skill's helpers instead of duplicating scripts:

- `../github/scripts/gh-plan.py` for compact planning issue and Project operations.
- `../github/scripts/gh-pr.py` for PR status, checks, merge, and rate-limit
  reads when planning work needs PR evidence. The helper is REST-first for
  normal PR orientation and owns quota-aware degraded behavior.
- `../github/scripts/gh-issue` and `../github/scripts/gh-comment` for safe
  multiline writes.
- `../github/references/issue-templates.md` and
  `../github/references/github-projects.md` for issue shape and Project fields.

If the helpers are unavailable, use `gh` directly with body files and compact
JSON reads. Do not fall back to repo docs or local plan files for durable
GitHub-backed planning.

For close comments, use `../github/scripts/gh-issue close` so stdin is posted as
a body-file-backed comment before the issue is closed. For other multiline
writes, prefer body files or stdin. Do not pass escaped `\n` through
shell-quoted flags. Follow `../references/every-code-formatting.md` when
writing durable issue bodies, planning comments, handoffs, or closeout evidence.

## Broad Workstream Rule

Create a parent issue plus sub-issues when a plan has independent tracks. Do not
hide broad work inside one checklist.

Use sub-issues when any two are true:

- touches three or more modules, repos, systems, or ownership areas
- has independent sequencing, blockers, or parallelizable tracks
- includes research, implementation, validation, and policy/design decisions
- has work that can finish or be reviewed independently
- needs roadmap/focus tracking beyond the current session

Parent issues should hold intent, finish line, dependency order, and recovery
state. Child issues should each have one scoped finish line and one next action.

## Default Session Ritual

1. Orient from the active issue: finish line, current status, next action, and
   blockers.
2. Pick one next action and start work.
3. When a new idea appears, classify it as do now, acceptance criterion, related
   issue, sub-issue, blocker, or later. Do not pivot without an explicit
   decision.
4. Before pausing, update `Current Status` or the owning PR/issue comment so the
   user can resume quickly. Do not leave a local handoff file as the only
   recovery source for GitHub-backed work.
5. Keep the user in maker mode; let Projects or other surfaces handle management
   state.

When another repo workflow is waiting on CI, deploy, review, or post-merge
health, keep the main checkout available for verification and parallelize safely:
use read-only exploration or isolated work only for independent planning or
implementation prep, then return to the waiting workflow before calling it done.

Use Focus lanes when configured:

- `Now`: one thing the user and Code are actively trying to finish.
- `Next`: ready after Now or after the manager chooses it.
- `Waiting`: blocked or awaiting an external decision/event.
- `Later`: real but intentionally out of focus.

Prefer at most one `Now` item unless the user explicitly chooses parallel work.

Use planning status labels with narrow meanings:

- `plan:active`: actionable now.
- `plan:blocked`: blocked by a real, current dependency, preferably represented
  by a native GitHub `blocked-by` relationship to an open issue.
- `plan:waiting`: intentionally parked on non-issue evidence, live validation,
  a user/customer/maintainer decision, or a future real-world event.
- `plan:stale`: needs review before it should guide work.
- `plan:done`: completed or superseded.

Do not use `plan:blocked` merely because work is not currently in focus. If an
issue has no open native blocker, prefer `plan:waiting` and make `Current
Status` say `Waiting for:` or `Parked until:` with the concrete condition.
If a non-issue condition truly blocks execution, include `Blocked by: No native
issue blocker; waiting for ...` so future audits do not chase missing edges.

If LaunchPlane or another local context helper is configured and useful for
orientation, call it once before or alongside `index`. Treat unavailable,
unauthorized, invalid, or missing context as normal absence and continue with
GitHub-only planning. Use local surface output only as a hint for source links,
readiness, blockers, and next inspection targets; do not copy private context
payloads into public issues, PRs, or handoffs unless they have been reviewed for
public safety.

## Token Discipline

Prefer `../github/scripts/gh-plan.py` for planning state. It returns compact JSON
and avoids loading issue bodies unless needed.

Project v2, native sub-issues, and native dependency operations may require
GraphQL. Before batching those operations, check rate limits when failures look
quota-related. If GraphQL is exhausted but REST/core is available, keep issue
body/status updates moving through REST-backed helpers and record Project or
native relationship updates as waiting rather than retrying until the LLM
workflow stalls.

- Use `index` or `search` before creating.
- Use `show` for selected sections; use `show --full` only when broad prose is
  required.
- Use `update-section` instead of rewriting the whole body.
- Use `../github/scripts/gh-issue` and `../github/scripts/gh-comment` for multiline
  Markdown bodies.

## Issue Shape

Durable planning issues should use the headings in
`../github/references/issue-templates.md`.

Keep `Current Status` short and concrete:

```text
State:
Next action:
Blocked by:
Waiting for:
Last verified:
```

Use `Blocked by:` for issue dependencies and `Waiting for:` or `Parked until:`
for non-issue conditions. Avoid listing completed work as a blocker; move it to
`Relationships` as completed or historical context.

Keep `Finish Line` observable. If the finish line is vague, narrow it before
creating sub-issues or Project fields.

## Relationship Semantics

- `blocked-by`: current issue cannot move until the target changes.
- `blocks`: current issue is holding up the target.
- `subissue`: target is part of the current workstream and can be tracked
  independently.
- `related`: useful context without execution dependency.

Use native relationships first when the helper/API supports them. Body
references are explanatory, not canonical.

## Related Issue Sweep

Stale GitHub planning state is a regression source. Before closeout, handoff,
or declaring a workstream done, search for related, duplicate, stale, parent,
sub-issue, blocker, and PR-linked issues that might still describe the old
state.

- Update every related issue whose `Current Status`, labels, blockers,
  relationships, or acceptance criteria changed.
- Close or relabel stale duplicate issues when they no longer represent current
  work.
- Prefer updating the canonical parent and linked sub-issues over leaving
  corrective context only in chat or PR comments.
- If an old issue might mislead a future agent, treat it as unfinished cleanup,
  not optional housekeeping.

After a canonical PR merges, inspect the issues it references with `Refs`,
`Closes`, `Fixes`, or `Resolves`. `Refs` should remain non-closing by default.
For each referenced issue, either close it with `../github/scripts/gh-issue
close` and a multiline evidence comment when the merge conclusively satisfies
the finish line, or update/comment the remaining state and leave it open.

Local handoff documents are not durable planning records unless the user asked
for offline/private handoff. If a session created `handoff*.md` or similar
scratch files, migrate recovery-critical content into the owning GitHub issue or
PR comment before closeout and then delete or explicitly preserve the file.

## Projects And Surfaces

Planning surfaces are optional views over GitHub issue-backed plan data. They
may help people choose work, inspect roadmap shape, or recover context, but they
must not replace the GitHub issue as the durable record.

Add plans to Projects when repo/workspace config defines a default Project or
the user asks for Project tracking. Use only a few human-facing fields: `Focus`,
`Manager`, `Finish Line`, `Roadmap Start`, and `Roadmap Target`.

Treat roadmap dates as planning anchors, not commitments. Keep them useful for
LLM-assisted coding by using honest day, week, or month windows and moving or
clearing stale dates when reality changes.

## Closeout Check

Before saying a plan is captured, verify:

- existing issues were searched
- parent issue exists for a broad workstream
- sub-issues exist when the Broad Workstream Rule applies
- blockers/dependencies are represented
- stale, duplicate, related, and PR-linked issues were swept and reconciled
- `Current Status` and next action are concrete
- docs are not being used as active plan state

## Workflow

1. Decide whether the work is ephemeral or durable.
2. Resolve the repo and run `index` or `search` before creating anything.
3. Draft or revise the shape with the user in chat when unclear.
4. Create or update the parent plan issue.
5. For broad workstreams, create scoped sub-issues and link or reference them.
6. Use blockers, sub-issues, and related links to represent the execution graph.
7. Add configured Project fields only as view/tracking layers.
8. Keep `Current Status`, acceptance criteria, decisions, and validation current.
9. When work completes, update status and close or relabel the issue; do not
   leave stale local plan files behind.
