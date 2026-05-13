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

- **Branch Discipline**: Protect default, shared, release, and production
  branches. Create focused task branches before editing when currently on a
  protected branch.
- **Merges & Stacks**: Prefer the repository's normal PR merge method. For
  stacked PRs, consider a rollup branch when merging each layer would rerun
  expensive checks or create avoidable conflict churn.
- **Cross-Repo PRs**: When creating a PR for a repository other than the current
  working directory, run `gh pr create` from that repository or pass both
  `--repo OWNER/REPO` and an explicit `--head` branch.
- **Pre-Push Quality**: For code changes, run targeted JetBrains inspections on
  changed files or touched directories before pushing a branch or updating a PR
  whenever the repo has an IDE project available. If unavailable, record the
  not-run reason before pushing.
- **Verification**: After merge, verify Actions and relevant security/quality
  signals before closing related planning state.
- **Formatting**: Use `scripts/gh-issue` for issue create/edit bodies,
  `scripts/gh-comment` for issue and PR timeline comments, and
  `gh pr review --body-file` for PR review feedback. Avoid unquoted heredocs
  for Markdown bodies because shell command substitution runs inside backticks.
- **Authentication**: Prefer bundled GitHub scripts and route ad hoc `gh`
  calls through `scripts/gh-with-env-token` so `.env` / `CODEX_GITHUB_TOKEN`
  auth overrides any exhausted inherited `GH_TOKEN`. Use
  `scripts/gh-with-env-token --print-auth-account ...` when the acting account
  should be visible; it writes the account receipt to stderr so JSON stdout
  remains parseable.
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
- `scripts/gh-issue`: Safe multiline issue create/edit bodies from stdin.
- `scripts/gh-comment`: Safe multiline commenting.

## Workflow Loop

1. **Orient**: Run `github-repo-snapshot.sh`; use `github-plan` if planning state matters.
2. **Plan**: Delegate durable planning to `github-plan`.
3. **Act**: Create a task branch, commit, and open a PR.
4. **Verify**: Address CI and review feedback using `github-ci-diagnose.py`.
5. **Close**: Merge, verify post-merge signals, use `github-plan` to sweep
   stale/duplicate/related planning issues, close or relabel reconciled issues,
   and clean up.
