---
name: github
description: "Comprehensive GitHub Expert persona for repository execution and hygiene: PRs, branches, Actions, reviews, merge/deploy state, issue comments, and safe cleanup. For durable planning, roadmaps, blockers, Projects, or workstream graphs, use github-plan."
metadata:
  short-description: Execute GitHub repo workflows
resources:
  - path: scripts/gh-pr.py
    kind: script
    description: REST-first pull request helper for PR view, list, create, edit, comment, checks, merge, supersede, and rate-limit operations.
  - path: scripts/gh-issue
    kind: script
    description: Safe issue create, edit, and close helper for multiline Markdown bodies.
  - path: scripts/gh-comment
    kind: script
    description: Safe stdin-backed comment helper for issues and pull requests.
  - path: scripts/gh-with-env-token
    kind: script
    description: GitHub CLI wrapper that selects configured automation auth before falling back to active local auth.
  - path: scripts/github-ci-diagnose.py
    kind: script
    description: Diagnose failing PR checks and summarize relevant CI log excerpts.
  - path: scripts/github-repo-snapshot.sh
    kind: script
    description: Capture compact repository, branch, PR, and workflow state for orientation.
  - path: scripts/gh-plan.py
    kind: script
    description: Shared planning issue and Project helper used by GitHub planning workflows.
  - path: references/repo-workflow.md
    kind: reference
    description: Detailed GitHub workflow, PR, checks, review, and cleanup guidance.
  - path: references/cli-reference.md
    kind: reference
    description: GitHub helper command reference.
  - path: references/issue-templates.md
    kind: reference
    description: Issue and planning body templates.
  - path: references/config-schema.md
    kind: reference
    description: Repository GitHub metadata schema reference.
  - path: references/github-projects.md
    kind: reference
    description: GitHub Projects configuration and field reference.
commands:
  - name: github-pr-view
    source: skill
    resource_path: scripts/gh-pr.py
    example_argv: ["scripts/gh-pr.py", "view", "<pr>"]
    purpose: Reads pull request metadata through the REST-first helper.
  - name: github-pr-create
    source: skill
    resource_path: scripts/gh-pr.py
    example_argv:
      [
        "scripts/gh-pr.py",
        "create",
        "--title",
        "<title>",
        "--body-file",
        "<file>",
      ]
    purpose: Creates pull requests through the helper with safe body handling.
  - name: github-pr-checks
    source: skill
    resource_path: scripts/gh-pr.py
    example_argv: ["scripts/gh-pr.py", "checks", "<pr>"]
    purpose: Reads PR check runs and commit statuses through the helper.
  - name: github-pr-merge
    source: skill
    resource_path: scripts/gh-pr.py
    example_argv:
      [
        "scripts/gh-pr.py",
        "merge",
        "<pr>",
        "--method",
        "merge",
        "--delete-branch",
      ]
    purpose: Merges pull requests through the helper with normalized defaults.
  - name: github-issue-create
    source: skill
    resource_path: scripts/gh-issue
    example_argv: ["github/scripts/gh-issue", "create", "<title>", "--repo", "OWNER/REPO"]
    purpose: Creates GitHub issues by reading Markdown from stdin; pass the body with shell redirection or a quoted heredoc.
  - name: github-issue-edit
    source: skill
    resource_path: scripts/gh-issue
    example_argv: ["github/scripts/gh-issue", "edit", "<issue>", "--repo", "OWNER/REPO"]
    purpose: Edits GitHub issues by reading replacement Markdown from stdin; pass the body with shell redirection or a quoted heredoc.
  - name: github-issue-close
    source: skill
    resource_path: scripts/gh-issue
    example_argv: ["github/scripts/gh-issue", "close", "<issue>", "--repo", "OWNER/REPO", "--reason", "completed"]
    purpose: Closes GitHub issues by reading an optional close comment from stdin; pass the comment with shell redirection or a quoted heredoc.
  - name: github-ci-diagnose
    source: skill
    resource_path: scripts/github-ci-diagnose.py
    example_argv: ["uv", "run", "scripts/github-ci-diagnose.py", "--pr", "<pr>"]
    purpose: Diagnoses failing PR checks and summarizes relevant logs.
  - name: github-repo-snapshot
    source: skill
    resource_path: scripts/github-repo-snapshot.sh
    example_argv: ["scripts/github-repo-snapshot.sh", "--json"]
    purpose: Captures compact repo and GitHub state for orientation.
