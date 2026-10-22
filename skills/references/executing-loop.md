# Executing loop

Use this loop when a repository has a root `DIRECTION.md`, or has none and its
repository owner keeps an overall direction in `OWNER/direction`. The
session-start hook prints this reference's command block and a link on both
supported harnesses, after the overall stop boundaries in the second case. The owning skills hold
the detailed procedures and [task scope and authorization](execution-scope.md)
still governs every action. When a permission check refuses an action, stop
that action, report the refusal with its exact text, and continue other work;
never redo it through another tool, agent, or identity.

```text
Executing loop for this repository (from DIRECTION.md):
  next          report the next available ranked item and stop
  go            start it: gh-plan.py --repo OWNER/REPO claim, then linked worktree, bot commits, review by another model when the review reference says so
  go <milestone title>  work that milestone's issues in next order without stopping between them
  next and go   both
  escalate      a finding that retires, stops, or redirects work is an issue labeled direction, not a change
  land          load github to open and land the PR by its Merging procedure; honor assigned train handoffs and complete After A Merge
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
The GitHub skill's [Merging procedure](../github/SKILL.md#merging) owns routing
for single PRs and stacks; load `launchplane` when it delegates landing to the
train. Use `babysit-pr` for CI and review follow-through, then complete GitHub's
[After A Merge](../github/SKILL.md#after-a-merge) procedure after confirmed landing.

Size implementation issues, choose PR closing language, and record event waits
under GitHub's
[issue closing and post-merge proof](../github/references/repo-workflow.md#issue-closing-and-post-merge-proof).
Close an issue at the PR completing its work. The PR's tests and the deploy's
existing health checks are the proof; add no per-issue deploy checks or agent
come-back steps. If the real finish line is an event, record a wait naming
that event and its actor. Preserve people's waits and the parent's or
milestone's own exit criteria. Real remaining engineering stays actionable:
source changes have their own completing PR, and operational steps are carried
through their supported path within the task's authority.
In a handoff's `Next action:`, name the next agent and its concrete step, such
as "Next agent: refresh PR #N against main". Name the Supervisor only for a step
that needs its authority: merge-train entry, presenting a Director question, a
release, or releasing a claim. The daily stranded-work sweep lists every open
issue whose Next action names the Supervisor.
Before recording a split or handoff, read github-plan's
[Status Labels](../github-plan/SKILL.md#status-labels).
Never substitute a comment for an unmet finish line, or keep completed work
open for an agent to return.

Use [reviews by another model](model-review.md) to decide when a review is
needed. Before the first PR push, run focused local checks and that required
review, address findings locally, and batch fixes into one push. Avoid fix-up
pushes in quick succession. Never merge the base into, rebase, or update your
own PR branch unless there is a real conflict or a check explicitly requires
it; the merge train owns freshness on train-routed PRs. Once a PR has asked its
Client for review, give every later commit a
[Client-impact trailer](../github/references/repo-workflow.md#client-impact-trailers).

For milestone work, honor the same authorization and direction stop
boundaries on each issue. If one issue must pause or escalate, continue other
independent milestone issues. Put close-out follow-ups outside the current
milestone by default. An executing agent may add one to a milestone listed in
`DIRECTION.md`; the direction audit lists it afterward for the direction session
to read for fit, and it needs no Director step. Do not work an issue added
during close-out in the same `go <milestone>` run.
If `next` finds no available milestone issue, report what is underway or waiting
and do not claim the milestone is complete until its exit criteria are met.
Account for every open Director decision at the end of an executing-loop
command under the [question queue rules](talking-with-the-director.md#decisions-holds-and-work-state).
Link the queued questions and name who brings the next one; an earlier
asynchronous question or issue update does not replace that handoff.
Preserve unsafe or active worktrees and runtime checkouts under their owning
skills rather than forcing cleanup. Preserve dirty or divergent default
checkouts too; report when they cannot be safely fast-forwarded.
Before a safe-exit all-clear, complete work-closeout's
[claim disposition](../work-closeout/SKILL.md#claim-disposition): release this
session's claims and verify retained PR handoffs, or report conditional/no.
Keep ownership while an assigned review is pending and the session must resume.
For a finding that would change `DIRECTION.md` itself, follow the
[direction skill](../direction/SKILL.md) escalation procedure.
