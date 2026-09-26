# Global Next

Read for `next` in an owner's direction repository, including explicit
`gh-plan.py --repo OWNER/direction next` calls from elsewhere.

1. Read the owner's current direction and known repository-wide holds from the
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
4. Select under the owner's direction: live incidents first, listed milestones
   in order, other tooling only with two linked occurrences of the stop it fixes,
   and eligible own projects from their share. Read each repository's direction;
   discovery does not adopt direction or grant execution permission. Do not infer
   that an unlinked child can proceed through its parent's whole-plan wait.
5. Report the highest-ranked independently available item, with higher-ranked
   underway work and human waits separately. If every discovered item is held,
   occupied, or waiting, say so within the searched scope. If sources, discussion,
   direction eligibility, or ownership remain unknown, name that missing evidence
   instead of inventing available work. Widen bounded reads when needed, and stop
   after the answer without changing planning state or starting implementation.

The helper's `candidates` are possible work and may need review;
`available_candidates` contains only current caller-reviewed work. An empty
available list with nonzero `review_required_count` means selection is unfinished,
not that the portfolio has no work. The agent can finish that review in its answer;
a service or caller needing the same classification in JSON may pass a temporary
`--selection-context FILE` on the next read. It is an evidence snapshot, never a
second plan or a reason to ask the owner for permission to inspect work.

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
`own_project`; linked graph work keeps its milestone priority. Repeat-stop tooling
also needs two distinct HTTPS links in `stop_occurrences`. These are the caller's
evidence judgments, not classifications guessed from names, labels, or keywords.
Set `ownership_complete` only when current evidence actually establishes the
item's availability, never from a partial task list. A changed discussion digest
invalidates the issue review. Recheck holds and ownership on every selection;
unchanged issue text alone cannot establish their freshness.

Repository holds apply across graph and discovery before any available review.
They exclude work in the held repository while preserving native paths to
independent work in other repositories; they do not invent cross-repository holds.
Keep snapshots private when they contain session or operational context. Ordinary
read-only `next` does not write a snapshot, update an issue, or start a worker.
