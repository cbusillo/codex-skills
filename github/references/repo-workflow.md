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

Summarize only actionable state:

- current branch and current branch PR
- open PRs and whether they are waiting, blocked, or ready
- failed or in-progress Actions that matter
- deploy health and deployed commit when relevant
- dirty files, extra worktrees, or stale local branches
- what is ours to do versus waiting on someone else

If the current branch has a PR, inspect that PR before relying on repo-wide
lists. Prefer the PR helper for repeated PR metadata, check, and
merge-readiness reads so normal agent polling lets one maintained script manage
transport choice, quota pressure, and degraded output:

```sh
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO view <pr-or-url>
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO checks <pr-or-url>
~/.code/skills/github/scripts/gh-pr.py --repo OWNER/REPO rate-limit
```

Use GraphQL-backed `gh pr view --json statusCheckRollup` only when the PR helper
does not expose the data you need.

If a deploy health endpoint reports a revision or tag, compare it with the
merge commit, PR head, or branch SHA the task cares about.

Use the bundled GitHub helpers before raw `gh`. They are intentionally shaped
for agent use: stable command ergonomics, structured output, safer multiline
body handling, centralized auth and retry behavior, and one maintained place to
improve workflows. For raw PR, run, or review commands that do not yet have a
dedicated helper, route through `gh-with-env-token`.

Do not guess GitHub CLI JSON field names. If needed, ask `gh` for available
fields before composing a large query:

```sh
helper=~/.code/skills/github/scripts/gh-with-env-token
$helper pr view <number> --repo OWNER/REPO --json 2>&1 |
  sed -n '/Available fields:/,$p'
$helper pr view <number> --repo OWNER/REPO \
  --json number,title,url,comments,reviews,statusCheckRollup
```

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
- Link PRs with `Refs #123` by default when an external reporter, QA, customer,
  or shared environment validation is involved. Use `Closes #123` or
  `Fixes #123` only when auto-close is clearly intended and local/CI evidence
  can conclusively close the issue.
- Do not close externally reported issues just because a fix PR merged. Leave a
  validation comment and close only after the reporter/current user confirms or
  explicitly asks.

Use repo-specific instructions for exceptions, deploy labels, preview behavior,
required checks, or release policy.

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
- For issue and PR timeline comments, use `scripts/gh-comment`; never pass
  escaped `\n` through `--body`.
- For PR review submissions without a dedicated helper, use
  `scripts/gh-with-env-token pr review --body-file`.

If both review feedback and CI failures are present, address actionable review
feedback first when the fix will create a new SHA.

## Post-Merge Verification

Merge success is not the finish line. After a PR merges, wait for relevant
post-merge Actions/check suites on the target/default branch when repo config
says to or when the task affects readiness, deploy, security, or shared quality.

Report GitHub security/quality signal outcomes explicitly:

- `clean`: checked and no relevant open findings
- `findings`: checked and open findings exist
- `unavailable`: feature, API, plan, or token cannot provide the signal
- `not_enabled`: feature appears disabled for the repo

Do not treat unavailable or not-enabled signals as clean.

## Safe Hygiene

Automatic cleanup is only for unambiguous cases. Ask before deleting when more
than one PR, branch, or worktree plausibly matches the task.

Allowed without asking when safe:

- create a focused local branch for the current task
- open a draft PR for work that implies review, CI, preview, QA handoff, or a
  durable checkpoint
- run `git fetch --prune`
- fast-forward the local default branch when it is an ancestor of the remote
- report unsafe cleanup candidates instead of touching them

Never include Codex Desktop auto-review worktrees under
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
