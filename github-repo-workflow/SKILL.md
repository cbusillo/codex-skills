---
name: github-repo-workflow
description: Use for GitHub-backed repository operations and current-state checks involving issues, pull requests, Actions, labels, reviews, merge or deploy state, QA handoff, recent merges, branch cleanup, or worktree cleanup. Start with read-only local and GitHub state, then create or update PRs/issues or perform unambiguous safe repo hygiene when the task implies it.
---

# GitHub Repo Workflow

## Purpose

Use this skill to ground GitHub-backed work in current GitHub and local repo state. Prefer a quick factual check before making claims about PRs, issues, Actions, deploys, QA state, or cleanup.

The goal is current state plus safe next repo action, not broad unsupervised product decisions.

For durable planning, workstreams, milestones, Projects, stale/duplicate plan
cleanup, or cross-repo issue blockers, use the `github-plan` skill. This skill
still owns branch, PR, Actions, merge, deploy, and repo hygiene workflows.

## PR and Issue Workflow

Default to PR-backed implementation work.

- If a local automation identity is configured, use it for GitHub write actions:
  branch pushes, PR creation/updates, issue creation/updates, comments, and
  labels. Fall back to the active human account only when the automation
  credential is unavailable or lacks permission, and say so. Keep bot
  credentials out of repos and committed config. Prefer
  `scripts/gh-with-env-token`, an ignored local helper, or a credential manager
  that injects tokens transiently; do not rewrite persistent remotes to include
  tokens.
- If `.local/github-repo-workflow.md` exists, read it before GitHub write
  actions and follow its private local conventions.
- For work the repository owner should see, include PR/issue links in the final
  response.

- For code-ready work, create a short-lived branch and use a PR as the working
  record when the change needs review, CI, preview environments, or a durable
  handoff point. Open a draft PR early for larger efforts or when a durable
  checkpoint would help pause, review, or continue the work.
- Pushing task branches and opening or updating PRs is allowed when it supports
  the work; do not wait for separate user permission just to publish a focused
  branch or create the PR record. Merging is the approval gate.
- Do not create a separate issue for the same focused implementation by
  default. A PR can hold scope, constraints, verification notes, screenshots,
  open questions, and review status.
- Create an issue when work is not ready for code, needs product/design/ops
  discussion, should be tracked for later, was discovered out of scope, or spans
  multiple PRs. If you find a real actionable problem that should survive the
  current chat and it is not fixed in the active PR, open or update an issue
  with evidence, expected outcome, and links. Search existing open and recently
  closed issues first; update or reference a match instead of creating a
  duplicate. Do not open speculative issues or issues without durable evidence.
- If an issue already exists, link the PR with `Refs #123` by default when the
  issue was opened by someone other than the current user, when the report came
  from QA/customer/user testing, or when the issue needs reporter validation in
  a shared environment after deploy. Use `Closes #123` or `Fixes #123` only
  when the user explicitly wants auto-close behavior, the reporter has already
  validated the fix, or the issue is an internal task that local/CI evidence can
  conclusively close.
- Do not close or auto-close externally reported issues just because a fix PR
  merged. Leave the issue open with a clear "ready for reporter validation"
  comment, the deployed environment or preview URL to test, expected behavior,
  and any preview/shared-environment caveats. Close only after the reporter or
  current user confirms validation, or after the current user explicitly asks to
  close it.
- Keep branches and PRs focused on one coherent change. Prefer small logical
  commits when that makes review, pause/resume, or rollback clearer.
- Before making implementation commits, resolve the repo default branch from
  config, `origin/HEAD`, or GitHub metadata. If currently on the default branch
  or another protected/shared branch, create and switch to a focused task branch
  before editing or committing. Do not use a rejected push to discover branch
  protection; treat protected/default/shared branches as no-direct-work zones
  unless the user explicitly asks for direct work there.
- To open or update a PR, push a newly created task branch containing only your
  current committed work. Never push default, protected, shared,
  release, production, or user-owned branches unless the user explicitly asks.
- After a PR is merged, clean up local branches and worktrees when the safe
  hygiene rules below are satisfied.

Use repo-specific instructions for exceptions, deploy labels, preview
environment behavior, required checks, or release policy.

## Pre-Merge Checklist

Before merging any PR, do a fresh PR read and account for feedback:

- confirm merge state, draft state, base/head branches, review decision, and
  required checks from `statusCheckRollup`
- inspect PR comments, reviews, and review threads; address or explicitly
  report unresolved actionable feedback
- inspect failed or cancelled check logs before deciding CI is non-blocking
- account for auto-review or system review findings that arrived after the last
  push
- proceed only after explicit user approval for the merge action

