# Executing loop

Use this loop when a repository has a root `DIRECTION.md`, or has none and its
repository owner keeps an overall direction in `OWNER/direction`. The
session-start hook prints this reference on both supported harnesses, after
the overall stop boundaries in the second case. The owning skills hold
the detailed procedures and [task scope and authorization](execution-scope.md)
still governs every action.

```text
Executing loop for this repository (from DIRECTION.md):
  next          report the next available ranked item and stop
  go            start it: gh-plan.py claim, then linked worktree, bot commits, review by another model when the review reference says so
  go <milestone title>  work that milestone's issues in next order without stopping between them
  next and go   both
  escalate      a finding that retires, stops, or redirects work is an issue labeled direction, not a change
  land          load github to open and merge the PR, babysit-pr to watch until merged, then reconcile the runtime checkout
  close out     load work-closeout to update the plan issue, open follow-up issues without starting them, remove the worktree (with the retire command its lock reason names, if any), leave main clean
```

Load each step's owning skill before acting, including when it is a step inside
another skill's task; see [using skills](using-skills.md). For `next` and plan
updates use `github-plan`; for reviews use `model-review` when the review
reference calls for one.

Before recommending or starting an item, apply
[Choose Work](../github-plan/SKILL.md#choose-work), including its ownership check and
[agent assignment](../github-plan/SKILL.md#agent-assignment). `go` stops when
`claim` reports another family’s label unless the Director explicitly overrides
it for that session.
Report higher-ranked work already underway separately from the next
independent item available to this session.

`go` authorizes implementation; merge still requires authorization for the
change and destination under [task scope and authorization](execution-scope.md).
When that authorization exists, carry `land` through without asking again.

Every issue ends done or split. Done means the result a person can see is
real: the lane reads green, the record reads back, the feature runs. A merge
alone is not done; when the rest is ordinary engineering, deploy it, switch it
on, and read it back yourself. Split means you close your part and open one
issue for what is left, naming who acts next: the Director (ask him on that
issue and name him in `Waiting for:`), a named event, or the next agent. If the
result can only show later, close on what you can check now and open a small
issue to look again. Never close with a comment standing in for a step, and
never leave an issue open waiting on nobody.

Use [reviews by another model](model-review.md) to decide when a review is
needed. For milestone work, honor the same authorization and direction stop
boundaries on each issue. If one issue must pause or escalate, continue other
independent milestone issues. Put close-out follow-ups outside the current
milestone by default. An executing agent may add one to a milestone listed in
`DIRECTION.md`; the direction audit lists it afterward for the direction session
to read for fit, and it needs no Director step. Do not work an issue added
during close-out in the same `go <milestone>` run.
If `next` finds no available milestone issue, report what is underway or waiting
and do not claim the milestone is complete until its exit criteria are met.
List every Director decision still open at the end of any executing-loop command in the final response,
each as a direct question with a recommendation and the effect of each choice.
An earlier asynchronous question or an issue update does not replace that
handoff.
Preserve unsafe or active worktrees and runtime checkouts under their owning
skills rather than forcing cleanup. Preserve dirty or divergent default
checkouts too; report when they cannot be safely fast-forwarded.
For a finding that would change `DIRECTION.md` itself, follow the
[direction skill](../direction/SKILL.md) escalation procedure.
