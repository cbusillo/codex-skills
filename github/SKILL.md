---
name: github
description: "Comprehensive GitHub Expert persona for repository execution and hygiene: PRs, branches, Actions, reviews, merge/deploy state, issue comments, and safe cleanup. For durable planning, roadmaps, blockers, Projects, or workstream graphs, use github-plan."
metadata:
  short-description: Execute GitHub repo workflows
resources:
  - path: scripts/github_api.py
    kind: script
    description: Shared body-safe GitHub API transport, terminal envelope, legacy failure classifier, GraphQL operation context, and rate-limit metadata layer.
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
    description: GitHub CLI wrapper that selects configured automation auth and refuses active-auth fallback unless explicitly allowed.
  - path: scripts/git-commit-as-bot
    kind: script
    description: Commit with the configured automation author and committer identity.
  - path: scripts/git-push-as-bot
    kind: script
    description: Push GitHub branches with the configured automation token.
  - path: scripts/github-ci-diagnose.py
    kind: script
    description: Diagnose failing PR checks and summarize relevant CI log excerpts.
  - path: scripts/github-repo-snapshot.sh
    kind: script
    description: Capture compact repository, branch, PR, and workflow state for orientation.
  - path: scripts/github-work-evidence.py
    kind: script
    description: Collect bounded read-only cross-repo GitHub work evidence as JSON for planning, readiness, closeout, or LLM-led reporting.
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
  - path: references/work-evidence.md
    kind: reference
    description: Contract for the read-only GitHub work evidence helper and downstream consumers.
  - path: references/operation-matrix.toml
    kind: reference
    description: Machine-readable transport, quota, actor, retry, and reconciliation decisions for GitHub helper operations.
