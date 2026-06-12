---
name: work-closeout
description: Use when the user asks to wrap up, clean up, close out, pause, park, hand off, determine what remains before stopping, preserve plan direction for the next session, update or remove stale plans/handoffs, reconcile issue graph state, remove transient artifacts, or asks whether they can exit. Coordinates GitHub plan cleanup, safe git/worktree hygiene, artifact cleanup, and final state summaries.
metadata:
  short-description: Close out workstreams cleanly
---

# Work Closeout

Use this skill to leave a workstream tidy and understandable. It is about
cleanup and handoff, not proving readiness; use `repo-readiness` when the main
question is whether checks pass or a PR can ship.

When the user asks whether work is done, ready to hand off, or safe to exit,
compose the skills in order: use `repo-readiness` first for gates and evidence,
then use this skill for final hygiene, cleanup, and parking state. Do not force
a single skill when both readiness and closeout are required.

## Core Goal

Leave the user with a truthful closeout answer:

- What is done?
- What remains?
- What cleanup happened?
- What artifacts or plans were removed, preserved, or updated?
- Is it safe to pause, exit, or hand off?

## Workflow

1. Identify the repo, branch, active task, and whether a PR/issue/plan is in
   play.
2. If `.github/github.json` exists, read it and check metadata
   freshness. Compare the current work with
   `metadataFreshness.updateWhen` and
   with common triggers: docs routing, validation gates, primary commands,
   important workflows, health endpoints, repo relationships, JetBrains
   expectations, cleanup policy, and ownership boundaries. If metadata should
   change, update it only with approval; otherwise record a concrete remaining
   item before saying it is safe to exit.

   Also read `cleanup`. During closeout, run or report repo-configured commands
   with `when: "routine"` as closeout evidence. Commands marked `explicit`,
   `cold`, `aggressive`, or any unrecognized value are report-only unless the
   user explicitly asks for that cleanup. Never run a cleanup command whose
   effect is unclear; record the not-run reason instead.
3. Inspect local state:

   ```bash
   git status --short --branch
   git worktree list
   ```

   If the active branch is clean and behind its configured upstream, run
   `git pull --ff-only` before final closeout and re-check status. Do this only
   for clean fast-forwardable branches. If the branch is dirty, ahead, diverged,
   lacks an upstream, or the fast-forward fails, do not pull further; report the
   state and the next safe action instead.

   If the shared Launchplane context helper is present and configured, call it
   once as optional closeout context for the repo/workstream:

   ```bash
   skills_home="${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/skills"
   uv run "$skills_home/launchplane/scripts/launchplane-context.py" --repo OWNER/REPO
   ```

   Use `available` context to notice pending Every Code work, preview readiness,
   deploy/product evidence, or source-of-truth links that should be reflected in
   the closeout. Treat `no_context`, `unavailable`, `unauthorized`, `invalid`,
   or helper failure as normal absence. Do not block safe-to-exit only because
   Launchplane context is unavailable, and do not copy raw helper payloads into
   issues, PRs, or final summaries.

4. If GitHub state matters for closeout, use `github` for PR,
   Actions, labels, merge state, post-merge verification, GitHub
   security/quality signals, and safe branch/worktree cleanup.
5. Use `github-plan` for durable plan state, blockers, stale/duplicate plan
   cleanup, and Project planning state. Use legacy `plan` only for explicit
   local/offline plan files that already exist or that the user asks to keep.
6. If design collaboration was part of the work, make sure accepted direction,
   browser QA evidence, tradeoffs, and remaining design work are captured in the
   relevant GitHub planning issue or PR.
7. Clean only artifacts clearly created by the current work: transient logs,
   screenshots, temp scripts, generated scratch files, generated caches, stopped
   test containers, or other consumed temporary files.
   If the repo still has legacy `handoff*.md` files or files matching
   `cleanup.handoffArtifacts.temporaryGlobs`, delete or migrate them once their
   content is captured in the owning GitHub issue or PR comment unless they are
   intentionally preserved as committed docs.
8. Do not remove user artifacts, broad system caches, unrelated untracked files,
   or remote resources without explicit approval.
9. Report final state concisely.

## Consuming Readiness Evidence

When `repo-readiness` has just run, consume its handoff instead of rerunning the
same gate discovery by default. Verify the evidence is fresh for the current
branch, PR, and commit, then use it to decide whether closeout can proceed:

- Status: ready, not ready, partially ready, or blocked.
- Required gates: checks inferred from `.github/github.json`, repo docs, CI, and
  the changed surface.
