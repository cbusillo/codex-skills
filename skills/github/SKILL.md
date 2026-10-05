---
name: github
description: "Comprehensive GitHub Expert persona for repository execution and hygiene: PRs, branches, Actions, reviews, merge/deploy state, issue comments, and safe cleanup. For durable planning, roadmaps, blockers, Projects, or workstream graphs, use github-plan."
metadata:
  short-description: Execute GitHub repo workflows
resources:
  - path: scripts/github_app_setup.py
    kind: script
    description: Director-run manifest registration and private configuration for a separate automation App.
  - path: scripts/github-capabilities.py
    kind: script
    description: Derives the supported permission profile and safely audits the configured App across installed repositories.
  - path: references/github-permissions.md
    kind: reference
    description: Read for App setup, missing access, or new GitHub API surfaces; covers scoped roles, safe evidence and drift maintenance.
  - path: scripts/github_api.py
    kind: script
    description: Shared body-safe GitHub API transport, terminal envelope, legacy failure classifier, GraphQL operation context, and rate-limit metadata layer.
  - path: scripts/github_read.py
    kind: script
    description: Shared paged REST readers, including private body-safe conditional GET caching for watcher use and the automation-only sanitized secret-scanning status signal.
  - path: scripts/gh-pr.py
    kind: script
    description: REST-first pull request helper for PR view, list, create, edit, comment, checks, merge, supersede, and rate-limit operations.
  - path: scripts/reconcile-runtime-checkout.py
    kind: script
    description: Safely fast-forward a runtime-bound checkout after a confirmed repository landing while preserving remote/local outcome separation.
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
  - path: scripts/github_workflow_babysit.py
    kind: script
    description: Dispatch or watch one exact GitHub Actions run with bounded protected-environment diagnosis and split-identity approval.
  - path: scripts/github-repo-snapshot.sh
    kind: script
    description: Capture compact repository, branch, PR, and workflow state for orientation.
  - path: scripts/github-work-evidence.py
    kind: script
    description: Collect bounded read-only cross-repo GitHub work evidence as JSON for planning, readiness, closeout, or LLM-led reporting.
  - path: scripts/gh-rulesets.py
    kind: script
    description: Plans or explicitly applies the standard repository owner, automation, and direction ruleset pair with active repository owner verification.
  - path: scripts/gh-plan.py
    kind: script
    description: Shared planning issue, milestone, Project, and next-work helper used by GitHub planning workflows.
  - path: scripts/github_milestone.py
    kind: script
    description: Shared actor-aware REST milestone lifecycle helper used by gh-plan milestone commands.
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
  - name: github-app-guided-setup
    source: skill
    resource_path: scripts/github_app_setup.py
    example_argv: ["uv", "run", "scripts/github_app_setup.py", "start", "--owner", "OWNER", "--name", "Repository automation"]
    purpose: Guides repository-owner browser creation/installation and writes verified private automation configuration without applying rulesets.
  - name: github-plan-milestone-list
    source: skill
    resource_path: scripts/gh-plan.py
    example_argv: ["uv", "run", "scripts/gh-plan.py", "milestone-list", "--state", "all"]
    purpose: Routes milestone container work through the maintained planning helper.
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
  - name: github-secret-scanning-status
    source: skill
    resource_path: scripts/github_read.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/github_read.py",
        "--repo",
        "OWNER/REPO",
        "secret-scanning-status",
      ]
    purpose: Reports clean, findings, unavailable, or not-enabled secret-scanning status without returning detected secret values or changing actor identity.
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
  - name: github-reconcile-runtime-checkout
    source: skill
    resource_path: scripts/reconcile-runtime-checkout.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/reconcile-runtime-checkout.py",
        "--merged-worktree",
        ".",
        "--repo",
        "OWNER/REPO",
        "--landing-sha",
        "<full-landing-sha>",
      ]
    purpose: Reconciles a runtime-bound checkout from landed repo-local source without switching, stashing, resetting, or overwriting unsafe local state.
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
  - name: github-workflow-babysit
    source: skill
    resource_path: scripts/github_workflow_babysit.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/github_workflow_babysit.py",
        "dispatch",
        "--repo",
        "OWNER/REPO",
        "--workflow",
        "operator.yml",
        "--ref",
        "main",
        "--approve-environment",
        "protected-admin",
      ]
    purpose: Dispatches with configured automation auth, captures the exact returned run id, diagnoses waiting runs immediately, and approves only explicitly authorized environments with the active human account.
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
  - name: github-rulesets-plan
    source: skill
    resource_path: scripts/gh-rulesets.py
    example_argv: ["uv", "run", "scripts/gh-rulesets.py", "plan", "--repo", "OWNER/REPO"]
    purpose: Reads the active repository owner's full ruleset state and reports the idempotent standard-pair changes without writing.
  - name: github-rulesets-apply
    source: skill
    resource_path: scripts/gh-rulesets.py
    example_argv: ["uv", "run", "scripts/gh-rulesets.py", "apply", "--repo", "OWNER/REPO", "--confirm-owner-admin-write"]
    purpose: Applies the standard pair only after active repository-owner verification and an explicit repository-admin write acknowledgement.
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
    - id: prefer-bounded-workflow-run-watch
      match:
        argv_prefix: ["gh", "run", "watch"]
      action: require_preferred
      message: Raw `gh run watch` does not diagnose protected-environment waits and can poll without a caller timeout. Use the exact-run workflow babysitter.
      preferred:
        - kind: script
          path: scripts/github_workflow_babysit.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/github_workflow_babysit.py",
              "watch",
              "--repo",
              "OWNER/REPO",
              "--run-id",
              "<run-id>",
            ]
          purpose: Watches one exact run, diagnoses waiting states immediately, and stops on a bounded timeout.
    - id: prefer-secret-scanning-status-reader
      match:
        shell_regex: "(?:\\b(?:gh|gh-with-env-token)\\b[\\s\\S]*\\bapi\\b|\\bgithub_api\\.py\\b[\\s\\S]*\\bcall\\b|\\b(?:curl|wget|http)\\b)[\\s\\S]*\\bsecret-scanning/alerts\\b"
      action: require_preferred
      message: Raw secret-scanning alert operations can expose sensitive alert payloads and are unsupported here. Use the sanitized automation-only status reader for reads.
      preferred:
        - kind: script
          path: scripts/github_read.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/github_read.py",
              "--repo",
              "OWNER/REPO",
              "secret-scanning-status",
            ]
          purpose: Forces hidden-secret REST reads and emits only status, counts, actor evidence, and degraded diagnostics.
    - id: reject-direct-issue-resource-patch
      match:
        shell_regex: "(?:\\b(?:gh\\s+api|gh-with-env-token\\s+api)\\b[\\s\\S]*\\brepos/\\S+/\\S+/issues/(?:[0-9]+|comments/[0-9]+)\\b[\\s\\S]*(?:(?:--method|-X)(?:=|\\s*)PATCH\\b)|\\b(?:gh\\s+api|gh-with-env-token\\s+api)\\b[\\s\\S]*(?:(?:--method|-X)(?:=|\\s*)PATCH\\b)[\\s\\S]*\\brepos/\\S+/\\S+/issues/(?:[0-9]+|comments/[0-9]+)\\b)"
      action: reject
      message: Direct issue-resource PATCH requests bypass issue-author ownership and comment-author checks. Use github-plan for planning sections, github/scripts/gh-issue for issue edits, or github/scripts/gh-comment for comments.
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
      message: Raw `gh issue edit` bypasses human-authorship ownership checks, uses active local auth, and can overwrite contributor text. Use `github-plan` for planning body updates and `github/scripts/gh-issue` for ownership-aware generic edits or metadata-only changes.
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
          purpose: Edits metadata safely and rejects cross-author title/body replacement unless an explicit source-content override is supplied.
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
          purpose: Preserves configured automation auth for non-body edit flags that are intentionally outside the REST helper's supported subset; do not use it to replace human-authored title/body text.
    - id: reject-wrapper-cross-author-issue-source-edit
      match:
        shell_regex: "\\bgh-with-env-token\\b.*\\bissue\\s+edit\\b.*(?:--body(?:-file)?|--title|-[bFt])\\b"
      action: reject
      message: Direct wrapper-backed issue title/body replacement bypasses human-authorship ownership checks. Use github-plan for managed planning sections or github/scripts/gh-issue with an explicit cross-author override when the user has specifically authorized replacing source content.
    - id: prefer-gh-issue-close-helper
      match:
        argv_prefix: ["gh", "issue", "close"]
      action: require_preferred
      message: Raw `gh issue close` can mangle close comments. For ordinary non-plan issues, run `scripts/gh-issue close 123 --repo OWNER/REPO --reason completed < comment.md` or use a quoted heredoc so the close comment is read safely from stdin. If the target is a completed durable plan issue, switch to `github-plan` and use `uv run scripts/gh-plan.py close 123 --comment-file comment.md` so planning labels and Project Status stay in sync.
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
      message: Raw `gh workflow` may dispatch or mutate workflows as the active local account. Use the exact-run babysitter for dispatch and the automation-token wrapper for other operations.
      preferred:
        - kind: script
          path: scripts/github_workflow_babysit.py
          example_argv:
            [
              "uv",
              "run",
              "scripts/github_workflow_babysit.py",
              "dispatch",
              "--repo",
              "OWNER/REPO",
              "--workflow",
              "<workflow>",
              "--ref",
              "<ref>",
            ]
          purpose: Dispatches with automation auth, treats GitHub's returned run id as authoritative, diagnoses waiting runs within one poll, and stops on a bounded timeout.
        - kind: script
          path: scripts/gh-with-env-token
          example_argv:
            ["scripts/gh-with-env-token", "workflow", "view", "<workflow>"]
          purpose: Routes non-dispatch workflow operations through configured automation auth; writes fail closed if bot auth is unavailable.
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
    - id: prefer-bot-commit-helper-with-git-options
      match:
        shell_regex: "(?:^|[;&|(`\\n]|\\s-[A-Za-z]*c\\s+['\"])\\s*(?:[A-Za-z_][A-Za-z0-9_]*=\\S*\\s+|(?:command|exec|time|nohup|env)\\s+)*(?:\\S*/)?git(?:\\s+(?:-[cC]\\s+(?:\"[^\"]*\"|'[^']*'|[^\\s;&|()'\"])+|--(?:git-dir|work-tree|namespace|config-env|exec-path|super-prefix|attr-source)(?:=|\\s+)(?:\"[^\"]*\"|'[^']*'|[^\\s;&|()'\"])+|--[a-z][a-z-]*(?:=(?:\"[^\"]*\"|'[^']*'|[^\\s;&|()'\"])+)?|-[pP]))+\\s+commit(?![\\w-])"
      action: require_preferred
      message: Global options such as `git -c` or `git -C` before `commit` still commit as the local human Git identity. Use the bot commit helper, from the target directory, so agent-authored commits are owned by the configured automation identity.
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
    - id: prefer-bot-push-helper-with-git-options
      match:
        shell_regex: "(?:^|[;&|(`\\n]|\\s-[A-Za-z]*c\\s+['\"])\\s*(?:[A-Za-z_][A-Za-z0-9_]*=\\S*\\s+|(?:command|exec|time|nohup|env)\\s+)*(?:\\S*/)?git(?:\\s+(?:-[cC]\\s+(?:\"[^\"]*\"|'[^']*'|[^\\s;&|()'\"])+|--(?:git-dir|work-tree|namespace|config-env|exec-path|super-prefix|attr-source)(?:=|\\s+)(?:\"[^\"]*\"|'[^']*'|[^\\s;&|()'\"])+|--[a-z][a-z-]*(?:=(?:\"[^\"]*\"|'[^']*'|[^\\s;&|()'\"])+)?|-[pP]))+\\s+push(?![\\w-])"
      action: require_preferred
      message: Global options such as `git -c` or `git -C` before `push` still push with the local human Git credential or SSH key. Use the bot push helper, from the target directory, so push events and resulting Actions runs are owned by the configured automation identity.
      preferred:
        - kind: script
          path: scripts/git-push-as-bot
          example_argv:
            ["scripts/git-push-as-bot", "-u", "origin", "task-branch"]
          purpose: Pushes to GitHub using the configured automation token while restoring the normal remote URL afterward.
    - id: prefer-github-plan-milestone-helper
      match:
        shell_regex: "\\b(?:gh\\s+api|gh-with-env-token\\s+api)\\b[\\s\\S]*\\brepos/\\S+/\\S+/milestones"
      action: require_preferred
      message: Milestone containers belong to github-plan. Raw milestone REST calls bypass normalized due dates, actor-aware write envelopes, exact-title conflict handling, and guarded close checks.
      preferred:
        - kind: script
          path: scripts/gh-plan.py
          example_argv: ["uv", "run", "scripts/gh-plan.py", "milestone-list", "--state", "all"]
          purpose: Lists, shows, creates, updates, and guarded-closes milestones through the maintained REST helper.
    - id: prefer-standard-ruleset-helper
      match:
        shell_regex: "\\bgh(?:-with-env-token)?\\s+api\\b(?=[\\s\\S]*(?:(?:-X|--method)(?:=|\\s+)(?:POST|PUT|PATCH|DELETE)\\b|-X(?:POST|PUT|PATCH|DELETE)\\b|(?:--input|-f|-F|--field|--raw-field)(?:=|\\s+)))[\\s\\S]*\\brepos/[^/\\s]+/[^/\\s]+/rulesets(?:/[^\\s]+)?\\b"
      action: require_preferred
      message: Direct ruleset operations bypass active repository-owner verification, standard-pair drift checks, explicit admin-write confirmation, and post-write idempotence verification. Use the maintained ruleset helper.
      preferred:
        - kind: script
          path: scripts/gh-rulesets.py
          example_argv: ["uv", "run", "scripts/gh-rulesets.py", "plan", "--repo", "OWNER/REPO"]
          purpose: Plans or applies the standard repository ruleset pair through the guarded helper.