commands:
  - name: github-api-call
    source: skill
    resource_path: scripts/github_api.py
    example_argv:
      ["uv", "run", "scripts/github_api.py", "call", "--method", "GET", "/rate_limit"]
    purpose: Runs one body-safe GitHub API request and emits the versioned diagnostics envelope.
  - name: github-api-rate-limit
    source: skill
    resource_path: scripts/github_api.py
    example_argv: ["uv", "run", "scripts/github_api.py", "rate-limit"]
    purpose: Reads and normalizes GitHub rate-limit metadata through the shared API layer.
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
    example_argv:
      ["github/scripts/gh-issue", "create", "<title>", "--repo", "OWNER/REPO"]
    purpose: Creates GitHub issues by reading Markdown from stdin; pass the body with shell redirection or a quoted heredoc.
  - name: github-issue-edit
    source: skill
    resource_path: scripts/gh-issue
    example_argv:
      ["github/scripts/gh-issue", "edit", "<issue>", "--repo", "OWNER/REPO"]
    purpose: Edits GitHub issues by reading replacement Markdown from stdin; pass the body with shell redirection or a quoted heredoc.
  - name: github-issue-close
    source: skill
    resource_path: scripts/gh-issue
    example_argv:
      [
        "github/scripts/gh-issue",
        "close",
        "<issue>",
        "--repo",
        "OWNER/REPO",
        "--reason",
        "completed",
      ]
    purpose: Closes GitHub issues by reading an optional close comment from stdin; pass the comment with shell redirection or a quoted heredoc.
  - name: github-issue-reopen
    source: skill
    resource_path: scripts/gh-issue
    example_argv:
      ["github/scripts/gh-issue", "reopen", "<issue>", "--repo", "OWNER/REPO"]
    purpose: Reopens GitHub issues by reading an optional reopen comment from stdin; pass the comment with shell redirection or a quoted heredoc.
  - name: github-comment
    source: skill
    resource_path: scripts/gh-comment
    example_argv: ["scripts/gh-comment", "pr", "<pr>"]
    purpose: Posts issue or PR comments through configured automation auth using stdin-backed Markdown.
  - name: github-gh-with-env-token
    source: skill
    resource_path: scripts/gh-with-env-token
    example_argv: ["scripts/gh-with-env-token", "api", "user", "--jq", ".login"]
    purpose: Routes unsupported gh commands through configured automation auth and fails closed without changing actor unless fallback is explicitly allowed.
  - name: github-git-commit-as-bot
    source: skill
    resource_path: scripts/git-commit-as-bot
    example_argv: ["scripts/git-commit-as-bot", "-m", "fix: describe change"]
    purpose: Commits with the configured automation identity as author and committer while preserving normal git commit flags.
  - name: github-git-push-as-bot
    source: skill
    resource_path: scripts/git-push-as-bot
    example_argv: ["scripts/git-push-as-bot", "-u", "origin", "task-branch"]
    purpose: Pushes GitHub branches using the configured automation token while restoring the normal remote URL afterward.
  - name: github-ci-diagnose
    source: skill
    resource_path: scripts/github-ci-diagnose.py
    example_argv: ["uv", "run", "scripts/github-ci-diagnose.py", "--pr", "<pr>"]
    purpose: Diagnoses PR checks through shared REST readers, summarizes relevant job logs, and reports quota or degraded components explicitly.
  - name: github-repo-snapshot
    source: skill
    resource_path: scripts/github-repo-snapshot.sh
    example_argv: ["scripts/github-repo-snapshot.sh", "--json"]
    purpose: Captures compact local-git and paged REST GitHub state with per-component request, quota, and degradation evidence.
  - name: github-work-evidence
    source: skill
    resource_path: scripts/github-work-evidence.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/github-work-evidence.py",
        "--repo",
        "OWNER/REPO",
        "--window",
        "24h",
      ]
    purpose: Collects JSON-only GitHub work evidence across repositories, subjects, releases, workflow runs, and mechanical buckets.
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
    - id: prefer-gh-pr-review-wrapper
      match:
        argv_prefix: ["gh", "pr", "review"]
      action: require_preferred
      message: Raw `gh pr review` uses the active local GitHub account. Use the automation-token wrapper so review submissions are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            [
              "scripts/gh-with-env-token",
              "pr",
              "review",
              "<pr>",
              "--body-file",
              "<file>",
            ]
          purpose: Posts PR reviews through the configured automation token and fails closed for writes if bot auth is unavailable.
    - id: prefer-gh-pr-state-wrapper
      match:
        argv_prefix: ["gh", "pr", "close"]
      action: require_preferred
      message: Raw PR state mutations use the active local GitHub account. Use the automation-token wrapper so PR state changes are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv: ["scripts/gh-with-env-token", "pr", "close", "<pr>"]
          purpose: Mutates PR state through the configured automation token and fails closed for writes if bot auth is unavailable.
    - id: prefer-gh-pr-reopen-wrapper
      match:
        argv_prefix: ["gh", "pr", "reopen"]
      action: require_preferred
      message: Raw PR state mutations use the active local GitHub account. Use the automation-token wrapper so PR state changes are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv: ["scripts/gh-with-env-token", "pr", "reopen", "<pr>"]
          purpose: Reopens PRs through the configured automation token and fails closed for writes if bot auth is unavailable.
    - id: prefer-gh-pr-ready-wrapper
      match:
        argv_prefix: ["gh", "pr", "ready"]
      action: require_preferred
      message: Raw PR readiness mutations use the active local GitHub account. Use the automation-token wrapper so PR state changes are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv: ["scripts/gh-with-env-token", "pr", "ready", "<pr>"]
          purpose: Marks PRs ready through the configured automation token and fails closed for writes if bot auth is unavailable.
    - id: prefer-gh-pr-update-branch-wrapper
      match:
        argv_prefix: ["gh", "pr", "update-branch"]
      action: require_preferred
      message: Raw PR branch updates use the active local GitHub account. Use the automation-token wrapper so branch updates are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            ["scripts/gh-with-env-token", "pr", "update-branch", "<pr>"]
          purpose: Updates PR branches through the configured automation token and fails closed for writes if bot auth is unavailable.
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
    - id: prefer-gh-pr-checks-helper
      match:
        argv_prefix: ["gh", "pr", "checks"]
      action: require_preferred
      message: Raw `gh pr checks` can start ad hoc polling and may use the active local GitHub account. Use the PR helper for point-in-time check state, or `babysit-pr` when the task needs ongoing CI/review follow-through.
      preferred:
        - kind: script
          path: scripts/gh-pr.py
          example_argv: ["scripts/gh-pr.py", "checks", "<pr>"]
          purpose: Reads PR check runs and commit statuses through the configured helper path.
        - kind: skill
          name: babysit-pr
          purpose: Watches an open PR until CI/review/mergeability follow-through reaches a terminal or user-help state.
    - id: prefer-gh-run-rerun-wrapper
      match:
        argv_prefix: ["gh", "run", "rerun"]
      action: require_preferred
      message: Raw `gh run rerun` uses the active local GitHub account. Use `babysit-pr` or the automation-token wrapper so Actions reruns are owned by the configured automation identity.
      preferred:
        - kind: skill
          name: babysit-pr
          purpose: Reruns failed PR jobs through the watcher when retry policy recommends it.
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            [
              "scripts/gh-with-env-token",
              "run",
              "rerun",
              "<run-id>",
              "--failed",
            ]
          purpose: Reruns Actions through the configured automation token and fails closed for writes if bot auth is unavailable.
    - id: prefer-gh-api-wrapper
      match:
        shell_regex: "\\bgh\\s+api\\s+(?!graphql\\b)"
      action: require_preferred
      message: Raw `gh api` uses the active local GitHub account. Route API calls through `scripts/gh-with-env-token`; write-like API calls must be owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            [
              "scripts/gh-with-env-token",
              "api",
              "repos/OWNER/REPO/actions/runs",
              "--method",
              "GET",
              "-f",
              "per_page=100",
            ]
          purpose: Runs GitHub API calls through configured automation auth and changes actor only when active-auth fallback is explicitly approved.
    - id: prefer-gh-issue-create-helper
      match:
        argv_prefix: ["gh", "issue", "create"]
      action: require_preferred
      message: Raw `gh issue create` uses active local auth and is fragile for multiline Markdown. Use `github/scripts/gh-issue` for REST-backed title, body, label, assignee, and milestone creation. For project, template, type, relationship, editor, recover, or web-only flags outside that REST subset, use `github/scripts/gh-with-env-token issue create --body-file ...` deliberately.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv:
            [
              "github/scripts/gh-issue",
              "create",
              "<title>",
              "--repo",
              "OWNER/REPO",
            ]
          purpose: Creates issues by reading Markdown from stdin; use `< body.md` or a quoted heredoc for the body.
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            [
              "github/scripts/gh-with-env-token",
              "issue",
              "create",
              "--title",
              "<title>",
              "--body-file",
              "<body-file>",
              "--project",
              "<project>",
            ]
          purpose: Preserves configured automation auth for create flags that are intentionally outside the REST helper's supported subset.
    - id: prefer-gh-issue-comment-helper
      match:
        argv_prefix: ["gh", "issue", "comment"]
      action: require_preferred
      message: Raw `gh issue comment` uses the active local GitHub account and is fragile for multiline Markdown. Use the safe comment helper.
      preferred:
        - kind: script
          path: scripts/gh-comment
          example_argv: ["scripts/gh-comment", "issue", "<issue>"]
          purpose: Reads comment Markdown from stdin and posts through the configured automation token.
    - id: prefer-gh-issue-edit-helper
      match:
        argv_prefix: ["gh", "issue", "edit"]
      action: require_preferred
      message: Raw `gh issue edit` uses active local auth and can mangle multiline issue bodies. Use `github/scripts/gh-issue` for REST-backed title, body, label, assignee, and milestone edits. For project, type, parent, sub-issue, or dependency flags outside that REST subset, use `github/scripts/gh-with-env-token issue edit --body-file ...` deliberately.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv:
            [
              "github/scripts/gh-issue",
              "edit",
              "<issue>",
              "--repo",
              "OWNER/REPO",
            ]
          purpose: Edits issues by reading replacement Markdown from stdin; use `< body.md` or a quoted heredoc for the body.
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            [
              "github/scripts/gh-with-env-token",
              "issue",
              "edit",
              "<issue>",
              "--add-project",
              "<project>",
            ]
          purpose: Preserves configured automation auth for edit flags that are intentionally outside the REST helper's supported subset.
    - id: prefer-gh-issue-close-helper
      match:
        argv_prefix: ["gh", "issue", "close"]
      action: require_preferred
      message: Raw `gh issue close` can mangle close comments. For ordinary non-plan issues, run `github/scripts/gh-issue close 123 --repo OWNER/REPO --reason completed < comment.md` or use a quoted heredoc so the close comment is read safely from stdin. If the target is a completed durable plan issue, switch to `github-plan` and use `uv run $CODE_HOME/skills/github/scripts/gh-plan.py close 123 --comment-file comment.md` so planning labels and Project focus stay in sync.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv:
            [
              "github/scripts/gh-issue",
              "close",
              "<issue>",
              "--repo",
              "OWNER/REPO",
              "--reason",
              "completed",
            ]
          purpose: Closes ordinary non-plan issues by reading an optional close comment from stdin; use `< comment.md` or a quoted heredoc for the comment.
    - id: prefer-gh-issue-reopen-helper
      match:
        argv_prefix: ["gh", "issue", "reopen"]
      action: require_preferred
      message: Raw `gh issue reopen` uses active local auth and cannot preserve multiline reopen comments safely. Use `github/scripts/gh-issue reopen 123 --repo OWNER/REPO < comment.md` or a quoted heredoc.
      preferred:
        - kind: script
          path: scripts/gh-issue
          example_argv:
            [
              "github/scripts/gh-issue",
              "reopen",
              "<issue>",
              "--repo",
              "OWNER/REPO",
            ]
          purpose: Reopens issues through REST and reads an optional reopen comment from stdin.
    - id: prefer-gh-release-wrapper
      match:
        argv_prefix: ["gh", "release"]
      action: require_preferred
      message: Raw `gh release` may create or mutate GitHub state as the active local account. Use the automation-token wrapper for release reads and writes.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            ["scripts/gh-with-env-token", "release", "view", "<tag>"]
          purpose: Routes release operations through configured automation auth; release writes fail closed if bot auth is unavailable.
    - id: prefer-gh-workflow-wrapper
      match:
        argv_prefix: ["gh", "workflow"]
      action: require_preferred
      message: Raw `gh workflow` may trigger or mutate workflows as the active local account. Use the automation-token wrapper.
      preferred:
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            ["scripts/gh-with-env-token", "workflow", "run", "<workflow>"]
          purpose: Routes workflow operations through configured automation auth; workflow writes fail closed if bot auth is unavailable.
    - id: prefer-bot-commit-helper
      match:
        argv_prefix: ["git", "commit"]
      action: require_preferred
      message: Raw `git commit` uses the local human Git identity. Use the bot commit helper so agent-authored commits are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/git-commit-as-bot
          example_argv:
            ["scripts/git-commit-as-bot", "-m", "fix: describe change"]
          purpose: Commits with the configured automation identity as author and committer while preserving normal git commit flags.
    - id: prefer-bot-push-helper
      match:
        argv_prefix: ["git", "push"]
      action: require_preferred
      message: Raw `git push` uses the local human Git credential or SSH key. Use the bot push helper so push events and resulting Actions runs are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/git-push-as-bot
          example_argv:
            ["scripts/git-push-as-bot", "-u", "origin", "task-branch"]
          purpose: Pushes to GitHub using the configured automation token while restoring the normal remote URL afterward.
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

