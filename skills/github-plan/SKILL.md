---
name: github-plan
description: Use when the user asks for a plan, what's next / what is next in a plan or workstream, how work fits the plan, plan direction/alignment, durable work tracking, roadmap, workstream planning, GitHub issue-backed planning, issue graphs, parent issues, sub-issues, blockers, milestones, Projects. Use declared repo docs before private operational lookup; use docs-lookup only for missing operational context. Think in chat first, then keep long-running work aligned over time by updating Current Status, blockers, relationships, and issue graph state as reality changes.
metadata:
  short-description: Plan durable work in GitHub issues
commands:
  - name: github-plan-reconcile-status
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "reconcile-status", "<issue>", "--apply"]
    purpose: Recomputes only status labels from native blockers and reviewed waits, including recovery after a confirmed fact write.
  - name: github-plan-wait
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "wait", "<issue>"]
    purpose: Reports the full legacy record for a one-time review; explicit --waiting-for/--ends-when or --clear with --evidence records structured authority.
  - name: github-plan-wait-inventory
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "wait", "--all"]
    purpose: Reads every open issue and full discussion and reports missing or malformed wait records without clearing them.
  - name: github-plan-claim
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "--repo", "OWNER/REPO", "claim", "<issue>", "--worker", "<worker>", "--session", "<session-id>", "--branch", "work/issue-<number>", "--next-action", "<action>"]
    purpose: Rechecks ownership, records and reads back the claim, and activates planning state before a task worktree is created.
  - name: github-plan-release
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "release", "<issue>", "--claim-comment", "<id>", "--session", "<native-id>", "--handoff-file", "<handoff.md>"]
    purpose: Releases this session's own claim with the exact directive and its handoff, and reads it back.
  - name: github-plan-release-claim
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "release-claim", "<issue>", "--claim-comment", "<id>", "--evidence-comment", "<attestation-url>", "--role", "supervisor", "--session", "<native-id>", "--confirm-session-ended", "--dry-run"]
    purpose: Reviews or records the exact release of an abandoned automation claim after verified session closure.
  - name: github-plan-index
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "index"]
    purpose: Lists durable planning issues through paged REST reads with compact status, label, and milestone fields.
  - name: github-plan-search
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "search", "<query>"]
    purpose: Searches planning issues with normalized compact output.
  - name: github-plan-create
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "create", "<title>", "--body-file", "<file>", "--wait-clear", "--wait-evidence", "<reviewed-evidence>"]
    purpose: Creates a durable plan issue with helper-owned labels and optional Project enrollment.
  - name: github-plan-update-section
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "update-section", "<issue>", "Current Status", "--body-file", "<file>"]
    purpose: Updates one markdown section of a planning issue safely.
  - name: github-plan-project-set
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-set", "<issue>", "--focus", "Next"]
    purpose: Applies only explicitly requested Project field edits; routine planning uses automatic views.
  - name: github-plan-close
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "close", "<issue>", "--comment-file", "<file>"]
    purpose: Closes a completed or explicitly not-planned durable plan through relationship preflight and recoverable metadata reconciliation.
  - name: github-plan-next
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "next", "--limit", "5"]
    purpose: Ranks actionable plans by milestone execution order, native blockers, dependency impact, and issue age without mutating planning state.
  - name: github-plan-milestone-list
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "milestone-list", "--state", "all"]
    purpose: Lists repository milestones with bounded pagination and normalized due dates and issue counts.
  - name: github-plan-milestone-show
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "milestone-show", "<number-or-exact-title>"]
    purpose: Shows one milestone by number or exact title through the shared REST helper.
  - name: github-plan-milestone-create
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "milestone-create", "<title>", "--due-on", "YYYY-MM-DD"]
    purpose: Creates an exact-title-idempotent milestone with actor-aware, conflict-safe writes.
  - name: github-plan-milestone-update
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "milestone-update", "<number-or-exact-title>", "--state", "open"]
    purpose: Updates milestone metadata or reopens a milestone; closing is intentionally refused here.
  - name: github-plan-milestone-close
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "milestone-close", "<number-or-exact-title>"]
    purpose: Closes a milestone only when no open issue or pull request remains assigned.
