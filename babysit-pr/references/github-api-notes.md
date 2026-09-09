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
workflow, and check reads. Respect Retry-After/reset advice. A quiet green PR
uses a five-minute cadence; active or changed work uses one minute. This is
still polling, not a webhook service; Launchplane owns the event-driven
follow-up under launchplane#2374.

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
Job detail is fetched only for completed failed runs that need diagnosis, never
as routine detail polling for queued/running or successful runs.

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