Use the bundled `gh-*`, `git-*`, and `github-*` helper scripts first for GitHub work. The
machine-readable `policy.command_policies` frontmatter owns the common raw `gh`
write/check-to-helper mapping; this prose keeps the judgment around branch
discipline, merge method, formatting, verification, and exceptions. Reach for raw
`gh` only when no helper covers the operation, and route those calls through
`scripts/gh-with-env-token`.

Helper-first ritual for PR work:

- Use `scripts/gh-pr.py view/checks/create/edit/comment/merge` for PR reads,
  writes, check snapshots, and approved merges.
- Use `scripts/git-commit-as-bot` for commits made by Code or spawned agents so
  the commit author and committer are `shiny-code-bot`.
- Use `scripts/git-push-as-bot` for pushes made by Code or spawned agents so
  GitHub push events and Actions runs are attributed to `shiny-code-bot`.
- Use `github-ci-diagnose.py` for CI failure diagnosis, and switch to
  `babysit-pr` when the task becomes repeated PR CI/review/mergeability
  follow-through.
- Use a normal merge commit by default via
  `scripts/gh-pr.py merge <pr> --method merge`; avoid squash or rebase unless the
  user requests it, repo policy requires it, or you have explicit confirmation.
- Use raw `gh` only for unsupported surfaces or fallback diagnostics, and say why
  the helper path did not fit.

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
  target to the active branch/PR head SHA, for example `git rev-parse HEAD` for
  the active checkout or `gh pr view --json headRefOid` for a PR. Treat blocking
  findings against that current target as review feedback to address or
  explicitly defer; do not merge or release solely on CI-green when relevant
  current-target findings are still in-flight or unresolved. Findings whose
  branch/path points at a detached generated `auto-review-<hex>` worktree are
  still current-target findings when their snapshot SHA matches the active
  target. Detached generated auto-review findings whose snapshot SHA differs from
  the active target are external proposal history until verified against current
  `HEAD`. Detached auto-review worktrees remain external review context and
  should not be treated as dirty active worktree state.
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
  available. If `.github/github.json` defines `qualityGate.inspection`, PR
  creation/update, ready-to-merge claims, and merges must carry JetBrains
  evidence from the delegated helper or an explicit not-run reason. If that
  inspection config is blank, missing, contradictory, or surprising, do not
  silently invent repo policy: use a safe one-off `changed_files` check only when
  the helper can infer the correct route, and ask the user before changing
  durable config or treating a suspicious value as authoritative. If unavailable,
  record the not-run reason before pushing.
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
- **Bot Ownership**: Work performed by Code or spawned agents should be owned by
  `shiny-code-bot` in GitHub. Use `scripts/git-commit-as-bot` for commits,
  `scripts/git-push-as-bot` for pushes, helper-backed PR/issue/comment/merge
  flows for GitHub writes, and `scripts/gh-with-env-token` for unsupported raw
  `gh` surfaces such as API, review, workflow, release, and Actions commands. Do
  not let write actions fall back to the active human `gh` account unless the
  user explicitly approves that one-off and you set
  `GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` for that command.