policy:
  command_policies:
    - id: prefer-gh-plan-index-for-issue-list
      match:
        argv_prefix: ["gh", "issue", "list"]
      action: require_preferred
      message: Raw `gh issue list` misses the planning helper's compact fields, label defaults, issue-only filtering, and stable REST pagination. Use the GitHub plan helper for planning issue indexes.
      preferred:
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "index"]
          purpose: Lists durable planning issues with compact status, label, and milestone fields while excluding pull requests.
    - id: prefer-gh-plan-search-for-issue-search
      match:
        argv_prefix: ["gh", "search", "issues"]
      action: require_preferred
      message: Raw `gh search issues` skips the planning helper's normalized output and stale/duplicate plan cues. Use the GitHub plan helper for planning discovery.
      preferred:
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "search", "<query>"]
          purpose: Searches planning issues with compact normalized output and state handling.
    - id: prefer-gh-plan-helper-for-project-commands
      match:
        argv_prefix: ["gh", "project"]
      action: require_preferred
      message: Raw `gh project` commands bypass planning config, rate-limit checks, and Project field normalization. Use the GitHub plan helper for planning Project operations.
      preferred:
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-list", "--owner", "<owner>"]
          purpose: Lists configured Projects with compact JSON.
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-add", "<issue>"]
          purpose: Adds a planning issue to the configured Project.
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-set", "<issue>", "--focus", "Now"]
          purpose: Applies a Director-requested Project field edit through configured names and values.
    - id: prefer-gh-plan-helper-for-planning-graphql
      match:
        shell_regex: "\\bgh\\s+api\\s+graphql\\b.*\\b(updateProjectV2ItemFieldValue|addProjectV2ItemById|deleteProjectV2Item|addSubIssue|removeSubIssue|createLinkedBranch|markIssueAsDuplicate)\\b"
      action: require_preferred
      message: Raw planning GraphQL mutations are easy to leave half-applied and usually skip helper-owned recovery behavior. Use the GitHub plan helper for Project and relationship changes.
      preferred:
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-set", "<issue>", "--focus", "Next"]
          purpose: Applies explicitly requested Project edits with helper-owned config and rate-limit handling.
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "link", "<issue>", "blocked-by", "<target>"]
          purpose: Creates native planning relationships through the helper.
    - id: prefer-gh-plan-helper-for-milestone-commands
      match:
        shell_regex: "\\b(?:gh\\s+api|gh-with-env-token\\s+api)\\b[\\s\\S]*\\brepos/\\S+/\\S+/milestones"
      action: require_preferred
      message: Raw milestone REST calls bypass normalized due dates, exact-title conflict handling, actor-aware write envelopes, and guarded closure. Use the maintained gh-plan milestone commands.
      preferred:
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "milestone-list", "--state", "all"]
          purpose: Lists, shows, creates, updates, and guarded-closes milestones through the maintained REST helper.
---

# GitHub Plan

Use GitHub issues as the durable planning database: one canonical issue or
graph with an observable finish line, current status, next action, and accurate
dependencies. Keep fuzzy ideas in chat until they need a durable record. Projects
and other views display that record. Keep offline/private drafts in chat until
they are ready for a GitHub issue.

Apply [task scope and authorization](../references/execution-scope.md) and
[talking with the Director](../references/talking-with-the-director.md). Follow the
[executing loop](../references/executing-loop.md) for `next` and `go`.
Use `github` for PRs, Actions, and landing; use `direction` for changes to
`DIRECTION.md` or the Director's waypoints.

