# Executing loop

Use this loop when a repository has a root `DIRECTION.md`. The session-start
hook prints this reference on both supported harnesses. The owning skills hold
the detailed procedures and [task scope and authorization](execution-scope.md)
still governs every action.

```text
Executing loop for this repository (from DIRECTION.md):
  next          report the ranked item and stop
  go            start it: linked worktree, bot commits, review by another model when the review reference says so
  go <milestone title>  work that milestone's issues in next order without stopping between them
  next and go   both
  escalate      a finding that retires, stops, or redirects work is an issue labeled direction, not a change
  land          load github for the PR and merge path, babysit-pr for CI/review follow-through, then reconcile the runtime checkout
  close out     load work-closeout to update the plan, record follow-ups without starting them, remove the worktree, leave main clean
```

Load each step's owning skill before acting, including when it is a step inside
another skill's task; see [using skills](using-skills.md). For `next` and plan
updates use `github-plan`; for reviews use `model-review` when the review
reference calls for one.

`go` authorizes implementation; merge still requires authorization for the
change and destination under [task scope and authorization](execution-scope.md).
When that authorization exists, carry `land` through without asking again.
Use [reviews by another model](model-review.md) to decide when a review is
needed. For milestone work, honor the same authorization and direction stop
boundaries on each issue. If one issue must pause or escalate, continue other
independent milestone issues. Put close-out follow-ups outside the current
milestone by default. An executing agent may admit one to a milestone listed in
`DIRECTION.md` when its issue body blockquotes an exact phrase from that
milestone's line that the issue proves or protects. Admission is checked by the
direction audit afterward; it needs no owner step. Do not work an issue admitted
during close-out in the same `go <milestone>` run.
If `next` finds no actionable milestone issue, report what is waiting and do
not claim the milestone is complete until its exit criteria are met.
List every owner decision still open at the end of any executing-loop command in the final response,
each as a direct question with a recommendation and the effect of each choice.
An earlier asynchronous question or an issue update does not replace that
handoff.
Preserve unsafe or active worktrees and runtime checkouts under their owning
skills rather than forcing cleanup. Preserve dirty or divergent default
checkouts too; report when they cannot be safely fast-forwarded.
For a finding that would change `DIRECTION.md` itself, follow the
[direction skill](../direction/SKILL.md) escalation procedure.
