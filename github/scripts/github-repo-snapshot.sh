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
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
gh_bin="${GITHUB_REPO_SNAPSHOT_GH:-$script_dir/gh-with-env-token}"
pr_helper="${GITHUB_REPO_SNAPSHOT_PR_HELPER:-$script_dir/gh-pr.py}"

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
  local stdout stderr status
  stdout="$(mktemp)"
  stderr="$(mktemp)"
  cleanup_paths+=("$stdout" "$stderr")
  if "$@" >"$stdout" 2>"$stderr"; then
    if jq -e . "$stdout" >/dev/null 2>&1; then
      cat "$stdout"
    else
      jq -n --arg message "$(cat "$stdout")" --arg stderr "$(cat "$stderr")" \
        '{error: {exitCode: 0, message: $message, stderr: $stderr}}'
    fi
    return
  fi
  status=$?
  jq -n --argjson exitCode "$status" --arg message "$(cat "$stderr")" --arg stdout "$(cat "$stdout")" \
    '{error: {exitCode: $exitCode, message: $message, stdout: $stdout}}'
}

capture_pr_helper_json() {
  local stdout stderr status
  stdout="$(mktemp)"
  stderr="$(mktemp)"
  cleanup_paths+=("$stdout" "$stderr")
  if GH_PR_GH="$gh_bin" "$pr_helper" "$@" >"$stdout" 2>"$stderr"; then
    jq 'if type == "object" and has("pr") then .pr elif type == "object" and has("pullRequests") then .pullRequests else . end' "$stdout"
    return
  fi
  status=$?
  if grep -q "No open PR found for current branch" "$stderr"; then
    jq -n 'null'
    return
  fi
  jq -n --argjson exitCode "$status" --arg message "$(cat "$stderr")" --arg stdout "$(cat "$stdout")" \
    '{error: {exitCode: $exitCode, message: $message, stdout: $stdout}}'
}

normalize_raw_pr_snapshot_json() {
  jq '
    def field_or_null($name): if has($name) then .[$name] else null end;
    def normalize_pr:
      . + {
        draft: (if has("draft") then .draft elif has("isDraft") then .isDraft else null end),
        isDraft: (if has("isDraft") then .isDraft elif has("draft") then .draft else null end),
        merged: field_or_null("merged"),
        mergeable: field_or_null("mergeable"),
        mergeable_state: field_or_null("mergeable_state"),
        mergeStateStatus: field_or_null("mergeStateStatus"),
        reviewDecision: field_or_null("reviewDecision"),
        statusCheckRollup: field_or_null("statusCheckRollup"),
        snapshotReadiness: {
          source: "gh-fallback",
          degraded: true,
          mergeReadinessAvailable: false,
          message: "gh-pr.py unavailable; merge readiness was not queried. Use gh-pr.py view/checks before readiness or merge decisions."
        }
      };
    if type == "array" then map(normalize_pr)
    elif type == "object" and has("error") then .
    elif type == "object" then normalize_pr
    else .
    end
  '
}

