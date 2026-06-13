---
name: repo-readiness
description: Use when the user asks whether a change, branch, PR, or workstream is ready to review, merge, ship, pause, or hand off, or asks to run gates, validate changes, check CI/PR readiness, inspect IDE warnings, verify UI, or identify blockers. Orchestrates local, GitHub, inspection, and browser evidence into a concise readiness report.
metadata:
  short-description: Check repo readiness to ship
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
   When `.github/github.json` defines `qualityGate.inspection`, readiness for a
   code change must include JetBrains evidence from the delegated helper
   (`closeout` for final readiness) or an explicit not-run reason. If that
   inspection config is blank, missing, contradictory, or surprising, do not
   silently invent durable policy: use a safe one-off `changed_files` default
   only when the helper can infer a correct route, and ask the user before
   changing repo policy or treating a suspicious config as authoritative.

If the shared Launchplane context helper is present and configured, call it once
as optional readiness context for the active repo/branch/PR:

```bash
skills_home="${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/skills"
uv run "$skills_home/launchplane/scripts/launchplane-context.py" --repo OWNER/REPO
```

Use `available` context only as a hint for product mapping, preview readiness,
Every Code state, deploy evidence, or source-of-truth links. Treat `no_context`,
`unavailable`, `unauthorized`, `invalid`, or helper failure as normal absence and
continue with local/GitHub readiness checks. Do not print raw helper stderr or
copy helper payloads into readiness output.

If you inspect worktrees, ignore Codex Desktop or Every Code auto-review
worktrees under `~/.code/working/<repo>/branches/auto-review*` unless the user's
task is specifically about that review. They are detached external review
context and should not affect readiness for the active repo/branch.

When background auto-review results or ledgers are available in session context
or repo tooling, treat them as review evidence only after matching the review
target to the active branch, PR, and head SHA. Do not declare a branch, PR,
release, or handoff green while blocking findings against the current target are
unresolved or while a review for the current target is still in-flight. If a
finding points at a detached `auto-review*` worktree or older snapshot, verify it
against current `HEAD` before treating it as blocking. This does not mean
detached auto-review worktrees are dirty local state; only relevant review
findings/status matter.

5. During implementation, choose the narrowest useful gate that matches the
   change and risk. Before saying code is ready, broaden to the largest
   practical gate for the repo and change.
6. If GitHub state matters, use `github` for PR checks, Actions,
   review status, labels, deploy health, and mergeability.
   For stacked PRs, include whether a rollup/integration PR would be safer or
   faster than merging each layer and rerunning expensive checks repeatedly,
   unless repo metadata or task context says Launchplane owns the merge train.
   For Launchplane-managed trains, verify or route through `launchplane` instead
   of recommending a hand-built GitHub rollup.
7. If UI was touched, use `browser-ui-review` for browser-visible validation.
8. If security is in scope, use `security-review` explicitly; do not silently
   turn normal readiness into a full security audit.
9. Report readiness concisely. If the user asked for handoff, wrap-up, or
   safe-to-exit, continue into `work-closeout` after the readiness answer is
   established.

## Readiness To Closeout Handoff

When `work-closeout` will run after this skill, make the readiness answer easy
to consume instead of asking closeout to rediscover gate state. Include these
fields in chat, a PR comment, or the owning issue when durable state is needed:

- Status: ready, not ready, partially ready, or blocked.
- Required gates: the checks inferred from `.github/github.json`, repo docs, CI,
  and the changed surface.
- Passed, failed, pending, and not-run evidence with concrete reasons.
- Metadata/docs impact: whether `.github/github.json` or docs changed, were
  checked, are stale, or were intentionally not updated.
- Next action: the smallest step that would change readiness.

This handoff is evidence for `work-closeout`; it is not cleanup. Do not delete
artifacts, remove worktrees, close planning issues, or claim safe-to-exit from
this skill alone.

Both this skill and `work-closeout` read `.github/github.json` with the same
schema expectations: `qualityGate`, `docs`, `metadataFreshness`, `cleanup`,
`importantWorkflows`, repo relationships, health signals, and ownership or
Launchplane routing when present. Readiness uses those fields to decide what
must be verified; closeout uses the same fields to decide what final evidence,
metadata updates, and cleanup remain.

Use `../references/every-code-formatting.md` for readiness reports and durable
readiness comments: lead with status, cite concrete evidence, and keep skipped
or pending checks explicit without copying large logs.

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

For code-change readiness, inspection evidence is one of: a clean
`jetbrains-inspection closeout` result with scope and route, actionable findings
that were fixed or tracked, or a concrete not-run reason such as unavailable IDE,
blocked indexing, docs-only scope, or user-approved parking. Missing inspection
evidence for a configured code gate means the readiness answer is not fully
ready.

Existing lint/inspection noise is not an invisible background condition. Fix
real findings the right way when straightforward or in the affected area. If
findings are broad but real, call them out and decide whether to include a
cleanup pass or track a focused cleanup item. If a finding is a false positive or
cannot be fixed cleanly, discuss an explicit suppression, baseline, or config
change.

Reviewability is part of readiness. Unless a change is mostly mechanical, treat
diffs over roughly 800 changed lines as a review-risk signal; for complex logic
changes, prefer stages under roughly 500 changed lines. If a change is larger,
explain whether it can be split into reviewable stages and identify the smallest
coherent stage to land first, based on the actual diff, dependencies, and
affected call sites.

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

If the repo inspection config is blank or feels wrong, ask the user when the
choice affects durable repo policy or readiness. Examples: configured IDE does
not match the open project, configured scope is disproportionate to the change,
or configured paths point away from the active worktree. For a one-off local
check with no durable policy change, prefer the helper's safe inferred route and
`changed_files` scope, and report that assumption.

## Output Format

Use a compact readiness report:

- Status: ready / not ready / partially ready / blocked.
- Passed: checks, inspections, browser review, CI, or manual evidence.
- Failed: actionable failures with file/run links where useful.
- Pending: CI, review, deploy, indexing, or user decisions still in flight.
- Not run: checks skipped or unavailable, with reason.
- Next: the smallest concrete step to reach readiness.

If there are no blockers, say so plainly.
