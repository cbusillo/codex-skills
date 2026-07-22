# GitHub Repo Workflow

Use this reference for repository operations that need current GitHub and local
state: PRs, issues, Actions, reviews, merge/deploy state, QA handoff, recent
merges, branch cleanup, or worktree cleanup.

## Orientation

Start with a factual snapshot before making claims about readiness, PR state,
reviews, Actions, deploys, or cleanup.

```sh
~/.code/skills/github/scripts/github-repo-snapshot.sh
~/.code/skills/github/scripts/github-repo-snapshot.sh --json
~/.code/skills/github/scripts/github-repo-snapshot.sh --fetch --json
```

The snapshot keeps local git evidence local, but reads issues, Actions runs,
repository settings, and PR fallbacks through shared paged REST helpers. JSON
output includes `github.diagnostics.components` with request IDs, quota headers,
observed/expected actor identity when active-auth fallback occurs, and explicit
degraded components. A missing or unauthorized GitHub component
does not erase the rest of the snapshot; do not treat an unavailable component
as an empty authoritative result.

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
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO view <pr-or-url>
~/.code/skills/github/scripts/gh-pr.py \
  --repo OWNER/REPO list --state open --limit 20
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO checks <pr-or-url>
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO rate-limit
```

The PR helper is REST-first for normal orientation. Its snapshot-compatible
`reviewDecision` and `statusCheckRollup` fields are intentionally nullable so
ordinary polling does not spend GraphQL quota. Use GraphQL-backed `gh pr view
--json statusCheckRollup,reviewDecision` only when that exact data is needed.
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

Use `github_workflow_babysit.py` instead of dispatching a protected operator
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
When `--approve-environment` is present, the exact name is the operator's
approval authorization; an unexpected environment stops without mutation.
The automation and reviewer identities must differ before a protected dispatch.

Workflow dispatch uses GitHub's current run-returning REST contract and treats
the returned run ID and URL as authoritative. If GitHub does not return exact
run details, the helper fails closed instead of rediscovering the run through a
workflow list filtered by time, name, or unsupported CLI fields. Use the
`watch --run-id <id>` subcommand to recover an already-known run.

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
and `github/scripts/git-push-as-bot` so GitHub attribution remains
`shiny-code-bot`. Write-like `gh-with-env-token` commands verify the
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
helper=~/.code/skills/github/scripts/gh-with-env-token
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
runs the full repository gate before replacing the automation-owned
`automation/pep723-dependencies` branch. It then dispatches `Validate Skills`
against the pushed commit and creates or refreshes the PR only after that run
passes. Manual runs are accepted only from `main`. Repository Actions defaults
remain read-only; the repository setting permitting Actions-created PRs is
enabled, and this workflow is the only workflow granted the write permissions
needed for that branch and PR. Checkout credentials are not persisted while
repository code runs. Existing automation branches and PRs must retain the bot
commit trailer, same-repository head/base identity, marker, and validated SHA
before the workflow will replace or edit them.

If automation fails, inspect the resolver or validation error before rerunning
it. Unsupported extras, markers, URLs, or compound constraints require a
deliberate policy change rather than an automatic rewrite. A failed run can
leave an unreviewed automation branch without creating a PR; the next successful
run safely replaces that branch with a newly validated commit. Direct pins can
also be updated locally with the command above and submitted through a normal
task branch if workflow recovery is not immediate.

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
  PRs.
- Search existing open and recently closed issues before creating a new issue.
- When picking up an existing issue, read its comments before acting on the
  title or body. Comments are often the newest source of scope, constraints,
  decisions, or direction changes.
- Link PRs with `Refs #123` by default when an external reporter, QA, customer,
  or shared environment validation is involved. Use `Closes #123` or
  `Fixes #123` only when auto-close is clearly intended and local/CI evidence
  can conclusively close the issue.
- `Refs #123` is deliberately non-closing. It keeps the relationship visible
  while preserving a required post-merge decision about whether the issue is
  truly done.
