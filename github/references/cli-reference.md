# GitHub Helper CLI Reference

The `github` and `github-plan` skills share helper scripts, but they own
different command surfaces. Use `github` helpers for repository execution and
safe GitHub writes. Use `github-plan` helpers for durable planning lookups,
Projects, issue relationships, and roadmap/focus state.

## Usage Standard

Always use `scripts/gh-plan.py` instead of ad hoc `gh` calls for planning state.
Prefer `uv run scripts/gh-plan.py` for hermetic execution.

Use `scripts/gh-pr.py`, `scripts/gh-issue`, and `scripts/gh-comment` for PR and
transactional issue workflows. Do not route broad planning, Project field, or
relationship work through the execution helper surface.

## Helper Invocation

Choose the interpreter from the helper's extension and shebang before running
`--help` or a workflow command.

- `scripts/gh-issue`, `scripts/gh-comment`, and
  `scripts/gh-with-env-token` are executable shell helpers even though they do
  not use a `.sh` suffix. Run them directly from this skill directory, or from
  the repo root as `github/scripts/<name>`. Do not run them with `python3` or
  `uv run`.
- `.sh` helpers are shell scripts. Run them directly or with `bash`.
- `.py` helpers that include PEP 723 inline metadata (`# /// script`) should be
  run with `uv run path/to/helper.py` when interpreter version or dependencies
  matter. Plain `python3` is only appropriate when the skill docs explicitly
  say the helper has no managed environment needs.

## Shared API Contract

`scripts/github_api.py` is the common body-safe REST and diagnostics layer.
It invokes `gh api --include` through `scripts/gh-with-env-token` by default,
sends mutation bodies as JSON on stdin, and emits one versioned JSON envelope
on stdout. Human diagnostics remain on stderr and terminal failures return a
nonzero exit code.

- `uv run scripts/github_api.py call --method GET /rate_limit`: Run one REST
  request.
- `uv run scripts/github_api.py call --method POST /path --body-file body.json`:
  Send a JSON body without exposing it in argv. Use `--body-file -` for stdin.
- `uv run scripts/github_api.py rate-limit`: Read and normalize quota metadata;
  the in-process probe is bounded to one live request.

Set `GITHUB_API_GH` only in tests or controlled local diagnostics that need to
replace the default `scripts/gh-with-env-token` executable.

The result envelope separates failure cause, write-outcome certainty,
retryability, fallback eligibility, and final disposition. It also carries the
GitHub request id and rate-limit headers when available. Authentication or quota
failure never changes the acting account implicitly.

Terminal envelopes use stable top-level fields for `schema_version`, `ok`,
`exit_code`, `operation`, `actor`, `expected_actor`, `host`, `transport`,
`bucket`, `status`, `request_id`, quota and retry timing, `write_outcome`,
`retryable`, `fallback_eligible`, `disposition`, `completed_steps`, and
`failed_step`. Helper-specific result fields remain top-level for compatibility;
legacy short operation names are exposed as `action` when needed.
Argument-validation failures use the same envelope with `exit_code: 2` instead
of bypassing machine output through argparse-only usage text.

GraphQL requests carry `graphql_operation` as `query`, `mutation`,
`subscription`, or conservative `unknown`. A GraphQL POST query is read-only;
only mutations and unknown documents receive write-outcome semantics. Direct
status/header/body evidence wins over diagnostics, and the bounded
`GET /rate_limit` probe is used only when legacy output reports a rate limit
without identifying its bucket.

Schema version 2 of `references/operation-matrix.toml` is the machine-readable source of truth for
each public helper operation's live and selected transport, quota bucket, actor
policy, idempotency/retry posture, reconciliation strategy, and retained
GraphQL rationale. A row with a pending transport or internal component change
must set `migration_status = "planned"` plus its current command and quota
bucket, even when both the current and selected top-level transports are
`composite`. This prevents automation from mistaking an approved target for
checked-in behavior. Validate the matrix with
`uv run scripts/validate-operation-matrix.py`.

## Common Commands

### Execution: PRs And Rate Limits

`scripts/gh-pr.py` emits one versioned JSON object on stdout for success or
terminal failure. Human-readable failure text stays on stderr; REST failures
also include the shared `api_result` diagnostics envelope.

