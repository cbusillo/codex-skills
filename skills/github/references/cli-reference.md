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
  the catalog root (`skills/`) as `github/scripts/<name>`. Do not run them with `python3` or
  `uv run`.
- `.sh` helpers are shell scripts. Run them directly or with `bash`.
- `.py` helpers that include PEP 723 inline metadata (`# /// script`) should be
  run with `uv run path/to/helper.py` when interpreter version or dependencies
  matter. Plain `python3` is only appropriate when the skill docs explicitly
  say the helper has no managed environment needs.
- Some hosts' shell tools leave stdin open and idle. `scripts/gh-issue edit`,
  `close`, and `reopen` treat stdin as optional input: a redirected file or
  heredoc is read, and a pipe that stays silent for about a second counts as
  no input. For a slow producer, pass `--body-file -` (edit) or
  `--comment-file -` (close, reopen) so the helper waits for end-of-file.
- Write helper arguments out literally. zsh does not split an unquoted variable
  into several arguments.

## Shared API Contract

Shared helper implementation lives in:

- `scripts/github_identity.py`: portable automation identity and local-environment
  resolution for Python helpers.
- `scripts/github_comment.py`: actor-aware REST timeline-comment creation,
  pagination, edit-last selection, and deletion-race handling.
- `scripts/github_issue.py`: actor-aware REST issue creation, edits, state changes,
  membership, milestones, and reconciliation.

These paths are relative to the `github` skill directory. Use the public helper
commands below for operations; consult these modules when changing helper code.

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

### Request Use

`uv run scripts/github_request_usage.py --hours 1` ranks the configured App's
local HTTP attempts by helper, operation, repository and quota bucket. Use
`--actor LOGIN` to select another observed actor. The private hourly receipts
live under the shared retry-state directory's `request-usage` folder and contain
no request/response bodies, endpoint queries or credentials. Delegating watchers
pass their caller name to the PR helper, so its reads count toward that watcher.

HTTP attempts and primary requests are separate: authenticated 304 responses
and `/rate_limit` probes use no primary requests. GraphQL point costs remain
unknown rather than being counted as REST requests. Raw CLI calls without HTTP
headers and Launchplane's server-side GitHub calls are outside this ledger;
the result is a lower bound, not an installation-wide audit. Offline tests must
set a temporary `GITHUB_RETRY_STATE_DIR`, which also isolates these receipts.

PR watchers back off unchanged pending snapshots to their quiet interval,
returning to the active interval when evidence changes. Shared core-budget
evidence at or below 20% remaining adds a five-minute polling floor until reset.
Budget evidence is scoped by host, App actor, repository owner (installation)
and quota bucket, so one installation cannot overwrite another's evidence.
The existing retry cooldown still coordinates by host, actor and bucket across
installations; a throttled installation can therefore delay another's reads.
This change preserves that retry contract and scopes only the new polling floor.
This slows PR/workflow/train polls without delaying writes or changing identity,
permission checks, write reconciliation or the existing bounded reset waits.
Receipts are retained for retrospective measurement; the pilot's closeout must
decide retention after preserving its acceptance evidence.
See [GitHub's conditional-request guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests).

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

Offline fake-CLI fixtures must set `GITHUB_RETRY_STATE_DIR` to their own temporary
directory. The catalog validation gate gives each helper test a separate directory;
live commands keep the default shared cooldown.

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

A merge response stating that required status checks are still expected is a
confirmed readiness rejection. The helper submits one merge request, reports
`required_status_checks_expected` with
`recommended_next_action: wait_for_required_checks`, and does not publish a
shared API cooldown. Run `gh-pr.py checks` for the current head and retry the
merge in a new invocation after GitHub receives the required checks. Other
unclassified responses keep the unknown-outcome and reconciliation behavior
described below.

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

### Capability Profile And Audit

Use `scripts/github-capabilities.py profile` for the derived full-operation App
profile, `profile --read-only` for its read-only projection, and `audit --repo
OWNER/REPO` or `audit --all-installed` for safe current capability evidence.
After an accepted App update, `audit --refresh-token --repo OWNER/REPO` renews the
same App token. See [permission coverage](github-permissions.md) for caller roles,
result meanings and the API surface drift check. No audit performs repository
writes or changes account grants.

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
  Use `--edit-comment ID` for a known comment; optional `--expected-body-file`
  and `--expected-updated-at` check its prior state as described below.
- `scripts/gh-pr.py checks <pr>`: Show check runs and commit statuses
  for the PR head.
- `scripts/gh-pr.py merge <pr> --method merge`: Merge a PR. The expected head
  SHA guards retries; an unknown response is reconciled by re-reading the PR,
  recovering only a trustworthy final merge SHA and failing closed on head
  drift or ambiguous state. Read the merge itself from `merge.sha`; the
  top-level `outcome_certainty` summarizes every step, including an optional
  `--delete-branch`. A branch GitHub had already removed is reported as
  `deleted: true` with `already_absent: true`, not as a failed delete.
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
the canonical PR as `[repo#N](https://github.com/OWNER/repo/pull/N) title`,
neutralizes `Closes`/`Fixes`/`Resolves` references in the
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

This operation is automation-only. It invokes the token wrapper with its
`--require-automation-auth` prefix, verifies the authenticated login against the
expected bot actor, and does not retry permission or ambiguous `404` results
under active user authentication. The wrapper consumes and freezes that prefix
before loading its env file, so local configuration cannot re-enable fallback
for this command.
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

The helper checks every host binding: `CODE_HOME`, `CODEX_HOME`, `~/.code`,
`~/.agents/skills` (whole-catalog link), `~/.agents/skills/shared` (installer
binding), and each entry under Claude Code's `skills` folder (`CLAUDE_CONFIG_DIR` or
`~/.claude`). `bindings_checked` in the receipt lists each with its outcome; `matched` means a
binding qualified, and the receipt's own `status` says what was reconciled. A
work-in-progress worktree linked for testing does not shadow an install on the
default branch, and another plugin's unreadable checkout is skipped. It acts only when that path belongs to the same Git
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
- `search <query>`: Search issues through the REST search endpoint with an
  `is:issue` constraint. Add the current repository only when the query has no
  positive `repo:`, `org:`, or `user:` scope; an explicit global `--repo` always
  adds its repository qualifier. GitHub ORs multiple `repo:` qualifiers, so
  this can widen a query that already names another repository.
  Each result's `repo` identifies its own
  repository, while the top-level `repo` records the caller's default context
  (null when a scoped search cannot resolve a GitHub origin).
  Result milestones retain the search payload's title. `--state open|closed` adds the matching
  search qualifier, `--state all` omits it, and quota evidence uses the search
  bucket. Compact states remain normalized as uppercase `OPEN` or `CLOSED`
  values.
