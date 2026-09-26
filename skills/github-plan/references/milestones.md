# Milestone Contract

Read before assigning work to a milestone or creating, changing, assessing, or
closing one. Milestones are release, phase, or date gates, not theme labels or
alternate backlogs.

- Read the milestone description and open issues before judging readiness or
  membership. Put exact exit criteria in that description, not transient repo
  instructions.
- Before admitting an issue, ask whether the milestone can honestly close while
  it remains open. If yes, keep it outside the milestone.
- Prefer one active milestone per release train. Independent trains may have
  separate milestones and exit gates.
- Give each active milestone a due date or a named gate, dependency, or decision
  that determines when it can close.
- Record why incomplete work no longer blocks before removing it to the backlog
  or admitting it elsewhere. For agent admission to a milestone listed in
  `DIRECTION.md`, blockquote in the issue the exact milestone phrase it proves
  or protects, as required by `direction`.
- When scope keeps growing, cut scope deliberately rather than silently
  extending the gate. Close the milestone when its release or phase ships;
  reconcile survivors instead of leaving it open for every themed issue.

With a root `DIRECTION.md`, use only milestone titles listed in the merged file.
Propose a new waypoint through the `direction` skill before creating it. The
helpers refuse other titles and renames. Closing a shipped milestone is normal
planning work; removing its stale direction line requires a direction PR.

Use `gh-plan.py milestone-list`, `milestone-show`, `milestone-create`,
`milestone-update`, and `milestone-close` for container operations.
`milestone-update` can reopen a milestone but cannot close it;
`milestone-close` refuses while any open issue or PR remains assigned. Resolve
scope and membership under existing authority rather than bypassing that check.
For command arguments and result fields, use the GitHub helper CLI reference.