Before private operational lookup during planning, read `.github/github.json` when present and use
its task-relevant `docs` routes and declared `relatedRepos`. A related repo name
alone establishes no operational authority; use the scope its instructions or
docs assign.
If a declared route answers the task, use it without opening the local docs
fallback.
When the task needs private infrastructure facts, access paths, or operational
ownership and the declared routes do not answer it, make loading `docs-lookup`
your next step. Complete that load before following a local-context pointer
from repository instructions or opening its private document. Then use the
loaded skill to select and read the task-relevant source. Loading it after the
read does not cover that read. Missing metadata alone
does not require private lookup for ordinary source work; do not read private
operational docs unless the task needs those operational facts. The local
docs source supplies task-specific context, not a repository inventory. Keep
private identities and routing details out of public plans. If neither route
is configured, report the local documentation configuration gap and continue
independent planning from checked-in evidence.

Run the maintained planning helper from the client repository, using this
skill's base directory for the path:

```bash
uv run <skill-dir>/../github/scripts/gh-plan.py <command>
```

The frontmatter at the top of this file owns command-policy routing; read it
if the host hides it. Use the sibling `github/scripts/gh-issue`
and `github/scripts/gh-comment` for multiline issue/comment writes. If planning
helpers are unavailable, use `gh` with body files and compact JSON reads under
the `github` skill's authentication rules. Keep GitHub issues as the durable
record; never bypass an ownership or dependency refusal by changing tools or
identities.

## Agent Assignment

`agent:claude` and `agent:codex` route an issue to that agent family. An
unlabeled issue is available to either family; a specific model stays in issue
text. `ensure-labels` creates both routing labels alongside the planning labels.
Apply assignments only after the installed skills support this convention.

`next --agent claude|codex` selects that family and unlabeled issues, and reports
other-family issues as `assigned_elsewhere`. Without the flag, the helper detects
the active harness when only one family’s session markers are present. When
markers conflict or are absent, it checks the bounded parent-process chain for
an executable named `codex` or `claude`, including Codex's remote app-server
daemon. An unfamiliar executable or script runtime stops the lookup; version-named
Claude workers still need markers or explicit selection. If the chain cannot
identify the worker, pass `--agent claude|codex` for the actual
running family. When detection is unavailable
or both routing labels are present, labeled issues remain excluded; identify the
running family with `--agent`, or resolve the conflicting assignment.

Before `go`, `claim` checks the labels again and stops on a mismatch, naming the
label. An explicit Director override for this session permits
`claim --agent-override "<recorded decision or user instruction>"`; the reason is
recorded in the claim and does not change the assignment or other claim gates.
Do not use `--agent` to impersonate the assigned family.

Assign new issues with `gh-plan.py create ... --agent claude|codex` or
`gh-issue create ... --agent claude|codex`. Creation ensures the requested label
exists and rejects conflicting family labels. It never infers an assignment
from the authoring harness.

Searches, `index`, and global `next` repository discovery exclude archived repositories
by default. Add `--include-archived` to the command to include historical work.
Explicit issue reads and linked dependency evidence remain available.

## Choose Work

Check known repository-wide Director holds before selection or implementation;
active issue labels and continuing background jobs do not lift a hold.

1. Run `gh-plan.py next`. Respect native `blocked-by` relationships and the
   merged `DIRECTION.md` milestone order; without that file, milestone creation
   order applies. Within a milestone, prioritize dependency impact, then issue
   age. Project Focus is context, not an override.
2. Read the candidate with `show <issue> --full`: original request, finish
   line, Current Status, blockers, and all comments. Reconcile comment-only
   requirements before recommending or implementing it.
   For milestone admission, apply the [milestone contract](references/milestones.md)
   before citing a Track.
3. Check current ownership using available issue/PR/branch/worktree evidence
   and supported read-only task/session tools. Recommend the highest-ranked
   available independent item; report work owned by another active worker as
   underway. A label, assignee, old PR, or old worktree alone does not prove
   active ownership; a partial session inventory does not prove availability.
   When a session tool can message the apparent owning session, ask it directly
   rather than asking the Director to relay.
