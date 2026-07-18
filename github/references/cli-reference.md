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

Matrix-approved operations also report `attempts`, `elapsed_wait`,
`retry_eligible`, `last_actor`, `last_bucket`, `outcome_certainty`,
`reconciliation`, `recommended_next_action`, `effective_deadline`, and
`retry_exhausted_reason`. Progress is concise and stderr-only, so stdout remains
one parseable terminal envelope even while a helper waits.
`gh-pr checks` aggregates those fields across all REST subrequests instead of
reporting only the final status read. Composite diagnostic tools that do not use
the terminal-envelope CLI contract expose the same aggregate under
`diagnostics.retry` and per-request evidence under `diagnostics.requests`.

### Shared Retry Policy

`scripts/github_api.py` loads retry eligibility, idempotency, quota bucket, and
reconciliation strategy from `references/operation-matrix.toml`. Operations
absent from the accepted matrix and rows marked `manual` execute at most one
remote call and return `retry_eligible: false`. Authentication, actor mismatch,
and permission failures never enter the quota retry path.

The production defaults allow one primary GitHub reset window:

- `GITHUB_RETRY_MAX_WAIT_SECONDS=3900`: Maximum elapsed policy window.
- `GITHUB_RETRY_MAX_ATTEMPTS=8`: Initial call plus bounded retries.
- `GITHUB_RETRY_PROGRESS_SECONDS=30`: Stderr progress cadence during long waits.
- `GITHUB_RETRY_JITTER_SECONDS=3`: Maximum non-negative reset/backoff jitter.
- `GITHUB_RETRY_DEADLINE_AT`: Optional inherited absolute Unix deadline. The
  effective deadline is the earlier of this value and the configured maximum.
- `GITHUB_RETRY_STATE_DIR`: Optional shared-state override. The default is
  `$CODE_HOME/state/github-retry`, then `$CODEX_HOME/state/github-retry`, then
  `~/.code/state/github-retry`.

Advanced bounded-backoff and state-lifecycle controls are
`GITHUB_RETRY_BASE_BACKOFF_SECONDS`, `GITHUB_RETRY_MAX_BACKOFF_SECONDS`,
`GITHUB_RETRY_WAIT_SLICE_SECONDS`, `GITHUB_RETRY_LOCK_POLL_SECONDS`,
`GITHUB_RETRY_DRAIN_SECONDS`, and `GITHUB_RETRY_STALE_SECONDS`.

Primary REST and GraphQL exhaustion waits until the reported reset plus bounded
jitter when that reset is inside the effective deadline. Secondary throttling
uses `Retry-After`, then reported reset metadata, then bounded increasing
backoff. Shared cooldown state uses advisory locking and atomic replacement,
is keyed by GitHub host, actor, and quota bucket, expires stale records, and
briefly serializes post-reset calls to avoid a stampede. Subprocess execution,
cooldown-lock acquisition, and any reconciliation reads share the same
effective deadline; none starts a fresh retry window after the parent request
expires or is cancelled.

Read calls may retry provider-classified transient failures. Writes marked
idempotent in the accepted matrix may also retry transient unknown outcomes.
Other writes retry only when the shared result marks the write `not_started` or
`rejected`; an `unknown` non-idempotent outcome requires an operation-specific
reconciliation callback. Issue and comment creates use a stable request
fingerprint, a unique provider-visible ID embedded in a hidden HTML comment,
start time, and a pre-write snapshot of matching object IDs. Reconciliation
requires the unique ID, so concurrent identical requests cannot claim one
another's object; pre-existing or ambiguous matches are rejected, and an
unknown no-match result fails closed without a second create. Provider-confirmed
`not_started` or `rejected` creates may retry without reconciliation. Explicitly authorized
actor changes are announced before execution and start a new actor-keyed
context; timeout results retain any announced fallback actor. Provider
`x-ratelimit-resource` values are normalized to the supported bucket taxonomy,
and an unannounced actor or bucket change fails closed. Legacy GraphQL failures
without reset metadata perform one bounded `/rate_limit` probe for accepted
retry operations and then wait on the reported GraphQL reset.

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

- `scripts/gh-pr.py view <pr>`: Show PR metadata, including `mergedAt` and
  `mergeCommitOid` when GitHub reports a completed merge.
- `scripts/gh-pr.py list --state open --limit 20`: List PR metadata.
- `scripts/gh-pr.py create --title TITLE --body-file BODY.md`:
  Create a PR through the automation-token wrapper.
- `scripts/gh-pr.py edit <pr> --body-file BODY.md`: Replace a PR
  body through the automation-token wrapper.
- `scripts/gh-pr.py comment <pr> --body-file COMMENT.md`: Add a PR
  timeline comment through the shared REST issue-comment endpoint. Add
  `--edit-last` to replace the authenticated actor's latest comment and
  `--create-if-none` only when a missing prior comment should create one.