- `scripts/gh-pr.py view <pr>`: Show PR metadata.
- `scripts/gh-pr.py list --state open --limit 20`: List PR metadata.
- `scripts/gh-pr.py create --title TITLE --body-file BODY.md`:
  Create a PR through the automation-token wrapper.
- `scripts/gh-pr.py edit <pr> --body-file BODY.md`: Replace a PR
  body through the automation-token wrapper.
- `scripts/gh-pr.py comment <pr> --body-file COMMENT.md`: Add a PR
  timeline comment through the shared REST issue-comment endpoint.
- `scripts/gh-pr.py checks <pr>`: Show check runs and commit statuses
  for the PR head.
- `scripts/gh-pr.py merge <pr> --method merge`: Merge a PR.
- `scripts/gh-pr.py supersede <pr> --by <canonical-pr>`: Comment on a
  superseded PR, rewrite issue-closing keywords to `Refs`, and close it unless
  `--keep-open` is supplied. Add `--delete-branch` to delete the stale same-repo
  remote task branch after the PR is closed and the helper verifies it is not
  the base branch. Use `--dry-run` to preview the body rewrite, comment,
  closure, and branch cleanup before mutating GitHub state.
- `scripts/gh-pr.py rate-limit`: Show REST/core and GraphQL rate
  buckets.

Use this helper for high-frequency PR polling, check polling,
PR create/edit/comment writes, merge readiness, and merge execution. Ask for
the PR operation you need; the helper is REST-first for normal PR orientation
and owns quota-aware degraded output.
GraphQL-only fields such as `reviewDecision` and `statusCheckRollup` are
intentionally nullable in helper output unless a future command explicitly opts
into enrichment. Keep raw GraphQL-backed `gh pr view`, Projects, sub-issues,
and dependency operations for data the helper cannot yet provide cleanly.

Use `supersede` after a canonical PR has been selected for a duplicate or
competing implementation. It is intentionally focused on the stale PR: it posts
the canonical PR link, neutralizes `Closes`/`Fixes`/`Resolves` references in the
stale body, closes the PR so future agents do not treat it as mergeable, and can
delete the unused remote task branch when `--delete-branch` is explicitly
requested.

### Planning: Orientation

Use these through the `github-plan` skill when the user is asking for durable
work tracking, roadmap state, parent/sub-issues, blockers, stale plan cleanup,
or Project focus state.

- `index`: List compact plan issues (no bodies).
- `search <query>`: Search for issues.
- `show <issue>`: Show selected sections. Use `--full` for the entire body.
- `deps <issue>`: Show dependencies and sub-issues.

### Planning: Management

- `create <title>`: Create a new plan issue. Supports `--title` (flag), `--body`,
  `--plan-status`, `--focus`, and `--finish-line`.
- `update-section <issue> <section>`: Patch a single markdown section.
- `link <issue> <rel> <target>`: Manage native `blocked-by`, `blocks`, or
  `subissue` relationships.
- `close <issue>`: Mark plan as done, update labels, and clear Project focus.

### Planning: Projects

- `project-list --owner <owner>`: List Projects.
- `project-add <issue> --project <name>`: Add issue to a Project and return the
  Project item id when GitHub provides one.
- `project-set <issue>`: Update Project fields (`--focus`, `--manager`,
  `--finish-line`). Pass `--item-id <id>` when using the id returned by
  `project-add` so the helper can skip lookup-sensitive rediscovery.

Project commands preflight GraphQL quota, cache Project metadata within the run,
and classify recoverable failures with `error_code` values such as
`rate_limited`, `project_auth_denied`, `lookup_stale`, `not_in_project`, and
`field_or_option_missing`. Project auth or visibility failures do not
automatically fall back to active human auth; the helper reports the acting
identity, target Project, and human choices instead.

When issue creation or close succeeds but optional Project sync fails, the
helper returns `ok: true` with a non-blocking Project warning, target context,
the Project sync operation that needs follow-up, and compact
`recommended_actions` when the failure needs an auth or config decision.

## Formatting Tip

For multiline ordinary issue create/edit bodies, prefer `scripts/gh-issue` so
literal Markdown is read from stdin and passed to `gh` with `--body-file`:

