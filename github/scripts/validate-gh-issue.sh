#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH='' cd -- "$script_dir/../.." && pwd)"
tmpdir="$(mktemp -d)"
cleanup() {
	rm -rf "$tmpdir"
}
trap cleanup EXIT

log="$tmpdir/calls.log"
stderr_log="$tmpdir/stderr.log"
stdout_log="$tmpdir/stdout.log"
env_log="$tmpdir/env.log"

cat >"$tmpdir/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'active:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
printf 'https://github.com/owner/repo/issues/1\n'
EOF
chmod +x "$tmpdir/gh"

cat >"$tmpdir/rate-limited-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'bot:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
exit 1
EOF
chmod +x "$tmpdir/rate-limited-gh"

cat >"$tmpdir/env-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "auth" && "${2:-}" == "status" ]]; then
	printf '%s\n' "${GH_TOKEN:-}" >"$GH_ISSUE_ENV_LOG"
	printf 'github.com\n  ✓ Logged in to github.com account code-bot (%s)\n' "${GH_TOKEN:-}" >&2
elif [[ "${1:-}" == "rate-limited-command" ]]; then
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
		exit 1
	fi
	printf 'active-success\n'
elif [[ "${1:-}" == "invalid-token-command" ]]; then
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'X Failed to log in to github.com using token (GH_TOKEN)\n' >&2
		printf 'The token in GH_TOKEN is invalid.\n' >&2
		exit 1
	fi
	printf 'active-success\n'
elif [[ "${1:-}" == "active-command" ]]; then
	if [[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}${CODEX_GITHUB_TOKEN:-}" ]]; then
		printf 'expected active auth without token env vars\n' >&2
		exit 1
	fi
	printf 'active-success\n'
elif [[ "${1:-}" == "issue" ]]; then
	printf '%s\n' "${GH_TOKEN:-}" >"$GH_ISSUE_ENV_LOG"
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'bot:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
		printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
		exit 1
	fi
	printf 'active:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
	printf 'active-success\n'
else
	printf '%s\n' "${GH_TOKEN:-}" >"$GH_ISSUE_ENV_LOG"
fi
EOF
chmod +x "$tmpdir/env-gh"

cat >"$tmpdir/path-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${GH_TOKEN:-}" >"$GH_ISSUE_ENV_LOG"
printf 'ok\n'
EOF
chmod +x "$tmpdir/path-gh"

workspace_env_dir="$tmpdir/.code"
mkdir -p "$workspace_env_dir"
printf 'CODEX_GITHUB_TOKEN=workspace-token\n' >"$workspace_env_dir/local.env"
generated_worktree="$tmpdir/.code/working/codex-skills/branches/review"
mkdir -p "$generated_worktree/github/scripts"
cp "$repo_root/github/scripts/gh-with-env-token" "$generated_worktree/github/scripts/gh-with-env-token"

: >"$env_log"
env -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN \
	PATH="$tmpdir:$PATH" \
	HOME="$tmpdir" \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/path-gh" \
	"$generated_worktree/github/scripts/gh-with-env-token" auth status >/dev/null

grep -qx 'workspace-token' "$env_log"

: >"$env_log"
env -u HOME -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN \
	PATH="$tmpdir:$PATH" \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/path-gh" \
	"$repo_root/github/scripts/gh-with-env-token" auth status >/dev/null

grep -qx '' "$env_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_TOKEN=exhausted-token GITHUB_TOKEN=github-token CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" auth status

grep -qx 'codex-token' "$env_log"

: >"$env_log"
PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_TOKEN=exhausted-token CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account token-command \
	>"$stdout_log" 2>"$stderr_log"

grep -qx 'codex-token' "$env_log"
grep -q 'Logged in to github.com account code-bot' "$stderr_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account rate-limited-command \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'warning: automation gh token failed; retrying with active gh auth' "$stderr_log"
grep -q 'gh auth status (automation token):' "$stderr_log"
grep -qx 'active-success' "$stdout_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" invalid-token-command \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'warning: automation gh token failed; retrying with active gh auth' "$stderr_log"
grep -qx 'active-success' "$stdout_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account active-command \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'warning: no automation gh token found; using active gh auth' "$stderr_log"
grep -q 'gh auth status (active gh auth):' "$stderr_log"
grep -qx 'active-success' "$stdout_log"

if PATH="$tmpdir:$PATH" GH_ISSUE_TEST_LOG="$log" \
	GH_ISSUE_GH="$tmpdir/rate-limited-gh" \
	"$repo_root/github/scripts/gh-issue" create "Rate limited" --repo owner/repo >"$stdout_log" 2>"$stderr_log" <<'EOF'; then
## Body
EOF
	echo "error: GH_ISSUE_GH override should surface the override failure" >&2
	exit 1
fi

if grep -q '^active:' "$log"; then
	echo "error: GH_ISSUE_GH override should not fall back to active gh" >&2
	exit 1
fi

mkdir -p "$tmpdir/github/scripts"
cp "$repo_root/github/scripts/gh-issue" "$tmpdir/github/scripts/gh-issue"
cp "$repo_root/github/scripts/gh-with-env-token" "$tmpdir/github/scripts/gh-with-env-token"

: >"$log"
PATH="$tmpdir:$PATH" GH_ISSUE_TEST_LOG="$log" GH_TOKEN=bot-token \
	GH_ISSUE_ENV_LOG="$env_log" GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$tmpdir/github/scripts/gh-issue" create "Rate limited" --repo owner/repo >"$stdout_log" 2>"$stderr_log" <<'EOF'
## Body
EOF

grep -q '^bot:issue create' "$log"
grep -q '^active:issue create' "$log"
grep -q 'GraphQL: API rate limit already exceeded' "$stderr_log"
grep -q 'retrying with active gh auth' "$stderr_log"
grep -q 'active-success' "$stdout_log"

cat >"$tmpdir/record-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue comment 9 --body-file '*' --repo owner/repo') printf 'commented\n' ;;
	'issue close 9 --repo owner/repo --reason completed') printf 'closed\n' ;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/record-gh"

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 9 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'
Closing with `literal markdown`.
EOF

