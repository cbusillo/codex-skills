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
read_helper="${GITHUB_REPO_SNAPSHOT_READ_HELPER:-$script_dir/github_read.py}"
if [[ -n "${GITHUB_REPO_SNAPSHOT_PYTHON:-}" ]]; then
  python_command=("$GITHUB_REPO_SNAPSHOT_PYTHON")
elif command -v uv >/dev/null 2>&1; then
  python_command=(uv run --no-project --no-config --python 3.12 python)
else
  printf 'error: github-repo-snapshot requires uv or GITHUB_REPO_SNAPSHOT_PYTHON\n' >&2
  exit 127
fi

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

capture_read_json() {
  local stdout stderr status
  stdout="$(mktemp)"
  stderr="$(mktemp)"
  cleanup_paths+=("$stdout" "$stderr")
  if GITHUB_READ_GH="$gh_bin" "${python_command[@]}" "$read_helper" --gh "$gh_bin" --repo-root "$repo_root" "$@" >"$stdout" 2>"$stderr"; then
    if jq -e . "$stdout" >/dev/null 2>&1; then
      cat "$stdout"
    else
      jq -n --arg message "$(cat "$stdout")" --arg stderr "$(cat "$stderr")" \
        '{ok: false, exit_code: 1, helperExitCode: 0, error: $message, diagnostics: {degraded: true, degradedComponents: ["response"], degradedReasons: [{component: "response", code: "invalid_json", message: $stderr}]}}'
    fi
    return
  fi
  status=$?
  if jq -e . "$stdout" >/dev/null 2>&1; then
    cat "$stdout"
  else
    jq -n --argjson exitCode "$status" --arg message "$(cat "$stderr")" --arg stdout "$(cat "$stdout")" \
      '{ok: false, exit_code: $exitCode, error: $message, stdout: $stdout, diagnostics: {degraded: true, degradedComponents: ["transport"], degradedReasons: [{component: "transport", code: "helper_failed", message: $message}]}}'
  fi
}