For stacked PRs, first identify whether the open PRs form a dependent chain
such as `feature-c -> feature-b -> feature-a -> main`. If the stack is more
than two PRs deep or expensive checks will rerun at every layer, propose a
rollup/integration PR before merging the chain one by one. Create a branch from
the final base, merge or cherry-pick the stack into that branch, open one PR to
the protected/default branch, wait for checks once on the final combined result,
then close the checkpoint PRs as superseded after the integration PR merges.
Do not call this a squash unless commits are intentionally collapsed; preserving
useful commits on the rollup branch with a normal merge commit is preferred for
auditability.

## Merge Method Preference

When the user asks whether work is ready to merge, interpret that as PR-backed
merge readiness by default: branch pushed, PR opened or updated, required checks
and relevant GitHub signals reviewed, and explicit user approval before the
merge action. Do not treat "merge" as permission to bypass the PR workflow or
push directly to the default branch unless the user explicitly says so.

Ask before merging any PR. When the user approves a merge and does not specify
the method, use a normal merge commit. Avoid squash merges by default. Keeping
the reviewed branch commits in the default branch ancestry makes audit trails,
`git branch -d`, `git merge-base --is-ancestor`, and cleanup automation simpler
and mechanically provable.

For PRs that address issues opened by another person, QA tester, customer, or
external reporter, treat reporter validation as part of the normal completion
path. Leave the PR open until the reporter can validate on the appropriate
environment unless the current user explicitly asks to merge earlier. If merged
early, make sure the linked issue remains open and update it with testing
instructions instead of closing it through PR keywords.

Use squash merge only when the user explicitly requests it, when repo-specific
instructions require it, or when you have a concrete reason such as noisy WIP
history and the user confirms that tradeoff. Name the cleanup cost before doing
so: after a squash merge, local branches may show as ahead/behind because their
original commits are patch-equivalent to `main` but not ancestors of `main`.
That makes normal branch deletion and divergence checks less obvious. Use
rebase merge rarely and only with explicit approval.

When merging an approved short stack directly, merge top-down and wait for the
next PR's refreshed required checks before continuing. Prefer the rollup PR path
for long stacks, early MVP spines, or branches with slow security/static
analysis workflows.

## Post-Merge Verification

Merge success is not the finish line. After a PR is merged and GitHub state is
available, wait for relevant post-merge Actions/check suites on the target or
default branch when repo config says to or when the task affects readiness,
deploy, security, or shared quality. Inspect available GitHub security/quality
signals before calling repo work fully closed.

Report GitHub security/quality signal outcomes explicitly:

- `clean`: checked and no relevant open findings.
- `findings`: checked and open findings exist.
- `unavailable`: feature, API, plan, or token cannot provide the signal.
- `not_enabled`: feature appears disabled for the repo.

Do not treat unavailable or not-enabled signals as clean. Use
`githubSignals.capabilities` as a hint to skip known-wasted routine calls, but
propose metadata updates instead of auto-writing newly discovered capability
facts.

Handle findings by relationship to the current work:

- Introduced by the current change: blocker; fix before readiness/merge when
  feasible.
- In touched files or affected behavior: blocker or explicit readiness decision.
- Unrelated existing finding: report and track durably, usually without forcing
  it into the current PR/session.

For unrelated findings, search existing issues first. If no suitable issue
exists and the finding is concrete, reproducible, and actionable, open or
propose an issue with evidence, affected files/categories, severity, GitHub
alert link when available, suggested validation, and whether it blocks current
work. Group broad noisy baselines into a cleanup plan/report instead of opening
many issues. For security-sensitive findings, avoid secrets, private exploit
details, or sensitive data in issue text; ask when disclosure boundaries are
unclear.

Keep PR scope focused: fix current-change failures and tightly coupled
touched-area issues in the current PR; track unrelated broad-gate findings in a
separate issue, plan, or focused cleanup PR unless they are tiny and safe.

## Quick Workflow

For a new repo session, take a lightweight situational snapshot when repo state
could affect the work. Surface only actionable state: current branch/PR, dirty
files, relevant open PRs, recent failed or in-progress important workflows, and
high-signal issues related to the task. Do not dump a full issue dashboard by
default.

1. Run the snapshot script when available:

```sh
~/.code/skills/github-repo-workflow/scripts/github-repo-snapshot.sh
```

Use JSON mode when another tool, agent, or careful follow-up parsing would benefit from structured state:

```sh
~/.code/skills/github-repo-workflow/scripts/github-repo-snapshot.sh --json
```

When stale remotes could change the answer, fetch first in the same snapshot:

```sh
~/.code/skills/github-repo-workflow/scripts/github-repo-snapshot.sh --fetch --json
```

2. If the repo has a known deploy health endpoint and deploy state matters, rerun with:

```sh
~/.code/skills/github-repo-workflow/scripts/github-repo-snapshot.sh --health-url https://example.com/api/health
```

