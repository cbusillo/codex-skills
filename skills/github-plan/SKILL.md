---
name: github-plan
description: Use when the user asks for a plan, what's next / what is next in a plan or workstream, how work fits the plan, plan direction/alignment, durable work tracking, roadmap, workstream planning, GitHub issue-backed planning, issue graphs, parent issues, sub-issues, blockers, milestones, Projects, or replacing local plans with GitHub issues. Think in chat first, then keep long-running work aligned over time by updating Current Status, blockers, relationships, and issue graph state as reality changes.
metadata:
  short-description: Plan durable work in GitHub issues
commands:
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
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "create", "<title>", "--body-file", "<file>"]
    purpose: Creates a durable plan issue with helper-owned labels and Project fields.
  - name: github-plan-update-section
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "update-section", "<issue>", "Current Status", "--body-file", "<file>"]
    purpose: Updates one markdown section of a planning issue safely.
  - name: github-plan-project-set
    source: repo
    example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-set", "<issue>", "--focus", "Next"]
    purpose: Updates configured Project fields through the planning helper.
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
          purpose: Updates planning Project fields through configured names and values.
    - id: prefer-gh-plan-helper-for-planning-graphql
      match:
        shell_regex: "\\bgh\\s+api\\s+graphql\\b.*\\b(updateProjectV2ItemFieldValue|addProjectV2ItemById|deleteProjectV2Item|addSubIssue|removeSubIssue|createLinkedBranch|markIssueAsDuplicate)\\b"
      action: require_preferred
      message: Raw planning GraphQL mutations are easy to leave half-applied and usually skip helper-owned recovery behavior. Use the GitHub plan helper for Project and relationship changes.
      preferred:
        - kind: script
          path: ../github/scripts/gh-plan.py
          example_argv: ["uv", "run", "../github/scripts/gh-plan.py", "project-set", "<issue>", "--focus", "Next"]
          purpose: Updates Project fields with helper-owned config and rate-limit handling.
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
and other views display that record; use local plans only for an explicitly
requested offline/private workflow.

Apply [task scope and authorization](../references/execution-scope.md) and
[talking with the owner](../references/talking-with-the-owner.md). Follow the
[executing loop](../references/executing-loop.md) for `next` and `go`.
Use `github` for PRs, Actions, and landing; use `direction` for changes to
`DIRECTION.md` or the owner's waypoints.

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

## Choose Work

Check known repository-wide owner holds before selection or implementation;
active issue labels and continuing background jobs do not lift a hold.

1. Run `gh-plan.py next`. Respect native `blocked-by` relationships and the
   merged `DIRECTION.md` milestone order; without that file, milestone creation
   order applies. Within a milestone, prioritize dependency impact, then issue
   age. Project Focus is context, not an override.
2. Read the candidate with `show <issue> --full`: original request, finish
   line, Current Status, blockers, and all comments. Reconcile comment-only
   requirements before recommending or implementing it.
3. Check current ownership using available issue/PR/branch/worktree evidence
   and supported read-only task/session tools. Recommend the highest-ranked
   available independent item; report work owned by another active worker as
   underway. A label, assignee, old PR, or old worktree alone does not prove
   active ownership; a partial session inventory does not prove availability.
4. Reuse preserved work only for this session's continuation or a verified
   handoff from a finished session, under the repository's worktree rules.
   Do not start duplicate implementation or take over another worker's worktree.
   If ownership remains uncertain, ask whether to resume the item or leave it
   with its recorded owner, with a recommendation; keep this decision visible
   while recommending independent work.
5. For `next`, report the selected issue, why it fits the plan, and recorded
   waits, then stop without changing planning state. On `go`, recheck ownership
   and, where posting is authorized, record the worker/session, branch, and next
   action in owned Current Status or a bot-authored planning comment before
   implementation, then read back for competing activity. Keep that record
   current through handoff or completion.

