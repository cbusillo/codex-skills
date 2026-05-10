---
name: github
description: Comprehensive GitHub Expert persona for planning, execution, and repository management. Use for durable planning (Issues, Projects), implementation workflows (PRs, Branches, Actions), and safe repository hygiene.
---

# GitHub Expert

Use this skill to manage the entire lifecycle of a change—from durable planning
and focus tracking to implementation, review, and repository hygiene.

## Core Mandate

Keep planning and execution in sync. Use GitHub Issues as the durable planning
database and Pull Requests as the implementation record.

## Durable Planning (Issues & Projects)

Promote work that should survive the current conversation to GitHub Issues.

- **Issue Shape**: Strictly follow the structure in `references/issue-templates.md`.
- **Focus & Projects**: Use `references/github-projects.md` to manage priority
  (`Focus` field), roadmap dates, and manager routing.
- **Workflow**: Reason in chat first, search before creating, and keep the
  `Current Status` section updated as a recovery point.

## Implementation & Workflow (PRs & Branches)

Use PRs for all non-trivial code changes.

- **Branch Discipline**: Protect default, shared, release, and production
  branches. Create focused task branches before editing when currently on a
  protected branch.
- **Merges & Stacks**: Prefer the repository's normal PR merge method. For
  stacked PRs, consider a rollup branch when merging each layer would rerun
  expensive checks or create avoidable conflict churn.
- **Verification**: After merge, verify Actions and relevant security/quality
  signals before closing related planning state.
- **Formatting**: Use `scripts/gh-comment` or `--body-file` for multiline
  issue and PR timeline comments. For PR review feedback, use
  `gh pr review --body-file`.
- **Workflow Detail**: See `references/repo-workflow.md` for orientation,
  PR/check/review handling, and cleanup guardrails.

## Diagnostics & Hygiene

- **CI Failure**: Use the `github-ci-diagnose.py` helper to classify and fix
  failures.
- **Hygiene**: Use `github-repo-snapshot.sh` for situational snapshots and clean
  up merged task branches/worktrees only when doing so cannot remove unrelated
  user work.

## Tools & Scripts

Always prefer the bundled scripts for structured state and safe formatting:

- `scripts/gh-plan.py`: Issue and Project management (see `references/cli-reference.md`).
- `scripts/github-repo-snapshot.sh`: Situational awareness.
- `scripts/github-ci-diagnose.py`: CI log analysis.
- `scripts/gh-comment`: Safe multiline commenting.

## Workflow Loop

1. **Orient**: Run `github-repo-snapshot.sh` and `gh-plan.py index`.
2. **Plan**: Draft or update a durable plan Issue.
3. **Act**: Create a task branch, commit, and open a PR.
4. **Verify**: Address CI and review feedback using `github-ci-diagnose.py`.
5. **Close**: Merge, verify post-merge signals, close the Issue, and clean up.