grep -q '^issue comment 9 --body-file .*[[:space:]]--repo owner/repo$' "$log"
grep -qx 'issue close 9 --repo owner/repo --reason completed' "$log"
grep -qx 'commented' "$stdout_log"
grep -qx 'closed' <(tail -n 1 "$stdout_log")

cat >"$tmpdir/record-comment-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue comment 10 --body-file '*' --repo owner/repo') printf 'commented\n' ;;
	'issue close 10 --repo owner/repo --reason completed') printf 'closed\n' ;;
	'issue comment 11 --body-file '*' -Rowner/repo') printf 'commented\n' ;;
	'issue close 11 -Rowner/repo --reason completed') printf 'closed\n' ;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/record-comment-gh"

: >"$log"
GH_ISSUE_GH="$tmpdir/record-comment-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 10 --repo owner/repo \
	--comment "duplicate comment" --reason completed \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'
Closing with stdin body only.
EOF

grep -q '^issue comment 10 --body-file .*[[:space:]]--repo owner/repo$' "$log"
grep -qx 'issue close 10 --repo owner/repo --reason completed' "$log"
if grep -q -- '--comment' "$log"; then
	echo "error: gh-issue close should strip caller --comment passthrough" >&2
	exit 1
fi

: >"$log"
GH_ISSUE_GH="$tmpdir/record-comment-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 11 -Rowner/repo \
	-c"duplicate comment" --reason completed \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'
Closing with stdin body only.
EOF

grep -q '^issue comment 11 --body-file .*[[:space:]]-Rowner/repo$' "$log"
grep -qx 'issue close 11 -Rowner/repo --reason completed' "$log"
if grep -q -- '-cduplicate comment' "$log"; then
	echo "error: gh-issue close should strip attached caller -c passthrough" >&2
	exit 1
fi

cat >"$tmpdir/gh-noisy-json" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'warning: automation gh token failed; retrying with active gh auth\n' >&2
case "${1:-} ${2:-}" in
	'api --method')
		case "$*" in
			*'/pulls?state=open'*) printf '[{"number":1,"title":"open"}]\n' ;;
			*) printf '[]\n' ;;
		esac
		;;
	'pr view') printf '{"number":123,"title":"demo","isDraft":false}\n' ;;
	'pr list') printf '[{"number":1,"title":"open","isDraft":false}]\n' ;;
	'issue list') printf '[{"number":2,"title":"issue"}]\n' ;;
	'repo view') printf '{"nameWithOwner":"owner/repo","defaultBranchRef":{"name":"main"},"deleteBranchOnMerge":false}\n' ;;
	'run list') printf '[{"databaseId":3,"workflowName":"ci"}]\n' ;;
	*) printf '[]\n' ;;