4. Reuse preserved work only for this session's continuation or a verified
   handoff from a finished session, under the repository's worktree rules.
   Do not start duplicate implementation or take over another worker's worktree.
   If ownership remains uncertain, ask whether to resume the item or leave it
   with its recorded holder, with a recommendation; keep this decision visible
   while recommending independent work.
5. For `next`, report the selected issue, why it fits the plan, and recorded
   waits, then stop without changing planning state. On authorized `go`, run
   `gh-plan.py --repo OWNER/REPO claim <issue> --worker <worker> --session <session-id>
   --branch <task-branch> --next-action "<action>"` before creating a
   branch or worktree, adding `--admission <kind>` when the overall Order
   admits the work: live-site breakage, a named `milestone` item (cite its overall Track
   with `--admission-link`; admission records its citation without changing the graph), a
   `repeat-stop` with both occurrences, `own-project` work from the Order's
   weekly own-projects share, or `spare-capacity`. Name the
   real kind: work that clears a milestone blocker is milestone work once
   linked, not spare capacity. Evidence may cite a decision comment, for example
   `--admission spare-capacity --admission-link https://github.com/OWNER/REPO/issues/123#issuecomment-456`.
   The claim retains that exact comment URL; admission grants no authority.
   A repository milestone absent from overall direction uses its open native
   milestone membership and that repository's merged `DIRECTION.md` instead;
   do not invent an overall Track dependency for it.
   A citation into the overall direction repository must still name a valid Track.
   Claim using exactly the branch the worktree helper will create
   (`work/<task-slug>` for the catalog's `github/scripts/dev-worktree`). Read
   [task worktrees](../github/references/worktrees.md) for its callable path,
   private per-host root setting and host integration. On a host with a guarded
   helper, keep its current command until `hostHelper` is provisioned rather
   than using the portable default. Read
   [Planning: Claim](../github/references/cli-reference.md#planning-claim)
   for refusal, partial recovery, or release. Continue only on confirmed success;
   preserve competing or uncertain ownership for the Director to decide. Keep
   Current Status current through handoff or completion, then release with the
   claim's printed `release_command` (`gh-plan.py release`), never a
   hand-written release comment.
   Claim waits come only from native blockers, labels and the structured wait
   record. Before a legacy record's first claim, run `gh-plan.py wait <issue>`,
   read its full evidence once and record every real wait/executing-session hold or verified
   absence with `wait --waiting-for ... --ends-when ... --evidence ...` or
   `wait --clear --evidence ...`. `wait --all` reports unreviewed open issues
   without clearing them. For new plans, `create --wait-clear --wait-evidence ...`
   or `create --waiting-for ... --ends-when ... --wait-evidence ...` records the
   reviewed request directly. Maintain waits with `wait`, not status prose;
   preserve all unresolved entries when replacing the whole record. Read
   [Planning: Claim](../github/references/cli-reference.md#planning-claim)
   for the format, authorship and existing-resolution contract.
   Claims automatically account for exact authored releases and expired
   claim leases, reporting preserved leftovers in `retained_artifacts`. An
   unreleased claim whose host-local lease is live or unknown still refuses.
   For an abandoned automation claim with no lease on this host, a direction or
   Supervisor session uses `release-claim`, with a closed-session attestation
   and fresh activity checks. Read Planning: Claim first; a merged PR never
   proves closure. This releases ownership only;
   recorded waits, native blockers and retained-artifact gates still apply.
   A session killed without closeout uses the linked Supervisor crash-recovery
   route with `--termination-record`, fresh source-host checks and preserved
   artifacts; it does not need a safe-exit verdict it never wrote.
   For an authorized PR-only conflict refresh of a finished session, claim its
   canonical open planning issue with `--resume-from <original-claim-id>`;
   read Planning: Claim for automatic retained-handoff qualification and the
   supported route before claiming. For PR-only work, use
   [Planning: Claim](../github/references/cli-reference.md#planning-claim).
   Active ownership still refuses.
   For verified pushed WIP without a PR on this same issue, follow
   [Planning: Claim's ordinary new-branch procedure](../github/references/cli-reference.md#planning-claim).
   For ordinary successor work before a retained PR can be refreshed, use
   `--resume-from <original-claim-id>`. Read
   Planning: Claim for the exact released multi-branch handoff proof; all
   retained artifacts and their recorded waits are checked. This claim does
   not lift a product hold or authorize changing the retained PRs.
   A native child in the same repository may use its parent's released claim
   and handoff IDs for that source slice; the helper verifies the native link
   and parent ownership on both readbacks and preserves the parent's waits.
   Ownership checks traverse the recorded source family across generations;
   releasing an intermediate or closing a child does not hide another holder.
   Read Planning: Claim before using this cross-record route.

For a global invalid-wait finding, read the
[supported wait correction procedure](references/global-next.md#correct-an-invalid-wait-before-claiming)
before correcting status or claiming. Global selection does not clear a recorded
wait; repo-local `next` and normal claim preflight retain it.

Also report each `blocking_work_elsewhere` pair from `gh-plan.py next`, naming
the local blocker and the repository/issue it holds up. Read the blocker's
recorded wait and current ownership before proposing action; visibility does
not override candidate exclusions or authorize starting it. Report incomplete
`blocking_work_elsewhere_context` separately from candidate/dependency coverage,
including truncation, unread gates, and unavailable reads.

Also report `dependabot_candidates` as Dependabot and Renovate PR work needing
an agent, with each PR's link and age. Supervisor and executing-loop selection
use this same candidate read. Read the PR discussion and check current ownership
before taking it on; the PR is its work record. For authorized PR-only work, claim the PR
through [Planning: Claim](../github/references/cli-reference.md#planning-claim)
before creating a linked worktree, then load `github` and follow its existing-PR
workflow. Use a linked canonical implementation issue when one exists.
Uncertain or competing ownership remains a stop.
Report `dependabot_unverified_candidates` and incomplete `dependabot_context`
as uncertainty. Read [Next Work](../github/references/cli-reference.md#planning-next-work)
for the PR discovery bounds. An empty issue list with PR candidates does not
mean no work is available. This visibility changes no priority, hold, or merge
authority.

`pr_followthrough` preserves a compatibility view of other open PRs. Global
`next` also puts PRs through the same `Next actor` queue and ranking as issues,
using one complete inventory by default. Each row keeps the current head and bounded
discussion excerpts with source links, including Client projections. Current
Ready handoffs and claims are underway; drafts, Client-wait handoffs and
structured waits stay held. Legacy or incomplete evidence
stays scope review or unknown. These rows require fresh source custody,
Client authority and readiness checks before action; they never clear an
issue wait or authorize a merge. `pr_followthrough_context` reports read
coverage and truncation independently of dependency PR coverage.

Check beyond occupied results before saying no work is available.
`candidate_count` greater than the returned list calls for a larger bounded
`--limit`; truncated inventory or incomplete dependencies remain a partial
answer. Report those limits and what is underway or waiting. An empty available
list does not establish milestone completion. Ownership checks and status
records do not provide an exclusive lock.

In an `<owner>/direction` repository, `next` follows `Track:` issues and also
discovers open issues in the repository owner's repositories that the
configured actor can access;
`gh-plan.py --repo <owner>/direction next` selects the same scope elsewhere.
Read the [global selection procedure](references/global-next.md) for this mode.
Graph coverage, repository discovery, and active ownership are separate evidence.
Apply repository-wide Director holds before considering any issue there, even
when labels say active or background work continues. Read full discussions and
current ownership evidence before recommending discovered work. Raw candidates
are possible work; `available_candidates` carries the observed queue evidence. A queue row still requires Choose
Work and fresh claim preflight; partial peer coverage never establishes an
exclusive lock.
The Director’s current overall Order owns work priority and tooling admission.
The linked global selection procedure defines the milestone availability flag;
the Supervisor and weekly check judge tooling admission.
Keep the own-project share available when business
tracks wait. Weekly capacity is audit context, not a per-call quota. Discovery
grants no write, merge, deployment, or direction-adoption authority under
another repository owner.
For graph paths, scope, or incomplete coverage, read
[Planning: Next Work](../github/references/cli-reference.md#planning-next-work).

## Create Or Update A Plan

Read `.local/github-plan.md` when present before creating, routing, or updating
plans. Resolve people through the optional `people` skill and local overlays;
absence of people context is normal. Verify unknown actors' claims and
permissions before routing or relying on them. Mention someone only when their
attention is needed now; assignees name the person with a concrete next action.

Search with `index` or `search` before creating or proposing an issue, helper,
or tooling; reuse an overlapping canonical issue. Search open and closed issues
across the repository owner's repositories, not only this one:
`gh-plan.py search "<terms> user:<owner>" --state all`. Say what you found or
what you searched. Read its full discussion before changing scope. Use the configured
planning label, normally `plan`, and existing label conventions; ask before
creating labels other than the `friction` label that
[task scope](../references/execution-scope.md) names.

Treat every human-authored title and body as immutable source material.
Repository role grants permissions, not automation ownership of authored words.
Never retitle or replace someone's request as plan maintenance. Use an authorized
bot-authored planning comment or linked maintainer-owned plan, or change only an
established automation-managed block. Before creating or editing an issue, read
[issue templates and ownership](../github/references/issue-templates.md);
inspect the helper's provenance and keep unknown ownership or disallowed
section updates read-only.

Use `create` for a new plan and `update-section` for an owned section. Use body
files or stdin for multiline content through the maintained helpers. Correct
Current Status on waiting issues without an execution claim; the writer
preserves wait and ownership records. Use `wait` for wait changes; source edits
and execution still require their normal claim.
Keep the finish line observable, with scope, acceptance evidence, and one next action.
Size implementation issues and record event waits under GitHub's
[issue closing and post-merge proof](../github/references/repo-workflow.md#issue-closing-and-post-merge-proof).
Keep Current Status concise:

```text
Next actor:
Next action:
Last verified:
```

Follow the [actor write contract](../github/references/cli-reference.md#planning-claim)
for claim, release and field-only status edits.

Maintain dependencies with `link`/`unlink` and person/event holds with `wait`;
`show` projects their current state and blockers. Record decisive evidence and
links needed to resume. Keep raw logs and lengthy validation details in the PR or linked evidence. Completed prerequisites belong
in Relationships, not the current blocker list.

### Broad Workstreams

Use a parent issue plus independently finishable sub-issues when any two apply:

- the work touches three or more modules, repositories, systems, or owning teams
- it has independent sequencing, blockers, or parallel tracks
- it includes research, implementation, validation, and policy/design decisions
- parts can finish or be reviewed independently
- tracking must survive this session

Keep intent, finish line, dependency order, and recovery state on the parent;
give each child its own finish line and next action.

### Relationships

Use native `blocked-by`, `blocks`, and `subissue` links for execution
dependencies and decomposition, including across repositories. `related`
provides context without changing sequencing. Body references explain links
but do not replace them.

Native relationship changes do not rewrite either issue's body and do not
require body ownership; they still require authorization for the relationship
write. `related` changes Markdown, so body-ownership rules apply.

### Missing Cross-Repository Gates

When a maintainer must create or identify a prerequisite, record who returns
the canonical links, the return thread, and who verifies and connects the native
blockers afterward. Explicitly request that return within existing posting
authority; otherwise prepare the draft and name the remaining action. Read the
[missing-gate template](../github/references/issue-templates.md#waiting-on-an-external-gate)
for the return request. Once the gate is known, verify and link it without
adding another handoff.

## Milestones And Status

Treat milestones as release or phase-exit gates. Before assigning an issue or
creating, changing, assessing, or closing a milestone, read the
[milestone contract](references/milestones.md), its description, and its open
issues. With `DIRECTION.md`, only listed milestone titles are eligible; propose
new waypoints through `direction`.

## Status Labels

Use status labels narrowly:

Status labels are views of native issue state, open `blocked-by` edges and the
authoritative structured wait record. `show`, `next` and the Supervisor goal
board derive those values; old prose or hand-applied blocked/waiting labels do
not override reviewed graph evidence. Maintain waits with `wait` and
dependencies with `link`/`unlink`, rather than copying blocker lists into
Current Status. Missing or malformed wait evidence still requires review.
Landing reconciliation and issue closure refresh native parents and dependents
without inferring their finish lines or changing human-authored bodies. Open
`stale`, `done` and unstatused planning scopes retain their review holds, except
`done` left on a reopened issue; a
closed issue retains `waiting` when its structured person/event wait remains.

- `plan:active`: actionable now, including work whose next actor is any agent.
  Keep actionable unstarted follow-ups, capacity/selection queues, and Supervisor
  handoffs active. Put the agent's next step in `Next action:`; record verified
  absence of person/event waits through `wait --clear --evidence ...`. Ordinary
  delivery awaiting agent or automated QA, review,
  merge-train routing, or deployment stays active too.
- `plan:blocked`: a current dependency on an open issue linked by native
  `blocked-by`.
- `plan:waiting`: a durable plan parked on a named person, decision, or external
  event. Record each actor/event and ending condition through
  `wait --waiting-for ... --ends-when ... --evidence ...`, preserving unresolved
  entries.
- `plan:stale`: needs review before guiding work.
- `plan:done`: completed or deliberately superseded.

Separate independent actionable work from an unresolved person or event wait
into its own issue, so it can be selected and claimed without clearing that wait.
For `plan:waiting`, the structured record names the actor/event and ending
condition; the derived blocker list contains native open issue blockers only.

During an authorized planning update, verify and link an open prerequisite with
`gh-plan.py link <issue> blocked-by <prerequisite>`.
A prose reference or milestone membership does not create that edge. If the
prerequisite has closed, reconcile the remaining work under these status labels. If a
maintainer must identify or create it, follow [Missing Cross-Repository
Gates](#missing-cross-repository-gates). Record actual person/event waits with
`wait`; the projection chooses the labels. To repair labels after a confirmed
fact write, use `reconcile-status <issue> --apply` without repeating that write.
A parent delegated only to open sub-issues stays `plan:active`; native sub-issue
links express that delegation without a `blocked-by` edge.

When Projects are configured or requested, use them as automatic views of the
issue graph. Do not maintain Focus, Manager, Finish Line, or roadmap dates in
Projects. Read [Project views](../github/references/github-projects.md) when
using a Project or local context surface, including explicit field requests,
synchronization, or access failures; views never replace the issue graph. If a
configured context helper is useful, read its Local Context Views guidance before
running `index`.

## Keep The Plan Current

Run a Plan Direction Checkpoint at implementation boundaries, surprising findings,
adjacent work, handoff, a "what's next" request, and before creating, closing, or
superseding issues: reconnect the next action to the current plan. Update Current
Status only when durable recovery state materially changes; a passing checkpoint
needs no artifact.

If scope changes, reconcile the canonical issue, sub-issues, blockers, and labels
before pivoting. Classify discoveries as current scope,
sub-issue, blocker, related context, or later work. Record Director decisions so a
future session does not ask again. Keep recovery-critical state in the issue;
link implementation evidence where it already lives under
[Agent Comments](../references/talking-with-the-director.md#agent-comments).

When the Director's reply is short or points elsewhere ("as recorded above"),
read every comment since the question, including bot `Director decision:`
records, and act on the record's wording. When an issue's finish line
paraphrases a quoted decision, build to the quote; ask on the issue before
adding any destructive or behavior-changing rule the quote does not contain.

While another workflow waits on CI, deployment, or review, use independent
read-only work or isolated preparation within authorization; return to the
waiting workflow before calling it complete.

## Close Or Hand Off

Stale GitHub planning state is a regression source. Before closeout, handoff,
or declaring a workstream done, search related issues. Update every related
issue whose Current Status, labels, blockers, relationships, or acceptance
criteria changed. Reconcile stale or duplicate plans rather than leaving
corrections only in chat or PR comments. Record the winning and superseded PRs
on the owning issue when competing PRs are resolved.

Before calling the plan captured or complete, verify that stale, duplicate,
related, and PR-linked issues were swept, the canonical graph has the needed
parent/children and blockers, and its finish line and next action match reality.

For completed durable plan issues, use `gh-plan.py close`. It owns `plan:done`
labels, cleanup of stale `plan:active`, `plan:blocked`, `plan:waiting`, and
`plan:stale` labels, and Project Status on close. The generic
`github/scripts/gh-issue close` helper is for non-plan issues, or when the plan
helper is unavailable. Closing a durable plan with the generic issue helper can
leave planning labels or Project Status stale.
It also skips the relationship and not-planned decision preflight. Before using
that fallback, read and apply the
[closure decision contract](../github/references/cli-reference.md#planning-management),
including its identity, timing, and edit-history checks.

Before closing a planning issue, run
`uv run <skill-dir>/../github-work-rollup/scripts/github_unanswered_comments.py --thread OWNER/REPO#NUMBER`.
Any attention result or degraded coverage requires a response or explicit
handoff; apply the [radar rule](../github-work-rollup/SKILL.md#external-comment-radar).
`close` itself
refuses while an outside person's comment on the issue or a thread it links has
no reply, naming each one: read and answer it on its thread, then rerun. Only a
Director decision passed as `--unanswered-decision` closes over it; read
[Planning: Management](../github/references/cli-reference.md#planning-management)
before using it.

Completed closure requires all native blockers and sub-issues closed and their
reads complete. Issues the plan blocks do not prevent its closure. Use
`--reason not_planned` only for explicitly superseded or abandoned plans;
retained open relationships are not completion evidence. For an issue in a
milestone listed in merged `DIRECTION.md`, a `not_planned` close of Director-opened
or Director-admitted work requires the Director's approval. Follow the
[closure decision contract](../github/references/cli-reference.md#planning-management)
for agent scope cuts and their required reason.
Record the exact action in a new comment; never edit existing question or
decision comments to migrate them, or bypass a closure refusal through another tool.

Choose PR closing language under GitHub's
[issue closing and post-merge proof](../github/references/repo-workflow.md#issue-closing-and-post-merge-proof).
After a landing, reconcile issues
referenced by the canonical PR body and comments, their native parents, and
every issue it unblocks,
including cross-repository dependents, within
[existing posting authority](#missing-cross-repository-gates). Check remaining
waits against the landed evidence; update Current Status, labels and native
relationships under [Status Labels](#status-labels), preserving unresolved waits,
blockers and Director holds. Close only issues whose finish lines are satisfied;
keep remaining work with its next actor under
[issue closing and post-merge proof](../github/references/repo-workflow.md#issue-closing-and-post-merge-proof).
Use `gh-plan.py close --comment-file` for durable plan issues.

For closure failures, partial Project synchronization, quota or retry/reconciliation,
or helper output details, read
[Planning: Management](../github/references/cli-reference.md#planning-management)
and the shared API contract there. Keep degraded evidence visible; do not
manually mark an issue done while closure is uncertain or silently switch to
human authentication.

Local handoff files are scratch unless the Director requested offline/private
handoff. Migrate recovery-critical content to the owning issue or PR before
closeout, then remove or explicitly preserve the scratch file. For valuable
local work found during cleanup, read
[repository preservation](../references/repo-cleanup.md) and
[parking and handoff](../work-closeout/references/parking-and-handoff.md).
Routine disposable artifacts do not need planning issues.

Finish with the current outcome, evidence or uncertainty, next action and who
takes it, and any Director decisions still open. Use the executing loop's handoff
rules for its commands.
