# Milestone Contract

Read before assigning work to a milestone or creating, changing, assessing, or
closing one. Milestones are release, phase, or date gates, not theme labels or
alternate backlogs.

- Read the milestone description and open issues before judging readiness or
  membership. Put exact exit criteria in that description; keep transient
  milestone state out of repository instructions.
- Before admitting work, compare the milestone's proof with its recorded
  evidence, independently of open blockers. If the proof is met, close it or
  surface it as a closing candidate; admit later work under the milestone it
  actually serves or another supported Order kind. A claim's native link is
  membership evidence, never an addition to the milestone's proof.
- Keep an issue outside the milestone if that milestone can honestly close
  while the issue remains open.
- Prefer one active milestone per release train. Use parallel milestones only
  for independent trains with separate exit gates.
- Give each active milestone a due date or a named gate, dependency, or decision
  that determines when it can close.
- Record why incomplete work no longer blocks before removing it to the backlog
  or admitting it elsewhere.
- Close when the recorded proof is met. Reconcile surviving links and milestone
  assignments with evidence, preserving each issue's work, custody and waits.
  Do this in the existing planning/closeout pass; a separate closeout issue or
  an agent's return is not another finish line. Propose a direction change only
  when the proof itself must change.

With a root `DIRECTION.md`, use only milestone titles listed in the merged file.
Propose a new waypoint through the `direction` skill before creating it. The
helpers refuse creating or renaming to unlisted titles. Closing a shipped
milestone is normal planning work. For removal of its stale direction line,
follow the [direction escalation procedure](../../direction/SKILL.md#escalation-and-disposition).

Use `gh-plan.py milestone-list`, `milestone-show`, `milestone-create`,
`milestone-update`, and `milestone-close` for container operations.
`milestone-update` can reopen a milestone but cannot close it;
`milestone-close` refuses while any open issue or PR remains assigned. Resolve
scope and membership under existing authority rather than bypassing that check.
For command arguments and result fields, use
[Planning: Milestones](../../github/references/cli-reference.md#planning-milestones).
