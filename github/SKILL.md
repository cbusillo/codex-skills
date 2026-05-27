---
name: github
description: "Comprehensive GitHub Expert persona for repository execution and hygiene: PRs, branches, Actions, reviews, merge/deploy state, issue comments, and safe cleanup. For durable planning, roadmaps, blockers, Projects, or workstream graphs, use github-plan."
---

# GitHub Expert

Use this skill to manage repository execution: branches, pull requests, Actions,
reviews, merge/deploy state, issue comments, and safe cleanup.

## Core Mandate

Keep execution grounded in current GitHub and local repo state. Use Pull
Requests as the implementation record. For durable planning, workstream graphs,
blockers, milestones, Projects, or roadmap tracking, use the `github-plan`
skill.

## Durable Planning Boundary

Use `github-plan` for planning surfaces: durable Issues, parent/sub-issue
graphs, blockers, milestones, Projects, roadmap/focus state, stale or duplicate
plan cleanup, and replacing local plan files with GitHub issues.

This skill may comment on, link to, or close issues as part of implementation
workflow, but it should not flatten broad planning work into a single issue.

Do not duplicate active roadmap, blocker, or checklist state into repo docs.
Update repo docs only through implementation work when they need to describe
current behavior, configuration, or operational policy.

## Implementation & Workflow (PRs & Branches)

Use PRs for all non-trivial code changes.

Raw `gh pr create`, `gh pr edit`, and `gh pr comment` use the active local
GitHub account. For normal agent PR writes, use `scripts/gh-pr.py create`,
`scripts/gh-pr.py edit`, and `scripts/gh-pr.py comment`, or `scripts/gh-comment
pr` for timeline comments. These helper-backed paths route through
`scripts/gh-with-env-token` so the configured automation token authors the
write by default.

Use the bundled `gh-*` and `github-*` helper scripts first for GitHub work. The
helpers are the agent-facing interface for this skill: they keep common flows
ergonomic, normalize output for LLM consumption, protect Markdown/body
formatting, centralize retry and auth behavior, and give us one controlled place
to improve GitHub workflows. Reach for raw `gh` only when no helper covers the
operation, and route those calls through `scripts/gh-with-env-token`.

- **Branch Discipline**: Protect default, shared, release, and production
  branches. Create focused task branches before editing when currently on a
  protected branch.
- **Merges & Stacks**: When the user approves a merge and does not specify the
  method, state that you are using a normal merge commit and run
  `scripts/gh-pr.py merge <pr> --method merge` for GitHub helper-backed merge
  execution. Do not use `--squash` or `--rebase` unless the user explicitly
  asks, repo policy requires it, or you ask and receive confirmation. For
  stacked PRs, consider a rollup branch when merging each layer would rerun
  expensive checks or create avoidable conflict churn.
- **Cross-Repo PRs**: When creating a PR for a repository other than the current
  working directory, run `scripts/gh-with-env-token pr create` from that
  repository or pass both `--repo OWNER/REPO` and an explicit `--head` branch.
- **Pre-Push Quality**: For code changes, use `jetbrains-inspection` to run
  targeted JetBrains inspections on changed files or touched directories before
  pushing a branch or updating a PR whenever the repo has an IDE project
  available. If unavailable, record the not-run reason before pushing.
- **Verification**: After merge, verify Actions and relevant security/quality
  signals before closing related planning state.
- **Formatting**: Use `scripts/gh-issue` for issue create/edit bodies,
  `scripts/gh-pr.py create --body-file` and `scripts/gh-pr.py edit --body-file`
  for PR bodies, `scripts/gh-pr.py comment --body-file` or
  `scripts/gh-comment pr` for PR timeline comments, and
  `scripts/gh-with-env-token pr review --body-file` for PR review feedback when
  no review helper exists. Avoid unquoted heredocs for Markdown bodies because
  shell command substitution runs inside backticks.
- **Authentication**: The helpers own token selection, fallback behavior,
  consistent warnings, and parseable output. If no automation token is
  configured, `scripts/gh-with-env-token` uses the active local `gh` account and
  warns on stderr. Use `scripts/gh-with-env-token --print-auth-account ...` when
  the acting account should be visible; it writes the account receipt to stderr
  so JSON stdout remains parseable.
- **Workflow Detail**: See `references/repo-workflow.md` for orientation,
  PR/check/review handling, and cleanup guardrails.
- **Superseded PRs**: When multiple PRs target the same issue or workstream,
  pick a canonical PR, ensure stale PRs use `Refs` instead of closing keywords,
  comment with the winning PR, and close superseded PRs with
  `scripts/gh-pr.py supersede` when appropriate. Clean up unused remote task
  branches and clean local worker/review worktrees only after confirming no
  active issue, PR, or uncommitted work still depends on them.

## Diagnostics & Hygiene

- **CI Failure**: Use the `github-ci-diagnose.py` helper to classify and fix
  failures.
- **Hygiene**: Use `github-repo-snapshot.sh` for situational snapshots and clean
  up merged task branches/worktrees only when doing so cannot remove unrelated
  user work.

## Tools & Scripts

Always prefer the bundled scripts for structured state, ergonomic workflows,
consistent auth/retry behavior, and safe formatting:

- `scripts/gh-plan.py`: Issue and Project management (see
  `references/cli-reference.md`).
- `scripts/github-repo-snapshot.sh`: Situational awareness.
- `scripts/github-ci-diagnose.py`: CI log analysis.
- `scripts/gh-pr.py`: REST-first PR view, list, checks, merge, and rate-limit
  diagnostics. The helper owns quota-aware degraded behavior; GraphQL-only
  fields remain nullable unless a future command explicitly opts into them.
- `scripts/gh-issue`: Safe multiline issue create/edit bodies from stdin.
- `scripts/gh-comment`: Safe multiline commenting.

## Workflow Loop

1. **Orient**: Run `github-repo-snapshot.sh`; use `github-plan` if planning
   state matters.
2. **Plan**: Delegate durable planning to `github-plan`.
3. **Act**: Create a task branch, commit, and open a PR.
4. **Verify**: Address CI and review feedback using `github-ci-diagnose.py`.
5. **Close**: Merge, verify post-merge signals, use `github-plan` to sweep
   stale/duplicate/related planning issues, close or relabel reconciled issues,
   and clean up.