build_repo_settings_json() {
  local raw_settings_path="$1"
  local config_json_path="$2"

  jq -n \
    --slurpfile raw "$raw_settings_path" \
    --slurpfile config "$config_json_path" \
    '
    def expected_settings:
      ($config[0].data.githubSettings.expected // {});
    def actual_settings:
      if ($raw[0] | type) == "object" and ($raw[0] | has("error") | not) then
        {
          nameWithOwner: ($raw[0].nameWithOwner // null),
          defaultBranch: ($raw[0].defaultBranchRef.name // null),
          deleteBranchOnMerge: (if $raw[0] | has("deleteBranchOnMerge") then $raw[0].deleteBranchOnMerge else null end)
        }
      else
        null
      end;
    def check($key; $actual; $expected; $message):
      {
        key: $key,
        ok: ($actual == $expected),
        expected: $expected,
        actual: $actual,
        severity: (if $actual == $expected then "ok" else "warning" end),
        message: (if $actual == $expected then null else $message end)
      };

    (expected_settings) as $expected |
    (actual_settings) as $actual |
    if ($expected | length) == 0 then
      {available: ($actual != null), actual: $actual, expected: $expected, checks: [], warnings: []}
    elif $actual == null then
      {
        available: false,
        actual: null,
        expected: $expected,
        checks: [],
        warnings: [{
          key: "repositorySettings",
          severity: "unavailable",
          message: "Repository settings could not be read; do not treat configured expectations as verified."
        }],
        error: ($raw[0].error // null)
      }
    else
      ([
        if $expected | has("deleteBranchOnMerge") then
          check(
            "deleteBranchOnMerge";
            $actual.deleteBranchOnMerge;
            $expected.deleteBranchOnMerge;
            "Repository is not configured to delete PR branches after merge; merged branch cleanup may require manual follow-up."
          )
        else empty end
      ]) as $checks |
      {
        available: true,
        actual: $actual,
        expected: $expected,
        checks: $checks,
        warnings: ($checks | map(select(.ok == false)))
      }
    end
    '
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
  if [[ -x "$gh_bin" ]] || command -v "$gh_bin" >/dev/null 2>&1; then
    gh_available=1
    if [[ -n "$current_branch" ]]; then
      if [[ -x "$pr_helper" ]] || command -v "$pr_helper" >/dev/null 2>&1; then
        capture_pr_helper_json view >"$tmpdir/current-pr.json"
      else
        capture_gh_json "$gh_bin" pr view --json number,title,state,isDraft,headRefName,baseRefName,headRefOid,labels,url \
          | normalize_raw_pr_snapshot_json >"$tmpdir/current-pr.json"
      fi
      capture_gh_json "$gh_bin" run list --branch "$current_branch" --limit 10 --json databaseId,workflowName,displayTitle,status,conclusion,headBranch,headSha,event,createdAt,url >"$tmpdir/branch-runs.json"
    else
      jq -n 'null' >"$tmpdir/current-pr.json"
      jq -n '[]' >"$tmpdir/branch-runs.json"
    fi
    if [[ -x "$pr_helper" ]] || command -v "$pr_helper" >/dev/null 2>&1; then
      capture_pr_helper_json list --state open --limit 20 >"$tmpdir/open-prs.json"
    else
      capture_gh_json "$gh_bin" pr list --state open --limit 20 --json number,title,state,isDraft,headRefName,baseRefName,labels,url \
        | normalize_raw_pr_snapshot_json >"$tmpdir/open-prs.json"
    fi
    capture_gh_json "$gh_bin" issue list --state open --limit 30 --json number,title,state,labels,url,updatedAt >"$tmpdir/open-issues.json"
    capture_gh_json "$gh_bin" run list --limit 10 --json databaseId,workflowName,displayTitle,status,conclusion,headBranch,headSha,event,createdAt,url >"$tmpdir/recent-runs.json"
    capture_gh_json "$gh_bin" repo view --json nameWithOwner,defaultBranchRef,deleteBranchOnMerge >"$tmpdir/repo-settings-raw.json"
  else
    jq -n 'null' >"$tmpdir/current-pr.json"
    jq -n '[]' >"$tmpdir/open-prs.json"
    jq -n '[]' >"$tmpdir/open-issues.json"
    jq -n '[]' >"$tmpdir/branch-runs.json"
    jq -n '[]' >"$tmpdir/recent-runs.json"
    jq -n '{error: {message: "gh not found"}}' >"$tmpdir/repo-settings-raw.json"
  fi
  build_repo_settings_json "$tmpdir/repo-settings-raw.json" "$tmpdir/config.json" >"$tmpdir/repo-settings.json"

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
    --slurpfile repoSettings "$tmpdir/repo-settings.json" \
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
        recentRuns: $recentRuns[0],
        repositorySettings: $repoSettings[0]
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
  run_or_note "config summary" jq -r '{defaultBranch: (.defaultBranch // null), projectType: (.projectType // null), docs: (.docs // {}), qualityGate: (.qualityGate // {}), importantWorkflows: (.importantWorkflows // []), qaLabels: (.qaLabels // []), deployLabels: (.deployLabels // []), healthUrls: (.healthUrls // []), relatedRepos: (.relatedRepos // []), jetbrains: (.jetbrains // {}), githubSignals: (.githubSignals // {}), cleanup: (.cleanup // {}), metadataFreshness: (.metadataFreshness // {})}' "$effective_config_path"
fi

if [[ -x "$gh_bin" ]] || command -v "$gh_bin" >/dev/null 2>&1; then
  section "Current Branch Pull Request"
  if [[ -n "$current_branch" ]] && [[ -x "$pr_helper" ]] && GH_PR_GH="$gh_bin" "$pr_helper" view 2>/dev/null; then
    :
  elif [[ -n "$current_branch" ]] && "$gh_bin" pr view --json number,title,state,isDraft,headRefName,baseRefName,headRefOid,labels,url 2>/dev/null; then
    :
  else
    echo "no pull request associated with the current branch"
  fi

  section "Open Pull Requests"
  if [[ -x "$pr_helper" ]] || command -v "$pr_helper" >/dev/null 2>&1; then
    run_or_note "gh-pr list" env GH_PR_GH="$gh_bin" "$pr_helper" list --state open --limit 20
  else
    run_or_note "gh pr list" "$gh_bin" pr list --state open --limit 20
  fi

  section "Open Issues"
  run_or_note "gh issue list" "$gh_bin" issue list --state open --limit 30

  if [[ -n "$current_branch" ]]; then
    section "Recent Actions for Current Branch"
    run_or_note "gh run list for current branch" "$gh_bin" run list --branch "$current_branch" --limit 10
  fi

  section "Recent Actions"
  run_or_note "gh run list" "$gh_bin" run list --limit 10

  if [[ -n "$config_path" ]] && jq -e '.githubSettings.expected // empty' "$effective_config_path" >/dev/null 2>&1; then
    section "Repository Settings"
    settings_tmp="$(mktemp)"
    raw_settings_tmp="$(mktemp)"
    cleanup_paths+=("$settings_tmp" "$raw_settings_tmp")
    capture_gh_json "$gh_bin" repo view --json nameWithOwner,defaultBranchRef,deleteBranchOnMerge >"$raw_settings_tmp"
    jq -n --arg path "$config_path" --slurpfile data "$effective_config_path" '{path: $path, data: $data[0]}' >"$settings_tmp.config"
    cleanup_paths+=("$settings_tmp.config")
    build_repo_settings_json "$raw_settings_tmp" "$settings_tmp.config" >"$settings_tmp"
    jq -r '
      (.warnings[]? | "warning: \(.key) - \(.message)"),
      (.checks[]? | if .ok then "ok: \(.key)=\(.actual)" else "warning: \(.key) expected \(.expected) but found \(.actual) - \(.message)" end)
    ' "$settings_tmp"
  fi
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