The script automatically reads `.github/github-repo-workflow.json` when present.
When using that default config, it also deep-merges
`.github/github-repo-workflow.override.json` if present and reports that the
local override was applied. Use `--config <path>` to point at a different
non-secret repo config.

3. Summarize only actionable state:
   - open PRs and whether they are waiting, blocked, or ready
   - open or recently closed issues that are relevant to the conversation
   - failed or in-progress Actions that matter
   - deploy health and deployed commit when relevant
   - dirty files, extra worktrees, or stale local branches
   - what is ours to do versus waiting on someone else

4. If the current branch has a PR, inspect that PR before relying on repo-wide lists. Escalate to deeper inspection only for items that affect the answer:

```sh
gh pr view <number> --json number,title,state,isDraft,mergeStateStatus,labels,reviewDecision,statusCheckRollup,headRefName,baseRefName,mergeCommit,url
gh issue view <number> --json number,title,state,labels,comments,url
gh run view <run-id> --json status,conclusion,workflowName,headSha,url,jobs
```

Do not guess GitHub CLI JSON field names. `gh` prints the available fields when
`--json` is omitted; use that to discover fields before composing a large query,
especially across different `gh` versions or resources. If a field is missing
such as `reviewThreads`, fall back to supported fields (`comments`, `reviews`,
`statusCheckRollup`, `latestReviews`) or use `gh api graphql` for GraphQL-only
data. A safe pattern is:

```sh
gh pr view <number> --repo OWNER/REPO --json 2>&1 | sed -n '/Available fields:/,$p'
gh pr view <number> --repo OWNER/REPO --json number,title,url,comments,reviews,statusCheckRollup
```

Prefer `statusCheckRollup` for the active PR and branch-specific runs before treating the latest repo-wide runs as relevant. When a deploy health endpoint reports a revision or tag, compare it with the merge commit, PR head, or branch SHA the task cares about.

The snapshot script uses bounded lists. If the result cap may hide relevant work, rerun the specific `gh` command with a higher `--limit` or a narrower branch/PR filter.

## CI Failure Diagnosis

When the user asks why checks failed, whether CI is actionable, or to fix a
failing PR check, inspect logs before guessing from check names or status alone.

1. Resolve the relevant PR. Prefer the current branch PR unless the user gave a
   PR number or URL.
2. Run the CI diagnosis helper when GitHub Actions logs are involved:

```sh
~/.code/skills/github-repo-workflow/scripts/github-ci-diagnose.py --pr <number-or-url>
```

Use JSON when another tool or a careful follow-up parse would help:

```sh
~/.code/skills/github-repo-workflow/scripts/github-ci-diagnose.py --pr <number-or-url> --json
```

3. Summarize the failing check name, run URL, head SHA, and concise failure
   snippet. Call out missing or pending logs explicitly.
4. If a check's details URL is not a GitHub Actions run, treat it as an external
   provider and report only the details URL unless the repo has a documented
   provider-specific workflow.
5. Classify failures before acting:
   - branch-related: compile, test, lint, typecheck, snapshot, static analysis,
     or package/build failures that point to touched code or config;
   - likely flaky/infra: runner provisioning, registry/network outages,
     provider incidents, timeouts without code-specific evidence;
   - ambiguous: inspect once manually and report what evidence is missing.
6. For branch-related failures, patch and verify locally when feasible. For
   likely flaky failures, ask before rerunning deploy or shared-environment
   workflows; non-deploy test reruns may follow repo-specific policy. For
   ambiguous failures, present the evidence and intended next diagnostic step.

Do not paste long logs into chat. Include the short snippet, the run link, and
the exact local test or command that should reproduce the failure when one is
obvious.

## Review Feedback Handling

When the task is to address PR review feedback, inspect the actual PR comments,
reviews, and inline threads before deciding what to change.

- Resolve the current branch PR with `gh pr view --json number,url,title,state`.
- Inspect top-level comments, review submissions, and review threads. For
  inline threads, distinguish unresolved/current feedback from resolved or
  outdated comments.
- Treat self-authored replies and bot noise as context unless they contain a
  clear actionable request.
- Summarize feedback as numbered actionable items with file paths, reviewer,
  thread/comment status, and proposed fix.
- Ask before posting GitHub replies, resolving threads, or dismissing feedback
  unless the user explicitly asked to handle review comments end-to-end.
- If both review feedback and CI failures are present, address actionable review
  feedback first when the fix will create a new SHA; avoid rerunning checks on
  an old SHA that is about to be replaced.

## Optional Repo Config

Repo-specific, non-secret defaults may live in `.github/github-repo-workflow.json`.
This keeps shared GitHub/deploy expectations with the repository while avoiding
`AGENTS.md` bloat.

