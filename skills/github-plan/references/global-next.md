# Global Next

Read for `next` in a repository owner's direction repository, including explicit
`gh-plan.py --repo OWNER/direction next` calls from elsewhere.

1. Read the Director's current direction and known repository-wide holds from the
   conversation and durable issue discussions. A hold overrides individual
   active labels, missing blockers, and apparent worker availability. Continuing
   an encode or another background job does not resume engineering in that repo.
2. Run the helper. It traverses the native milestone graph and inventories open
   issues outside it, including issues without the planning label. Inspect
   `graph_context` and `discovery_context` separately, including each source's
   exclusions, merged direction, failures, and bounds. An inaccessible source is
   unknown. A complete graph never means all repositories or sessions were read.
3. Apply Choose Work to possible candidates from both sources. Read each full
   discussion, including the latest comments, and current PR/branch/worktree and
   supported session evidence. A complete `discussion` snapshot is the full
   issue body and comments; use `show ISSUE --full` when it is incomplete or needs
   refreshing. Discovered children include bounded parent discussions; a
   whole-plan parent wait remains excluded, and changes to parent discussion
   invalidate the child's review digest. Comments can reveal a wait or completed implementation despite
   active labels. A partial ownership inventory never proves freedom to start.
4. Select under the Director's direction: live incidents first, listed milestones
   in order, other tooling with two linked occurrences of the stop it fixes, or
   when every milestone candidate waits on a person and provider capacity would
   otherwise go unused, and eligible own projects from their share. Read each repository's direction;
   discovery does not adopt direction or grant execution permission. Do not infer
   that an unlinked child can proceed through its parent's whole-plan wait.
5. Report the highest-ranked independently available item, with higher-ranked
   underway work and human waits separately. If every discovered item is held,
   occupied, or waiting, say so within the searched scope. If sources, discussion,
   direction eligibility, or ownership remain unknown, name that missing evidence
   instead of inventing available work. Widen bounded reads when needed, and stop
   after the answer without changing planning state or starting implementation.

Outside the read-only `next` answer, an explicit Director incident decision may
be recorded on the issue with the `live-breakage` label under existing write
authority. Create the label if needed in the repository owner's repository using the
GitHub automation wrapper; preserve human-authored issue text. Writes to another
person's repository need that repository owner's authorization under
execution-scope.
Remove the marker when the incident is resolved. The marker only prioritizes
possible work; it never establishes ownership, lifts holds, or overrides blockers.

Director-marked issues are evaluated outside the ordinary discovery scan allowance.
A separate label inventory covers marked issues beyond a repository's ordinary
issue-list bound. Repository access/inventory limits and failed reads remain
explicit coverage limits. Inspect `candidate_coverage` beside the ranked list:
a partial list cannot establish that no higher-priority work exists.

Each candidate's `overall_milestone_context` reports an established native Track
path/ancestry/blocking target or an exact waypoint title shared by the issue's open milestone,
its repository direction and the overall direction. A title match carries
`basis: exact_listed_title_match`; it does not claim a native Track link.
`none_found` means the inspected context
contains no such link; `unknown` preserves incomplete context. This explanation
does not change ranking, adopt direction or establish availability.
`candidate_coverage.unevaluated_repositories` names the inventoried sources and
issue counts omitted by the ordinary evaluation allowance after graph overlap
and marked-incident handling. Other source bounds/failures remain in
`discovery_context.repositories`; omitted counts are not a complete portfolio
inventory when those sources are truncated or inaccessible.

The helper's `candidates` are possible work and may need review;
`available_candidates` contains only current caller-reviewed work. An empty
available list with nonzero `review_required_count` means selection is unfinished,
not that the portfolio has no work. The agent can finish that review in its answer;
a service or caller needing the same classification in JSON may pass a temporary
`--selection-context FILE` on the next read. It is an evidence snapshot, never a
second plan or a reason to ask the Director for permission to inspect work.

```json
{
  "repository_holds": {
    "owner/paused-project": {
      "reason": "Owner parked development until an existing job ends and direction is revisited.",
      "evidence": ["Current owner instruction or canonical issue comment URL"]
    }
  },
  "issues": {
    "owner/product#42": {
      "state": "available",
      "category": "own_project",
      "reason": "Within direction; full discussion and current ownership checked.",
      "evidence": ["Direction source and current issue, PR, worktree, and session evidence"],
      "discussion_digest": "Copy the digest from the discussion just reviewed",
      "ownership_complete": true
    }
  }
}
```

Issue review states are `available`, `underway`, `waiting`, and `ineligible`.
Categories for discovered work are `live_incident`, `repeated_stop_tooling`, and
`own_project`; linked graph work keeps its milestone priority. Tooling keeps the existing
`repeated_stop_tooling` category. Two distinct HTTPS links in `stop_occurrences`
admit it under the repeated-stop rule. With fewer links, capacity admission needs
complete graph and portfolio coverage and current reviews of every milestone
frontier issue, including excluded waits and discovered milestone work. Each must
be `state: waiting`, with `waiting_on: person`, the current `discussion_digest`,
complete discussion and ownership evidence, and a reason and evidence identifying
who must act. A CI/event wait, held repository, underway or unreviewed milestone
issue, or incomplete coverage does not establish this rule. Empty Tracks contain
no milestone candidates; no frontier waits at all cannot establish the rule.
A `--milestone` run cannot establish portfolio-wide capacity admission.
Deferred/stale milestone issues report `milestone_issue_excluded` with the issue
and exclusion, rather than asking for a person-wait review that cannot clear it.
No person is inferred from free text. Refresh these reviews on each selection. Whether provider capacity would
otherwise go unused remains the caller's judgment under direction; `next` proves
the milestone-wait condition, not provider usage.

`tooling_capacity_context` explains whether the capacity rule is established,
including the first unresolved issue when a milestone review or context is missing.
Held repositories are omitted from discovery, so their inventory cannot establish
a portfolio-wide person-wait rule; any recorded repository hold disables capacity
admission while leaving ordinary eligible work selectable.
Every available tooling candidate carries `tooling_admission_rule`
(`repeated_stops` or `all_milestones_waiting_on_people`), the same value in
`reasons`, and `recorded_stop_count` for weekly audit counts. During capacity
admission, tooling sorts by distinct recorded stop links, most first, then age.
Own projects stay ahead of capacity-admitted tooling so spare-capacity work
cannot crowd them out of bounded results; repeated-stop tooling retains its
existing precedence over own projects; this is not a per-call share quota. Without capacity
admission, existing ranking is unchanged. Service callers of
`rank_portfolio_work` must supply `coverage_complete: true` only after proving
their portfolio inventory complete; omission leaves capacity admission disabled.

These are the caller's
evidence judgments, not classifications guessed from names, labels, or keywords.
The Director-applied `live-breakage` marker independently puts an incident first
among possible candidates until removed; it does not supply an availability
review or change the caller's category.
Set `ownership_complete` only when current evidence actually establishes the
item's availability, never from a partial task list. A changed discussion digest
invalidates the issue review. Recheck holds and ownership on every selection;
unchanged issue text alone cannot establish their freshness.

Repository holds apply across graph and discovery before any available review.
They exclude work in the held repository while preserving native paths to
independent work in other repositories; they do not invent cross-repository holds.
Keep snapshots private when they contain session or operational context. Ordinary
read-only `next` does not write a snapshot, update an issue, or start a worker.
