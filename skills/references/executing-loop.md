# Executing loop

Use this loop when a repository has a root `DIRECTION.md`. The session-start
hook prints this reference on both supported harnesses. The owning skills hold
the detailed procedures and [task scope and authorization](execution-scope.md)
still governs every action.

```text
Executing loop for this repository (from DIRECTION.md):
  next          report the ranked item and stop
  go            start it: linked worktree, bot commits, review by another model
  next and go   both
  escalate      a finding that retires, stops, or redirects work is an issue labeled direction, not a change
  land          open the PR, babysit it until merged, reconcile the runtime checkout
  close out     update the plan issue, remove the worktree, leave main clean
```

`go` authorizes implementation; merge still requires authorization for the
change and destination under [task scope and authorization](execution-scope.md).
When that authorization exists, carry `land` through without asking again.
Preserve unsafe or active worktrees and runtime checkouts under their owning
skills rather than forcing cleanup. Preserve dirty or divergent default
checkouts too; report when they cannot be safely fast-forwarded.
For a finding that would change `DIRECTION.md` itself, follow the
[direction skill](../direction/SKILL.md) escalation procedure.
