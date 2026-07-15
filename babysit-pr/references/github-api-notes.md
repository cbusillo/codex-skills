# GitHub CLI / API Notes For `babysit-pr`

The watcher routes GitHub CLI calls through
`github/scripts/gh-with-env-token` by default. Reads and writes preserve the
configured automation actor when auth or quota failures occur. Active local
`gh` is used only when active-auth fallback is explicitly allowed for a one-off;
write-like calls such as Actions reruns remain fail-closed by default.

## Primary commands used

### PR metadata

- `github/scripts/gh-with-env-token pr view --json number,url,state,mergedAt,closedAt,headRefName,headRefOid,headRepository,headRepositoryOwner`

Used to resolve PR number, URL, branch, head SHA, and closed/merged state.

### PR checks summary

- `github/scripts/gh-with-env-token pr checks --json name,state,bucket,link,workflow,event,startedAt,completedAt`

Used to compute pending/failed/passed counts and whether the current CI round is terminal.

### Workflow runs for head SHA

- `github/scripts/gh-with-env-token api repos/{owner}/{repo}/actions/runs --method GET -f head_sha=<sha> -f per_page=100`

Used to discover failed workflow runs and rerunnable run IDs.

### Failed log inspection

- `github/scripts/gh-with-env-token run view <run-id> --json jobs,name,workflowName,conclusion,status,url,headSha`
- `github/scripts/gh-with-env-token api repos/{owner}/{repo}/actions/runs/{run_id}/jobs --method GET -f per_page=100`
- `github/scripts/gh-with-env-token api repos/{owner}/{repo}/actions/jobs/{job_id}/logs > /tmp/pr-watch-gh-job-{job_id}-logs.zip`
- `github/scripts/gh-with-env-token run view <run-id> --log-failed`

Used by Codex to classify branch-related vs flaky/unrelated failures. Prefer the direct job log endpoint as soon as a job has failed because `gh run view --log-failed` may not produce failed-job logs until the overall workflow run completes.

### Retry failed jobs only

- `github/scripts/gh-with-env-token run rerun <run-id> --failed`

Reruns only failed jobs (and dependencies) for a workflow run. This is a
GitHub write and must be owned by `shiny-code-bot`.

## Review-related endpoints

- Issue comments on PR:
  - `github/scripts/gh-with-env-token api repos/{owner}/{repo}/issues/<pr_number>/comments?per_page=100 --method GET`
- Inline PR review comments:
  - `github/scripts/gh-with-env-token api repos/{owner}/{repo}/pulls/<pr_number>/comments?per_page=100 --method GET`
- Review submissions:
  - `github/scripts/gh-with-env-token api repos/{owner}/{repo}/pulls/<pr_number>/reviews?per_page=100 --method GET`

## JSON fields consumed by the watcher

### `gh pr view`

- `number`
- `url`
- `state`
- `mergedAt`
- `closedAt`
- `headRefName`
- `headRefOid`

### `gh pr checks`

- `bucket` (`pass`, `fail`, `pending`, `skipping`)
- `state`
- `name`
- `workflow`
- `link`

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
