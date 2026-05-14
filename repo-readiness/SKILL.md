---
name: repo-readiness
description: Use when the user asks whether a change, branch, PR, or workstream is ready to review, merge, ship, pause, or hand off, or asks to run gates, validate changes, check CI/PR readiness, inspect IDE warnings, verify UI, or identify blockers. Orchestrates local, GitHub, inspection, and browser evidence into a concise readiness report.
---

# Repo Readiness

Use this skill to answer whether a change, branch, PR, or workstream is ready.
It is an orchestrator: follow repo-specific instructions first, then call the
relevant focused skills or tools instead of duplicating their details.

When the user asks whether work is done, ready to hand off, or safe to exit,
use this skill first for gates and evidence, then use `work-closeout` for final
hygiene, artifact cleanup, and durable parking state. Do not force a single
skill when both readiness and closeout are required.

## Core Goal

Leave the user with a truthful readiness answer:

- What checks are required?
- What passed?
- What failed or is pending?
- What was intentionally not run?
- What blocks review, merge, ship, or handoff?

## Workflow

1. Identify the repo, branch, active task, changed files, and whether a PR/issue
   is in play.
2. Check `.github/github.json` before inferring gates.
   If it has a `qualityGate` block, use it for how to run tests, lint/static
   analysis, format checks, typechecks, builds, and IDE/static inspections. Use
   `qualityGate.docsRequiredWhen` for docs freshness triggers. If it has a
   `docs` block, prefer `docs.index` and relevant `docs.*` paths as the docs
   entry points. Fall back to repo instructions (`AGENTS.md`, README,
   docs/policies/gates), CI files, and manifests for any unset metadata fields.
   If a durable command is confidently inferred, cite the source and propose a
   metadata addition rather than auto-writing it.
3. Inspect local state:

```bash
git status --short --branch
```

4. For code changes, use `jetbrains-inspection` during the edit loop when a
   JetBrains IDE project is available or required. Start with changed files or
   the touched directory so the checked scope stays aligned with the session's
   actual edits. Repeat after later edits that could affect inspected code.
   Record explicit not-run reasons when inspections are unavailable or
   intentionally parked.

If the shared Launchplane context helper is present and configured, call it once
as optional readiness context for the active repo/branch/PR:

```bash
~/.code/skills/launchplane/scripts/launchplane-context.py --repo OWNER/REPO
```

Use `available` context only as a hint for product mapping, preview readiness,
Every Code state, deploy evidence, or source-of-truth links. Treat `no_context`,
`unavailable`, `unauthorized`, `invalid`, or helper failure as normal absence and
continue with local/GitHub readiness checks. Do not print raw helper stderr or
copy helper payloads into readiness output.

If you inspect worktrees, ignore Codex Desktop auto-review worktrees under
`~/.code/working/<repo>/branches/auto-review*` unless the user's task is
specifically about that review. They are detached external review context and
should not affect readiness for the active repo/branch.

5. During implementation, choose the narrowest useful gate that matches the
   change and risk. Before saying code is ready, broaden to the largest
   practical gate for the repo and change.
6. If GitHub state matters, use `github` for PR checks, Actions,
   review status, labels, deploy health, and mergeability.
   For stacked PRs, include whether a rollup/integration PR would be safer or
   faster than merging each layer and rerunning expensive checks repeatedly.
7. If UI was touched, use `browser-ui-review` for browser-visible validation.
8. If security is in scope, use `security-review` explicitly; do not silently
   turn normal readiness into a full security audit.
9. Report readiness concisely. If the user asked for handoff, wrap-up, or
   safe-to-exit, continue into `work-closeout` after the readiness answer is
   established.

## Gate Selection

- Docs-only: validate links/rendering when practical; do not run full code gates
  unless docs describe executable behavior. Prefer `docs.index` when repo
  metadata provides it.
- Narrow code change: run targeted tests/checks for touched behavior.
- Shared contract, deployment, release, or security-sensitive change: run the
  broader repo gate and any required inspection/security checks.
- Frontend/UI change: run browser validation in addition to build/type/test
  checks required by the repo.
- Docker image change: prefer real image build/smoke/inspection over static
  Dockerfile review when downstream behavior changes.

Quality default: all code changes should trend toward zero known lint/static
analysis and IDE inspection noise. Run narrow gates while iterating, but before
declaring a code change ready, safe to merge, safe to ship, or ready to hand off,
run broad practical gates when tooling exists: normally whole-repo lint/static
analysis and whole-project IDE inspection. Use downstream/related repo gates
when contracts, shared packages, deployment behavior, or cross-repo ownership
changes.

If a broad gate is too slow, unavailable, blocked by IDE indexing/tooling,
likely destructive, or disproportionate for a docs-only change, report it as not
run with the reason. Ask before running gates that mutate shared environments or
external resources. If linting a file for the first time, dry-run first when the
environment supports it.

Existing lint/inspection noise is not an invisible background condition. Fix
real findings the right way when straightforward or in the affected area. If
findings are broad but real, call them out and decide whether to include a
cleanup pass or track a focused cleanup item. If a finding is a false positive or
cannot be fixed cleanly, discuss an explicit suppression, baseline, or config
change.

When work changes docs routing, validation commands, lint/inspection routing,
required docs conditions, important workflows, health endpoints, repo
relationships, cleanup policy, or ownership boundaries, check whether
`.github/github.json` is stale.
Do not treat manifest-owned dependency or inventory changes as a reason to copy
those facts into workflow metadata; point to the canonical manifests or docs
instead.
Report metadata drift as a readiness warning or blocker only when it changes
what "ready" means for the current task.

For every non-trivial code change, perform a docs-impact check. If docs-required
triggers match, inspect the relevant docs and update them when stale. If no docs
update is needed, say why briefly in readiness output.

## JetBrains IDE And Inspections

Delegate JetBrains/PyCharm/IntelliJ/WebStorm inspection execution and triage to
`jetbrains-inspection`. Treat stale results, incomplete capture, indexing,
session drift, unavailable IDE, ambiguous routing, or wrong-worktree routing as
not-clean states. Do not add suppressions, disable inspections, or change IDE
inspection profiles without explicit approval unless the repo already has an
approved convention.

## Output Format

Use a compact readiness report:

- Status: ready / not ready / partially ready / blocked.
- Passed: checks, inspections, browser review, CI, or manual evidence.
- Failed: actionable failures with file/run links where useful.
- Pending: CI, review, deploy, indexing, or user decisions still in flight.
- Not run: checks skipped or unavailable, with reason.
- Next: the smallest concrete step to reach readiness.

If there are no blockers, say so plainly.