extract_read_data() {
  jq '
    if .ok == true and has("data") then
      .data
    else
      {
        error: {
          exitCode: (.exit_code // 1),
          status: (.status // 0),
          message: (.error // .failure.message // "GitHub REST read failed"),
          cause: (.error_code // .failure.cause // "helper_error"),
          requestId: (.request_id // null),
          quota: (.quota // null),
          retryable: (.retryable // false)
        },
        diagnostics: (.diagnostics // null)
      }
    end
  '
}

read_unavailable_json() {
  local component="$1"
  local message="$2"
  jq -n --arg component "$component" --arg message "$message" '
    {
      ok: false,
      exit_code: 127,
      status: 0,
      error: $message,
      diagnostics: {
        transport: "rest_api",
        bucket: "rest_core",
        requestCount: 0,
        requests: [],
        quota: null,
        degraded: true,
        degradedComponents: [$component],
        degradedReasons: [{component: $component, code: "helper_unavailable", message: $message}]
      }
    }
  '
}

render_pull_read() {
  jq -r '
    if .ok != true then
      "warning: pull request metadata unavailable - " + (.error // .failure.message // "GitHub REST read failed")
    elif (.data | length) == 0 then
      "no pull request associated with the current branch"
    else
      .data[0] |
      "#\(.number) [\(.state)] \(.title)\nbase/head: \(.baseRefName) <- \(.headRefName)\n\(.url)"
    end
  '
}

render_pulls_read() {
  jq -r '
    if .ok != true then
      "warning: open pull requests unavailable - " + (.error // .failure.message // "GitHub REST read failed")
    elif (.data | length) == 0 then
      "no open pull requests"
    else
      .data[] | "#\(.number) [\(.state)] \(.title) - \(.url)"
    end
  '
}

render_issues_read() {
  jq -r '
    if .ok != true then
      "warning: open issues unavailable - " + (.error // .failure.message // "GitHub REST read failed")
    elif (.data | length) == 0 then
      "no open issues"
    else
      .data[] | "#\(.number) [\(.state)] \(.title) - \(.url)"
    end
  '
}

render_runs_read() {
  jq -r '
    if .ok != true then
      "warning: workflow runs unavailable - " + (.error // .failure.message // "GitHub REST read failed")
    elif (.data | length) == 0 then
      "no workflow runs"
    else
      .data[] |
      "\(.databaseId) \(.workflowName // "workflow") [\(.conclusion // .status // "unknown")] \(.displayTitle // "") - \(.url)"
    end
  '
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
        jq -n 'null' >"$tmpdir/current-pr-read.json"
      else
        capture_read_json pulls --state open --limit 1 --head-branch "$current_branch" >"$tmpdir/current-pr-read.json"
        extract_read_data <"$tmpdir/current-pr-read.json" \
          | jq 'if type == "array" then .[0] // null else . end' \
          | normalize_raw_pr_snapshot_json >"$tmpdir/current-pr.json"
      fi
      capture_read_json workflow-runs --branch "$current_branch" --limit 10 >"$tmpdir/branch-runs-read.json"
      extract_read_data <"$tmpdir/branch-runs-read.json" >"$tmpdir/branch-runs.json"
    else
      jq -n 'null' >"$tmpdir/current-pr.json"
      jq -n 'null' >"$tmpdir/current-pr-read.json"
      jq -n '[]' >"$tmpdir/branch-runs.json"
      jq -n 'null' >"$tmpdir/branch-runs-read.json"
    fi
    if [[ -x "$pr_helper" ]] || command -v "$pr_helper" >/dev/null 2>&1; then
      capture_pr_helper_json list --state open --limit 20 >"$tmpdir/open-prs.json"
      jq -n 'null' >"$tmpdir/open-prs-read.json"
    else
      capture_read_json pulls --state open --limit 20 >"$tmpdir/open-prs-read.json"
      extract_read_data <"$tmpdir/open-prs-read.json" \
        | normalize_raw_pr_snapshot_json >"$tmpdir/open-prs.json"
    fi
    capture_read_json issues --state open --limit 30 >"$tmpdir/open-issues-read.json"
    extract_read_data <"$tmpdir/open-issues-read.json" >"$tmpdir/open-issues.json"
    capture_read_json workflow-runs --limit 10 >"$tmpdir/recent-runs-read.json"
    extract_read_data <"$tmpdir/recent-runs-read.json" >"$tmpdir/recent-runs.json"
    capture_read_json repository >"$tmpdir/repository-read.json"
    extract_read_data <"$tmpdir/repository-read.json" >"$tmpdir/repo-settings-raw.json"
  else
    jq -n 'null' >"$tmpdir/current-pr.json"
    read_unavailable_json currentBranchPullRequest "gh not found" >"$tmpdir/current-pr-read.json"
    jq -n '[]' >"$tmpdir/open-prs.json"
    read_unavailable_json openPullRequests "gh not found" >"$tmpdir/open-prs-read.json"
    jq -n '[]' >"$tmpdir/open-issues.json"
    read_unavailable_json openIssues "gh not found" >"$tmpdir/open-issues-read.json"
    jq -n '[]' >"$tmpdir/branch-runs.json"
    read_unavailable_json currentBranchRuns "gh not found" >"$tmpdir/branch-runs-read.json"
    jq -n '[]' >"$tmpdir/recent-runs.json"
    read_unavailable_json recentRuns "gh not found" >"$tmpdir/recent-runs-read.json"
    jq -n '{error: {message: "gh not found"}}' >"$tmpdir/repo-settings-raw.json"
    read_unavailable_json repositorySettings "gh not found" >"$tmpdir/repository-read.json"
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
    --slurpfile currentPrRead "$tmpdir/current-pr-read.json" \
    --slurpfile openPrsRead "$tmpdir/open-prs-read.json" \
    --slurpfile openIssuesRead "$tmpdir/open-issues-read.json" \
    --slurpfile branchRunsRead "$tmpdir/branch-runs-read.json" \
    --slurpfile recentRunsRead "$tmpdir/recent-runs-read.json" \
    --slurpfile repositoryRead "$tmpdir/repository-read.json" \
    --slurpfile health "$tmpdir/health.json" \
    '
    def present($value): ($value | type == "string" and length > 0);
    def read_component($value; $source; $applicable):
      if $applicable == false then
        {source: $source, applicable: false, available: false, degraded: false}
      elif $value == null then
        {source: $source, applicable: true, available: true, degraded: false}
      else
        {
          source: $source,
          applicable: true,
          available: ($value.ok == true),
          degraded: (($value.ok != true) or ($value.diagnostics.degraded // false)),
          status: ($value.status // 0),
          requestId: ($value.request_id // null),
          quota: ($value.quota // null),
          actor: ($value.actor // $value.diagnostics.actor // null),
          expectedActor: ($value.expected_actor // $value.diagnostics.expectedActor // null),
          retryable: ($value.retryable // false),
          error: ($value.error // $value.failure.message // null),
          diagnostics: ($value.diagnostics // null)
        }
      end;
    def cleanup_summary($config):
      if $config == null then
        {
          status: "no_config",
          commands: [],
          routineCommands: [],
          handoffArtifacts: null,
          warnings: [{code: "missing_config", message: "No repo metadata config was loaded."}]
        }
      else
        ($config.data.cleanup // {}) as $cleanup |
        ($cleanup.commands // []) as $commands |
        {
          status: (if ($config.data.cleanup // null) == null then "not_configured" else "configured" end),
          deleteMergedLocalBranches: ($cleanup.deleteMergedLocalBranches // null),
          removeMergedCleanWorktrees: ($cleanup.removeMergedCleanWorktrees // null),
          commands: $commands,
          routineCommands: [
            $commands[] |
            select(
              (.when // "routine") == "routine" and
              present(.name // "") and
              present(.command // "")
            )
          ],
          handoffArtifacts: ($cleanup.handoffArtifacts // null),
          warnings: ([
            $commands[]? |
            select((present(.name // "") | not) or (present(.command // "") | not)) |
            {code: "invalid_cleanup_command", message: "Cleanup command entries should include name and command."}
          ])
        }
      end;
    def launchplane_summary($config):
      if $config == null then
        {
          status: "no_config",
          enabled: false,
          warnings: [{code: "missing_config", message: "No repo metadata config was loaded."}]
        }
      elif ($config.data.launchplane // null) == null then
        {
          status: "not_configured",
          enabled: false,
          warnings: [{code: "missing_launchplane_metadata", message: "Launchplane metadata is not configured for this repo."}]
        }
      else
        ($config.data.launchplane) as $lp |
        {
          status: (if ($lp.enabled // false) then "configured" else "disabled" end),
          enabled: ($lp.enabled // false),
          service: {
            contextUrlEnv: ($lp.service.contextUrlEnv // null),
            operatorUrlEnv: ($lp.service.operatorUrlEnv // null),
            localConfigExample: ($lp.service.localConfigExample // null)
          },
          context: {
            enabled: ($lp.context.enabled // false),
            helper: ($lp.context.helper // null)
          },
          operator: {
            enabled: ($lp.operator.enabled // false),
            helper: ($lp.operator.helper // null),
            requiresPrivateConfig: ($lp.operator.requiresPrivateConfig // true)
          },
          mergeTrain: {
            enabled: ($lp.mergeTrain.enabled // false),
            controller: ($lp.mergeTrain.controller // false),
            readyLabel: ($lp.mergeTrain.readyLabel // null),
            baseBranch: ($lp.mergeTrain.baseBranch // null),
            githubActionsRunner: ($lp.mergeTrain.githubActionsRunner // null)
          },
          warnings: ([
            if ($lp.enabled // false) and (($lp.service.publicUrl // null) != null) then
              {code: "committed_service_url", message: "Launchplane service.publicUrl should not be committed; use env or private operator config for concrete service URLs."}
            else empty end,
            if ($lp.enabled // false) and (present($lp.service.contextUrlEnv // "") | not) then
              {code: "missing_context_url_env", message: "Launchplane service.contextUrlEnv is missing."}
            else empty end,
            if ($lp.enabled // false) and (present($lp.service.operatorUrlEnv // "") | not) then
              {code: "missing_operator_url_env", message: "Launchplane service.operatorUrlEnv is missing."}
            else empty end,
            if ($lp.context.enabled // false) and (present($lp.context.helper // "") | not) then
              {code: "missing_context_helper", message: "Launchplane context helper path is missing."}
            else empty end,
            if ($lp.operator.enabled // false) and (present($lp.operator.helper // "") | not) then
              {code: "missing_operator_helper", message: "Launchplane operator helper path is missing."}
            else empty end,
            if ($lp.mergeTrain.enabled // false) and (present($lp.mergeTrain.readyLabel // "") | not) then
              {code: "missing_ready_label", message: "Launchplane mergeTrain.readyLabel is missing."}
            else empty end,
            if ($lp.mergeTrain.enabled // false) and (($lp.mergeTrain.githubActionsRunner // null) == null) then
              {code: "missing_actions_runner", message: "Launchplane mergeTrain.githubActionsRunner is missing."}
            else empty end
          ])
        }
      end;
    {
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
      cleanup: cleanup_summary($config[0]),
      launchplane: launchplane_summary($config[0]),
      github: {
        ghAvailable: $ghAvailable,
        currentBranchPullRequest: $currentPr[0],
        openPullRequests: $openPrs[0],
        openIssues: $openIssues[0],
        currentBranchRuns: $branchRuns[0],
        recentRuns: $recentRuns[0],
        repositorySettings: $repoSettings[0],
        diagnostics: (
          {
            transport: "rest_api",
            components: {
              currentBranchPullRequest: read_component($currentPrRead[0]; (if $currentPrRead[0] == null then "gh-pr" else "shared-rest" end); ($currentBranch | length) > 0),
              openPullRequests: read_component($openPrsRead[0]; (if $openPrsRead[0] == null then "gh-pr" else "shared-rest" end); true),
              openIssues: read_component($openIssuesRead[0]; "shared-rest"; true),
              currentBranchRuns: read_component($branchRunsRead[0]; "shared-rest"; ($currentBranch | length) > 0),
              recentRuns: read_component($recentRunsRead[0]; "shared-rest"; true),
              repositorySettings: read_component($repositoryRead[0]; "shared-rest"; true)
            }
          }
          | .degradedComponents = ([.components | to_entries[] | select(.value.degraded == true) | .key])
          | .degraded = ((.degradedComponents | length) > 0)
        )
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
  run_or_note "config summary" jq -r '{defaultBranch: (.defaultBranch // null), projectType: (.projectType // null), docs: (.docs // {}), qualityGate: (.qualityGate // {}), importantWorkflows: (.importantWorkflows // []), qaLabels: (.qaLabels // []), deployLabels: (.deployLabels // []), healthUrls: (.healthUrls // []), relatedRepos: (.relatedRepos // []), launchplane: (.launchplane // {}), jetbrains: (.jetbrains // {}), githubSignals: (.githubSignals // {}), cleanup: (.cleanup // {}), metadataFreshness: (.metadataFreshness // {})}' "$effective_config_path"

  section "Cleanup"
  # shellcheck disable=SC2016 # jq variables are intentionally single-quoted.
  run_or_note "cleanup metadata summary" jq -r '
    if (.cleanup // null) == null then
      "status: not_configured"
    else
      .cleanup as $cleanup |
      [
        "status: configured",
        "deleteMergedLocalBranches: " + (($cleanup.deleteMergedLocalBranches // "") | tostring),
        "removeMergedCleanWorktrees: " + (($cleanup.removeMergedCleanWorktrees // "") | tostring),
        "routineCommands: " + ([
          ($cleanup.commands // [])[] |
          select(
            (.when // "routine") == "routine" and
            ((.name // "") | length > 0) and
            ((.command // "") | length > 0)
          ) |
          .name
        ] | join(", ")),
        "handoffTemporaryGlobs: " + (($cleanup.handoffArtifacts.temporaryGlobs // []) | join(", ")),
        "handoffDurableSurface: " + ($cleanup.handoffArtifacts.durableSurface // "")
      ] | .[]
    end
  ' "$effective_config_path"

  section "Launchplane"
  # shellcheck disable=SC2016 # jq variables are intentionally single-quoted.
  run_or_note "launchplane metadata summary" jq -r '
    if (.launchplane // null) == null then
      "status: not_configured"
    else
      .launchplane as $lp |
      [
        "status: " + (if ($lp.enabled // false) then "configured" else "disabled" end),
        "contextUrlEnv: " + ($lp.service.contextUrlEnv // ""),
        "operatorUrlEnv: " + ($lp.service.operatorUrlEnv // ""),
        "localConfigExample: " + ($lp.service.localConfigExample // ""),
        "contextHelper: " + ($lp.context.helper // ""),
        "operatorHelper: " + ($lp.operator.helper // ""),
        "operatorRequiresPrivateConfig: " + (($lp.operator.requiresPrivateConfig // true) | tostring),
        "mergeTrainEnabled: " + (($lp.mergeTrain.enabled // false) | tostring),
        "mergeTrainReadyLabel: " + ($lp.mergeTrain.readyLabel // ""),
        "mergeTrainBaseBranch: " + ($lp.mergeTrain.baseBranch // ""),
        "mergeTrainWorkflow: " + ($lp.mergeTrain.githubActionsRunner.workflow // ""),
        "mergeTrainWorkflowRepo: " + ($lp.mergeTrain.githubActionsRunner.repo // "")
      ] | .[]
    end
  ' "$effective_config_path"
fi

if [[ -x "$gh_bin" ]] || command -v "$gh_bin" >/dev/null 2>&1; then
  section "Current Branch Pull Request"
  if [[ -z "$current_branch" ]]; then
    echo "no pull request associated with a detached HEAD"
  elif { [[ -x "$pr_helper" ]] || command -v "$pr_helper" >/dev/null 2>&1; } && GH_PR_GH="$gh_bin" "$pr_helper" view 2>/dev/null; then
    :
  else
    current_pr_tmp="$(mktemp)"
    cleanup_paths+=("$current_pr_tmp")
    capture_read_json pulls --state open --limit 1 --head-branch "$current_branch" >"$current_pr_tmp"
    render_pull_read <"$current_pr_tmp"
  fi

  section "Open Pull Requests"
  if [[ -x "$pr_helper" ]] || command -v "$pr_helper" >/dev/null 2>&1; then
    run_or_note "gh-pr list" env GH_PR_GH="$gh_bin" "$pr_helper" list --state open --limit 20
  else
    open_prs_tmp="$(mktemp)"
    cleanup_paths+=("$open_prs_tmp")
    capture_read_json pulls --state open --limit 20 >"$open_prs_tmp"
    render_pulls_read <"$open_prs_tmp"
  fi

  section "Open Issues"
  open_issues_tmp="$(mktemp)"
  cleanup_paths+=("$open_issues_tmp")
  capture_read_json issues --state open --limit 30 >"$open_issues_tmp"
  render_issues_read <"$open_issues_tmp"

  if [[ -n "$current_branch" ]]; then
    section "Recent Actions for Current Branch"
    branch_runs_tmp="$(mktemp)"
    cleanup_paths+=("$branch_runs_tmp")
    capture_read_json workflow-runs --branch "$current_branch" --limit 10 >"$branch_runs_tmp"
    render_runs_read <"$branch_runs_tmp"
  fi

  section "Recent Actions"
  recent_runs_tmp="$(mktemp)"
  cleanup_paths+=("$recent_runs_tmp")
  capture_read_json workflow-runs --limit 10 >"$recent_runs_tmp"
  render_runs_read <"$recent_runs_tmp"

  if [[ -n "$config_path" ]] && jq -e '.githubSettings.expected // empty' "$effective_config_path" >/dev/null 2>&1; then
    section "Repository Settings"
    settings_tmp="$(mktemp)"
    raw_settings_tmp="$(mktemp)"
    repository_read_tmp="$(mktemp)"
    cleanup_paths+=("$settings_tmp" "$raw_settings_tmp" "$repository_read_tmp")
    capture_read_json repository >"$repository_read_tmp"
    extract_read_data <"$repository_read_tmp" >"$raw_settings_tmp"
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

if [[ ${#health_urls[@]} -gt 0 ]]; then
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
