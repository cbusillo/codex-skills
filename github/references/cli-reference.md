# gh-plan.py CLI Reference

The `gh-plan.py` script is a compact helper for managing GitHub issues. It
returns compact JSON and avoids loading large issue bodies unless requested.

## Usage Standard

Always use `scripts/gh-plan.py` instead of ad hoc `gh` calls for planning state.
Prefer `uv run scripts/gh-plan.py` for hermetic execution.

## Common Commands

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
- `project-add <issue> --project <name>`: Add issue to a Project.
- `project-set <issue>`: Update Project fields (`--focus`, `--manager`, `--finish-line`).

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

For timeline comments, use `scripts/gh-comment`. For PR review feedback, use
`gh pr review --body-file`.

`scripts/gh-issue` routes through `scripts/gh-with-env-token` by default so it
uses the skill's configured GitHub token. Set `GH_ISSUE_GH` only in tests or
special local cases where a different `gh` executable should be used.

Planning helpers are bot-first by default. If the bot token hits a GraphQL/API
rate limit, helpers may retry with the active `gh` account and report the actor
in their JSON output. Do not make active-user execution the default; reserve
`GH_PLAN_ALLOW_ACTIVE_FIRST=1` for explicit local debugging.

Avoid passing escaped `\n` through shell-quoted `--body`. Also avoid unquoted
heredocs like `<<EOF` for Markdown bodies: shell command substitution runs
inside backticks before the body reaches GitHub. Use `<<'EOF'` for literal
Markdown when a heredoc is necessary.
