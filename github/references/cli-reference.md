# gh-plan.py CLI Reference

The `gh-plan.py` script is a compact helper for managing GitHub issues. It
returns compact JSON and avoids loading large issue bodies unless requested.

## Usage Standard

Always use `scripts/gh-plan.py` instead of ad hoc `gh` calls for planning state.
Prefer `uv run scripts/gh-plan.py` for hermetic execution.

## Common Commands

### PRs And Rate Limits

- `../github/scripts/gh-pr.py view <pr>`: Show PR metadata.
- `../github/scripts/gh-pr.py list --state open --limit 20`: List PR metadata.
- `../github/scripts/gh-pr.py create --title TITLE --body-file BODY.md`:
  Create a PR through the automation-token wrapper.
- `../github/scripts/gh-pr.py edit <pr> --body-file BODY.md`: Replace a PR
  body through the automation-token wrapper.
- `../github/scripts/gh-pr.py comment <pr> --body-file COMMENT.md`: Add a PR
  timeline comment through the automation-token wrapper.
- `../github/scripts/gh-pr.py checks <pr>`: Show check runs and commit statuses
  for the PR head.
- `../github/scripts/gh-pr.py merge <pr> --method merge`: Merge a PR.
- `../github/scripts/gh-pr.py rate-limit`: Show REST/core and GraphQL rate
  buckets.

Use this helper for high-frequency PR polling, check polling,
PR create/edit/comment writes, merge readiness, and merge execution. Ask for
the PR operation you need; the helper is REST-first for normal PR orientation
and owns quota-aware degraded output.
GraphQL-only fields such as `reviewDecision` and `statusCheckRollup` are
intentionally nullable in helper output unless a future command explicitly opts
into enrichment. Keep raw GraphQL-backed `gh pr view`, Projects, sub-issues,
and dependency operations for data the helper cannot yet provide cleanly.

### Orientation

- `index`: List compact plan issues (no bodies).
- `search <query>`: Search for issues.
- `show <issue>`: Show selected sections. Use `--full` for the entire body.
- `deps <issue>`: Show dependencies and sub-issues.

### Management

- `create <title>`: Create a new plan issue. Supports `--title` (flag), `--body`,
  `--plan-status`, `--focus`, and `--finish-line`.
- `update-section <issue> <section>`: Patch a single markdown section.
- `link <issue> <rel> <target>`: Manage native `blocked-by`, `blocks`, or
  `subissue` relationships.
- `close <issue>`: Mark plan as done, update labels, and clear Project focus.

### Projects

- `project-list --owner <owner>`: List Projects.
- `project-add <issue> --project <name>`: Add issue to a Project and return the
  Project item id when GitHub provides one.
- `project-set <issue>`: Update Project fields (`--focus`, `--manager`,
  `--finish-line`). Pass `--item-id <id>` when using the id returned by
  `project-add` so the helper can skip lookup-sensitive rediscovery.

Project commands preflight GraphQL quota, cache Project metadata within the run,
and classify recoverable failures with `error_code` values such as
`rate_limited`, `lookup_stale`, `not_in_project`, and
`field_or_option_missing`.

## Formatting Tip

For multiline issue create/edit bodies, prefer `scripts/gh-issue` so literal
Markdown is read from stdin and passed to `gh` with `--body-file`:

```bash
scripts/gh-issue create "Audit repo metadata" --repo OWNER/REPO <<'EOF'
## Objective

Review `.github/github.json` and keep backticks literal.
EOF

scripts/gh-issue edit 123 --repo OWNER/REPO <<'EOF'
## Current Status

State: Active
EOF
```

For timeline comments, use `scripts/gh-comment` or
`scripts/gh-pr.py comment --body-file`. For PR review feedback, use
`scripts/gh-with-env-token pr review --body-file`.

Raw `gh pr create`, `gh pr edit`, and `gh pr comment` use the active local
account. Prefer the PR helper write subcommands above so PR creation, PR body
edits, and PR timeline comments route through `scripts/gh-with-env-token` and
use the configured automation token by default.

`scripts/gh-issue` routes through `scripts/gh-with-env-token` by default so it
uses the skill's configured GitHub token. Set `GH_ISSUE_GH` only in tests or
special local cases where a different `gh` executable should be used.

`scripts/gh-with-env-token` is automation-first when a token is configured. It
loads `~/.code/local.env` by default, then prefers `CODEX_GITHUB_TOKEN`,
`GH_TOKEN`, and `GITHUB_TOKEN` in that order. If no automation token is
configured, it warns and uses the active local `gh` account. If the automation
token is invalid or rate-limited, it retries with the active local `gh` account.
Set `CODEX_SKILLS_ENV_FILE` only in tests or special local cases where a
different env file should be used.

Planning helpers are bot-first by default. If the bot token hits a GraphQL/API
rate limit, helpers may retry with the active `gh` account and report the actor
in their JSON output. Set `GH_PLAN_SKIP_BOT=1` for temporary active-account
planning work, or reserve `GH_PLAN_ALLOW_ACTIVE_FIRST=1` for explicit local
debugging of Project operations that already request active auth.

Avoid passing escaped `\n` through shell-quoted `--body`. Also avoid unquoted
heredocs like `<<EOF` for Markdown bodies: shell command substitution runs
inside backticks before the body reaches GitHub. Use `<<'EOF'` for literal
Markdown when a heredoc is necessary.