- **Authentication**: The helpers own token selection, fallback behavior,
  consistent warnings, and parseable output. `scripts/gh-with-env-token` loads
  automation auth and fails closed without changing actor when bot auth is
  unavailable, rejected, or rate-limited. Active local `gh` auth is used only
  when the user explicitly approves the one-off and
  `GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` is set. Use
  `scripts/gh-with-env-token --print-auth-account ...` when the acting account
  should be visible; it writes the account receipt to stderr so JSON stdout
  remains parseable.
- **Workflow Detail**: See `references/repo-workflow.md` for orientation,
  PR/check/review handling, and cleanup guardrails.
- **PR Follow-through**: When PR diagnosis or an update/rebase/rerun/review-fix
  push leaves an open PR needing repeated CI, review, mergeability, or
  merged/closed polling, hand off to `babysit-pr` instead of continuing ad hoc
  polling in this skill. Use a `babysit-pr --once` snapshot for already
  merged/closed PR closeout evidence.
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
- `scripts/github_api.py`: Shared JSON-stdin REST transport, response-header
  parsing, diagnostics envelope, legacy command classification, GraphQL
  query/mutation context, redaction, bounded rate-limit probe, matrix-gated
  reset-aware retries, inherited deadlines, and lock-safe shared cooldowns.
