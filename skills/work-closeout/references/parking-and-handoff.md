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

### Cleanup preservation routes

Use this route only when cleanup finds valuable retained work that needs to
survive its current branch, worktree, or checkout. Routine disposable output
does not require a parking issue or remote publication.

Before publishing cleanup recovery state, preflight live Issues capability and
the destination's applicable access permissions, and identify the canonical
owner. Reuse current scoped identity/permission evidence; a known denial remains
a blocker. Create or update that owner only
when the valuable work needs the durable parking surface and the same action and
publication scope are already authorized.
If Issues are disabled, use an already authorized owner or consumer tracker when
one exists. Ask only when the scope or publication surface would expand, or when
no authorized durable owner can be selected.

A cleanup request or local cleanup authority does not authorize pushing a branch,
opening a PR, merging, deleting a remote branch, or archiving or deleting a
remote repository. Perform those actions only under their existing GitHub
authorization rules.

For a pushed-branch parking route, record the exact branch and SHA, original
intent, review and validation status, why the work was excluded from cleanup or
how another implementation superseded it, and the next adopt-or-discard action.
Before calling it parked, verify the pushed SHA at the remote and verify the
durable issue link that owns the recovery state; link any related PR from that
owner.

A local-only route is valid when it uses a known, already approved durable
location outside the removal target and verification shows the Git state, dirty
patches, and needed local files are reconstructable. Trash, a reflog, or a
temporary cleanup manifest alone is not durable parking. If no authorized route
exists, retain the original work, name the missing choice, and continue any
independent authorized cleanup.

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

If a cross-repository prerequisite issue must still be created or identified,
use `github-plan`'s [missing-gate handoff rule](../../github-plan/SKILL.md#missing-cross-repository-gates)
to record who returns the canonical links and who verifies/connects the native
blockers. Keep the waiting record on its permitted issue surface.

- If a handoff file names an active issue or PR, copy the actionable summary,
  blockers, next action, validation state, and relevant point-in-time links to
  that GitHub thread before relying on it.
- If a handoff file is intentionally committed, make sure it describes durable
  product or repo behavior, not session-only coordination.
- Before declaring closeout complete, sweep temporary handoff files matching
  configured globs and either delete them after migration or report why they are
  intentionally left behind.