- Passed, failed, pending, and not-run evidence with concrete reasons.
- Metadata/docs impact: whether `.github/github.json` or docs changed, were
  checked, are stale, or were intentionally not updated.
- Next action: the smallest step that would change readiness.

If the readiness handoff is missing, stale, tied to a different commit/PR, or
does not cover the current closeout question, run `repo-readiness` first or
record the gap as a conditional/no safe-to-exit reason. Do not treat closeout
cleanup as proof that gates passed.

Both this skill and `repo-readiness` read `.github/github.json` with the same
schema expectations: `qualityGate`, `docs`, `metadataFreshness`, `cleanup`,
`importantWorkflows`, repo relationships, health signals, and ownership or
Launchplane routing when present. Readiness uses those fields to decide what
must be verified; closeout uses the same fields to decide what final evidence,
metadata updates, and cleanup remain.

When writing final summaries, closeout comments, or migrated handoff content,
follow `../references/every-code-formatting.md`: cite point-in-time evidence,
keep recovery-critical facts in GitHub for GitHub-backed work, and avoid
boilerplate that does not help the next session resume.

## Safe To Exit

Treat "safe to exit" as strict hygiene, not merely context preservation. Safe
to exit means work is ready and hygiene is complete, or unfinished work is
intentionally parked with durable state.

Safe to exit: yes

- Work is complete or explicitly out of scope.
- Gates, inspections, docs checks, metadata checks, and post-merge checks are
  done or explicitly not applicable.
- Background auto-review findings, when available, have been matched to the
  current branch/PR/head SHA; current-target findings are resolved,
  non-blocking, or explicitly tracked for follow-up; relevant current-target
  auto-review runs are not still in-flight.
- PR, issue, GitHub plan, and any explicit local plan state is current.
- No important untracked artifacts, transient processes, or hidden follow-up
  remain.

Safe to exit: conditional

- Work is unfinished but intentionally parked.
- Blockers and next steps are recorded in a PR, issue, GitHub plan, or explicit
  local/offline saved plan.
- Failing and not-run checks are recorded with reasons.
- Current auto-review blockers, if any, are recorded in the PR, issue, or plan
  that owns the unfinished work.
- Remaining docs, metadata, security, or quality follow-up is tracked durably.
- No transient local state is required to resume.

Safe to exit: no

- Uncommitted or unexplained work remains.
- Expected gates/readiness checks have not been run and no reason is recorded.
- Failing checks, docs/metadata/security follow-up, PR/CI/review state, or
  cleanup work is unresolved and untracked.
- Temporary artifacts or processes could confuse the next session.

For code changes, broad practical lint/static analysis and `jetbrains-inspection`
state must be included in closeout, or there must be a documented not-run reason
or intentional parking decision.

## Parking Work

Use one durable place as the primary owner for intentionally parked work, and
link related artifacts when useful:

- PR: current branch scope, verification state, review/CI/deploy status, and
  remaining items that belong to this branch.
- GitHub plan issue: durable planning, cross-session agent memory, multi-step
  strategy, cross-repo coordination, blockers, and Project state.
- Issue: durable repo work not tied to the current branch, including bugs,
  security/quality findings, and cleanup tasks someone may pick up later.
- Saved local plan: only explicit offline/private context not ready or
  appropriate for GitHub.

For conditional safe-to-exit, at least one durable place must hold the next
step. Avoid duplicating every detail everywhere; link PRs, issues, and plans
when that improves continuity.

## Handoff Surfaces

For GitHub-backed repos, recovery-critical handoff content belongs in the
owning GitHub issue or PR comment. Use local handoff files only as temporary
scratch while drafting or when the user explicitly asks for an offline/private
handoff.

- If a handoff file names an active issue or PR, copy the actionable summary,
  blockers, next action, validation state, and relevant point-in-time links to
  that GitHub thread before relying on it.
- If a handoff file is intentionally committed, make sure it describes durable
  product or repo behavior, not session-only coordination.
- Before declaring closeout complete, sweep temporary handoff files matching
  configured globs and either delete them after migration or report why they are
  intentionally left behind.

## Plan Hygiene

- Use `github-plan` plus the sibling `github` helpers to update the active
  issue's `Current Status`, finish line, blockers, and Project fields before
  parking work.
- Before parking or closing a workstream, run a Plan Direction Checkpoint:
  identify the next action, how it fits the current plan, whether the plan or
  issue graph changed, and where that durable state was updated.
- If the work revealed a new blocker, dependency, sub-workstream, or stale
  assumption, update the GitHub issue graph before relying on a handoff summary.