- `scripts/gh-pr.py checks <pr>`: Show check runs and commit statuses
  for the PR head.
- `scripts/gh-pr.py merge <pr> --method merge`: Merge a PR. The expected head
  SHA guards retries; an unknown response is reconciled by re-reading the PR,
  recovering only a trustworthy final merge SHA and failing closed on head
  drift or ambiguous state.
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

### Security Signal Reads

Use the shared REST reader for repository secret-scanning status:

```sh
uv run github/scripts/github_read.py \
  --repo OWNER/REPO \
  secret-scanning-status
```

The command verifies the configured automation actor, reads repository
visibility, and requests only open alerts with `hide_secret=true`. Its result
contains a status and count, never raw alerts or detected secret values. The
status is one of `clean`, `findings`, `unavailable`, or `not_enabled`;
`unavailable` and `not_enabled` are never evidence that the repository is
clean. Public repositories report `unavailable` because GitHub's repository
alerts endpoint does not provide that signal for public repositories even
though public secret scanning still runs.

This operation is automation-only. It removes
`GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK`, sets
`GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1`, verifies the authenticated login
against the expected bot actor, and does not retry permission or ambiguous
`404` results under active user authentication. The wrapper snapshots the
requirement before loading its env file, so local configuration cannot re-enable
fallback for this command.
Do not read `/secret-scanning/alerts` through raw `gh api`, generic HTTP
clients, or `github_api.py call`. The skill routes those commands to this
reader, the generic API CLI refuses raw repository alert operations, and the token
wrapper admits the reader's underlying request only when automation auth is
required and the exact generated GET retains `state=open` and
`hide_secret=true`.
Use `--limit` to bound the number of open alerts counted; the default is `100`
and the accepted range is `1` through `1000`. A count at the requested limit is
reported as a lower bound.

### Runtime Checkout Reconciliation

Run the reconciler from landed repo-local source after GitHub confirms the final
landing commit. Use `merge.sha` from the successful direct-merge result or
`mergeCommitOid` from a fresh merged-PR view; never use the PR head SHA:

```sh
uv run github/scripts/reconcile-runtime-checkout.py \
  --merged-worktree "$PWD" \
  --repo OWNER/REPO \
  --landing-sha <full-landing-sha>
```

The helper resolves the active runtime skills path using `CODE_HOME`, then
`CODEX_HOME`, then `~/.code`. It acts only when that path belongs to the same Git
repository as `--merged-worktree` and its `origin` identifies `--repo`. It
requires a clean runtime checkout already on the configured default branch,
fetches only that branch from the captured origin URL, requires the landing
SHA on the fetched tip's first-parent history, and verifies the executing helper
against both the landing and fetched-tip Git blobs. It fast-forwards to the
immutable fetched commit with autostash disabled, ignored-file overwrite
disabled, and repository hooks disabled. It never switches branches, resets,
stashes, cleans, or overwrites unsafe local state.

The JSON receipt reports `synchronized`, `already_current`, `not_applicable`,
`blocked`, `retryable`, or `failed`, plus stable reason codes and before/fetched/
after SHAs. Successful or not-applicable results exit `0`, blocked local state
exits `2`, and retryable or failed reconciliation exits `1`. These exit codes
describe local reconciliation only. A confirmed GitHub merge remains successful
when reconciliation is blocked or fails; report both outcomes and never retry a
merge because of the local result.

### Planning: Orientation

Use these through the `github-plan` skill when the user is asking for durable
work tracking, roadmap state, parent/sub-issues, blockers, stale plan cleanup,
or Project focus state.

- `index`: List compact plan issues through paged REST reads, excluding pull
  requests and ordering by most recently updated. Supports `--state`, `--label`,
  and an exact positive `--limit`. Compact states remain normalized as uppercase
  `OPEN` or `CLOSED` values.
- `search <query>`: Search issues through the REST search endpoint with fixed
  `repo:` and `is:issue` constraints. `--state open|closed` adds the matching
  search qualifier, `--state all` omits it, and quota evidence uses the search
  bucket. Compact states remain normalized as uppercase `OPEN` or `CLOSED`
  values.
- `show <issue>`: Show selected sections. Use `--full` for the entire body.
- `deps <issue>`: Show dependencies and sub-issues.

### Planning: Management

- `create <title>`: Create a new plan issue. Exact-title dedupe uses REST issue
  search, labels are ensured through REST, and the shared issue helper creates
  the issue with reconciliation evidence for unknown write outcomes. Supports
  `--title` (flag), `--body`, `--plan-status`, `--focus`, and `--finish-line`.
- `update-section <issue> <section>`: Patch a single markdown section.
- `link <issue> <rel> <target>`: Manage native `blocked-by`, `blocks`, or
  `subissue` relationships.