```bash
scripts/gh-issue create "Audit repo metadata" --repo OWNER/REPO <<'EOF'
## Objective

Review `.github/github.json` and keep backticks literal.
EOF

scripts/gh-issue edit 123 --repo OWNER/REPO <<'EOF'
## Current Status

State: Active
EOF

scripts/gh-issue close 123 --repo OWNER/REPO --reason completed <<'EOF'
Closing with a multiline Markdown comment before closing the issue.
EOF
```

`gh issue close` does not support `--body-file`; `scripts/gh-issue close` reads
stdin and uses the best available safe transport for non-plan issues. Ordinary
close comments are passed as `gh issue close --comment` so the close and comment
are one `gh` operation. Large comments are posted first with
`gh issue comment --body-file`, then the issue is closed, so the body is streamed
instead of inlined into argv. For completed durable plan issues, use
`scripts/gh-plan.py close --comment-file` so plan labels and Project focus stay
in sync.

For timeline comments, use `scripts/gh-pr.py comment --body-file` in PR-centric
workflows that already resolve PR numbers, URLs, or branches. Use
`scripts/gh-comment issue|pr` for the generic stdin interface and its
`--edit-last` / `--create-if-none` compatibility surface. The planned comment
migration will route both entry points through one shared REST implementation.
For PR review feedback, use `scripts/gh-with-env-token pr review --body-file`.

Raw `gh pr create`, `gh pr edit`, and `gh pr comment` use the active local
account. Prefer the PR helper write subcommands above so PR creation, PR body
edits, and PR timeline comments use the configured automation token. PR
timeline comments use the shared REST transport; create/edit retain the guarded
CLI path until their full option surface is migrated.

`scripts/gh-issue` routes through `scripts/gh-with-env-token` by default so it
uses the skill's configured GitHub token. Set `GH_ISSUE_GH` only in tests or
special local cases where a different `gh` executable should be used.
`GH_COMMENT_GH` provides the equivalent test-only override for
`scripts/gh-comment`.

`scripts/gh-issue` and `scripts/gh-comment` emit the same single terminal JSON
envelope contract as the Python helpers. Delegated command output is available
as `body`; compound large-comment close flows report `completed_steps` and the
failing step without printing multiple machine objects. Human warnings and
progress remain on stderr, and the process exit code matches `exit_code`.

`scripts/gh-with-env-token` is automation-first when a token is configured. It
loads `$CODE_HOME/local.env` by default, falling back to
`$CODEX_HOME/local.env` and then `~/.code/local.env`, then prefers
`CODEX_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN` in that order. Commands fail
closed without changing actor when automation auth is missing, rejected, or
rate-limited. Write-like commands also require the authenticated login to match
`shiny-code-bot`. Set `GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` only for
an explicitly approved one-off command whose human-owned actor is acceptable.
Set `CODEX_SKILLS_ENV_FILE` only in tests or special local cases where a
different env file should be used.

The wrapper remains a transparent transport for delegated `gh` stdout, but its
failure decision is owned by `scripts/github_api.py classify-legacy` rather
than independent shell greps. Structured HTTP evidence is preferred, explicit
GraphQL/secondary-limit output is classified without changing actor, and an
unknown legacy rate-limit bucket may use one bounded diagnostic probe.
Even with explicit active-auth fallback authorization, a write classified with
`write_outcome: unknown` is never replayed under another identity. Planning
commands also fail closed when the automation helper is unavailable unless
active auth was explicitly selected with the documented planning override.
Before any explicitly authorized active-auth command runs, the wrapper reports
the resolved active login (or `unknown` when it cannot be resolved). Write actor
preflight failures use the shared classifier before the mutation is refused.

For commits and pushes performed by Code or spawned agents, use
`scripts/git-commit-as-bot` and `scripts/git-push-as-bot` so Git author,
committer, push events, and resulting Actions runs stay owned by
`shiny-code-bot`.

Planning helpers preserve the selected actor when authentication or quota
failures occur. Set `GH_PLAN_SKIP_BOT=1` for explicitly authorized temporary
active-account planning work, or reserve `GH_PLAN_ALLOW_ACTIVE_FIRST=1` for
explicit local debugging of Project operations that already request active
auth. Neither setting is an automatic rate-limit retry.

Avoid passing escaped `\n` through shell-quoted `--body`. Also avoid unquoted
heredocs like `<<EOF` for Markdown bodies: shell command substitution runs
inside backticks before the body reaches GitHub. Use `<<'EOF'` for literal
Markdown when a heredoc is necessary.