- Treat an accurate issue graph as closeout evidence. A handoff that describes
  work not represented in the owning issue, PR, or related issue graph is
  incomplete unless the user explicitly asked for private/offline parking.
- After a PR merges, sweep issues referenced by the canonical merged PR body and
  comments. `Refs #...` is intentionally non-closing; close only issues whose
  acceptance criteria were conclusively satisfied by the merge. Otherwise,
  update `Current Status` or leave a comment with what remains.
- Mark completed checklist items, record blockers, and remove or rewrite stale
  assumptions in the GitHub plan issue.
- Before declaring safe to exit after closing or merging implementation work,
  inspect the remaining open GitHub issues labeled `plan` and verify their
  labels and Project status/focus fields match their `Current Status`:
  `plan:active`, `plan:blocked`, `plan:waiting`, `plan:stale`, or `plan:done`.
  Re-read the issue or Project item after updating because labels and board
  fields can drift independently. The main LLM owns the final label/status
  decision and any mutations. For large issue sets, a read-only agent may
  summarize likely mismatches, but the main LLM must make and verify the final
  updates.
- If workflow metadata changes are deferred, record the exact `.github/github.json`
  follow-up in the GitHub plan or closeout remaining items.
- Delete or migrate finished local working plans once useful planning context
  has been captured in GitHub, or once implemented behavior has been reflected
  in repo docs when docs are actually stale.
- Migrate stale local plans into active GitHub plans instead of leaving archive
  clutter.

## Git And Worktree Hygiene

- Preserve unrelated user changes.
- Do not run destructive git commands.
- Do not force-delete branches or worktrees.
- If `git status --short --branch` shows a clean branch behind its upstream,
  fast-forward it with `git pull --ff-only` before saying the checkout is tidy.
  Treat dirty, ahead, diverged, missing-upstream, or failed fast-forward states
  as report-only unless the user explicitly asks for a specific git action.
- When the user asks to delete or remove a worktree, first preserve or confirm
  disposal of any uncommitted changes. Removing a worktree is not approval to
  lose its branch or local edits.
- Use `github` for PR-backed branch/worktree cleanup and GitHub
  state.
- After merged PRs, include relevant post-merge Actions and GitHub
  security/quality signal outcomes when GitHub data is available. Report signals
  as clean, findings, unavailable, or not enabled; do not treat unavailable or
  not-enabled signals as clean.
- Concrete reproducible broad-gate findings that are not fixed now should be
  tracked after a duplicate search. Group speculative or huge-baseline findings
  into a cleanup plan/report instead of opening many issues.
- If cleanup safety is ambiguous, report the candidate and ask before acting.

## Auto Review Worktrees

Codex Desktop and Every Code auto-review flows may create detached review
worktrees under paths like:

```text
~/.code/working/<repo>/branches/auto-review*
```

Treat these as external review context, not the active workstream.

- Ignore them for normal safe-to-exit and dirty-worktree decisions.
- Do not treat their files as blocking the current repo closeout.
- Do not clean, prune, delete, or modify them unless the user explicitly asks
  about that review worktree.
- Mention them only when relevant, for example: "Ignored detached auto-review
  worktrees."
- If the user asks about a review result or review worktree specifically, switch
  context deliberately and inspect that worktree as the task target.

## The Love Gate

Before finalizing the closeout, perform a "Love Gate" check. This is an
emotional and qualitative alignment step where the agent evaluates the session's
output against the user's ultimate satisfaction and the agent's own engineering
standards.

- **Check if you "love" the work**: Does the implementation feel clean, idiomatic,
  and complete? Is the solution robust, or does it feel like a "just-in-case"
  patch?
- **Identify what you do not love**: Are there any compromises, technical debt,
  missing edge cases, or "smells" that remain? Be honest about shortcuts taken
  due to context limits or task complexity.
- **Report findings**: Include a brief "Love Gate" section in your closeout
  summary.

This gate ensures that the session ends not just with technical passing, but
with a shared understanding of the work's quality and "soul."

## Output Format

Use a compact closeout report:

- Done: what changed or was handled.
- Remaining: concrete blockers or follow-up work, including how the next action
  fits the active plan.
- Checks: gates, inspections, docs, metadata, CI/Actions, and GitHub
  security/quality signals that passed, failed, were pending, or were not run.
- Love Gate: what you love about the results, and anything you do not love.
- Cleanup: artifacts, plans, handoffs, branches, or worktrees removed or left
  intentionally.
- State: dirty files, PR status, CI status, or plan status when relevant.
- Safe to exit: yes/no/conditional, with the condition if needed.

If there are no remaining items, say so plainly. Do not over-explain command
output unless the user asks for it.
