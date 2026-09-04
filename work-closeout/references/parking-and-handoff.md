# Parking and handoff procedures

Read when leaving unfinished work, choosing its durable owner, or migrating
local handoff files. The closeout verdict and required Love Gate remain in
the parent skill.

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

When configured Focus lanes are part of the durable planning surface, make sure
the owning item's lane reflects the closeout state: `Now` for the active finish,
`Waiting` for blocked work or work awaiting an external decision/event,
or `Next`/`Later` for deferred work. For completed planning issues, use the
`github-plan` close flow so done labels and Project focus are updated together.
Do not leave the lane stale when parking or closing a workstream.

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