---

# GitHub Expert

Use this skill for repository execution: branches, pull requests, Actions,
reviews, merge and deploy state, issue comments, and safe cleanup. Pull
requests are the implementation record. For repositories with `DIRECTION.md`,
follow the shared [executing loop](../references/executing-loop.md) through PR
landing and runtime reconciliation. Apply
[task scope and authorization](../references/execution-scope.md); it defines
how existing approval and task boundaries apply.

## Workflow

1. **Orient**: Run `scripts/github-repo-snapshot.sh`. Its merge-train enrollment
   comes from Launchplane's active policy; unavailable reads mean unknown,
   regardless of `.github/github.json`. Use `github-plan` when
   planning state matters. [Repo workflow](references/repo-workflow.md) holds
   orientation, PR, check, review, merge-readiness, and cleanup detail; read
   the section for the step you are on.
2. **Act**: On a default, shared, release, or production branch, create a
   focused task branch before editing. Commit and push with the bot helpers and
   open a PR.
3. **Verify**: Run the pre-push quality gate, diagnose CI failures, and address
   review feedback. Hand repeated CI, review, or mergeability follow-through to
   `babysit-pr`.
4. **Land**: Merge only as described in [Merging](#merging).
5. **Close**: Complete [After A Merge](#after-a-merge), then use `github-plan`
   to sweep stale, duplicate, and related planning issues, and clean up.

## Planning Boundary

`github-plan` owns durable planning: plan issues, parent and sub-issue graphs,
blockers, milestones, Projects, roadmap and focus state, stale or duplicate
plan cleanup, replacing local plan files, and the raw commands behind them
(`gh issue list`, `gh search issues`, `gh project`, and planning GraphQL
relationship and Project operations). This skill owns transactional execution:
PR create, edit, comment, and merge; issue create, edit, and close bodies; CI
diagnosis; and repository cleanup. It may comment on, link, or close issues
during implementation, but it does not flatten broad planning into one issue.
Do not copy roadmap, blocker, or checklist state into repo docs; change docs
only when they must describe current behavior, configuration, or policy.

## Helpers And Identity

The `policy.command_policies` block in this file's frontmatter maps raw `gh`
write and check commands to helpers. A host that hides frontmatter enforces it
when a command runs, and the refusal names the replacement; read the top of
this file for the full mapping and the `commands` entries. Use raw `gh` only
for a surface no helper covers, route it through `scripts/gh-with-env-token`,
and say why.

- **PRs**: `scripts/gh-pr.py view|checks|create|edit|comment|merge|supersede`.
  `--repo` is global and comes first:
  `uv run scripts/gh-pr.py --repo OWNER/REPO merge 123 --method merge`. To
  create a PR in another repository, run from it or pass `--repo` with an
  explicit `--head BRANCH`.
- **Commits and pushes** by Code or spawned agents: `scripts/git-commit-as-bot`
  and `scripts/git-push-as-bot`.
- **Issue bodies and close comments**: `scripts/gh-issue`; from the catalog
  root (`skills/`), `github/scripts/gh-issue create "Title" --repo OWNER/REPO < body.md`.
- **Comments and reviews**: `scripts/gh-pr.py comment --body-file` or
  `scripts/gh-comment` for timeline comments;
  `scripts/gh-with-env-token pr review --body-file` for review feedback.
- **CI failures**: `github-ci-diagnose.py`. Raw `gh run view` or `gh api` log
  reads are fallback diagnostics.

`gh-issue`, `gh-comment`, and `gh-with-env-token` are shell executables without
a `.sh` suffix; run them directly. Run PEP 723 `.py` helpers with `uv run`; see
[Helper Invocation](references/cli-reference.md#helper-invocation). Pass
Markdown bodies through a file or stdin; an unquoted heredoc runs command
substitution inside backticks.

GitHub writes belong to the configured automation account. The helpers select
its credentials and fail closed, without changing actor, when bot auth is
unavailable, rejected, or rate-limited. The helpers choose the identity for
each target repository:

- **Where the App is installed on that repository**: the App, in any
  account's repository.
- **The App's registering account, without an installation on that
  repository**: refused; the Director installs the App there.
- **Any other account's repository without an installation**: your own GitHub
  user, only on an explicit opt-in. An installation on some of that account's
  repositories does not change this for its others. Writing to another
  person's repository is a stop: ask your Director first, then set
  `GH_WITH_ENV_TOKEN_OWN_USER=1` on each command that writes there. Without
  it the helpers refuse and name the opt-in; a value in `local.env` does not
  count. With it, the agent comments, commits, pushes, and opens PRs as the
  active `gh` login, and the helpers print `acting as your own GitHub user`.
  Commits keep your git identity, and `gh-pr.py create` adds the
  AI-assistance sentence to the body unless it already says so.
  `GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1` refuses even with the opt-in.

Otherwise never fall back to the active human `gh` account unless the user
explicitly approves that one-off; then set
`GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` for that command only. For
credential sources, automation-only mode, or identity configuration, read
[Authentication And Identity](references/cli-reference.md#authentication-and-identity).

Helpers own retries and write reconciliation through `scripts/github_api.py`
and `references/operation-matrix.toml`; do not add retry loops or rebuild
helper behavior. When a result is not a confirmed success, for example an
`unknown` `outcome_certainty` or `write_outcome`, or a
`recommended_next_action`, read that operation's entry in the
[CLI reference](references/cli-reference.md) and its
[Shared Retry Policy](references/cli-reference.md#shared-retry-policy) before
acting. Read the object back before any retry. Never replay an unknown write,
and never under another identity. For another operation's arguments or response
contract, read its CLI reference section; skip unrelated recipes.

## Pull Requests

Use PRs for all non-trivial changes.

- **Pre-push quality**: For code changes with an available IDE project, run
  `jetbrains-inspection` on the changed files before pushing or updating a PR.
  If `.github/github.json` defines `qualityGate.inspection`, PR creation and
  updates, ready-to-merge claims, and merges carry that evidence or an explicit
  not-run reason. A recorded contention or preemption `UNKNOWN` under the
  `repo-readiness` milestone exception permits an already authorized merge while
  readiness stays not fully ready; keep the reason and follow-up. If that config
  is blank, missing, contradictory, or surprising, use a one-off
  `changed_files` check only when the helper can infer the route, and ask before
  changing durable config or trusting a suspicious value. If inspection is
  unavailable, record the not-run reason before pushing.
- **Body**: Explain why before what changed. Describe the net change, not
  abandoned attempts, and include purposeful verification rather than routine
  CI steps. Preserve existing screenshots, images, and links. Use repo-relative
  paths or GitHub links and no self-references. Follow
  [talking with the Director](../references/talking-with-the-director.md) for durable
  PR, issue, review, and closeout text.
  For context-only follow-up issues whose implementation has not started, use
  one unwrapped body line starting `Code follow-ups recorded without starting
  implementation:` followed by their links. Keep implemented work elsewhere.
- **Labels**: Planning labels are only for durable planning issues. PR labels
  follow the [label taxonomy](references/repo-workflow.md#label-taxonomy):
  `preview-ready`, the optional `awaiting-qa` handoff, and `ready-to-merge`,
  which still needs a fresh readiness check and merge authorization.
- **Follow-through**: When an open PR needs repeated CI, review, mergeability,
  or merged/closed polling, hand off to `babysit-pr`. Use its `--once` snapshot
  for a merged or closed PR's closeout evidence.
- **Superseded PRs**: Pick the canonical PR, make stale PRs use `Refs` instead
  of closing keywords, comment with the winner, and close them with
  `scripts/gh-pr.py supersede`. Remove their branches and worktrees only under
  [repository cleanup](../references/repo-cleanup.md), after confirming no
  issue, PR, session, or runtime depends on them.
- **Handoffs**: Put recovery-critical handoff content in the owning issue or
  PR. Local handoff files are scratch unless intentionally committed.

## Merging

Merging implementation work means merging its PR through GitHub. Never merge a
task branch locally into a protected branch as a shortcut; local integration is
only for explicit synchronization or stack maintenance, and the result still
lands through a PR. If that happens by accident, preserve the work, restore the
local protected branch to the remote tip, push the task branch, and continue
through the PR. Never push the accidental merge.

Before a merge:

- **Authority**: Merge only when [task scope](../references/execution-scope.md)
  authorizes the change and destination. A readiness question is not merge
  authority.
- **Fresh head**: This also applies before calling a PR green, ready to merge,
  merged, releasable, or clean, and before a release. Read the PR and its
  checks for the current head SHA. Match background review evidence to that
  SHA. Unresolved blocking findings against it block the merge or release even
  when CI is green, until they are addressed, deferred,
  or declined with a recorded reason under
  [reviews by another model](../references/model-review.md). Findings from a
  detached `auto-review-<hex>` worktree are current when their snapshot SHA
  matches the head and history otherwise; those worktrees are not dirty local
  state. Report review lifecycle under
  [background review reporting](../references/background-review-reporting.md):
  no matching evidence is `not yet observable`, never `skipped`, and a final
  response does not wait for a review that starts after it.
- **Human comments**: Before a merge or close settles an issue or PR, run
  `uv run ../github-work-rollup/scripts/github_unanswered_comments.py --thread OWNER/REPO#NUMBER`.
  An attention result or degraded coverage needs a response or explicit
  handoff first; a bot response never proves Director acknowledgement.

When the user does not name a method, say you are using a normal merge commit
and run `scripts/gh-pr.py merge <pr> --method merge`. Use `--method squash`
or `--method rebase` only when the user asks, repo policy requires it, or you ask and get
confirmation.

For stacked PRs, when repo metadata or task context says Launchplane owns the
merge train, delegate stack handling to the `launchplane` workflow and never
hand-collapse the stack in GitHub. Otherwise consider a rollup branch when
merging each layer would rerun expensive checks or churn conflicts.

## After A Merge

Remote merge success and local reconciliation are separate outcomes; report
both. A blocked or failed local step never turns a confirmed merge into a
failure and never causes a merge retry.

- **Landing SHA**: Use `merge.sha` from a successful direct merge,
  `mergeCommitOid` from a fresh merged-PR view, or the final landing commit from
  a terminal train controller result. Never substitute a PR head, candidate, or
  other intermediate SHA.
- **Runtime checkout**: If the repository is bound into the active skills
  runtime, run the landed repo-local `scripts/reconcile-runtime-checkout.py`
  with the source worktree and full landing SHA; see
  [Runtime Checkout Reconciliation](references/cli-reference.md#runtime-checkout-reconciliation).
  It fast-forwards only a clean checkout already on the default branch that
  shares Git identity with the merged worktree. Treat runtime-dependent evidence
  as stale until it succeeds or the source revision is verified.
- **Local default checkout**: After every confirmed merge, inspect the
  repository's unique local default-branch worktree when one exists. Before
  evaluating or refreshing it, read
  [post-merge checkouts](references/post-merge-checkouts.md) for its gate
  order, landing-SHA proofs, the explicitly requested untracked-only exception,
  and the stale-checkout report. A runtime-bound checkout only ever uses the
  reconciler. Never reset, stash, clean, or overwrite an unsafe checkout. The
  active task worktree stays the agent's source.
- **Verify and sweep**: Check Actions and relevant security and quality signals
  before closing planning state. `Refs #...` is non-closing: after the canonical
  PR merges, close only issues whose finish line is conclusively met and update
  the rest.

## Diagnostics And Hygiene

- **Permissions**: For App setup, access refusals, or new API surfaces, use
  [capability profiles](references/github-permissions.md): derive the
  full-operation profile from the operation matrix and audit the configured
  installation. Distinguish missing grants from disabled features, installation
  scope, and unavailable evidence, and keep existing authorization and actor
  boundaries. A repository grant does not change Launchplane author policy.
- **Cleanup**: For task branch or worktree cleanup, a bulk cleanup audit, or
  repository retirement, read
  [repository cleanup and preservation](../references/repo-cleanup.md) and
  apply its evidence, disposition, preservation, authorization, and reporting
  contract.
