# GitHub Repo Workflow

Use this reference for repository operations that need current GitHub and local
state: PRs, issues, Actions, reviews, merge/deploy state, QA handoff, recent
merges, branch cleanup, or worktree cleanup.

For ordinary task cleanup, an explicit bulk repository/worktree audit, or
repository retirement, read the shared
[repository cleanup and preservation](../../references/repo-cleanup.md). It owns
the evidence, disposition, preservation, authorization, and result-reporting
contract; this reference continues to own GitHub execution and remote actions.

## Orientation

Start with a factual snapshot before making claims about readiness, PR state,
reviews, Actions, deploys, or cleanup.

`<skill-dir>` is the `github` skill's base directory, the folder that holds its
`SKILL.md`; your host shows it when the skill loads. Run these from the client
repository.

```sh
<skill-dir>/scripts/github-repo-snapshot.sh
<skill-dir>/scripts/github-repo-snapshot.sh --json
<skill-dir>/scripts/github-repo-snapshot.sh --fetch --json
```

The snapshot keeps local git evidence local, but reads issues, Actions runs,
repository settings, and PR fallbacks through shared paged REST helpers. JSON
output includes `github.diagnostics.components` with request IDs, quota headers,
observed/expected actor identity when active-auth fallback occurs, and explicit
degraded components. A missing or unauthorized GitHub component
does not erase the rest of the snapshot; do not treat an unavailable component
as an empty authoritative result. Launchplane routing metadata with a string
`context` is reported under `launchplane.routing`; helper-object metadata stays
under `launchplane.context`. Invalid Launchplane metadata carries warnings.
A failed JSON projection returns nonzero instead of an empty successful snapshot.
Use `launchplane.mergeTrain` for landing under the GitHub skill's
[Merging](../SKILL.md#merging) procedure. `not_configured` is local configuration
absence; that procedure checks the Director's overall direction and all
applicable instructions before using a protected-branch route. A Director
without Launchplane needs no policy read or Launchplane repair.
Missing repository routing metadata alone cannot establish absence;
configured-but-unreadable enrollment stays `unknown`.

Summarize only actionable state:

- current branch and current branch PR
- open PRs and whether they are waiting, blocked, or ready
- failed or in-progress Actions that matter
- deploy health and deployed commit when relevant
- dirty files, extra worktrees, or stale local branches
- what is ours to do versus waiting on someone else

If the current branch has a PR, inspect that PR before relying on repo-wide
lists. Prefer the PR helper for repeated PR metadata, check, and
merge-readiness reads. Normal agent polling should let one maintained script
manage REST-first defaults, quota pressure, and degraded output:

```sh
<skill-dir>/scripts/gh-pr.py --repo OWNER/REPO view <pr-or-url>
<skill-dir>/scripts/gh-pr.py \
  --repo OWNER/REPO list --state open --limit 20
<skill-dir>/scripts/gh-pr.py --repo OWNER/REPO checks <pr-or-url>
<skill-dir>/scripts/gh-pr.py --repo OWNER/REPO rate-limit
```

The PR helper is REST-first for normal orientation. Standalone `gh-pr.py view`
derives `reviewDecision` from REST pull request reviews
(`readiness_evidence.review_decision_source: "rest_reviews"`, reflecting latest
reviewer states rather than branch-protection required approval counts) when a
decisive review exists, and omits `reviewDecision` (reporting
`readiness_evidence.review_decision: "not_read"`) when no decisive review is
present, in delegated `watch` polling, or in `gh-pr.py list`, while
`statusCheckRollup` remains intentionally nullable so ordinary polling does not
spend GraphQL quota. Use GraphQL-backed `gh pr view --json
statusCheckRollup,reviewDecision` only when branch-protection-evaluated review
requirement or rollup data is needed.
The PR babysitter's readiness boundary is the maintained exception: after
complete REST check evidence and clean mergeability are observed, it issues
one short-deadline, same-actor GraphQL document pinned to the exact PR head.
Successful `reviewDecision: null` remains nullable and is accepted only with
same-document `mergeStateStatus: CLEAN`; transport errors, partial responses,
missing fields, and head mismatches remain unavailable readiness evidence.
If a workflow wait by name reports no runs found, or GitHub returns transient
mergeability/rollup states such as `mergeable: UNKNOWN` or queued
`statusCheckRollup` entries, switch to `gh-pr.py checks <pr-or-url>` for PR-head
check state instead of repeatedly polling workflow names or GraphQL rollups.
Do not use raw `gh pr checks --watch` or `gh run watch` as the normal PR wait
loop; use `babysit-pr` when checks need ongoing PR-level follow-through.
When `github-repo-snapshot.sh --json` cannot use `gh-pr.py`, PR entries are
explicitly degraded with `snapshotReadiness.degraded: true` and no merge
readiness. Do not make readiness or merge decisions from that fallback shape;
run `gh-pr.py view` and `gh-pr.py checks` first.

If a deploy health endpoint reports a revision or tag, compare it with the
merge commit, PR head, or branch SHA the task cares about.

Use the bundled GitHub helpers before raw `gh`. They are intentionally shaped
for agent use: stable command ergonomics, structured output, safer multiline
body handling, centralized auth and retry behavior, and one maintained place to
improve workflows. For raw PR, run, or review commands that do not yet have a
dedicated helper, route through `gh-with-env-token`.

## Protected Workflow Dispatch And Waiting

Use `github_workflow_babysit.py` instead of dispatching a protected admin
workflow and then polling `gh run` by name:

```sh
uv run github/scripts/github_workflow_babysit.py dispatch \
  --repo OWNER/REPO \
  --workflow operator.yml \
  --ref main \
  --field mode=dry_run \
  --approve-environment protected-admin \
  --timeout-seconds 1800
```

The helper uses the configured automation token for dispatch and ordinary run
reads. It uses the active local `gh` account for protected-environment review,
explicitly clearing automation-token environment variables before those calls.
Recovering an existing run resolves the reviewer only when the run reaches a
waiting state that needs protected-environment diagnosis; completed and running
observations use automation alone. The review actor is empty until resolved.
When `--approve-environment` is present, the exact name is the Director's
approval authorization; an unexpected environment stops without mutation.
The automation and reviewer identities must differ before a protected dispatch.

Workflow dispatch uses GitHub's current run-returning REST contract and treats
the returned run ID and URL as authoritative. If GitHub does not return exact
run details, the helper fails closed instead of rediscovering the run through a
workflow list filtered by time, name, or unsupported CLI fields. Use the
`watch --run-id <id>` subcommand to recover an already-known run.
If a request fails after dispatch returns exact run details (including an early
404 from the jobs endpoint), the error result keeps the failed request diagnostics
and nonzero exit code, reports `dispatch.outcome=confirmed_success`, and includes
a shell-quoted `recovery_command` for that exact run. Recovery watches without
dispatching or authorizing environment approvals; pass an already authorized
`--approve-environment` name explicitly when needed.

PR and workflow watchers use 60-second active defaults and positive jitter.
The workflow default clips to a shorter requested timeout, ending without an
extra poll. They honor a longer `x-poll-interval` response header. Automation workflow run
and job GETs reuse the shared private conditional cache, including 304 bodies;
identity and protected-environment reads remain fresh. An unavailable cache falls
back to the same live transport, actor checks and shared cooldown. Completed GETs
are not repeated when cache persistence fails. Cache-lock waits and poll waits stay inside
the workflow deadline and do not start another read when that deadline expires.

Every `status=waiting` poll immediately reads `pending_deployments`. Reviewer
waits report environment names, eligible reviewer identities, wait timers, and
`current_user_can_approve`. Eligible review is submitted only for explicitly
authorized environments on runs GitHub attributes to the configured automation
actor. Before submitting, the helper reads the active human's existing review
history so a resumed watch does not repeat a non-idempotent approval.
Triggering-actor self-review denial and reviewer ineligibility are terminal
actionable results, not reasons to keep polling. Timer-only or custom protection
waits remain observable until the bounded timeout. A waiting run with no pending
deployment is distinguished from queued runner jobs and reported as a
concurrency, workflow-queue, or protection-rule diagnostic rather than silently
treated as ordinary execution.

Agent-authored commits and pushes should use `github/scripts/git-commit-as-bot`
and `github/scripts/git-push-as-bot`, passing `--worktree /absolute/task/path`
first to assert the intended worker checkout, so GitHub attribution remains
the configured automation account. Write-like `gh-with-env-token` commands verify the
authenticated login before running and fail closed instead of silently using the
active human account.

Raw `gh pr create`, `gh pr edit`, and `gh pr comment` author writes as the
active local `gh` account. Normal PR write flows should use the PR helper so the
configured automation token is used first:

```sh
github/scripts/gh-pr.py --repo OWNER/REPO create \
  --title "Short imperative title" \
  --body-file /path/to/pr-body.md \
  --base main --head feature-branch
github/scripts/gh-pr.py --repo OWNER/REPO edit <pr> \
  --body-file /path/to/pr-body.md
github/scripts/gh-pr.py --repo OWNER/REPO comment <pr> \
  --body-file /path/to/comment.md
```

Do not guess GitHub CLI JSON field names. If needed, ask `gh` for available
fields before composing a large query:

```sh
helper=<skill-dir>/scripts/gh-with-env-token
$helper pr view <number> --repo OWNER/REPO --json 2>&1 |
  sed -n '/Available fields:/,$p'
$helper pr view <number> --repo OWNER/REPO \
  --json number,title,url,comments,reviews,statusCheckRollup
```

## GitHub Actions Supply-Chain Policy

Treat publisher approval and reference immutability as separate controls. This
repository currently approves GitHub-maintained `actions/checkout` for
repository checkout and Astral's `astral-sh/setup-uv` as a trusted third-party
Python tool bootstrap. Approval determines which remote code sources may run;
it does not permit their executable references to move without review.

Every remote `uses:` reference in a workflow or composite action must use a
reviewed, lowercase, full 40-character Git commit SHA and retain an inline
release-tag provenance comment. Same-repository actions and reusable workflows
use relative `./...` references and do not carry a remote revision. The mutable
remote-reference allowlist defaults to empty; any future exception must be
literal, narrowly scoped, documented, and security-reviewed.

The Launchplane runner branch named in `.github/github.json` is routing
metadata rather than an executable `uses:` reference. Keep that metadata under
its resolved-revision audit path instead of treating it as an action pin.

Dependabot tracks the current workflow pins through the `github-actions`
ecosystem weekly. Review each generated update as a supply-chain change: verify
the source repository, confirm the SHA matches the documented release tag,
inspect relevant release notes, and ensure workflow permissions remain
unchanged. If a future pin is not surfaced by Dependabot, update it manually
with the same review steps rather than leaving it stale. Run the policy checks
locally with:

```sh
uv run scripts/test_validate_github_actions_security.py
uv run scripts/validate_github_actions_security.py
```

## PEP 723 Direct Dependency Policy

Standalone Python helpers keep their PEP 723 execution model instead of
sharing a repository lockfile. Every declared direct dependency must use an
exact `==` pin, and every occurrence of the same normalized package name must
use the same version repository-wide. A missing or empty `dependencies` field
means the script has no direct third-party dependencies. The policy does not
lock transitive dependencies; `uv` resolves those when each helper runs.

Dependabot does not discover dependencies embedded in Python script metadata.
The repository therefore uses `scripts/update_pep723_dependencies.py` for this
surface. Its offline check parses each header as TOML, rejects unsupported
direct requirement forms, and verifies exact consistent pins without network
access. Update mode asks `uv pip compile --no-deps` to resolve the highest
stable release compatible with Python 3.12 from PyPI, independently confirms
that the selected release has a non-yanked PyPI file, then rewrites only the
canonical top-level `dependencies` assignment in deterministic package-name
order. Resolver subprocesses receive an allowlisted environment so ambient uv
constraints, overrides, indexes, and source configuration cannot change the
result.

Run the policy and updater locally with:

```sh
uv run scripts/update_pep723_dependencies.py --check
uv run scripts/update_pep723_dependencies.py --update --dry-run
uv run scripts/update_pep723_dependencies.py --update
```

The weekly and manually dispatchable `Update PEP 723 Dependencies` workflow
runs only from `main` and has two jobs. Neither stores an App key or holds a
repository write token, and neither dispatches another workflow.

- `calculate` has read-only contents and no OIDC. It runs the updater, then
  `scripts/pep723_proposal.py package` records the changed tracked Python files
  as a candidate bound to this run, its attempt and the exact run commit, and
  outputs the candidate's SHA-256 before the new pins execute. The full
  repository gate then runs on the candidate, which is uploaded as an artifact.
- `submit` is the only job that can request OIDC. It checks out the run commit,
  downloads the artifact outside the checkout and runs
  `scripts/pep723_proposal.py prepare` with the runner's Python and the
  standard library only. It accepts the candidate only when its digest, run
  and commit match and every changed file is an existing tracked regular Python
  file whose new content is exactly what `scripts/pep723_metadata.py` renders
  from the base file with new exact stable pins for the same packages. Repository
  pins must stay consistent, and the request must fit Launchplane's
  [updater route limits](https://github.com/cbusillo/launchplane/blob/main/docs/service-boundary.md#updater-source-proposals):
  32 files, 2 MiB of old-plus-new content and a 1 MiB JSON body. A candidate
  that breaks any rule stops before the credential-bearing call. The pinned
  `launchplane-request` action then sends the request, and Launchplane opens a
  new PR as its App.

The run summary reports the route's answer. `created` and `replayed` link the
returned PR and head; `replayed` covers an identical proposal that already
exists, open or closed. `pending` means this run's candidate was not applied and
the existing proposal needs ordinary review first. A refusal, conflict,
malformed response or unconfirmed effect fails the run, prints the request's
SHA-256 and keeps the exact request and response as an artifact. To reconcile
an unconfirmed effect, re-run only the failed `submit` job: it reads the same
candidate and sends the identical request, which returns any proposal it
created. A new workflow run recalculates against PyPI and may send a different
request. Do not publish the change another way.

Launchplane must allow-list this workflow before a submission can succeed;
that access change belongs to
[Launchplane#3404](https://github.com/cbusillo/launchplane/issues/3404).

`uv run scripts/test_pep723_proposal.py` exercises packaging, validation and
response reporting offline against real Git repositories.

If automation fails, inspect the resolver, validation or proposal error before
rerunning it. Unsupported extras, markers, URLs, or compound constraints require
a deliberate policy change rather than an automatic rewrite. An update that
exceeds the route limits is refused rather than split or truncated. Direct pins
can also be updated locally with the command above and submitted through a
normal task branch if workflow recovery is not immediate.

## Automation Command Diagnosis

Before concluding that a GitHub bot, App, or automation command was ignored or
failed, inspect the full lifecycle around the command rather than only compact
timeline events.

- Identify the command, actor, timestamp, expected side effect, and target
  issue, PR, branch, check, label, deployment, or release.
- Fetch issue/PR comments and reactions; a thumbs-up or bot reply may mean the
  command was acknowledged or queued even when no timeline event says so.
- Inspect issue/PR bodies for automation-owned status blocks, repeated banners,
  hidden markers, or generated progress text.
- Check labels, reviews, check runs, merge queue state, deployments, release
  state, and branch/ref mutations before deciding the command had no effect.
- Compare relevant refs, SHAs, labels, statuses, or other state before and after
  the command timestamp when the expected result is a state mutation.
- Report the lifecycle precisely: not acknowledged, acknowledged/queued, in
  progress/stuck, completed, or completed with an unexpected result.
- Use fallback actions only after verifying the target is safe to mutate; for
  example, update a same-repo PR branch manually only after confirming that the
  branch is the intended automation target.

## PR And Issue Workflow

Default to PR-backed implementation work.

- For code-ready work, create a short-lived branch and use a PR as the working
  record when the change needs review, CI, preview environments, or a durable
  handoff point.
- Do not create a separate issue for the same focused implementation by
  default. Create or update an issue when work is not ready for code, needs
  product/design/ops discussion, was discovered out of scope, or spans multiple
  PRs, or needs splitting under
  [issue closing and post-merge proof](#issue-closing-and-post-merge-proof).
- Search existing open and recently closed issues before creating a new issue.
- When picking up an existing issue, read its comments before acting on the
  title or body. Comments are often the newest source of scope, constraints,
  decisions, or direction changes.
- Size an implementation issue for one PR-sized deliverable and follow
  [issue closing and post-merge proof](#issue-closing-and-post-merge-proof).
- When PR work shifts from one-shot diagnosis into repeated CI, review,
  mergeability, or merged/closed follow-through, route that loop to
  `babysit-pr`. This includes after branch updates, rebases, check reruns,
  review-fix pushes, and closeout questions such as whether the PR merged or it
  is safe to exit. Use `babysit-pr --once` for already merged/closed evidence,
  `--wait-ci` to wait in the foreground for CI before a `ready` handoff, and
  `--watch` while an open PR still needs active follow-through.

### Client-Impact Trailers

Each change still needs its own Client acceptance; release acceptance is a
separate final check and never replaces it. After a PR asks its Client for
review, end every later commit message with one trailer, for example
`git-commit-as-bot --worktree /absolute/task/path -m "..." --trailer "Client-impact: none"`:

- `Client-impact: none` only when the commit cannot change anything the Client
  saw or accepted, such as tests, CI or comments.
- `Client-impact: visible` for anything else, or when unsure.

Launchplane carries an acceptance only across `none` commits on a single-parent
history. A `visible` or missing trailer, or a merge you commit (a
conflict-resolving merge is always `visible`), waits for a new Client
decision; the train's own base refresh keeps its existing carry. See
Launchplane's
[carried acceptance](https://github.com/cbusillo/launchplane/blob/main/docs/owner-acceptance.md#carried-acceptance).
Reviewers check each `none` claim against that commit's actual diff, not its
message.

### Issue Closing And Post-Merge Proof

Use `Closes #123` by default when the PR's merge completes the issue's
deliverable. Use `Refs #123` when the PR delivers only part of a multi-PR issue.
Qualify either keyword as `OWNER/repo#N` when its issue is in another repository.
Superseded or competing PRs also retain non-closing references under the
[canonical implementation procedure](#superseded-or-competing-prs).

An issue closes when the PR completing its work lands. The PR's tests and
the deploy's existing health checks are the proof; do not add per-issue deploy
checks or an agent come-back step to verify, qualify, or read back the change.
Do not move that chore to a parent or create another issue for it. A later
failure is breakage: search for an existing issue and record the actual fix.

If the real finish line is an event, such as a lane turning healthy, a Client
accepting a release, or an outside event, keep the issue open with a recorded
wait naming the event and its actor under github-plan's
[wait procedure](cli-reference.md#planning-claim) and
[Status Labels](../../github-plan/SKILL.md#status-labels).
Name the event itself, never an agent's return to confirm it.
Use `Refs` while that event remains outstanding. Preserve unresolved Director,
Client, and other people's waits; a merge does not resolve them or authorize a
release. A parent or milestone closes only when its own exit criteria hold under
the [milestone contract](../../github-plan/references/milestones.md).
Real remaining engineering work stays actionable: a source change has its own
completing PR, and an operational step within the task's authority is carried
through its supported path. Neither is recorded as an event wait.
Split old oversized issues into PR-sized deliverables when working them;
preserve their original requests under the
[issue ownership rules](issue-templates.md), rather than rewriting them.

For a Client's or outside reporter's issue awaiting confirmation, request it
on the issue through the authorized communication path, linking existing
validation evidence, and record that confirmation as the event wait.
Keep closing keywords off that issue until
the reporter/current user confirms the result or explicitly asks to close it;
a separate completed implementation issue may close on merge. The wait does
not require creating a separate issue.

The session responsible for landing reconciles GitHub issue state and planning
metadata as part of the landing: the Supervisor after an assigned handoff,
otherwise the executing session. This is bookkeeping, not another product
qualification step or a reason to leave completed work open. If `Closes` did
not fire, follow github-plan's
[Close Or Hand Off](../../github-plan/SKILL.md#close-or-hand-off) to reconcile
the completed issue. When its finish line remains unmet, record the actual
event wait or remaining engineering work; never force closure or clear a real wait.
Reconcile durable plan metadata under the same Close Or Hand Off procedure
even when GitHub already auto-closed the issue; auto-close does not settle its
Current Status, planning labels or Project Status.

Apply the same rule to legacy qualification-only records: use `gh-plan.py close`
when work is complete and existing deployment health evidence passes. A pending
deploy remains authorized operational work or a real event wait; a pending
Client release keeps its person's wait. A queue category alone proves none of these.

### Superseded Or Competing PRs

When two or more PRs target the same issue, title, branch prefix, or workstream,
choose one canonical implementation before merge or closeout. The canonical PR
owns issue-closing language; competing PRs should use `Refs #123`, not
`Closes #123` or `Fixes #123`, unless they become the selected implementation.

Before opening a new PR, and again before merging or closing out a PR, sweep for
open competing PRs that reference the same issue number, title phrase, branch
prefix, Launchplane request id, or workstream. At minimum, inspect:

```sh
<skill-dir>/scripts/gh-pr.py --repo OWNER/REPO list \
  --state open --limit 50
gh search prs 'repo:OWNER/REPO is:pr is:open 123 OR "workstream phrase"'
```

If a competing PR is superseded, comment on the stale PR with the canonical PR
link and close it when it is no longer a viable implementation. Use the helper
when available because it also rewrites issue-closing keywords to `Refs` on the
superseded PR body:

```sh
<skill-dir>/scripts/gh-pr.py --repo OWNER/REPO supersede 70 \
  --by 71 \
  --reason 'Its taxonomy and missing tests match the agreed scope.'
```

The helper's comment already names the canonical PR as
`[repo#N](https://github.com/OWNER/repo/pull/N) title`, so `--reason` only says
why that PR was selected.

Use `--dry-run` first when validating the comment, closure, and body rewrite
that would happen. Use `--keep-open` only when a PR should remain open for
comparison or follow-up, and say why in the comment.

When a superseded PR is closed and its remote task branch is no longer needed,
delete the unused remote branch after confirming the branch belongs to the same
repository, is not the base branch, and no active issue, PR, worktree, or
review still depends on it. `gh-pr.py supersede --delete-branch` performs this
same-repo/base-branch safety check for the remote ref; otherwise report the
branch as a cleanup candidate.

Evaluate local worker and review worktrees for completed or superseded PRs under
the shared cleanup policy. Dirt or unmerged commits require further read-only
investigation; they are not by themselves a reason to stop and ask. Preserve
unique or ambiguous work by default. Remove an exact worktree or branch only
when current evidence establishes its disposition and existing authorization
covers that same action and scope; ask only for a missing disposal/preservation
choice or an expanded scope. Prefer `git branch -d <branch>` after worktree
removal when Git can prove the branch is merged.

For issue closeout after the winning PR lands, follow github-plan's
[Close Or Hand Off](../../github-plan/SKILL.md#close-or-hand-off).

Handoff content follows the same durability rule. For GitHub-backed work, write
recovery-critical handoff notes to the owning issue or PR timeline. Local
`handoff*.md` files are temporary scratch unless intentionally committed as repo
documentation; migrate their actionable content before deleting them during
closeout.

Use repo-specific instructions for exceptions, deploy labels, preview behavior,
required checks, or release policy.

## Label Taxonomy

Keep planning state separate from PR execution state:

- `plan:*` labels belong on durable planning issues; before setting them, read
  github-plan's [Status Labels](../../github-plan/SKILL.md#status-labels).
- `preview-ready` means a preview environment is available for review. It is
  repo-local workflow evidence, not approval and not a QA result.
- `awaiting-qa` is an optional repo-local manual QA handoff label. Use it only
  when the repo documents a manual tester workflow, usually in `qaLabels`.
- `own-project` marks a PR, or the issue it links, as work for one of the
  own projects in the overall direction's Order when its repository is not on
  that list, such as lab or house work in an infrastructure repository. The
  direction skill owns [capacity classification](../../direction/SKILL.md#weekly-audit).
  At PR creation, check the work
  against the merged overall direction's Order. For own-project work in a
  mixed repository, pass `gh-pr.py --repo OWNER/REPO create --label own-project`
  alongside the other creation arguments, unless a linked issue already
  carries that label or belongs to an Order-listed own-project repository.
  If the label is missing, run `gh-plan.py --repo OWNER/REPO ensure-labels`
  first. For an existing PR, use `gh-pr.py --repo OWNER/REPO edit NUMBER
  --add-label own-project`. Keep the classification list in the overall
  direction; do not classify every PR in a mixed repository as own.
- A merge-train enqueue label is the readiness signal recorded in Launchplane's
  active policy (often `ready-to-merge`). It does not replace a fresh PR read, passing required checks,
  review accounting, and merge authorization as defined in
  `../../references/execution-scope.md`.

Discourage generic labels such as `waiting`, `blocked`, `ready`, or `qa` unless
the repo documents a narrow local convention. When auditing a repo, compare
`gh label list -R OWNER/REPO --limit 500`, open issue labels, and open PR labels
against `.github/github.json` `qaLabels` and `deployLabels`. For merge-train
enqueue labels, read Launchplane's active policy with `merge-train-policy-read`
or use the snapshot's `launchplane.mergeTrain.targets` before recommending
label cleanup; an unavailable policy read leaves that part unverified.

## Merge Readiness

First resolve the landing route under the GitHub skill's
[Merging procedure](../SKILL.md#merging), for a single PR or a stack.
Train-owned branch refresh and landing follow the Launchplane workflow or the
assigned handoff. Apply the fresh-read checklist below before any merge or
authorized train entry; only the direct merge and branch-update operations are
replaced by that route.

Before merging any PR, do a fresh PR read and account for feedback:

- confirm merge state, draft state, base/head branches, and required checks with
  `scripts/gh-pr.py view` and `scripts/gh-pr.py checks`
- inspect review decision, PR comments, reviews, and review threads with raw
  `gh` only when needed; those surfaces may still require GraphQL
- inspect PR comments, reviews, and review threads
- inspect failed or cancelled check logs before deciding CI is non-blocking
- account for auto-review or system review findings that arrived after the last
  push
- apply `../../references/background-review-reporting.md`: absence before a
  possible post-turn trigger is `not yet observable`, not skipped or not
  emitted, and is not a reason to poll indefinitely before reporting the
  point-in-time merge or closeout state
- confirm that explicit user approval, including approval already given in the
  session, covers this merge; ask only when that authorization is missing or no
  longer covers the action and scope

Never merge the base into, rebase, or update your own PR branch merely because
it is `BEHIND`. Refresh only for a real conflict or a check that explicitly
requires it. The merge train owns freshness on train-routed PRs; follow its
workflow or assigned handoff rather than refreshing proactively.
An enforced up-to-date branch-protection rule counts as an explicit requirement
immediately before an authorized direct merge, after other checks are green and
review feedback is accounted for. Do not refresh while that work is still pending.

When a branch update is needed and belongs to this session, use `gh-pr.py view`
to verify `headRepository` matches the PR repository, the head branch is automation-owned,
and its own diff remains within the approved change. For a mergeable base update,
use `scripts/gh-pr.py update-branch <pr>`; the helper observes the asynchronous
update. Both helpers refuse a refresh this section does not permit; name an
explicit requirement GitHub cannot see with `--required-by` or
`--refresh-required`. For a real conflict, merge the base locally with
`git-commit-as-bot --worktree /absolute/task/path --git-command merge`, resolve it and continue with
`git-commit-as-bot --worktree /absolute/task/path --git-command merge --continue`, then push with
`git-push-as-bot --worktree /absolute/task/path`.
Re-read the PR to capture the new head SHA, then use `babysit-pr` to wait for
checks and review evidence on that exact head. A check result for the old head
does not qualify the updated branch. Do not update a fork or another person's
branch without approval from the account that owns it. `BLOCKED` alone does not identify a
behind branch: inspect base/head state and required-check evidence before
choosing an action. Treat a confirmed 405 required-check rejection as a stop
and wait for new readiness evidence.

Read the PR helper's `readiness_evidence`, `mergeability`, and
`observed_merge_capability` separately. `BLOCKED` is provider mergeability
metadata, not proof that the acting App lacks merge authority; an App check-read
403 is degraded readiness evidence, not a merge denial. If App check reads are
denied, use a separately authorized read-only identity to verify the required
checks on the exact PR head and record which actor supplied that evidence.
Confirm the branch protection or ruleset checks that apply. With explicit merge
authorization, a clean exact-head check result, and accounted-for review
feedback, one SHA-guarded `gh-pr.py merge` call is the authoritative App
capability observation even if metadata remains `BLOCKED`. Do not probe by
merging when required checks are unknown, incomplete, or failing; do not treat
a bypass permission as check evidence. A confirmed 405 required-check rejection
is one rejected attempt, followed by waiting for new exact-head evidence.

When the user approves a merge and does not specify the method, use
`scripts/gh-pr.py merge <pr> --method merge` for a normal merge commit.
Avoid squash merges by default because normal merge commits keep branch ancestry
and cleanup mechanically provable. Use squash or rebase only when requested,
required by repo policy, or explicitly confirmed.

If GraphQL is exhausted but REST/core quota remains available, do not keep
retrying GraphQL-backed `gh pr view` or `gh pr merge`. Use the PR helper and
report GraphQL-only surfaces, such as Projects or native sub-issues, as deferred
when the helper cannot update or inspect them safely.

Outside a train-owned landing, consider a rollup/integration PR for stacks more
than two PRs deep or when expensive checks would rerun at every layer.

## CI Failure Diagnosis

When checks fail, inspect logs before guessing from check names or status alone.

```sh
<skill-dir>/scripts/github-ci-diagnose.py --pr <number-or-url>
<skill-dir>/scripts/github-ci-diagnose.py --pr <number-or-url> --json
```

Classify failures before acting:

- branch-related: compile, test, lint, typecheck, snapshot, static analysis, or
  package/build failures that point to touched code or config
- likely flaky/infra: runner provisioning, registry/network outages, provider
  incidents, or timeouts without code-specific evidence
- ambiguous: inspect once manually and report what evidence is missing

When a branch-related failure fits a pattern, such as case sensitivity, paths,
or naming, search all files covered by the failing check for the same pattern
before pushing a fix.

The diagnosis helper uses the same paged REST check reader as `gh-pr.py`, then
reads workflow-run metadata, latest-attempt jobs, and individual job logs over
REST. JSON output preserves the existing count and check fields and adds
`diagnostics.requests`, quota/reset metadata, and named `degradedComponents`.
Missing run metadata or log permission remains explicit while any independently
available check and log evidence is still returned. `countsComplete: false`
means the numeric counts are lower bounds because one check source was
unavailable; `null` counts mean no authoritative check source was available.
Transient provider failures
are classified with retry metadata, but this read path does not silently switch
actors or automatically retry outside the shared retry policy.

Synthetic checks such as CodeQL can link to `/runs/<check-run-id>` rather than
an Actions workflow run. Diagnosis keeps their failure or pending state and
details URL, and reads check annotations for failures. JSON exposes
`checkRunId` separately from `runId` and `jobId`, plus `annotations` or an
explicit `annotationError`. Without a linked Actions run, the helper reports
unavailable job logs without requesting a workflow run using the check-run ID.
Annotation strings use the log reader's terminal-control filter. `--max-lines`
also bounds the annotation count; `annotationsTruncated` signals omitted items.
Third-party check runs likewise retain failure/pending state and annotations;
commit statuses without an Actions link remain classified as external.

Log reads probe the configured CLI's help for `--allow-escape-sequences`, use
that supported opt-in when available, and strip terminal controls before
returning text or failure snippets. Older CLIs retain their existing log-read
behavior. This does not change the selected identity, permissions, or metadata
JSON reads; unavailable logs still produce a named degraded component.

Failure excerpts prefer unittest failure/error details, then pytest failure/error
summary lines. Actions failed-command summaries bound the search before service
teardown output. When multiple commands fail, selection uses
the last failed command's output, after the preceding failed-command summary.
This also means a later failing cleanup command takes precedence over an earlier
failed test step. Actions timestamps are supported. Without a test summary, the
last generic failure marker in that command's output remains the fallback, then
its exit-code line, or the log tail when no failure marker exists.
`--context` selects surrounding lines and `--max-lines` caps the excerpt while
keeping its selected failure marker visible.

Do not paste long logs into chat. Include the short snippet, the run link, and
the exact local command that should reproduce the failure when one is obvious.

## Review Feedback

When addressing PR review feedback, inspect actual PR comments, reviews, and
inline threads before deciding what to change.

- Distinguish unresolved/current feedback from resolved or outdated comments.
- Treat self-authored replies and bot noise as context unless they contain a
  clear actionable request.
- Summarize feedback as actionable items with file paths, reviewer, status, and
  proposed fix.
- Ask before posting GitHub replies, resolving threads, or dismissing feedback
  unless the user explicitly asked to handle review comments end-to-end.
- For issue and PR timeline comments, use `scripts/gh-comment` or
  `scripts/gh-pr.py comment --body-file`; both use the shared actor-aware REST
  implementation and never pass escaped `\n` through `--body`.
- For issue close comments, use `scripts/gh-issue close` with stdin. The comment
  is posted through the shared REST implementation before the explicit issue
  state PATCH, and the final envelope records partial success if close fails
  afterward. Use `scripts/gh-issue reopen` for the equivalent reopen flow.
- For PR review submissions without a dedicated helper, use
  `scripts/gh-with-env-token pr review --body-file`.

If both review feedback and CI failures are present, address actionable review
feedback first when the fix will create a new SHA.

## Post-Merge Verification

As part of landing, follow relevant
post-merge Actions/check suites on the target/default branch when repo config
says to or when the task affects readiness, deploy, security, or shared quality.

When the merged repository is bound into an active local runtime (see
[runtime binding lookup](https://github.com/cbusillo/codex-skills/blob/main/README.md#runtime-binding-lookup)), run the
landed repo-local runtime reconciler after the final landing SHA is known. Use
the merge result's `merge.sha` or a fresh merged-PR view's `mergeCommitOid`,
never the PR head SHA:

```sh
uv run github/scripts/reconcile-runtime-checkout.py \
  --merged-worktree "$PWD" \
  --repo OWNER/REPO \
  --landing-sha <full-landing-sha>
```

Invoke it from a worktree containing the landed helper source. A behind runtime
checkout can also run its own copy with only `--repo` to catch up to the
default-branch tip, unless the helper itself changed upstream; see
[Runtime Checkout Reconciliation](cli-reference.md#runtime-checkout-reconciliation).
A train landing through `launchplane-train-drive.py` reconciles itself. Preserve two independent receipts: the remote merge/landing
result and local runtime reconciliation. `blocked`, `retryable`, or `failed`
runtime reconciliation does not undo the merge and must not trigger another
merge attempt. It does block claims that installed runtime behavior or
provenance-sensitive evidence is current. `not_applicable` is normal when no
host binding is a worktree of the merged clone; `bindings_checked` shows what was
looked at.

Before evaluating or executing a local default-branch refresh, read
[post-merge checkouts](post-merge-checkouts.md). Runtime-bound checkouts use only
the landed reconciler above; a blocked local refresh never undoes or retries
the confirmed remote merge.

Report GitHub security/quality signal outcomes explicitly:

- `clean`: checked and no relevant open findings
- `findings`: checked and open findings exist
- `unavailable`: feature, API, plan, or token cannot provide the signal
- `not_enabled`: feature appears disabled for the repo

Do not treat unavailable or not-enabled signals as clean.

For repository secret-scanning status, use
`uv run github/scripts/github_read.py --repo OWNER/REPO secret-scanning-status`.
The reader is automation-only, forces `hide_secret=true`, and emits only status,
counts, actor evidence, and diagnostics. It does not return raw alert records or
detected secret values. Public repositories report `unavailable` because the
repository alerts API does not expose their signal; private-repository `403` or
ambiguous `404` results also remain unavailable rather than triggering active
user authentication or being interpreted as clean. Do not bypass this projection
with raw `gh api`, `github_api.py call`, or generic HTTP requests; raw alert
operations are unsupported, and status reads route through the sanitized helper.

## Safe Hygiene

Apply the shared repository cleanup policy to resolve exact targets. When more
than one PR, branch, or worktree plausibly matches, continue read-only evidence
collection and preserve ambiguous candidates rather than turning ambiguity into
an automatic permission prompt.

Allowed without asking when safe:

- create a focused local branch for the current task
- open a draft PR for work that implies review, CI, preview, QA handoff, or a
  durable checkpoint
- run `git fetch --prune` for remote-ref orientation; fetching does not update
  or reconcile a runtime-bound working tree
- fast-forward a non-runtime-bound local default branch when it is an ancestor
  of the remote; runtime-bound checkouts must use the landed repo-local
  reconciler instead
- report unsafe cleanup candidates instead of touching them

Exclude detached host auto-review worktrees (for example `auto-review-<hex>`)
from ordinary cleanup. An explicit
bulk or retirement scope still requires the shared policy's current owner/job,
lock, runtime, content, and disposition evidence.

Use `git branch -d <branch>` when Git can prove the exact branch is merged.
Consider `-D` only when the shared policy establishes the exact already
authorized disposition; force syntax never substitutes for missing evidence.

## Authorization For Gated Actions

Before actions that encode product, workflow, or shared-environment decisions,
confirm that the user has authorized the action and scope. Reuse applicable
approval already given, including session-wide approval; ask only for missing
authorization or a scope change that the existing approval does not cover.
Apply this check to:

- merge a PR
- use squash or rebase merge instead of a normal merge commit
- close or reopen an issue
- add, remove, or change QA/review labels
- delete a remote branch
- delete a branch with unmerged or unpushed commits
- force-delete anything
- restart Docker or mutate runtime environments
- rerun failed deploys when they may affect shared environments
- change secrets, environment variables, deploy config, or production resources