- Do not close externally reported issues just because a fix PR merged. Leave a
  validation comment and close only after the reporter/current user confirms or
  explicitly asks.
- When PR work shifts from one-shot diagnosis into repeated CI, review,
  mergeability, or merged/closed follow-through, route that loop to
  `babysit-pr`. This includes after branch updates, rebases, check reruns,
  review-fix pushes, and closeout questions such as whether the PR merged or it
  is safe to exit. Use `babysit-pr --once` for already merged/closed evidence and
  `--watch` while an open PR still needs active follow-through.

### Superseded Or Competing PRs

When two or more PRs target the same issue, title, branch prefix, or workstream,
choose one canonical implementation before merge or closeout. The canonical PR
owns issue-closing language; competing PRs should use `Refs #123`, not
`Closes #123` or `Fixes #123`, unless they become the selected implementation.

Before opening a new PR, and again before merging or closing out a PR, sweep for
open competing PRs that reference the same issue number, title phrase, branch
prefix, Launchplane request id, or workstream. At minimum, inspect:

```sh
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO list \
  --state open --limit 50
gh search prs 'repo:OWNER/REPO is:pr is:open 123 OR "workstream phrase"'
```

If a competing PR is superseded, comment on the stale PR with the canonical PR
link and close it when it is no longer a viable implementation. Use the helper
when available because it also rewrites issue-closing keywords to `Refs` on the
superseded PR body:

```sh
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO supersede 70 \
  --by 71 \
  --reason 'PR #71 matches the agreed taxonomy and includes the missing tests.'
```

Use `--dry-run` first when validating the comment, closure, and body rewrite
that would happen. Use `--keep-open` only when a PR should remain open for
comparison or follow-up, and say why in the comment.

When a superseded PR is closed and its remote task branch is no longer needed,
delete the unused remote branch after confirming the branch belongs to the same
repository, is not the base branch, and no active issue, PR, worktree, or
review still depends on it. `gh-pr.py supersede --delete-branch` performs this
same-repo/base-branch safety check for the remote ref; otherwise report the
branch as a cleanup candidate.

Clean local worker and review worktrees for completed or superseded PRs can be
removed when they have no uncommitted work, no unpushed commits, and no active
issue or PR still depends on them. Remove the associated local branch with
`git branch -d <branch>` after the worktree is gone and Git can prove the
branch is merged or otherwise unnecessary. Ask before deleting any dirty
worktree, branch with unmerged commits, or ambiguous review/automation worktree.

Issue closeout belongs to the winning PR. After the canonical PR merges, sweep
every issue referenced by that PR body or its closing comments. For each issue:

- close it only when the merged PR conclusively satisfies the issue finish line
  and acceptance criteria
- otherwise update `Current Status` or add a comment with what remains and leave
  it open
- use `scripts/gh-issue close` with stdin for close comments so Markdown is
  passed through a formatting-safe close path

Also update stale planning state, duplicate issues, or workstream comments so
future agents can see which PR was selected and which PRs were superseded.

Handoff content follows the same durability rule. For GitHub-backed work, write
recovery-critical handoff notes to the owning issue or PR timeline. Local
`handoff*.md` files are temporary scratch unless intentionally committed as repo
documentation; migrate their actionable content before deleting them during
closeout.

Use repo-specific instructions for exceptions, deploy labels, preview behavior,
required checks, or release policy.

## Label Taxonomy

Keep planning state separate from PR execution state:

- `plan:*` labels belong on durable planning issues. `plan:waiting` means a plan
  is parked on a decision, external event, or other non-issue condition. It is
  not the right label for an ordinary bug waiting on QA, a PR waiting on preview
  review, or a branch waiting on merge approval.
- `preview-ready` means a preview environment is available for review. It is
  repo-local workflow evidence, not approval and not a QA result.
- `awaiting-qa` is an optional repo-local manual QA handoff label. Use it only
  when the repo documents a manual tester workflow, usually in `qaLabels`.
