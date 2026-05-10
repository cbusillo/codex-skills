#!/usr/bin/env bash
set -u
# Keep section-level graceful degradation through run_or_note instead of set -e.

config_path=""
config_override_path=""
effective_config_path=""
json_output=0
fetch_first=0
health_urls=()
cleanup_paths=()

cleanup() {
  local path
  for path in ${cleanup_paths[@]+"${cleanup_paths[@]}"}; do
    if [[ -n "$path" ]]; then
      rm -rf "$path"
    fi
  done
}

trap cleanup EXIT

validate_health_url() {
  local url="$1"
  if [[ ! "$url" =~ ^https?:// ]]; then
    echo "error: health URL must be an http or https URL: $url" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      if [[ $# -lt 2 ]]; then
        echo "error: --config requires a path" >&2
        exit 2
      fi
      config_path="$2"
      shift 2
      ;;
    --health-url)
      if [[ $# -lt 2 ]]; then
        echo "error: --health-url requires a URL" >&2
        exit 2
      fi
      validate_health_url "$2"
      health_urls+=("$2")
      shift 2
      ;;
    --json)
      json_output=1
      shift
      ;;
    --fetch)
      fetch_first=1
      shift
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: github-repo-snapshot.sh [--json] [--fetch] [--config PATH] [--health-url URL]

Print a snapshot of local git, GitHub PRs/issues/actions, and optional deploy
health endpoints. By default the script does not mutate repo or GitHub state;
use --fetch when a post-fetch local snapshot is needed. When the default repo
config is used, .github/github.override.json is deep-merged when
present and reported as an applied local override.

Options:
  --json             Emit one JSON object for agent/tool consumption.
  --fetch            Run git fetch --prune before taking the snapshot and
                     include the fetch result in JSON output.
  --config PATH      Read optional repo config. Defaults to
                     .github/github.json when present.
  --health-url URL   Check an http(s) deploy health endpoint. May be repeated.
USAGE
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

section() {
  printf '\n== %s ==\n' "$1"
}

run_or_note() {
  local description="$1"
  shift

  if ! "$@"; then
    printf 'warning: %s failed\n' "$description" >&2
  fi
}

capture_lines_json() {
  local output status
  output="$({ "$@"; } 2>&1)"
  status=$?
  jq -n --argjson exitCode "$status" --arg output "$output" \
    '{exitCode: $exitCode, ok: ($exitCode == 0), lines: ($output | split("\n") | if .[-1] == "" then .[:-1] else . end)}'
}

capture_gh_json() {
  local output status
  output="$({ "$@"; } 2>&1)"
  status=$?
  if [[ "$status" -eq 0 ]]; then
    printf '%s\n' "$output"
  else
    jq -n --argjson exitCode "$status" --arg message "$output" \
      '{error: {exitCode: $exitCode, message: $message}}'
  fi
}

if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "error: not inside a git repository" >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root" || exit 1
current_branch="$(git branch --show-current 2>/dev/null || true)"

if [[ -z "$config_path" ]]; then
  if [[ -f ".github/github.json" ]]; then
    config_path=".github/github.json"
  elif [[ -f ".github/github-repo-workflow.json" ]]; then
    config_path=".github/github-repo-workflow.json"
  fi
fi

if [[ -n "$config_path" ]]; then
  if [[ ! -f "$config_path" ]]; then
    echo "error: config file not found: $config_path" >&2
    exit 2
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "error: jq is required to read config: $config_path" >&2
    exit 2
  fi
  if ! jq empty "$config_path" >/dev/null; then
    echo "error: config file is not valid JSON: $config_path" >&2
    exit 2
  fi

  if [[ "$config_path" == ".github/github.json" && -f ".github/github.override.json" ]]; then
    config_override_path=".github/github.override.json"
  elif [[ "$config_path" == ".github/github-repo-workflow.json" && -f ".github/github-repo-workflow.override.json" ]]; then
    config_override_path=".github/github-repo-workflow.override.json"
  fi

  if [[ -n "$config_override_path" ]]; then
    if ! jq empty "$config_override_path" >/dev/null; then
      echo "error: config override file is not valid JSON: $config_override_path" >&2
      exit 2
    fi
    effective_config_path="$(mktemp)"
    cleanup_paths+=("$effective_config_path")
    jq -s '.[0] * .[1]' "$config_path" "$config_override_path" >"$effective_config_path"
  else
    effective_config_path="$config_path"
  fi

  while IFS= read -r config_health_url; do
    if [[ -n "$config_health_url" ]]; then
      validate_health_url "$config_health_url"
      health_urls+=("$config_health_url")
    fi
  done < <(jq -r '.healthUrls[]? | if type == "string" then . elif type == "object" then .url // empty else empty end' "$effective_config_path")
fi

if [[ "$json_output" -eq 1 ]]; then
  if ! command -v jq >/dev/null 2>&1; then
    echo "error: --json requires jq" >&2
    exit 2
  fi

  tmpdir="$(mktemp -d)"
  cleanup_paths+=("$tmpdir")

  if [[ "$fetch_first" -eq 1 ]]; then
    capture_lines_json git fetch --prune >"$tmpdir/fetch.json"
  else
    jq -n 'null' >"$tmpdir/fetch.json"
  fi

  capture_lines_json git status --short --branch >"$tmpdir/status.json"
  capture_lines_json git branch -vv >"$tmpdir/branches.json"
  capture_lines_json git worktree list >"$tmpdir/worktrees.json"
  capture_lines_json git remote -v >"$tmpdir/remotes.json"

  if [[ -n "$config_path" ]]; then
    if [[ -n "$config_override_path" ]]; then
      jq -n \
        --arg path "$config_path" \
        --arg overridePath "$config_override_path" \
        --slurpfile baseData "$config_path" \
        --slurpfile overrideData "$config_override_path" \
        --slurpfile effectiveData "$effective_config_path" \
        '{path: $path, data: $effectiveData[0], baseData: $baseData[0], override: {path: $overridePath, applied: true, data: $overrideData[0]}}' >"$tmpdir/config.json"
    else
      jq -n --arg path "$config_path" --slurpfile data "$effective_config_path" \
        '{path: $path, data: $data[0], baseData: $data[0], override: null}' >"$tmpdir/config.json"
    fi
  else
    jq -n 'null' >"$tmpdir/config.json"
  fi

  gh_available=0
  if command -v gh >/dev/null 2>&1; then
    gh_available=1
    if [[ -n "$current_branch" ]]; then
      capture_gh_json gh pr view --json number,title,state,isDraft,mergeStateStatus,reviewDecision,headRefName,baseRefName,headRefOid,labels,url,statusCheckRollup >"$tmpdir/current-pr.json"
      capture_gh_json gh run list --branch "$current_branch" --limit 10 --json databaseId,workflowName,displayTitle,status,conclusion,headBranch,headSha,event,createdAt,url >"$tmpdir/branch-runs.json"
    else
      jq -n 'null' >"$tmpdir/current-pr.json"
      jq -n '[]' >"$tmpdir/branch-runs.json"
    fi
    capture_gh_json gh pr list --state open --limit 20 --json number,title,state,isDraft,mergeStateStatus,headRefName,baseRefName,labels,url >"$tmpdir/open-prs.json"
    capture_gh_json gh issue list --state open --limit 30 --json number,title,state,labels,url,updatedAt >"$tmpdir/open-issues.json"
    capture_gh_json gh run list --limit 10 --json databaseId,workflowName,displayTitle,status,conclusion,headBranch,headSha,event,createdAt,url >"$tmpdir/recent-runs.json"
  else
    jq -n 'null' >"$tmpdir/current-pr.json"
    jq -n '[]' >"$tmpdir/open-prs.json"
    jq -n '[]' >"$tmpdir/open-issues.json"
    jq -n '[]' >"$tmpdir/branch-runs.json"
    jq -n '[]' >"$tmpdir/recent-runs.json"
  fi

  : >"$tmpdir/health.ndjson"
  for health_url in ${health_urls[@]+"${health_urls[@]}"}; do
    if command -v curl >/dev/null 2>&1; then
      health_body="$(curl --max-time 10 -fsS "$health_url" 2>&1)"
      health_status=$?
    else
      health_body="curl not found; skipping deploy health."
      health_status=127
    fi
    jq -n --arg url "$health_url" --arg body "$health_body" --argjson exitCode "$health_status" \
      '{url: $url, exitCode: $exitCode, ok: ($exitCode == 0), body: $body, json: (try ($body | fromjson) catch null)}' >>"$tmpdir/health.ndjson"
  done
  jq -s '.' "$tmpdir/health.ndjson" >"$tmpdir/health.json"

  jq -n \
    --arg repoRoot "$repo_root" \
    --arg currentBranch "$current_branch" \
    --argjson ghAvailable "$gh_available" \
    --slurpfile status "$tmpdir/status.json" \
    --slurpfile fetch "$tmpdir/fetch.json" \
    --slurpfile branches "$tmpdir/branches.json" \
    --slurpfile worktrees "$tmpdir/worktrees.json" \
    --slurpfile remotes "$tmpdir/remotes.json" \
    --slurpfile config "$tmpdir/config.json" \
    --slurpfile currentPr "$tmpdir/current-pr.json" \
    --slurpfile openPrs "$tmpdir/open-prs.json" \
    --slurpfile openIssues "$tmpdir/open-issues.json" \
    --slurpfile branchRuns "$tmpdir/branch-runs.json" \
    --slurpfile recentRuns "$tmpdir/recent-runs.json" \
    --slurpfile health "$tmpdir/health.json" \
    '{
      repository: {
        root: $repoRoot,
        currentBranch: $currentBranch,
        fetch: $fetch[0],
        status: $status[0],
        branches: $branches[0],
        worktrees: $worktrees[0],
        remotes: $remotes[0]
      },
      config: $config[0],
      github: {
        ghAvailable: $ghAvailable,
        currentBranchPullRequest: $currentPr[0],
        openPullRequests: $openPrs[0],
        openIssues: $openIssues[0],
        currentBranchRuns: $branchRuns[0],
        recentRuns: $recentRuns[0]
      },
      deployHealth: $health[0]
    }'
  exit 0
fi

if [[ "$fetch_first" -eq 1 ]]; then
  section "Fetch"
  run_or_note "git fetch --prune" git fetch --prune
fi

section "Repository"
printf 'root: %s\n' "$repo_root"
run_or_note "git status" git status --short --branch
run_or_note "git branches" git branch -vv

section "Worktrees"
run_or_note "git worktree list" git worktree list

section "Remotes"
run_or_note "git remote -v" git remote -v

if [[ -n "$config_path" ]]; then
  section "Config"
  printf 'path: %s\n' "$config_path"
  if [[ -n "$config_override_path" ]]; then
    printf 'override: %s (applied)\n' "$config_override_path"
  fi
  run_or_note "config summary" jq -r '{defaultBranch: (.defaultBranch // null), projectType: (.projectType // null), docs: (.docs // {}), qualityGate: (.qualityGate // {}), importantWorkflows: (.importantWorkflows // []), qaLabels: (.qaLabels // []), deployLabels: (.deployLabels // []), healthUrls: (.healthUrls // []), relatedRepos: (.relatedRepos // []), validatedThrough: (.validatedThrough // []), jetbrains: (.jetbrains // {}), githubSignals: (.githubSignals // {}), cleanup: (.cleanup // {}), metadataFreshness: (.metadataFreshness // {})}' "$effective_config_path"
fi

if command -v gh >/dev/null 2>&1; then
  section "Current Branch Pull Request"
  if [[ -n "$current_branch" ]] && gh pr view --json number,title,state,isDraft,mergeStateStatus,reviewDecision,headRefName,baseRefName,headRefOid,labels,url,statusCheckRollup 2>/dev/null; then
    :
  else
    echo "no pull request associated with the current branch"
  fi

  section "Open Pull Requests"
  run_or_note "gh pr list" gh pr list --state open --limit 20

  section "Open Issues"
  run_or_note "gh issue list" gh issue list --state open --limit 30

  if [[ -n "$current_branch" ]]; then
    section "Recent Actions for Current Branch"
    run_or_note "gh run list for current branch" gh run list --branch "$current_branch" --limit 10
  fi

  section "Recent Actions"
  run_or_note "gh run list" gh run list --limit 10
else
  section "GitHub CLI"
  echo "gh not found; skipping GitHub state."
fi

if [[ -n "${health_urls[*]-}" ]]; then
  section "Deploy Health"
  for health_url in ${health_urls[@]+"${health_urls[@]}"}; do
    printf '%s\n' "$health_url"
    if command -v curl >/dev/null 2>&1; then
      run_or_note "curl health endpoint" curl --max-time 10 -fsS "$health_url"
      printf '\n'
    else
      echo "curl not found; skipping deploy health."
    fi
  done
fi
