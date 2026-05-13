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

cat >"$tmpdir/gh-noisy-json" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'warning: automation gh token failed; retrying with active gh auth\n' >&2
case "${1:-} ${2:-}" in
	'pr view') printf '{"number":123,"title":"demo"}\n' ;;
	'pr list') printf '[{"number":1,"title":"open"}]\n' ;;
	'issue list') printf '[{"number":2,"title":"issue"}]\n' ;;
	'run list') printf '[{"databaseId":3,"workflowName":"ci"}]\n' ;;
	*) printf '[]\n' ;;
esac
EOF
chmod +x "$tmpdir/gh-noisy-json"

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-noisy-json" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json |
	jq -e '.github.openPullRequests[0].number == 1 and .github.ghAvailable == 1' >/dev/null

echo "ok validate-gh-issue"