policy:
  command_policies:
    - id: prefer-gh-pr-create-helper
      match:
        argv_prefix: ["gh", "pr", "create"]
      action: require_preferred
      message: Raw `gh pr create` uses the active local GitHub account and is fragile for multiline Markdown. Use the GitHub helper-backed PR create path.
      preferred:
        - kind: script
          path: scripts/gh-pr.py
          example_argv:
            [
              "scripts/gh-pr.py",
              "create",
              "--title",
              "<title>",
              "--body-file",
              "<file>",
            ]
          purpose: Creates PRs through the configured automation token and preserves body formatting.
    - id: prefer-gh-pr-edit-helper
      match:
        argv_prefix: ["gh", "pr", "edit"]
      action: require_preferred
      message: Raw `gh pr edit` uses the active local GitHub account and can mangle body text. Use the GitHub helper-backed PR edit path.
      preferred:
        - kind: script
          path: scripts/gh-pr.py
          example_argv:
            ["scripts/gh-pr.py", "edit", "<pr>", "--body-file", "<file>"]
          purpose: Edits PR metadata or body through the configured automation token and safe body-file handling.
    - id: prefer-gh-pr-comment-helper
      match:
        argv_prefix: ["gh", "pr", "comment"]
      action: require_preferred
      message: Raw `gh pr comment` is easy to quote incorrectly and may use the active local account. Use the GitHub helper-backed PR comment path.
      preferred:
        - kind: script
          path: scripts/gh-pr.py
          example_argv:
            ["scripts/gh-pr.py", "comment", "<pr>", "--body-file", "<file>"]
          purpose: Posts PR comments with safe Markdown/body-file handling.
        - kind: script
          path: scripts/gh-comment
          example_argv: ["scripts/gh-comment", "pr", "<pr>"]
          purpose: Reads comment Markdown from stdin and posts it safely.
    - id: prefer-gh-pr-merge-helper
      match:
        argv_prefix: ["gh", "pr", "merge"]
      action: require_preferred
      message: Raw `gh pr merge` bypasses the helper's merge defaults, branch cleanup, and token handling. Use the GitHub helper-backed merge path.
      preferred:
        - kind: script
          path: scripts/gh-pr.py
          example_argv:
            [
              "scripts/gh-pr.py",
              "merge",
              "<pr>",
              "--method",
              "merge",
              "--delete-branch",
            ]
          purpose: Performs the approved merge through the REST helper with normalized defaults and optional branch cleanup.
    - id: prefer-gh-issue-create-helper
      match:
        argv_prefix: ["gh", "issue", "create"]
      action: require_preferred
      message: Raw `gh issue create` is fragile for multiline Markdown. From the repo root, run `github/scripts/gh-issue create "Issue title" --repo OWNER/REPO < body.md` or use a quoted heredoc so the body is read safely from stdin.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv: ["github/scripts/gh-issue", "create", "<title>", "--repo", "OWNER/REPO"]
          purpose: Creates issues by reading Markdown from stdin; use `< body.md` or a quoted heredoc for the body.
    - id: prefer-gh-issue-edit-helper
      match:
        argv_prefix: ["gh", "issue", "edit"]
      action: require_preferred
      message: Raw `gh issue edit` can mangle multiline issue bodies. From the repo root, run `github/scripts/gh-issue edit 123 --repo OWNER/REPO < body.md` or use a quoted heredoc so the body is read safely from stdin.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv: ["github/scripts/gh-issue", "edit", "<issue>", "--repo", "OWNER/REPO"]
          purpose: Edits issues by reading replacement Markdown from stdin; use `< body.md` or a quoted heredoc for the body.
    - id: prefer-gh-issue-close-helper
      match:
        argv_prefix: ["gh", "issue", "close"]
      action: require_preferred
      message: Raw `gh issue close` can mangle close comments. From the repo root, run `github/scripts/gh-issue close 123 --repo OWNER/REPO --reason completed < comment.md` or use a quoted heredoc so the close comment is read safely from stdin.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv: ["github/scripts/gh-issue", "close", "<issue>", "--repo", "OWNER/REPO", "--reason", "completed"]
          purpose: Closes issues by reading an optional close comment from stdin; use `< comment.md` or a quoted heredoc for the comment.
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

Raw planning lookups and Project mutations are intentionally not owned by this
skill's command policies. `github-plan` owns `gh issue list`, `gh search issues`,
`gh project`, and planning GraphQL relationship/Project operations. This skill
owns transactional execution such as PR create/edit/comment/merge, issue
create/edit/close bodies, CI diagnosis, and repository cleanup.

This skill may comment on, link to, or close issues as part of implementation
workflow, but it should not flatten broad planning work into a single issue.

Do not duplicate active roadmap, blocker, or checklist state into repo docs.
Update repo docs only through implementation work when they need to describe
current behavior, configuration, or operational policy.

## Implementation & Workflow (PRs & Branches)

Use PRs for all non-trivial code changes.

Use the bundled `gh-*` and `github-*` helper scripts first for GitHub work. The
machine-readable `policy.command_policies` frontmatter owns the common raw `gh`
write-to-helper mapping; this prose keeps the judgment around branch discipline,
merge method, formatting, verification, and exceptions. Reach for raw `gh` only
when no helper covers the operation, and route those calls through
`scripts/gh-with-env-token`.