- `close <issue>`: Mark plan as done through shared REST label, comment, and
  issue-state helpers, then report optional Project status/focus reconciliation
  separately. Re-running close on an already closed issue requests only missing
  label changes, so it can safely reconcile stale plan labels or Project fields.
- `ensure-labels`: Page through repository labels and create documented missing
  planning labels through REST. Concurrent-create conflicts are reconciled by
  reading the requested label instead of blindly retrying the write.

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
literal Markdown is read from stdin and serialized through the shared REST
JSON-stdin transport:

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

scripts/gh-issue close 123 --repo OWNER/REPO --duplicate-of 456

scripts/gh-issue reopen 123 --repo OWNER/REPO <<'EOF'
Reopening with a multiline Markdown comment before changing issue state.
EOF
```

Create supports repeated or comma-separated `--label` and `--assignee` values
plus milestone titles through `--milestone`. Edit supports body/title changes,
label and assignee add/remove flags, and milestone set/remove operations. The
helper resolves milestone titles through paged REST reads before writing.
Edit, close, and reopen accept issue numbers, `#NUMBER`, `OWNER/REPO#NUMBER`, or
full issue URLs; a repository encoded in the target takes precedence over
`--repo`.
Flags that require templates, Projects, issue types, parent/sub-issue or
dependency relationships, editor/recovery state, or browser interaction remain
outside this focused REST surface. Route those deliberate exceptions through
`scripts/gh-with-env-token issue create|edit` with a body file so automation
identity and literal Markdown remain explicit.

`scripts/gh-issue close` and `scripts/gh-issue reopen` read stdin and post any
state-change comment through the shared JSON-stdin REST comment implementation
before sending an explicit REST state/state-reason PATCH. The final envelope
reports `post_close_comment` in `completed_steps` when the comment succeeded but
the close failed, and comment failure stops before close. Successful state
changes append `close_issue` or `reopen_issue` to `completed_steps`.
`--duplicate-of` accepts the same issue-reference forms, resolves the target's
REST database id, and sends `state_reason=duplicate` plus `duplicate_issue_id`
in the close PATCH. It is mutually exclusive with `--reason`. For completed durable
plan issues, use
`scripts/gh-plan.py close --comment-file` so plan labels and Project focus stay
in sync.

For timeline comments, use `scripts/gh-pr.py comment --body-file` in PR-centric
workflows that already resolve PR numbers, URLs, or branches. Use
`scripts/gh-comment issue|pr` for the generic stdin interface and its
`--edit-last` / `--create-if-none` surface. Both entry points resolve the
authenticated actor through REST, page through all comments for edit-last,
select that actor's newest comment by creation time and id, and stream Markdown
through JSON stdin. If the selected comment is deleted before PATCH, the helper
fails without creating a replacement; `--create-if-none` applies only when the
initial paged lookup finds no actor-owned comment.
For PR review feedback, use `scripts/gh-with-env-token pr review --body-file`.

Raw `gh pr create`, `gh pr edit`, and `gh pr comment` use the active local
account. Prefer the PR helper write subcommands above so PR creation, PR body
edits, and PR timeline comments use the configured automation token. PR
timeline comments use the shared REST transport; create/edit retain the guarded
CLI path until their full option surface is migrated.

`scripts/gh-issue` routes its REST calls through `scripts/gh-with-env-token` by
default so it uses the skill's configured GitHub token. Set `GH_ISSUE_GH` only
in tests or special local cases where a different `gh` executable should be
used.
`GH_COMMENT_GH` provides the equivalent test-only override for
`scripts/gh-comment`.

`scripts/gh-issue` and `scripts/gh-comment` emit the same single terminal JSON
envelope contract as the Python helpers. Comment results report
`comment_action` (`created` or `updated`), the authenticated `actor`, normalized
comment evidence, and the returned URL; the compatibility `body` field remains
the URL. Compound close flows report `completed_steps` and the failing step
without printing multiple machine objects. Non-idempotent create results include
a stable request fingerprint and unique hidden operation ID; ambiguous create
failures require the documented read-after-failure reconciliation before retry.
Human warnings and progress remain on stderr, and the process exit code matches
`exit_code`.

`scripts/gh-with-env-token` is automation-first when a token is configured. It
loads `$CODE_HOME/local.env` by default, falling back to
`$CODEX_HOME/local.env` and then `~/.code/local.env`, then prefers
`CODEX_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN` in that order. Commands fail
closed without changing actor when automation auth is missing, rejected, or
rate-limited. Write-like commands also require the authenticated login to match
`shiny-code-bot`. Set `GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` only for
an explicitly approved one-off command whose human-owned actor is acceptable.
`GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1` is the stronger helper-owned mode:
it overrides the fallback setting even when an env file enables fallback.
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