- `ready-to-merge` is a repo-configured merge readiness signal, often used by
  merge trains. It does not replace a fresh PR read, passing required checks,
  review accounting, and explicit user approval for the merge action.

Discourage generic labels such as `waiting`, `blocked`, `ready`, or `qa` unless
the repo documents a narrow local convention. When auditing a repo, compare
`gh label list -R OWNER/REPO --limit 500`, open issue labels, and open PR labels
against `.github/github.json` `qaLabels`, `deployLabels`, and merge-train ready
label metadata before recommending label cleanup.

## Merge Readiness

Before merging any PR, do a fresh PR read and account for feedback:

- confirm merge state, draft state, base/head branches, and required checks with
  `scripts/gh-pr.py view` and `scripts/gh-pr.py checks`
- inspect review decision, PR comments, reviews, and review threads with raw
  `gh` only when needed; those surfaces may still require GraphQL
- inspect PR comments, reviews, and review threads
- inspect failed or cancelled check logs before deciding CI is non-blocking
- account for auto-review or system review findings that arrived after the last
  push
- proceed only after explicit user approval for the merge action

When the user approves a merge and does not specify the method, use
`scripts/gh-pr.py merge <pr> --method merge` for a normal merge commit.
Avoid squash merges by default because normal merge commits keep branch ancestry
and cleanup mechanically provable. Use squash or rebase only when requested,
required by repo policy, or explicitly confirmed.

If GraphQL is exhausted but REST/core quota remains available, do not keep
retrying GraphQL-backed `gh pr view` or `gh pr merge`. Use the PR helper and
report GraphQL-only surfaces, such as Projects or native sub-issues, as deferred
when the helper cannot update or inspect them safely.

For stacked PRs, consider a rollup/integration PR when the stack is more than
two PRs deep or expensive checks would rerun at every layer.

## CI Failure Diagnosis

When checks fail, inspect logs before guessing from check names or status alone.

```sh
~/.code/skills/github/scripts/github-ci-diagnose.py --pr <number-or-url>
~/.code/skills/github/scripts/github-ci-diagnose.py --pr <number-or-url> --json
```

Classify failures before acting:

- branch-related: compile, test, lint, typecheck, snapshot, static analysis, or
  package/build failures that point to touched code or config
- likely flaky/infra: runner provisioning, registry/network outages, provider
  incidents, or timeouts without code-specific evidence
- ambiguous: inspect once manually and report what evidence is missing

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

Merge success is not the finish line. After a PR merges, wait for relevant
post-merge Actions/check suites on the target/default branch when repo config
says to or when the task affects readiness, deploy, security, or shared quality.

When the merged repository is bound into an active local runtime such as the
skills checkout behind `${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/skills`, run the
landed repo-local runtime reconciler after the final landing SHA is known. Use
the merge result's `merge.sha` or a fresh merged-PR view's `mergeCommitOid`,
never the PR head SHA:

```sh
uv run github/scripts/reconcile-runtime-checkout.py \
  --merged-worktree "$PWD" \
  --repo OWNER/REPO \
  --landing-sha <full-landing-sha>
```

Invoke it from a worktree containing the landed helper source, not from a stale
runtime checkout. Preserve two independent receipts: the remote merge/landing
result and local runtime reconciliation. `blocked`, `retryable`, or `failed`
runtime reconciliation does not undo the merge and must not trigger another
merge attempt. It does block claims that installed runtime behavior or
provenance-sensitive evidence is current. `not_applicable` is normal when the
active runtime belongs to another repository.

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

Automatic cleanup is only for unambiguous cases. Ask before deleting when more
than one PR, branch, or worktree plausibly matches the task.

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

Never include Codex Desktop or Every Code auto-review worktrees under
`~/.code/working/<repo>/branches/auto-review*` in automatic cleanup.

Use `git branch -d <branch>` before considering `-D`; do not force-delete unless
the user explicitly approves.

## Ask First

Ask before actions that encode product, workflow, or shared-environment
decisions:

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