Check beyond occupied results before saying no work is available.
`candidate_count` greater than the returned list calls for a larger bounded
`--limit`; truncated inventory or incomplete dependencies remain a partial
answer. Report those limits and what is underway or waiting. An empty available
list does not establish milestone completion. Ownership checks and status
records do not provide an exclusive lock.

In an `<owner>/direction` repository, `next` follows `Track:` issues and also
discovers open issues in the configured actor's accessible owner repositories;
`gh-plan.py --repo <owner>/direction next` selects the same scope elsewhere.
Read the [global selection procedure](references/global-next.md) for this mode.
Graph coverage, repository discovery, and active ownership are separate evidence.
Apply repository-wide owner holds before considering any issue there, even
when labels say active or background work continues. Read full discussions and
current ownership evidence before recommending discovered work. Raw candidates
are possible work; `available_candidates` requires current caller review.
Live breakage comes first, then eligible milestone work; unrelated tooling needs
two linked repeated stops. Keep the own-project share available when business
tracks wait. Weekly capacity is audit context, not a per-call quota. Discovery
grants no cross-owner write, merge, deployment, or direction-adoption authority.
For graph paths, scope, or incomplete coverage, read
[Planning: Next Work](../github/references/cli-reference.md#planning-next-work).

## Create Or Update A Plan

Read `.local/github-plan.md` when present before creating, routing, or updating
plans. Resolve people through the optional `people` skill and local overlays;
absence of people context is normal. Verify unknown actors' claims and
permissions before routing or relying on them. Mention someone only when their
attention is needed now; assignees name the person with a concrete next action.

Search with `index` or `search` before creating; reuse an overlapping canonical
issue. Read its full discussion before changing scope. Use the configured
planning label, normally `plan`, and existing label conventions; ask before
creating labels.

Treat every human-authored title and body as immutable source material.
Repository role grants permissions, not automation ownership of authored words.
Never retitle or replace someone's request as plan maintenance. Use an authorized
bot-authored planning comment or linked maintainer-owned plan, or change only an
established automation-managed block. Before creating or editing an issue, read
[issue templates and ownership](../github/references/issue-templates.md);
inspect the helper's provenance and keep unknown ownership or disallowed
section updates read-only.

Use `create` for a new plan and `update-section` for an owned section. Use body
files or stdin for multiline content through the maintained helpers. Keep the
finish line observable, with scope, acceptance evidence, and one next action.
Keep Current Status concise:

```text
State:
Next action:
Blocked by:
Waiting for:
Last verified:
```

Record decisive evidence and links needed to resume. Keep raw logs and lengthy
validation details in the PR or linked evidence. Completed prerequisites belong
in Relationships, not the current blocker list.

### Broad Workstreams

Use a parent issue plus independently finishable sub-issues when any two apply:

- the work touches three or more modules, repositories, systems, or owners
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
for the waiting record. Once the gate is known, verify and link it without
adding another handoff.

## Milestones And Status

Treat milestones as release or phase-exit gates. Before assigning an issue or
creating, changing, assessing, or closing a milestone, read the
[milestone contract](references/milestones.md), its description, and its open
issues. With `DIRECTION.md`, only listed milestone titles are eligible; propose
new waypoints through `direction`.

Use status labels narrowly:

- `plan:active`: actionable now.
- `plan:blocked`: a current dependency, preferably an open native blocker.
- `plan:waiting`: a durable plan parked on a named person, decision, or event.
  Do not apply it to ordinary bugs or PRs awaiting QA, review, or deployment.
- `plan:stale`: needs review before guiding work.
- `plan:done`: completed or deliberately superseded.

Do not label an item blocked just because it is out of focus. Without an open
native blocker, prefer `plan:waiting`; use `Waiting for:` or `Parked until:`
with the concrete condition.
For a blocking non-issue condition, say `Blocked by: No native issue blocker;
waiting for ...`.

When Projects are configured or requested, use the small set of human-facing
fields and Focus lanes in [Projects and roadmaps](../github/references/github-projects.md).
Prefer one `Now` item unless the owner chooses parallel work. Read that reference
when using a Project or local context surface, including synchronization or
access failures; views never replace the issue graph. If a configured context
helper is useful, read its Local Context Views guidance before running `index`.

## Keep The Plan Current

Run a Plan Direction Checkpoint at implementation boundaries, surprising findings,
adjacent work, handoff, a "what's next" request, and before creating, closing, or
superseding issues: reconnect the next action to the current plan. Update Current
Status only when durable recovery state materially changes; a passing checkpoint
needs no artifact.

If scope changes, reconcile the canonical issue, sub-issues, blockers, labels,
and configured Focus before pivoting. Classify discoveries as current scope,
sub-issue, blocker, related context, or later work. Record owner decisions so a
future session does not ask again. Keep detailed implementation evidence in
the PR and recovery-critical state in the issue.

While another workflow waits on CI, deployment, or review, use independent
read-only work or isolated preparation within authorization; return to the
waiting workflow before calling it complete.

## Close Or Hand Off

Stale GitHub planning state is a regression source. Before closeout, handoff,
or declaring a workstream done, search related issues. Update every related
issue whose Current Status, labels, blockers, relationships, or acceptance
criteria changed. Reconcile stale or duplicate plans rather than leaving
corrections only in chat or PR comments.

Before calling the plan captured or complete, verify that stale, duplicate,
related, and PR-linked issues were swept, the canonical graph has the needed
parent/children and blockers, and its finish line and next action match reality.

For completed durable plan issues, use `gh-plan.py close`. It owns `plan:done`
labels, cleanup of stale `plan:active`, `plan:blocked`, `plan:waiting`, and
`plan:stale` labels, and Project focus updates. The generic
`github/scripts/gh-issue close` helper is for non-plan issues, or when the plan
helper is unavailable. Closing a durable plan with the generic issue helper can
leave planning labels or Project fields stale.
It also skips the relationship and owner-decision preflight; perform those
checks below yourself before using that fallback.

Before closing a planning issue, run
`uv run <skill-dir>/../github-work-rollup/scripts/github_unanswered_comments.py --thread OWNER/REPO#NUMBER`.
Any attention result or degraded coverage requires a response or explicit
handoff; a bot response never proves owner acknowledgement.

Completed closure requires all native blockers and sub-issues closed and their
reads complete. Issues the plan blocks do not prevent its closure. Use
`--reason not_planned` only for explicitly superseded or abandoned plans;
retained open relationships are not completion evidence. For an issue in a
milestone listed in merged `DIRECTION.md`, a `not_planned` close requires the
owner's decision comment after the last Current Status update. Never bypass a
closure refusal through another tool.

Prefer non-closing `Refs` from PRs unless the owner requests auto-close or an
internal task is conclusively complete. After merge, inspect referenced issues:
close only those whose finish lines are satisfied; otherwise record what
remains. Use `gh-plan.py close --comment-file` for durable plan issues.

For closure failures, partial Project synchronization, quota or retry/reconciliation,
or helper output details, read
[Planning: Management](../github/references/cli-reference.md#planning-management)
and the shared API contract there. Keep degraded evidence visible; do not
manually mark an issue done while closure is uncertain or silently switch to
human authentication.

Local handoff files are scratch unless the owner requested offline/private
handoff. Migrate recovery-critical content to the owning issue or PR before
closeout, then remove or explicitly preserve the scratch file. For valuable
local work found during cleanup, read
[repository preservation](../references/repo-cleanup.md) and
[parking and handoff](../work-closeout/references/parking-and-handoff.md).
Routine disposable artifacts do not need planning issues.

Finish with the current outcome, evidence or uncertainty, next action and its
owner, and any owner decisions still open. Use the executing loop's handoff
rules for its commands.
