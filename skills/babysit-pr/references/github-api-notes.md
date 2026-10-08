# GitHub CLI / API Notes For `babysit-pr`

The watcher routes GitHub CLI calls through
`github/scripts/gh-with-env-token` by default. Reads and writes preserve the
configured automation actor when auth or quota failures occur. Active local
`gh` is used only when active-auth fallback is explicitly allowed for a one-off;
write-like calls such as Actions reruns remain fail-closed by default.

## Conditional polling and cache boundaries

Watcher REST reads use the shared reader's private, validator-backed cache. It
stores a body only with an ETag or Last-Modified validator, keyed separately by
authenticated actor, endpoint, query/page, and representation. A `304` reuses
only its matching stored body; a missing/corrupt body triggers one unconditional
recovery request and never becomes a clean or merge-ready result. `401`/`403`
responses never fall back to cache. Cache files are private, bounded to the
watcher's state location, and short coalescing prevents duplicate concurrent
observers from immediately repeating a just-completed read.

The watcher uses the shared GitHub retry/cooldown transport for comment,
workflow, and check reads. Respect Retry-After/reset advice and the emitted
`next_poll_seconds`; [Request Use](../../github/references/cli-reference.md#request-use)
owns adaptive cadence and budget behavior. This is still polling;
Launchplane owns the event-driven
follow-up under launchplane#2374.

An uncaught shared transport failure emits a terminal `read_error` JSON event
with the API result and exits with status 1. This includes retry-budget/deadline
exhaustion, authentication refusals, and structured failure envelopes from the
initial PR metadata helper. Invalid response shapes and local helper-launch or
JSON parsing failures retain their existing exception handling.
Inspect its failure cause and retry diagnostics before resuming; the watch loop
adds no retries or identity fallback. Optional check and review-readiness reads
may instead expose incomplete evidence in a snapshot; that evidence cannot
permit reruns or establish merge readiness.

## Primary commands used

### PR metadata

- `github/scripts/gh-pr.py --repo OWNER/REPO view <pr>`

Used to resolve PR number, URL, branch, head SHA, and closed/merged state through
the shared REST-first helper. The watcher preserves the helper's compact
transport, quota, retry, and actor diagnostics. REST does not provide the
GraphQL `reviewDecision` field. When every other readiness input is green, the
watcher performs one bounded same-actor GraphQL readiness query pinned to the
REST repository, PR number, base branch, and head SHA. A successful nullable
`reviewDecision` remains null and is only treated as `not_applicable` when the
same document reports `mergeStateStatus: CLEAN`; it is never rewritten as
`APPROVED`. Missing fields, partial GraphQL errors, quota/cooldown responses,
actor failures, and identity mismatches remain `unknown`, emit
`review_readiness_unavailable`, and preserve REST monitoring. Successful
decisions are not cached indefinitely because review rules can change without
a head SHA change.

### PR checks summary

- `github/scripts/gh-pr.py --repo OWNER/REPO checks <pr>`

Used to read check runs and commit statuses through the shared REST-first
helper. A partial response can prove a failure, but incomplete counts, lower
bounds, unavailable components, or a head SHA mismatch cannot prove the
current CI round terminal.
The watcher emits `check_evidence_incomplete` for that state and keeps
monitoring without treating the incomplete round as rerunnable.
The watcher passes the PR view's exact head SHA into the check reader so a
snapshot does not repeat PR metadata merely to rediscover that SHA.

### Workflow runs for head SHA

- `github/scripts/gh-with-env-token api repos/{owner}/{repo}/actions/runs --method GET -f head_sha=<sha> -f per_page=100`

Used to discover failed workflow runs and rerunnable run IDs.
The shared check reader selects the current execution by head SHA, workflow ID,
event, source branch and source repository. Run number orders executions and
run attempt orders retries; workflow/job names and update times cannot prove
supersession. Actions checks must match the run, head and check suite. A queued
or running replacement remains unfinished even before it has checks.

Superseded executions remain in `superseded_workflow_runs` and
`superseded_check_runs`, separate from counts, diagnosis and retry candidates.
Missing execution/check identity or unavailable selection reads preserve the
checks and make evidence incomplete. External Apps' checks remain independent.
For an execution with multiple attempts, GitHub's latest job inventory selects
current checks, including successful jobs reused by a partial rerun. Job detail
is otherwise fetched only for completed current failed runs needing diagnosis.

The watcher shares its head-pinned run inventory with the check reader. Saved
rerun intents reconcile against the full inventory, including superseded runs:
supersession alone never releases an unknown write. Before any retry write,
an uncached run read rechecks current execution and attempt; a replacement that
appeared since the snapshot skips the obsolete retry without spending budget.

### Failed log inspection

- `github/scripts/gh-with-env-token run view <run-id> --json jobs,name,workflowName,conclusion,status,url,headSha`
- `github/scripts/gh-with-env-token api repos/{owner}/{repo}/actions/runs/{run_id}/jobs --method GET -f per_page=100`
- `github/scripts/gh-with-env-token api repos/{owner}/{repo}/actions/jobs/{job_id}/logs > /tmp/pr-watch-gh-job-{job_id}-logs.zip`
- `github/scripts/gh-with-env-token run view <run-id> --log-failed`

Used by Codex to classify branch-related vs flaky/unrelated failures. Prefer the direct job log endpoint as soon as a job has failed because `gh run view --log-failed` may not produce failed-job logs until the overall workflow run completes.

### Retry failed jobs only

- `github/scripts/gh-with-env-token run rerun <run-id> --failed`

Reruns only failed jobs (and dependencies) for a workflow run. This is a
GitHub write and must be owned by the configured automation account.
A retry needs a completed failed or timed-out job; a cancelled notification
run with no such job is skipped. An explicit API rejection saying the run cannot
be retried (including gh’s rewritten HTTP 403 message) is also skipped without discarding earlier successful reruns.
Each cycle consumes one retry, even when multiple runs are submitted. Intent
and budget are saved before the write; confirmed rejections alone consume no
budget. Other explicit API rejections release the rejected intent and return partial
progress with a failure exit. Transport errors retain intent and stop further
submissions. Watch and retry processes serialize load/update/save through the
same state-file lock.
Saved intents suppress further retries and merge readiness until a later read
shows a higher `run_attempt`. Unchanged attempt evidence remains
`check_rerun_outcome`; a confirmed run absent from the complete head inventory
also emits `stop_missing_rerun`. Its intent and budget remain saved for direct
run investigation, rather than polling forever or resetting the state.
Interrupted or unknown writes also emit
`stop_unknown_rerun` rather than waiting indefinitely or resetting intent.
Confirmed "cannot be retried" rejections are saved by head, run and attempt.
They consume no budget and are skipped on later submissions; when all eligible
attempts have that rejection, `stop_nonretryable_rerun` asks for a distinct
supported recovery. A higher observed attempt permits normal selection again.
Other confirmed rejections remain retryable after correcting their reported
access/request problem. Neither rejection path infers rejection from an unchanged
terminal attempt. A later acquisition PR-read failure returns `rerun_read_error`
with the already confirmed `rerun_run_ids`, retry charge and redacted API error.

## State lifetime and recovery

Default state lives in `$XDG_STATE_HOME/pr-babysit` when configured, otherwise
`~/.local/state/pr-babysit`, one JSON file per repository and PR. A relative
`XDG_STATE_HOME` is ignored. `--state-file`
still selects an explicit location; use a persistent filesystem for restart
recovery. State has no age-based expiry. Each new head has separate retry,
pending and rejected-attempt records; old-head evidence remains in the file.
Watch and retry processes must select the same file. If their `HOME` or
`XDG_STATE_HOME` settings differ, pass the same persistent `--state-file` to
each; environment-specific defaults otherwise have independent locks and budgets.

When the new default file is absent, the watcher copies the previous matching
`/tmp/pr-babysit-OWNER-REPO-prNUMBER.json` under both state locks before collecting
a snapshot. It leaves that source evidence in place and never overwrites an
existing new-default file. Stop watchers running older code before switching
the default; concurrent old and new watchers otherwise use different files.
To continue an existing watcher during a transition, keep passing its exact
`--state-file`. If the old temporary file has already disappeared, its evidence
cannot be reconstructed from an unchanged terminal attempt: do not treat a fresh
file as permission to repeat an uncertain write.
An invalid legacy JSON file also stops migration. Preserve it; recover a valid
backup into a persistent location and use `--state-file <recovered-file>` to
continue. Without a valid backup, reconcile the writes from authoritative
evidence first rather than selecting an empty file to bypass lost evidence.

Writes flush and fsync the private temporary file before atomic replacement,
then fsync its directory. These are filesystem durability requests, with
process-restart coverage; no kernel crash, reboot or power-loss experiment was
performed and storage hardware guarantees are not claimed.
Pre-write sync failure stops before sending a command and reports
`not_sent_run_ids`. The watcher attempts one state restoration, removing only
that unsent intent and returning its charge when the cycle has no earlier
confirmed write. `unsent_state_restored: true` permits a later retry after the
filesystem is repaired. A failed restoration reports `recovery_error`; retain
the saved state and unsent receipt rather than clearing unknown evidence.
A sync failure after a
confirmed command returns `state_save_error` with its already confirmed run IDs
and available saved budget. Replacement may already have saved the intent;
keep that evidence while repairing the filesystem, rather than treating the
error as permission to rerun.

CLI rerun commands have a 60-second ceiling (shorter under an inherited GitHub
deadline or a reduced `GITHUB_RETRY_MAX_WAIT_SECONDS`); timeout kills the command group, keeps the pre-write intent and
budget, and returns `rerun_outcome_unknown`. Lock acquisition has a 60-second
ceiling too; one-shot contention fails without changing evidence, so resume with
the same file after its holder exits. Watch mode emits `state_busy` and tries
the next poll. Reads retain the existing transport's managed cooldown and
deadline policy; lock holders can therefore wait on a legitimate GitHub cooldown.
An already-expired inherited command deadline refuses launch and returns the
unspent budget, since no write was sent. The child wrapper receives a shorter
deadline so managed preflight reads can report a definite refusal before the
parent ceiling. The maintained wrapper supplies a structured receipt tied to
that invocation's nonce when it refuses before launching the requested command.
This covers missing or mismatched identity, missing credentials, and App
configuration/installation refusals without interpreting error text. The
watcher releases only that unsent intent and charge. A genuine timeout without
that receipt remains unknown,
including a hung preflight; an unchanged attempt cannot prove no write was sent.
On the main thread, SIGTERM and SIGHUP, unless already ignored, use the
interruption cleanup path: kill
and reap the command group, restore the parent's signal handlers, and exit while
retaining submitting intent and budget. Cleanup does not prove no write occurred.
An uncatchable SIGKILL, termination during process creation, or termination
outside that cleanup path can leave the command alive without its parent-enforced
ceiling. Its saved unknown intent blocks replay. Identify the outstanding
command and descendants before stopping them; read back GitHub through the
configured automation route. A higher attempt in complete inventory reconciles
the intent; if its outcome cannot be established, retain it and start distinct
checks only on a reviewed task fix's new head. No automatic replay or PID-based
cleanup of an orphan is supported.
No outer transport retry loop,
identity fallback, expiry or replay is introduced.

To resume after a missing-run stop, read the run directly through the configured
automation wrapper (`api repos/OWNER/REPO/actions/runs/RUN_ID`). Resume
`--watch --state-file <same-file>` when a complete head inventory includes its
higher attempt; ordinary readback reconciles it. A direct read alone does not
change the saved state. If the run is gone or GitHub says its attempt cannot be retried,
the next reviewed task fix commit starts fresh checks on a distinct head, using
that same state file and retaining the old evidence. Never clear unknown intent
or treat unchanged terminal evidence as a rejection to force a replay.
After partial progress, a later invocation is a new retry cycle using the
remaining per-head budget.

### Required scans cancelled before runner acquisition

The same retry command can offer a full-workflow rerun (`run rerun <run-id>`)
for a completed **failed** run when every job in its exact attempt is cancelled,
has no executed steps or assigned runner, and has the explicit failure annotation
"The job was not acquired by Runner of type hosted even after multiple attempts".
At least one associated check must be required for this PR, verified with
[`CheckRun.isRequired`](https://docs.github.com/en/graphql/reference/checks).
Alternatively, a CodeQL code-scanning rule must apply to the PR base branch,
and the workflow source at the exact PR head must contain the CodeQL analysis
action. This covers rules requiring scan results rather than Actions job checks.
Jobs, checks and run readback must match the head and attempt. Missing or
unavailable evidence never admits this recovery.

Before the write the helper bypasses polling cache coalescing and rechecks
the PR head/base, run attempt, annotations
and requirement. The full retry uses the same persisted intent, same actor,
readback and per-head cycle budget as failed-job retries. A cancelled run,
notification or concurrency cancellation, or an attempt with any executed or
successful job remains excluded from full retry. This bounded route does not
cover CodeQL default setup, which has no exact-head authored workflow source
to verify here (see the recovery investigation in [#1103](https://github.com/cbusillo/codex-skills/issues/1103)),
or implement [individual cancelled-job retries](https://cli.github.com/manual/gh_run_rerun)
for mixed-success workflows; those require a distinct job-selection contract.

## Review-related endpoints

- Issue comments on PR:
  - `github/scripts/gh-with-env-token api repos/{owner}/{repo}/issues/<pr_number>/comments?per_page=100 --method GET`
- Inline PR review comments:
  - `github/scripts/gh-with-env-token api repos/{owner}/{repo}/pulls/<pr_number>/comments?per_page=100 --method GET`
- Review submissions:
  - `github/scripts/gh-with-env-token api repos/{owner}/{repo}/pulls/<pr_number>/reviews?per_page=100 --method GET`

## JSON fields consumed by the watcher

### REST-first PR view

- `number`
- `url`
- `state`
- `merged`
- `mergedAt`
- `mergeCommitOid`
- `draft`
- `baseRefName`
- `headRefName`
- `headRefOid`
- `mergeable`
- `mergeStateStatus`
- `reviewDecision` (normally unavailable through REST)

### REST-first PR checks

- `pr`
- `headSha`
- `summary.checkRunCount`
- `summary.statusCount`
- `summary.failingCount`
- `summary.pendingCount`
- `summary.countsComplete`
- `summary.countsAreLowerBounds`
- `summary.unavailableComponents`

The watcher supports `GH_PR_WATCH_PR_HELPER` for an explicit helper path and
passes its configured `GH_PR_WATCH_GH` command to that helper as `GH_PR_GH`, so
both layers use the same automation identity route.

### Actions runs API (`workflow_runs[]`)

- `id`
- `name`
- `status`
- `conclusion`
- `html_url`
- `head_sha`

### Actions run jobs API (`jobs[]`)

- `id`
- `name`
- `status`
- `conclusion`
- `html_url`

### Automated inline review threads

Inline Codex and GitHub Advanced Security comments are actionable review
inventory. When those comments exist, the watcher reads GraphQL `reviewThreads`
with the REST repository, PR number, URL, and head SHA pinned on every page.
`pr.review_threads` retains thread IDs, resolution, outdated state, comment
anchor commit SHA, and whether that anchor matches the current head. GitHub can
advance the anchor commit when a line still applies after a push:
`matches_current_head: true` does not prove the bot analyzed that head. Verify
the finding against the current code and its original review/snapshot revision
before acting, even when the anchor matches. Resolved comments
are omitted from new feedback. Unresolved threads remain a `resolve_review_threads`
action after their comment IDs have been seen; old-head findings require
verification on the current head before changing code, and still need disposition
before the watcher offers a merge. GitHub requires resolution for merge only
when its branch protection or rulesets require conversation resolution. Missing, mismatched, or truncated resolution
evidence emits `review_thread_resolution_unavailable` and cannot prove readiness.
Thread pagination is bounded to ten pages; a thread with over 100 comments is
reported incomplete.


### Resolve an addressed automated thread

After pushing and verifying the fix on the current head, or recording why the
finding is declined under the review policy, use the thread ID from fresh
`pr.review_threads` evidence. The maintained bot wrapper supports GitHub's
[resolveReviewThread mutation](https://docs.github.com/en/graphql/reference/pulls#resolvereviewthread):

```bash
<skill-dir>/../github/scripts/gh-with-env-token api graphql \
  -f query='mutation($thread: ID!) { resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } } }' \
  -f thread='<verified thread ID>'
```

This uses the existing CLI passthrough because there is no dedicated resolve
command. Verify the ID belongs to the intended PR in the current watcher
snapshot before sending it. Confirm no GraphQL errors and the same returned
ID with `isResolved: true`, then re-read the watcher; a successful CLI exit
alone is insufficient. Preserve auth/refusal behavior, never change identity
or grants to resolve a thread, and do not resolve an unaddressed finding.
For an ambiguous response, re-read the thread before another write. If the
configured bot cannot resolve it, record that specific refusal on the owning
issue and retain the blocker for supported disposition.
