---
name: work-closeout
description: Use when the user asks to wrap up, clean up, close out a session or workstream, prepare handoff, determine what remains, update or remove stale plans, remove transient artifacts, or asks whether they can exit. Coordinates plan/handoff cleanup, safe git/worktree hygiene, artifact cleanup, and final state summaries.
---

# Work Closeout

Use this skill to leave a workstream tidy and understandable. It is about
cleanup and handoff, not proving readiness; use `repo-readiness` when the main
question is whether checks pass or a PR can ship.

## Core Goal

Leave the user with a truthful closeout answer:

- What is done?
- What remains?
- What cleanup happened?
- What artifacts/plans/handoffs were removed or preserved?
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
3. Inspect local state:

```bash
git status --short --branch
git worktree list
```

If the shared Launchplane context helper is present and configured, call it once
as optional closeout context for the repo/workstream:

```bash
~/.code/skills/launchplane/scripts/launchplane-context.py --repo OWNER/REPO
```

Use `available` context to notice pending Every Code work, preview readiness,
deploy/product evidence, or source-of-truth links that should be reflected in
the closeout. Treat `no_context`, `unavailable`, `unauthorized`, `invalid`, or
helper failure as normal absence. Do not block safe-to-exit only because
Launchplane context is unavailable, and do not copy raw helper payloads into
handoffs, issues, PRs, or final summaries.

4. If GitHub state matters for closeout, use `github` for PR,
   Actions, labels, merge state, post-merge verification, GitHub
   security/quality signals, and safe branch/worktree cleanup.
5. Use `github` for durable plan state, blockers, stale/duplicate plan
   cleanup, and Project updates. Use legacy `plan` only for explicit
   local/offline plan files that already exist or that the user asks to keep.
6. Remove consumed `handoff*.md` files after durable planning decisions are
   captured in GitHub or an explicit offline/local plan, and after implemented
   behavior is reflected in repo docs when docs are actually stale, unless the
   user asks to keep iterating.
7. Clean only artifacts clearly created by the current work: transient logs,
   screenshots, temp scripts, generated scratch files, generated caches, stopped
   test containers, or consumed handoff files.
8. Do not remove user artifacts, broad system caches, unrelated untracked files,
   or remote resources without explicit approval.
9. Report final state concisely.

## Safe To Exit

Treat "safe to exit" as strict hygiene, not merely context preservation. Safe
to exit means work is ready and hygiene is complete, or unfinished work is
intentionally parked with durable state.

Safe to exit: yes

- Work is complete or explicitly out of scope.
- Gates, inspections, docs checks, metadata checks, and post-merge checks are
  done or explicitly not applicable.
- PR, issue, GitHub plan, and any explicit local plan state is current.
- No important untracked artifacts, transient processes, or hidden follow-up
  remain.

Safe to exit: conditional

- Work is unfinished but intentionally parked.
- Blockers and next steps are recorded in a PR, issue, GitHub plan, or explicit
  local/offline saved plan.
- Failing and not-run checks are recorded with reasons.
- Remaining docs, metadata, security, or quality follow-up is tracked durably.
- No transient local state is required to resume.

Safe to exit: no

- Uncommitted or unexplained work remains.
- Expected gates/readiness checks have not been run and no reason is recorded.
- Failing checks, docs/metadata/security follow-up, PR/CI/review state, or
  cleanup work is unresolved and untracked.
- Temporary artifacts or processes could confuse the next session.

For code changes, broad practical lint/static analysis and IDE inspection state
must be included in closeout, or there must be a documented not-run reason or
intentional parking decision.

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

## Plan Hygiene

- Prefer `github` and update the active issue's `Current Status`, finish
  line, blockers, and Project fields before parking work.
- Mark completed checklist items, record blockers, and remove or rewrite stale
  assumptions in the GitHub plan issue.
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

Codex Desktop may create detached review worktrees under paths like:

```text
~/.code/working/<repo>/branches/auto-review*
```

Treat these as external review context, not the active workstream.

- Ignore them for normal safe-to-exit and dirty-worktree decisions.
- Do not treat their files as blocking the current repo closeout.
- Do not clean, prune, delete, or modify them unless the user explicitly asks
  about that review worktree.
- Mention them only when relevant, for example: "Ignored Codex Desktop
  auto-review worktrees."
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
- Remaining: concrete blockers or follow-up work.
- Checks: gates, inspections, docs, metadata, CI/Actions, and GitHub
  security/quality signals that passed, failed, were pending, or were not run.
- Love Gate: what you love about the results, and anything you do not love.
- Cleanup: artifacts, plans, handoffs, branches, or worktrees removed or left
  intentionally.
- State: dirty files, PR status, CI status, or plan status when relevant.
- Safe to exit: yes/no/conditional, with the condition if needed.

If there are no remaining items, say so plainly. Do not over-explain command
output unless the user asks for it.