Do not infer Python from a `scripts/` path. `scripts/gh-issue`,
`scripts/gh-comment`, and `scripts/gh-with-env-token` are executable shell
helpers without `.sh` suffixes; run them directly. Python `.py` helpers with PEP
723 inline metadata should use `uv run path/to/helper.py` when dependency or
interpreter selection matters. See `references/cli-reference.md` for the helper
invocation rules.

- **Branch Discipline**: Protect default, shared, release, and production
  branches. Create focused task branches before editing when currently on a
  protected branch.
- **Merges & Stacks**: For GitHub-backed repositories, merging implementation
  work means merging a Pull Request through GitHub. When the user approves a
  merge and does not specify the method, state that you are using a normal merge
  commit and run `scripts/gh-pr.py merge <pr> --method merge` for GitHub
  helper-backed merge execution. Do not locally merge a task branch into a
  protected, default, shared, release, or production branch as an implementation
  shortcut. Local branch integration is only appropriate for explicit local
  synchronization or stack maintenance, and the resulting implementation still
  lands through a PR. Do not use `--squash` or `--rebase` unless the user
  explicitly asks, repo policy requires it, or you ask and receive confirmation.
  For stacked PRs, consider a rollup branch when merging each layer would rerun
  expensive checks or create avoidable conflict churn, unless repo metadata or
  task context says Launchplane owns the merge train. In Launchplane-managed
  trains, do not hand-collapse stacks in GitHub; delegate stack handling to the
  `launchplane` workflow.
- **Auto-Review Signals**: Before declaring a PR green, ready to merge, merged,
  releasable, or otherwise clean, check background auto-review evidence when it
  is available in the session context or repo tooling. First match each review
  target to the active branch/PR head SHA. Treat blocking findings against the
  current target as review feedback to address or explicitly defer; do not merge
  or release solely on CI-green when relevant current-target findings are still
  in-flight or unresolved. Detached auto-review worktrees remain external review
  context and should not be treated as dirty active worktree state.
- **Accidental Local Default-Branch Merge Recovery**: If implementation work is
  accidentally merged into a protected/default/shared branch locally, preserve
  the commit or branch if needed, restore the local protected branch to the
  remote tip, push or update the task branch, and continue through the PR flow.
  Do not push the accidental local protected-branch merge.
- **Cross-Repo PRs**: When creating a PR for a repository other than the current
  working directory, run `scripts/gh-with-env-token pr create` from that
  repository or pass both `--repo OWNER/REPO` and an explicit `--head` branch.
- **Pre-Push Quality**: For code changes, use `jetbrains-inspection` to run
  targeted JetBrains inspections on changed files or touched directories before
  pushing a branch or updating a PR whenever the repo has an IDE project
  available. If unavailable, record the not-run reason before pushing.
- **Verification**: After merge, verify Actions and relevant security/quality
  signals before closing related planning state.
- **Labels**: Use `github-plan` labels only for durable planning issues. For PR
  execution state, follow the repo workflow taxonomy in
  `references/repo-workflow.md`: `preview-ready` means a preview is available
  for review, `awaiting-qa` is an optional repo-local QA handoff label, and
  `ready-to-merge` is a configured merge readiness signal that still requires a
  fresh readiness check and explicit merge approval.
- **Refs Closeout**: Treat `Refs #...` as intentionally non-closing. After the
  canonical PR merges, sweep referenced issues and close only those whose finish
  line is conclusively satisfied; otherwise comment/update durable state and
  leave them open.
- **Handoffs**: For GitHub-backed work, put recovery-critical handoff content in
  the owning issue or PR comment. Local handoff files are scratch unless they
  are intentionally committed docs.
- **Formatting**: From this repository root, use `github/scripts/gh-issue` for
  issue create/edit bodies and issue close comments, for example
  `github/scripts/gh-issue create "Issue title" --repo OWNER/REPO < body.md`.
  From inside this skill directory, use `scripts/gh-pr.py create --body-file`
  and `scripts/gh-pr.py edit --body-file` for PR bodies,
  `scripts/gh-pr.py comment --body-file` or `scripts/gh-comment pr` for PR
  timeline comments, and `scripts/gh-with-env-token pr review --body-file` for
  PR review feedback when no review helper exists. Avoid unquoted heredocs for
  Markdown bodies because
  shell command substitution runs inside backticks. Follow
  `../references/every-code-formatting.md` when writing durable PR, issue,
  review, or closeout text.
- **PR Body Quality**: Preserve important existing PR body content, especially
  screenshots, images, and links that the author may not be able to recover.
  Explain why the change is being made before listing what changed. Describe the
  net change of the PR, not abandoned implementation attempts. Include
  purposeful verification evidence, but avoid padding the body with routine CI
  steps. Avoid absolute local paths; use repo-relative paths or GitHub links.
  Mention related issues or PRs when useful, and avoid self-references to the PR
  being edited.
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
  failures when available. Raw `gh run view` / `gh api` log commands are
  fallback diagnostics or watcher-specific probes, not the preferred path.
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
- `scripts/gh-issue`: Safe multiline issue create/edit/close bodies from stdin.
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