- `show <issue>`: Show selected sections and all issue comments, paginated in
  GitHub's chronological order. Each comment includes its ID, author login,
  creation/update timestamps, URL, and full body. Use `--full` for the entire
  issue body and comments before implementation; `--sections` changes only the
  selected body sections. An empty discussion returns `comments: []`; an
  unreadable comment page fails the command instead of returning a partial
  thread as success.
- `deps <issue>`: Page and show validated native `blocked_by`, `blocking`, and
  sub-issue relationships, preserving cross-repository issue references.

### Planning: Claim

Before an authorized `go` creates a branch or worktree, run from a checkout of
the target repository:

```bash
uv run <skill-dir>/scripts/gh-plan.py claim <issue> \
  --worker <worker-token> --session <session-id> \
  --branch work/<task-slug> --next-action "<action>"
```

Agent routing and the session override are defined in
[Agent Assignment](../../github-plan/SKILL.md#agent-assignment). `next` and
`claim` accept `--agent claude|codex`; `claim --agent-override REASON` records an
explicit Director exception. Both creation helpers accept `--agent` as an
assignment.

Claim the exact branch that the worktree helper will create. For
`dev-worktree <repo> <task-slug> <start>`, that is `work/<task-slug>`.
Verify holds and recorded waits under Choose Work before invoking claim.
For a recorded wait or parked/blocked/stale/done state, pass
`--wait-resolved "<existing resolution evidence>"` only after verifying its
condition or a recorded Director release. Without that evidence the command refuses
before writing. It records the resolution and previous Current Status in the
claim comment and returns the previous status for recovery. Claim and this
argument grant no Director decision or permission to lift a repository hold.

The command checks Current Status and the complete discussion, unresolved
native blockers, registered worktrees, local branches, live remote heads, open
PRs, and Claude's native `claude agents --json` session inventory when available.
Codex CLI peer coverage is reported unavailable; a caller's supported session
tools can add evidence but cannot turn partial coverage into a clear inventory.
Known holders and ambiguous or stale records cause a nonzero refusal with the
competing evidence. Age never expires a claim. Do not bypass a refusal by
changing the worker, tool, or identity; ask the Director about ambiguous ownership
and continue independent work.

Unstructured Current Status ownership evidence remains fail-closed for `Owned by`,
`Claimed by`, and worker/session fields, including informal prose and Markdown.
Recognized responsibility sentences start a line with entries, records, or
resources, optionally prefixed by `Remaining`, a two-group count, or `provider-only`.
`After these proposals,` may precede that resource subject. They assign one actor
for evidence and another for disposition approval; these are not worker claims.
Other resource-prose shapes remain ambiguous and refuse conservatively.
Other ownership assertions in the same status, structured claims, and unreleased
claim comments still refuse independently.

An issue URL on a PR body line starting exactly `Code follow-ups recorded
without starting implementation:` is context-only. Title or branch ownership,
implementation references (`Refs`, `Fixes`, `Closes`, `Resolves`, including full
issue URLs), and links elsewhere in the body still cause refusal. Unmarked or
wrapped follow-up links remain uncertain ownership evidence. A context line
containing ownership keywords such as `Fixes`, `Closes`, or `Implements` tied
to the issue also remains evidence, including a colon or Markdown link.
Keep implemented work out of this explicitly unstarted line.

Use the actual native session ID, not a made-up label. On Claude Code, use
session metadata or `claude agents --json` to identify this session by its
directory, name, and process; if ambiguous, resolve that identity before claim.

Success posts and reads back a claim, updates owned Current Status (or leaves
the contributor's body intact and uses the claim comment), sets `plan:active`,
and reads the metadata back. Check `ok`, `outcome_certainty`, and
`completed_steps`; create the worktree only after confirmed success. This
narrows a race and supplies no exclusive lock. Recheck competing activity
during execution too.

After a partial failure, read the issue before retrying. The same worker,
session, and branch can resume its existing claim without another comment;
unknown comment writes remain governed by the shared retry policy. Never erase
a competing claim to recover. On completion or verified handoff, reconcile
Current Status and post `Released claim <claim-comment-id>` through the same bot
identity. Release affects that exact comment, not another worker's
record or retained branch/worktree evidence; those still need ordinary
ownership and preservation review.
The first release line is exactly `Released claim <id>`, or ends its exact ID
with a period followed by optional handoff prose. Conditional prose after a
bare ID does not release ownership.

An exact release may also be a standalone final paragraph after the handoff
prose, optionally followed by the helper's operation marker. It must be an
unquoted, unindented `Released claim <id>` line (an ending period is allowed);
fenced or raw HTML examples, inline mentions, and later prose do not count.
Conditional text on the release line, a preceding paragraph starting with
`If`, `After`, `Once`, `When`, `Unless`, or `Until`, or an introduction ending
with a colon also refuses. Use a separate first-line release when the embedded
format is ambiguous. The same author must post it after the source claim;
release does not resolve a recorded wait or authorize the next task's actions.
Use one exact-ID release per comment; a first-line release takes precedence
over a final release paragraph.

#### Abandoned automation claims

A direction or Supervisor session may release a finished native session's
claim through `release-claim`. Keep the same configured automation identity;
this route cannot release a human or another automation identity's claim.
Verify the original session's transcript and actual closure first. Post a
closed-session attestation through `gh-comment` on the canonical issue or
Supervisor handoff issue. Its first line is `Closed session <native-id>`,
followed by `Ended at: <timezone-qualified-ISO-timestamp>` recording actual
closure, `Safe to exit: yes`, and links to the transcript-verified handoff.
Do not substitute the attestation's posting time for the closure time.

```bash
uv run <skill-dir>/scripts/gh-plan.py release-claim <issue> \
  --claim-comment <original-claim-id> \
  --evidence-comment https://github.com/OWNER/REPO/issues/884#issuecomment-ID \
  --role supervisor --session <releasing-native-session-id> \
  --confirm-session-ended --dry-run
```

Inspect the returned release body, then repeat without `--dry-run`. The helper
reads back its recorded exact-ID release. `--related-claim-comment <id>` may
name an unstructured `Claimed by <same-worker>` ownership follow-up on that
issue; it cannot release another structured claim. Partial writes preserve the
posted records and completed steps: read them before any recovery, never switch
identity or replay an unknown write.

For retained PRs, pass each verified same-repository PR URL with repeatable
`--retained-pr`. Each PR must independently link the canonical issue and belong
to the source automation identity. The release comment is the new handoff for
`claim --resume-from <original-id> --refresh-pr <url> --handoff-comment <release-id>`.
Unmentioned siblings, live retained-worktree peers and other ownership still
refuse. Commit activity newer than the cited closure refuses for listed PRs and every
open PR on the source branch. Commit timestamps do not prove push time; verify
the newer session's actual handoff rather than moving the closure timestamp.

Later authorized successor or merge-train pushes do not undo a historical
release. At claim time, recheck live peers, source ownership, issue/PR waits and
current heads under the ordinary retained-work procedure; unavailable remote
peer inventory is not proof of inactivity. The closure attestation remains the
caller's responsibility, and release-time commit timestamps alone cannot
attribute later pushes to a native session.

Renewed source claims or source edits after closure refuse the release and
invalidate its receipt during later claims. An invalid, edited or unavailable
attestation makes that release stop counting and restores the original claim
conflict; it never permanently blocks the issue. Verify actual closure, post a
corrected attestation and rerun `release-claim` to recover without deleting
history. Available native sessions are
checked and their coverage is returned. `--confirm-session-ended` records the
caller's transcript/closure verification, including peers or hosts unavailable
to the helper; partial inventory alone is never closure evidence. The command
leaves issue status, waits, blockers, branches and worktrees intact. Use
`--resume-from` for an identical stale structured Current Status marker; other
status ownership still needs its supported reconciliation. Closing a landed
prerequisite and proving its finish line is separate from release: `next` and
`claim` accept a closed native blocker without deleting dependency history.

Use a unique worker token per native session. Legacy `Released by <worker>`
comments are accepted only when the earlier structured claims for that token
all belong to one session; reuse requires exact comment-ID releases.

After a verified retained-work handoff, use `--resume-from <claim-comment-id>`.
The source must be one structured claim explicitly released by its author.
An identical structured Current Status marker and its helper-generated active
worker line and worker/session fields are accepted when the source author's
exact-ID release is newer than both
the marker's claimed-at time and the source comment's last edit. Missing or
invalid timestamps remain uncertain ownership. The successor claim replaces
automation-managed stale status on normal readback; contributor-owned status
stays intact, with the successor's claim in a comment. Unstructured ownership
outside the helper-generated fields still refuses. The original status remains quoted in
the successor's claim comment. Only the exact retained branch, worktree, and
PR evidence is accepted; other Current Status ownership, unreleased comments,
other artifacts, and visible peer sessions still refuse. Records on another issue must use that
canonical planning issue's supported handoff route.
This flag supplies no cleanup or takeover authority: apply Choose Work's
verified-handoff and preservation rules before passing it.

For an explicitly authorized **conflict-only refresh** of a finished session's
PR, claim its **canonical open planning issue**, even when the brief names only
the PR. Run from a checkout of the PR's repository:

```bash
uv run <skill-dir>/scripts/gh-plan.py claim <canonical-issue-url> \
  --worker <worker-token> --session <native-session-id> \
  --branch work/<new-task-slug> --next-action "Conflict-only refresh of <PR>" \
  --resume-from <released-claim-comment-id> \
  --refresh-pr https://github.com/OWNER/REPO/pull/123 \
  --handoff-comment <handoff-comment-id> \
  --wait-resolved "<verified evidence resolving the recorded wait for this refresh>"
```

For a planning issue in another repository, both repositories receive their
own ownership inventory and planning label configuration. Supply
`--planning-checkout /path/to/planning-repo`
when its verified checkout is not discoverable through repository configuration.
The remote identity is checked; inventory covers that checkout's registered
worktrees and remote heads, not every clone or Codex peer. No worktree is
adopted or mutated there.

The source claim and handoff must be on that planning issue. The source author
must have posted an exact-ID release as described above before or in the handoff, and
the handoff must name the target PR. A same-repository `#123`, qualified
`OWNER/REPO#123`, or full PR URL identifies it; cross-repository handoffs require
a qualified reference. The open PR must independently link the planning issue
and use a head and base in the PR's repository; fork refreshes are not supported.
The issue and target PR's recorded waits still require verified resolution.

The handoff must start with the exact-ID release line described above, or with
`Handoff from <source-worker>` and include the exact claim ID and native source
session ID. A generic bot rollup or refresh claim is not a handoff. Authorship
checks are at GitHub identity level; verify the actual finished-session handoff
before invoking the route because several sessions can share that identity.
Every open PR on a retained branch must be named in this handoff and authored
by the source author, including a new PR on the original source branch.
An embedded release alone does not establish this handoff identity. For an old
handoff with a different opening, have the source session record a new
`Handoff from <source-worker>` comment after release, naming the source claim,
native source session, and every retained PR, and linking its original handoff
and release comments. Use supported session routing to reach that session;
another session sharing its bot login must not impersonate it. When the source
session cannot record this, retained-work recovery remains blocked. Preserve
the original records and use the new comment ID with `--handoff-comment`.
A release posted after the old handoff does not validate it retroactively.
Unmentioned same-bot PRs also refuse.
The named PR identities bind their current branches to that finished session's
handoff, including split branches that differ from the original claim branch.
Only open or merged same-repository PRs independently linked to the canonical
issue and named in that handoff count as retained artifacts; a closed unmerged
PR does not. A superseded closed PR with no remaining artifacts is ignored,
while its unreleased claims and unaccounted branches/worktrees still refuse.
The refresh target itself must still be open. Cross-repository
links must qualify the canonical issue, since bare `#123` belongs to the PR
repository. Merged siblings may retain their branches/worktrees without
requiring cleanup to refresh the remaining PR. The helper reads the
issue and those PR discussions, local/remote branches, registered worktrees,
and available peer sessions again during readback. Unreleased claims, active
Current Status, live peers on retained worktrees, unaccounted artifacts, and
races still refuse. Sibling references identify retained artifacts and do not
authorize refreshing those siblings. A refresh claim records its target PR so recovering it
cannot silently become a general implementation claim.

This route grants no refresh, push, merge, cleanup, or takeover authority.
Verify the finished-session handoff and existing conflict-refresh authorization
first. On confirmed claim success, create your own new linked task worktree;
leave the original worktree and its lease intact. Reusing any retained
PR/source branch as the new task branch refuses. From that new task branch,
push explicitly to the verified existing PR head
with a normal fast-forward refspec (`task-branch:pr-head-branch`) under the
brief's authority; never check out the PR branch in two worktrees or force-push.
Recheck its live head before integrating and pushing so a concurrent update
is preserved. Then release the new issue claim
with its exact comment ID. Direct `claim <PR>` remains unsupported; the
canonical issue is the durable ownership and status record.

A refused write/readback race includes `claim_recovery.release_own_claim` when
this invocation posted a claim. Post its exact `Released claim <comment-id>`
body through the same bot to release only that comment, then preserve the
competing worker's state and recheck before any retry. If an owned Current
Status was already updated, reconcile only that record too; never rewrite a
competitor's record.

### Planning: Management

- `create <title>`: Create a new plan issue. Exact-title dedupe uses REST issue
  search, labels are ensured through REST, and the shared issue helper creates
  the issue with reconciliation evidence for unknown write outcomes. Supports
  `--title` (flag), `--body`, `--plan-status`, and `--finish-line` (issue body
  only). Configured Projects still enroll new issues. Manual fields are not
  synchronized by default; `--focus` and `--manager` are explicit edits, and
  configured Manager defaults are ignored.
- `update-section <issue> <section>`: Patch a single markdown section.
- `link|unlink <issue> <rel> <target>`: Manage native `blocked-by`, `blocks`,
  or `subissue` relationships. `related` edits a body note instead and follows
  body-ownership rules.
- `close <issue>`: Close a durable plan with a fail-closed relationship
  preflight and an issue-state commit point. Before any mutation, the helper
  pages native `blocked_by` dependencies and sub-issues; `--reason completed`
  rejects open entries with compact references and `write_outcome=not_started`.
  Issues blocked by the plan do not prevent closure. `--reason not_planned`
  requires the Director's approval, given as the repository owner, for work in a
  milestone listed in merged `DIRECTION.md`: a repository owner comment after
  the last issue-body edit, or a repository owner `+1` reaction after
  that edit on an unedited comment starting with the exact first line
  `Owner decision: Close #<number> as not planned.` Other accounts, comment
  types, actions, and edited decisions do not qualify; unreadable identity,
  reaction time, or edit history fails closed. Then it
  retains and reports remaining relationships, closes with the distinct
  `not_planned` state reason, and does not present superseded work as completed.
  Optional Project Status=Done synchronization remains before issue closure so
  the Project item can still be found; manual fields remain untouched. If closure
  then fails, the result reports
  that split Project state explicitly. Confirmed or read-reconciled issue
  closure is the commit point for `plan:done`, `plan:active` removal, and an
  optional close comment. Exact same-body comments by the acting identity are
  reused on retry, and failures return deterministic `recovery` state so
  rerunning the same command reconciles missing metadata without duplicating
  evidence.
- `ensure-labels`: Page through repository labels and create documented missing
  planning labels through REST. Concurrent-create conflicts are reconciled by
  reading the requested label instead of blindly retrying the write.

When GraphQL quota is exhausted but REST is available, keep independently
authorized REST-backed body/status work moving. Record unavailable Project or
relationship updates as waiting; respect the helper's retry limits instead of
retrying until planning stalls.

### Planning: Next Work

- `next [--milestone <number-or-title>] [--limit <n>] [--scan-limit <n>]`:
  Rank actionable open plans without mutating labels, relationships, milestones,
  Projects, or workflows. Native `blocked-by` relationships are authoritative.
- The command excludes done, stale, waiting, inconsistently
  `plan:blocked`, dependency-unknown, and parent plans with open sub-issues. Each
  exclusion includes a normalized reason and dependency evidence when present.
- Configured Project Focus and milestone state/due date are advisory ranking and
  context signals only. A closed milestone does not hide an otherwise open,
  unblocked plan. Project read failures degrade to explicit notes rather than
  making dependency state look safe. Project item truncation is also explicit.
- `--scan-limit` independently bounds both plan and inbound-gate scans, and
  each relationship collection has its own fixed safety limit. Truncated or
  otherwise unavailable plan relationship reads fail closed as `unknown_dependencies`
  and are summarized in top-level dependency context. `--limit` bounds the
  ranked candidate list while preserving evaluated exclusion evidence.

`blocking_work_elsewhere` separately names open native cross-repository blocker
pairs, including waiting or unmilestoned issues and non-plan blockers. These
entries provide visibility; review recorded waits and ownership before starting
work. They do not override candidate exclusions.
`blocking_work_elsewhere_context` reports the independent inventory and scan
budget, truncation indicators, errors, and unread gates when the scan runs. If
inventory cannot be read, it reports only `complete: false` and `error`.
The report scans repository-wide even with `--milestone`, subject to its
reported truncation. Each open issue with a nonzero or unknown blocking count
consumes an inbound scan slot, including issues blocking only same-repository
work. Permission-denied, not-found, and unclassified response errors degrade
inbound coverage; other classified API failures, including quota,
authentication, provider/network, and timeout failures, stop the command under
the existing plan relationship policy.

For `<owner>/direction`, `next` automatically selects global direction scope;
no flag is needed. The target repository's merged `DIRECTION.md` is required,
including when the command runs elsewhere with `--repo`. In milestone order,
it walks the open `Track:` plans through native blockers and sub-issues, across
repository owners as well as repositories. Waiting summary labels on these tracking
containers do not hide their linked work. Ordinary waiting, stale, completed,
and inconsistently blocked plans remain excluded. Tracking issues
without open work are reported, never selected as implementation tasks.

Global candidates have `repo`, `number`, overall `milestone`, `issue_milestone`,
`reasons`, and a `via` path. Shared prerequisites appear once under the earliest
milestone that reaches them. A blocker can itself have blockers; the command
continues to actionable leaves instead of selecting an intermediate blocked
issue. Product-repository candidate ranking remains independent of this global walk.

`waiting` contains explicit Current Status reports, with `waiting_for`,
`reported_by`, and `reported_at`. A wait naming another issue or PR identifies
that subject without turning prose into a native dependency or claiming a live
review decision. Missing people/conditions remain unknown. Whole-plan waiting
states stop that branch, while a partial wait in an active parent does not hide
its independent work. Read the selected issue's full discussion before acting.

The global `--scan-limit` bounds unique graph nodes, not just roots, with a
separate allowance of the same size for other-family evidence when the running
family is known (at most twice the limit in total). Cycles,
missing tracking issues, inaccessible nodes, and truncated reads are explicit;
`dependency_context.complete=false` means the answer is partial. Missing or
unreadable direction never silently falls back to product-repository ranking.
An explicitly empty Milestones section is valid: discovery can still find own
projects when there is no business milestone to traverse. Nonempty text with no
parseable milestone titles is an error, not an empty graph.
Closed milestones still listed in direction appear in `completed_milestones`,
instead of making the graph incomplete while their direction edit is pending.
The returned `direction_context` preserves Order and Capacity for caller
judgment: unlinked live incidents and repeat-stop tooling exceptions need their
own evidence, and the weekly own-project share remains an audit, not a per-call
quota. With no `--milestone`, the query also inventories the repository owner's
accessible repositories and their open issues, including non-plan issues, and returns their
merged direction. Other repository owners, archived/disabled repos, disabled issue trackers,
and empty repos without open issues have explicit exclusions. Forks remain
eligible for discovery. An App uses `/installation/repositories`; a configured
non-App actor uses `/user/repos`, without switching identities on failure.

Service adapters using the shared direction module must inventory marked issues
beyond ordinary repository list bounds, pass their inventory through
`discovery_scan` before evaluating nodes, and report graph/discovery coverage
beside the ranked list; `rank_portfolio_work` alone cannot find omitted issues.

`graph_context` covers native traversal; `discovery_context` covers repository
inventory and issue reads; neither proves active ownership. `--repo-limit`
(default 100), `--repository-issue-limit` (100), and `--comment-limit` (100) bound
the new sources. `--scan-limit` separately bounds graph nodes and discovered issue
evaluations of ordinary issues. Other-family discoveries use a separate allowance
of the same size; they retain dependency, parent-wait and capacity evidence before
the final family filter. Unknown and conflicting assignments use the ordinary
allowance. See [global selection](../../github-plan/references/global-next.md)
for the graph stopping rule and the existing held-repository allowance. Issues labeled `live-breakage` after a Director
incident decision are evaluated outside that discovery allowance and ranked
first among possible work, while normal holds, blockers, waits, and ownership
review still apply. When an ordinary repository issue list is truncated, a
separate label inventory reads up to the plan inventory limit and reports its
coverage. `candidate_coverage` accompanies the ranked list with a warning when
graph or discovery coverage is incomplete; its counts are not an availability
claim. Its `scope` distinguishes a portfolio scan from an explicit milestone
scan; complete milestone coverage says nothing about incidents outside it.
`candidate_coverage.unevaluated_repositories` lists named repositories and counts
of inventoried issues omitted by the evaluation allowance, after graph overlap
and marked-incident reads. Source truncation/access failures remain explicit in
`discovery_context.repositories`, so these counts do not claim full inventory.
Every candidate has `overall_milestone_context` with `matched`, `none_found` or
`unknown`, titles and its evidence source. Matches require a native Track
path/ancestry/blocking target or an open issue milestone title listed in both repository and
overall direction. Title matches explicitly carry `basis: exact_listed_title_match`,
not a native Track-link claim. Unread or unparsed direction stays unknown.
Context does not alter eligibility, availability or ranking.
Relationship endpoints are skipped only when the already-read native issue
summary explicitly reports an integer-zero total for that relationship; missing,
malformed or nonzero totals keep the bounded reads, including closed history.
Zero totals reflect the issue inventory's read-time snapshot; a relationship
added afterward appears on a fresh read, not retroactively in this result.
Mark only current incidents: every marked issue gets normal relationship,
discussion and ancestry reads outside the ordinary scan allowance, so API cost
increases with the marked inventory. Explicit milestone scope remains narrow.
Discovery visits repositories round-robin after local milestone
ordering, so one large backlog does not consume the entire evaluation budget.
`--limit` caps displayed candidates, not coverage. Unevaluated issues, truncated
comments/inventories, and inaccessible sources remain explicit. `--milestone`
retains its narrow graph scope and reports portfolio discovery as excluded.
For a discovered leaf, up to ten native parent links are read using GitHub's
[parent issue endpoint](https://docs.github.com/en/rest/issues/sub-issues#get-parent-issue).
Parent discussions accompany the child, whole-plan waits remain excluded, and
failed reads, cyclic, or deeper ancestry stays unknown. Parent discussion changes
also invalidate the review digest. These reads are cached within the invocation.
As with repository inventory, coverage is limited to the actor's visible data:
a parent-endpoint 404 establishes no visible parent, not proof that an
inaccessible parent cannot exist. Global-only options are rejected by local next.

Candidates carry full bounded `discussion` snapshots and a digest. Their
`availability=needs_review` is not a recommendation to start. Current caller
judgments may be supplied with `--selection-context FILE`; read the
[global selection procedure](../../github-plan/references/global-next.md) for
the small evidence format. Known repository holds exclude all work there.
Reviewed available work appears in `available_candidates`, occupied work in
`underway`, and comment-only waits in `waiting`. Stale/incomplete discussion,
partial ownership evidence, and unproven tooling eligibility cannot produce an
available candidate. Selection context is temporary evidence, not a plan database.
No `next` mode mutates planning state or authorizes execution.

`scripts/github_direction_next.py:rank_direction_work` is the reusable ranking
entry point for service consumers such as Launchplane. It accepts tracking
roots, ordered milestone titles, completed milestone titles, a node reader,
and the scan bound. The same module's `evaluate_direction_node` classifies raw
issue, relationship and label evidence, including Current Status waits;
`rank_next_candidates` supplies the common ordering for local and global calls.
Bounded readers must pass `truncated_relationships` or `relationship_error`
to the classifier when their evidence is incomplete; it then returns an unknown
node rather than treating the visible prefix as a complete dependency list.
Neither classification nor ranking makes network calls or mutations. Adapters
only collect evidence with their authenticated, bounded readers, mapping
inaccessible nodes to unknown and preserving provider/auth/quota stop behavior.
`discussion_snapshot` normalizes bounded comments for both adapters, and
`include_parent_context` preserves ancestor waits and discussion freshness;
`rank_portfolio_work` combines graph candidates, discoveries, and caller selection
evidence with the same holds, review states, and priority. Services must supply
repository milestone order and bounded parent evidence through those shared
functions, and report their discovery coverage separately. Capacity callers also
read bounded ancestry for excluded discoveries, preserving their original wait
reports while marking capacity ancestry/discussion evidence incomplete. Held
repositories use a separate bounded read allowance and remain unselectable;
`discovery_context.capacity_complete` reports discovery evidence completeness
separately from admission. For capacity admission, pass `coverage_complete=True`
to `rank_portfolio_work` only for complete milestone graph coverage with no known
milestone issues left uninspected; its default is false. Portfolio discovery
bounds or unavailable sources remain explicit coverage warnings, not a blanket
admission veto. Current milestone person-wait reviews and
`tooling_admission_rule` outputs follow the linked global selection procedure.
Supply parsed
`repository_waypoints` separately from ranking order for waypoint explanations
and capacity eligibility:
an absent direction file or empty section maps to `[]`, an unread/unparsed
source to `None`. Omitted waypoint evidence stays unknown; the ranking map
alone cannot prove parsing. A repository hold
filters work in that repository without cutting native paths to independent
work in another. Calling only `rank_direction_work` proves
graph coverage, not portfolio discovery.

### Planning: Milestones

- `milestone-list --state open|closed|all [--limit <n>]`: List milestones with
  bounded REST pagination. Output normalizes `number`, `title`, `state`,
  `description`, issue counts, URLs, timestamps, and `due_on` as UTC RFC3339
  seconds or `null`.
- `milestone-show <number-or-exact-title>`: Resolve a numeric milestone or an
  exact title, then read its normalized REST representation.
- `milestone-create <title>`: Create a milestone with `--description` or
  `--description-file`, optional `--due-on`, and `--state open|closed`. An
  existing exact title is returned as a no-op only when its requested fields
  match; conflicting duplicates or fields fail closed. Unknown writes use a
  pre-write snapshot and exact-title reconciliation before any retry.
- `milestone-update <number-or-exact-title>`: Update `--title`, description,
  `--due-on`, or `--clear-due-on`; `--state open` is the only state mutation
  permitted. A matching current representation is returned as `no_op`, and
  `--state closed` is rejected in favor of `milestone-close`.
- `milestone-close <number-or-exact-title>`: Guarded-close a milestone only
  after paging open milestone assignments. Any open issue or pull request
  returns a conflict with compact blockers and no write is attempted. Closing
  an already closed milestone is a no-op.

All milestone writes use the configured automation actor, emit `actor` and
`expected_actor`, and preserve fail-closed write-outcome and reconciliation
fields from the shared API layer. Use these commands instead of raw
`gh api .../milestones` calls.

### Planning: Projects

- `project-list --owner <owner>`: List Projects.
- `project-add <issue> --project <name>`: Add issue to a Project and return the
  Project item id when GitHub provides one.
- `project-set <issue>`: Apply explicitly requested Project fields (`--focus`, `--manager`,
  `--finish-line`). Pass `--item-id <id>` when using the id returned by
  `project-add` so the helper can skip lookup-sensitive rediscovery. Without any
  field values, it performs no Project reads or writes.

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
Title/body replacement is allowed by default only when the authenticated actor
authored the issue. Use `--allow-cross-author-source-edit REASON` only after the
user explicitly authorizes replacing another author's source content; planning
updates should use `gh-plan.py update-section`, and ordinary clarification
should use a separate comment.
Edit, close, and reopen accept issue numbers, `#NUMBER`, `OWNER/REPO#NUMBER`, or
full issue URLs; a repository encoded in the target takes precedence over
`--repo`.
Flags that require templates, Projects, issue types, parent/sub-issue or
dependency relationships, editor/recovery state, or browser interaction remain
outside this focused REST surface. Route those deliberate exceptions through
`scripts/gh-with-env-token issue create|edit` with a body file so automation
identity and literal Markdown remain explicit.

`scripts/gh-issue close` and `scripts/gh-issue reopen` take a comment from
`--comment-file PATH` (`-` for stdin), from optional stdin as described in
[Helper Invocation](#helper-invocation), or from `--comment`; stdin input wins
over `--comment`, and `--comment-file` cannot be combined with it. `edit`
takes a new body the same way through `--body-file`. They post any
state-change comment through the shared JSON-stdin REST comment implementation
before sending an explicit REST state/state-reason PATCH. The final envelope
reports `post_close_comment` in `completed_steps` when the comment succeeded but
the close failed, and comment failure stops before close. Successful state
changes append `close_issue` or `reopen_issue` to `completed_steps`.
`--duplicate-of` accepts the same issue-reference forms, resolves the target's
REST database id, and sends `state_reason=duplicate` plus `duplicate_issue_id`
in the close PATCH. It is mutually exclusive with `--reason`. For completed durable
plan issues, use
`scripts/gh-plan.py close --comment-file` so plan labels and Project Status stay
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
When sessions share an automation actor, save the returned `comment.id` and
use `--edit-comment ID` rather than actor-wide `--edit-last`. Both comment
entry points support it. The helper reads that exact comment, validates its
repository/thread and authenticated author, and PATCHes only that ID. A missing,
deleted, foreign-thread or foreign-author target fails without selecting another
comment or creating one. Results include `selected_comment_id` and `comment.id`.

```bash
scripts/gh-comment issue 42 --repo OWNER/REPO --edit-comment 123456 \
  --body-file replacement.md --expected-body-file prior.md
```

`--expected-body-file` compares the full prior body exactly, including whitespace
and hidden operation markers. `--expected-updated-at` compares GitHub's exact
prior `updated_at` value; either or both may be supplied. A mismatch returns
`comment_conflict` with `write_outcome: not_started`, without printing bodies.
These are preflight comparisons, not an atomic compare-and-swap: a concurrent
edit after the read can still win or be overwritten, and timestamp precision
alone cannot detect every same-second edit. Exact edits never automatically
replay PATCH; read back the same ID before another invocation after a failure.
The expected-value flags require `--edit-comment`, which cannot combine with
`--edit-last`, `--create-if-none`, or body deduplication. Give expected and new
bodies separate files when both are required; a stdin stream can be consumed
only once.

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

## Authentication And Identity

`scripts/gh-with-env-token` is automation-first when a token is configured. It
loads the first of `$CODE_HOME/local.env`, `$CODEX_HOME/local.env`, and
`~/.code/local.env` that exists, so a home variable without its own
`local.env` falls through to the next. When
`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and
`GITHUB_APP_PRIVATE_KEY_PATH` are all configured, it verifies the App identity,
mints and caches an owner-only installation token, and uses the App for every
command. Each account that installs the App has its own installation, so when a
command names a repository (`-R`/`--repo`, an `api` path under `repos/`, or
`GH_REPO`), the wrapper uses the installation on that repository. A write to a
repository where the App is not installed is refused with that reason when the
account that registered the App (`owner` in `GET /app`) owns the repository; ask the
Director to install the App there. In any other account's repository, even one
whose account has the App installed on other repositories, the write runs as
the active `gh` login only when the caller sets `GH_WITH_ENV_TOKEN_OWN_USER=1`
for that command, after its Director approved writing there. Without it the
write is refused with a message naming the opt-in, and a value in `local.env`
does not count. With it, stderr carries
`notice: acting as your own GitHub user on OWNER/REPO`; Python
helpers report that login as the actor. Comments resolve the repository's write
identity before edit-last selection, exact-comment author checks, or body
deduplication, and retain it for request fingerprints and uncertain-write
readback. An edit reports the authenticated writer as `actor` and preserves
the original author separately in `comment.author`.
The read-only `--write-actor-for OWNER/REPO` wrapper prefix supports only a
GET `/user` actor probe; it applies the same installation, own-user opt-in,
and required-automation checks as a write without sending a mutation.
Malformed repository or probe arguments return a structured `validation_error`
with exit code 2 and `write_outcome: not_started` before authentication or
mutation. The API retry layer preserves this failure and does not retry it.
`git-push-as-bot` pushes there with
the active login's token, `git-commit-as-bot` keeps the person's own git
identity, and `gh-pr.py create` requires `--body-file` and appends
`I wrote this change with AI assistance and reviewed it.` unless the body
already mentions AI assistance (its result carries `"identity": "own_user"`).
All three take the same opt-in. `github_identity.py app-auth
--require-installation` exits 3 for that case and 4 for the refusal; a
repository that moved without an installation is refused, since its current
account is unknown under the old name. Only an operand URL names the target
repository; a URL inside an option value such as `--body` does not.
Reads there, and commands that name no
repository such as GraphQL by node ID, use the configured installation.
`GITHUB_APP_INSTALLATION_ID` stays the default. Otherwise it prefers `CODEX_GITHUB_TOKEN`, `GH_TOKEN`, and
`GITHUB_TOKEN` in that order. `--check` reports the selected credential source
and verifies the current App installation and actor without performing a write.
For a non-`github.com` `GH_HOST`, configure the matching
`GITHUB_APP_API_URL`; the wrapper fails closed instead of sending the App JWT to
the public GitHub API. Commands fail closed without
changing actor when automation auth is missing, rejected, or rate-limited.
Write-like commands also require the authenticated login to match the configured
automation account. Set `GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1` only for
an explicitly approved one-off command whose human-owned actor is acceptable.
`GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH=1` is the stronger helper-owned mode:
it overrides the fallback setting even when an env file enables fallback, and
refuses the own-user path in other accounts' repositories even with
`GH_WITH_ENV_TOKEN_OWN_USER=1`.
Set `CODEX_AUTOMATION_LOGIN` and `CODEX_AUTOMATION_EMAIL` in the ignored
`local.env`; use a quoted `CODEX_AUTOMATION_BOT_LOGINS` value for optional
additional Director-controlled automation accounts. Those logins are used for bot
classification and trusted managed-plan authorship, so do not list third-party
bots. Values from the selected local env file override ambient values for the
same identity key.
Automation-only Python readers use the equivalent wrapper prefix
`--require-automation-auth`, avoiding an explicit process-environment copy while
preserving the same fail-closed behavior.
Set `CODEX_SKILLS_ENV_FILE` only in tests or special local cases where a
different env file should be used. It always wins, and a missing file then
loads nothing, which keeps tests isolated from the real credentials.

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
the configured automation account. The push helper picks credentials the same
way as the wrapper: a configured GitHub App first, then `CODEX_GITHUB_TOKEN`,
`GH_TOKEN`, and `GITHUB_TOKEN`.

`git-push-as-bot [options] origin <refspec>` remains the default. When
`origin` points at upstream and the Director's repository is a named remote, use
`git-push-as-bot --remote public -u work/task` or
`git-push-as-bot --remote fork --delete work/task`. Put `--remote NAME` first;
the remaining arguments are push options and refspecs, without another remote.
The helper resolves that configured remote to a GitHub owner/repository, selects
and verifies its credentials, temporarily uses its HTTPS URL, checks all its
push URLs, and restores its fetch URL on exit. It refuses non-GitHub targets,
URL operands, `--repo` overrides, multiple push destinations, and credential
rewrites to another transport. Use this route instead of environment URL
rewrites or renaming remotes to get around a refusal. Existing App-installation,
automation-identity and explicit own-user authorization rules still apply.

Planning helpers preserve the selected actor when authentication or quota
failures occur. Set `GH_PLAN_SKIP_BOT=1` for explicitly authorized temporary
active-account planning work, or reserve `GH_PLAN_ALLOW_ACTIVE_FIRST=1` for
explicit local debugging of Project operations that already request active
auth. Neither setting is an automatic rate-limit retry.

Avoid passing escaped `\n` through shell-quoted `--body`. Also avoid unquoted
heredocs like `<<EOF` for Markdown bodies: shell command substitution runs
inside backticks before the body reaches GitHub. Use `<<'EOF'` for literal
Markdown when a heredoc is necessary.

### Comments-only merge rejection

An HTTP405 containing only `All comments must be resolved.`, optionally under
GitHub's `Repository rule violations found` heading, returns
`unresolved_review_threads`, `write_outcome: rejected`, and
`recommended_next_action: resolve_review_threads`. It makes one merge PUT and
writes no shared retry cooldown. Resolve the PR review threads before another
merge attempt. Mixed-rule and other ambiguous HTTP405 responses retain bounded
retry and exact-head reconciliation.