- `scripts/github_comment.py`: Shared actor-aware REST timeline-comment create,
  pagination, edit-last selection, and deletion-race handling.
- `scripts/github_issue.py`: Shared actor-aware REST issue create, edit,
  close, reopen, membership, milestone, and reconciliation behavior.
- `scripts/gh-pr.py`: REST-first PR view, list, checks, merge, and rate-limit
  diagnostics. The helper owns quota-aware degraded behavior; GraphQL-only
  fields remain nullable unless a future command explicitly opts into them.
- `scripts/gh-issue`: Safe multiline REST issue create/edit/close/reopen flows
  from stdin with one versioned terminal JSON envelope, including compound-step
  and reconciliation evidence.
- `scripts/gh-comment`: Safe multiline REST commenting with actor-aware
  edit-last semantics, the same terminal JSON envelope, and stderr-only human
  diagnostics.

Retry behavior is owned by `scripts/github_api.py` and
`references/operation-matrix.toml`. Do not add ad hoc helper loops. A matrix
row marked `safe` or `conditional` may retry only when the shared failure
contract permits it; an absent or `manual` row performs one remote call and
fails closed. Primary exhaustion waits for the reported reset plus bounded
jitter, secondary throttling honors `Retry-After`, and all waits honor the
earlier of the configured maximum and inherited request deadline. Concurrent
helpers share `$CODE_HOME/state/github-retry` cooldowns by host, actor, and
bucket. That same deadline bounds subprocesses, cooldown-lock acquisition, and
reconciliation reads. Progress stays on stderr, provider bucket evidence is
validated, actor changes require explicit authorization and begin a distinct
retry context, and unknown non-idempotent outcomes must reconcile by operation
marker plus a pre-write candidate snapshot; a unique new match is recovered
and every other unknown outcome fails closed without replay.

## Workflow Loop

1. **Orient**: Run `github-repo-snapshot.sh`; use `github-plan` if planning
   state matters.
2. **Plan**: Delegate durable planning to `github-plan`.
3. **Act**: Create a task branch, commit, and open a PR.
4. **Verify**: Address CI and review feedback using `github-ci-diagnose.py`.
5. **Close**: Merge, verify post-merge signals, use `github-plan` to sweep
   stale/duplicate/related planning issues, close or relabel reconciled issues,
   and clean up.