esac
EOF
chmod +x "$tmpdir/gh-noisy-json"

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json |
	jq -e '
		.github.openPullRequests[0].number == 1 and
		.github.ghAvailable == 1 and
		.github.openPullRequests[0].isDraft == false and
		.github.openPullRequests[0].draft == false and
		.github.openPullRequests[0].mergeStateStatus == null and
		.github.openPullRequests[0].snapshotReadiness.degraded == true and
		.github.openPullRequests[0].snapshotReadiness.mergeReadinessAvailable == false and
		.launchplane.status == "configured" and
		.launchplane.service.contextUrlEnv == "LAUNCHPLANE_CONTEXT_URL" and
		.launchplane.service.operatorUrlEnv == "LAUNCHPLANE_OPERATOR_URL" and
		.launchplane.service.localConfigExample == "launchplane/references/launchplane-operator.local.example.json" and
		.launchplane.mergeTrain.readyLabel == "ready-to-merge" and
		.launchplane.mergeTrain.githubActionsRunner.workflow == "merge-train-runner.yml" and
		(.launchplane.warnings | length) == 0 and
		.cleanup.status == "configured" and
		.cleanup.routineCommands[0].name == "git status" and
		.cleanup.handoffArtifacts.durableSurface == "GitHub issue or PR comment" and
		(.cleanup.warnings | length) == 0
	' >/dev/null

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" |
	grep -A8 '^== Cleanup ==$' >"$tmpdir/cleanup-section.txt"

grep -qx 'status: configured' "$tmpdir/cleanup-section.txt"
grep -q '^routineCommands: git status, worktree list, merged local branches$' "$tmpdir/cleanup-section.txt"
grep -q '^handoffDurableSurface: GitHub issue or PR comment$' "$tmpdir/cleanup-section.txt"

cat >"$tmpdir/cleanup-policy.json" <<'EOF'
{
  "cleanup": {
    "commands": [
      {"name": "routine audit", "command": "git status --short", "when": "routine"},
      {"name": "cold prune", "command": "rm -rf .cache/example", "when": "explicit"},
      {"name": "broken"}
    ],
    "handoffArtifacts": {
      "temporaryGlobs": ["handoff*.md"],
      "durableSurface": "GitHub issue or PR comment"
    }
  }
}
EOF

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json --config "$tmpdir/cleanup-policy.json" |
	jq -e '
		.cleanup.status == "configured" and
		(.cleanup.commands | length) == 3 and
		(.cleanup.routineCommands | length) == 1 and
		.cleanup.routineCommands[0].name == "routine audit" and
		.cleanup.handoffArtifacts.temporaryGlobs[0] == "handoff*.md" and
		([.cleanup.warnings[].code] | index("invalid_cleanup_command")) != null
	' >/dev/null

cat >"$tmpdir/incomplete-launchplane.json" <<'EOF'
{
  "defaultBranch": "main",
  "launchplane": {
    "enabled": true,
    "context": {"enabled": true},
    "operator": {"enabled": true},
    "mergeTrain": {"enabled": true}
  }
}
EOF

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json --config "$tmpdir/incomplete-launchplane.json" |
	jq -e '
		.launchplane.status == "configured" and
		.launchplane.enabled == true and
		([.launchplane.warnings[].code] | sort) == [
			"missing_actions_runner",
			"missing_context_helper",
			"missing_context_url_env",
			"missing_operator_helper",
			"missing_operator_url_env",
			"missing_ready_label"
		]
	' >/dev/null

cat >"$tmpdir/committed-launchplane-url.json" <<'EOF'
{
  "defaultBranch": "main",
  "launchplane": {
    "enabled": true,
    "service": {
      "publicUrl": "https://launchplane.example.invalid",
      "contextUrlEnv": "LAUNCHPLANE_CONTEXT_URL",
      "operatorUrlEnv": "LAUNCHPLANE_OPERATOR_URL"
    }
  }
}
EOF

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json --config "$tmpdir/committed-launchplane-url.json" |
	jq -e '
		.launchplane.status == "configured" and
		([.launchplane.warnings[].code] | index("committed_service_url")) != null
	' >/dev/null

snapshot_config="$tmpdir/snapshot-config.json"
cat >"$snapshot_config" <<'EOF'
{
  "githubSettings": {
    "expected": {
      "deleteBranchOnMerge": true
    }
  }
}
EOF

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json --config "$snapshot_config" |
	jq -e '
		.github.repositorySettings.available == true and
		.github.repositorySettings.actual.deleteBranchOnMerge == false and
		.github.repositorySettings.expected.deleteBranchOnMerge == true and
		.github.repositorySettings.warnings[0].key == "deleteBranchOnMerge" and
		.github.repositorySettings.warnings[0].severity == "warning"
	' >/dev/null

cat >"$tmpdir/gh-repo-view-fails" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-} ${2:-}" in
	'repo view')
		printf 'repo settings unavailable\n' >&2
		exit 1
		;;
	*) printf '[]\n' ;;
esac
EOF
chmod +x "$tmpdir/gh-repo-view-fails"

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-repo-view-fails" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --config "$snapshot_config" |
	grep -q 'warning: repositorySettings - Repository settings could not be read; do not treat configured expectations as verified.'

echo "ok validate-gh-issue"