Before writing this file in a repo, show the proposed JSON and ask for approval
for that specific repo. Keep secrets, tokens, credentials, private host notes,
and local-only operator details out of committed metadata.

Read `references/repo-config-schema.md` only when creating, updating, or
diagnosing `.github/github-repo-workflow.json` or its local override.

Codex Desktop auto-review worktrees under
`~/.code/working/<repo>/branches/auto-review*` are external review context, not
normal cleanup candidates. Ignore them for routine repo hygiene and readiness;
do not remove or mutate them unless the user explicitly asks about that review
worktree.

## Automatic Safe Hygiene

After gathering facts, perform safe hygiene automatically when the conditions are unambiguous. Announce cleanup in a progress update, then proceed for allowed actions. If more than one PR, branch, or worktree plausibly matches the task, ask before deleting anything.

Use the repo default branch from `.github/github-repo-workflow.json`, then
`origin/HEAD`, then the active branch if no better source exists. Do not assume
the default branch is `main`.

Allowed without asking:

- Create a focused local branch for the current task.
- Open a draft PR for the current implementation when the task implies review,
  CI, preview, QA handoff, or a durable checkpoint.
- Open or update an issue for actionable out-of-scope work discovered during
  the task.
- Update a PR body with current scope, verification, screenshots, and open
  questions.
- Add factual PR or issue comments with verification state or links.
- Run `git fetch --prune`.
- Remove a clean local worktree for a PR that is already merged only when repo
  config sets `cleanup.removeMergedCleanWorktrees` to `true`. Otherwise report
  it as a cleanup candidate. Report closed-but-unmerged PR worktrees as unsafe
  cleanup candidates instead of removing them automatically.
  Never include Codex Desktop auto-review worktrees
  (`~/.code/working/<repo>/branches/auto-review*`) in automatic cleanup.
- Delete a local branch for a merged PR when all are true:
  - repo config sets `cleanup.deleteMergedLocalBranches` to `true`,
  - the branch is not currently checked out in any worktree,
  - the branch has no unpushed commits,
  - the PR is merged,
  - the worktree, if any, is clean.
- Fast-forward the local default branch to its remote-tracking branch when the
  local branch is an ancestor of the remote branch and is not divergent.
- Verify remote PR branches are gone after merged PR cleanup.
- Confirm expected deploy health after a successful deploy workflow.
- Report unsafe cleanup candidates instead of touching them.

Useful checks before deleting a local branch:

```sh
git status --short
git worktree list
git log --oneline origin/<branch>..<branch>
git branch --contains <merge-commit-or-head-sha>
```

Use `git branch -d <branch>` before considering `-D`; do not force-delete unless the user explicitly approves.

## GitHub Body Formatting

When creating or editing issue, PR, release, or comment bodies from the shell,
avoid passing Markdown with `\n` inside normal double-quoted strings. GitHub will
receive literal backslash-n text instead of line breaks.

For multiline Markdown, default to writing the body to a temporary Markdown file
and passing it with the relevant `--body-file` flag when the `gh` subcommand
supports it. This is the most reliable path for PRs, issues, releases, and
comments because it preserves blank lines, lists, and code fences without
depending on shell-specific quoting.

Prefer one of these:

```sh
tmp_body=$(mktemp)
cat > "$tmp_body" <<'EOF'
Line one

## Summary
- Item
EOF
gh pr create --body-file "$tmp_body"
gh pr create --body $'Line one\n\n## Summary\n- Item'
printf '%s\n' 'Line one' '' '## Summary' '- Item' | gh api ... --input -
```

Use ANSI-C quoting (`$'...'`) or `printf` only for short bodies or API paths
that lack a body-file option. After updating a body, verify it with
`gh pr view --json body` or `gh issue view --json body` when formatting matters.

## Ask First

Ask before actions that encode product, workflow, or shared-environment decisions:

- Merge a PR.
- Use squash or rebase merge instead of the default normal merge commit.
- Close or reopen an issue.
- Add, remove, or change QA/review labels.
- Delete a remote branch.
- Delete a branch with unmerged or unpushed commits.
- Delete the currently checked-out branch.
- Force-delete anything.
- Restart Docker or mutate runtime environments.
- Rerun failed deploys when they may affect shared environments.
- Change secrets, environment variables, deploy config, or production resources.

## Reporting Style

Keep the answer concise and state-based:

- Lead with the current truth.
- Separate completed work, our remaining work, and waiting-on-others work.
- Include links or IDs for PRs, issues, and runs when useful.
- Mention cleanup performed and cleanup intentionally skipped.
- Avoid long raw command output unless the user asks for it.

## Failure Handling

If `gh` is unavailable, unauthenticated, or missing permissions, say that and continue with local git facts. Separate verified local facts from GitHub facts that could not be checked. If GitHub API state conflicts with local memory, trust the fresh GitHub check and call out the discrepancy.
