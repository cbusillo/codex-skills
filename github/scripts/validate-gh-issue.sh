#!/usr/bin/env bash
set -euo pipefail
export GITHUB_RETRY_MAX_ATTEMPTS=1

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
stdin_log="$tmpdir/stdin.log"

assert_helper_envelope() {
	local file="$1"
	local operation="$2"
	local expected_body="$3"
	python3 - "$file" "$operation" "$expected_body" <<'PY'
import json
import pathlib
import sys

lines = [line for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
assert len(lines) == 1, lines
payload = json.loads(lines[0])
assert payload["schema_version"] == 1, payload
assert payload["ok"] is True, payload
assert payload["exit_code"] == 0, payload
assert payload["operation"] == sys.argv[2], payload
assert payload["body"] == sys.argv[3], payload
PY
}

assert_failure_envelope() {
	local file="$1"
	local operation="$2"
	local cause="$3"
	local failed_step="$4"
	local write_outcome="$5"
	python3 - "$file" "$operation" "$cause" "$failed_step" "$write_outcome" <<'PY'
import json
import pathlib
import sys

lines = [line for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if line.strip()]
assert len(lines) == 1, lines
payload = json.loads(lines[0])
assert payload["schema_version"] == 1, payload
assert payload["ok"] is False, payload
assert payload["exit_code"] != 0, payload
assert payload["operation"] == sys.argv[2], payload
assert payload["failure"]["cause"] == sys.argv[3], payload
assert payload["failed_step"] == sys.argv[4], payload
assert payload["write_outcome"] == sys.argv[5], payload
PY
}

assert_marked_body() {
	local expected_file="$1"
	local actual_file="$2"
	python3 - "$expected_file" "$actual_file" <<'PY'
import pathlib
import re
import sys

expected = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
actual = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
assert actual.startswith(expected), (expected, actual)
suffix = actual[len(expected):]
separator = "" if expected.endswith("\n") else "\n"
assert re.fullmatch(
    rf"{re.escape(separator)}\n<!-- github-skill-operation:[0-9a-f]{{32}} -->\n",
    suffix,
), suffix
PY
}

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
elif [[ "${1:-}" == "api" && "$*" == *"--method GET"* && "$*" == *"/user"* ]]; then
	if [[ "${GH_TOKEN:-}" == "invalid-write-token" ]]; then
		printf 'HTTP/2.0 401 Unauthorized\r\ncontent-type: application/json\r\n\r\n{"message":"Bad credentials"}\n'
		printf 'gh: HTTP 401\n' >&2
		exit 1
	elif [[ "${GH_TOKEN:-}" == "provider-failure-token" ]]; then
		printf 'HTTP/2.0 503 Service Unavailable\r\ncontent-type: text/html\r\nx-github-request-id: TEST-503\r\n\r\n<!DOCTYPE html><title>Unicorn! &middot; GitHub</title><p>Sorry about that.</p>\n'
		printf 'gh: HTTP 503\n' >&2
		exit 1
	elif [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n{"login":"shiny-code-bot"}\n'
	else
		printf 'HTTP/2.0 200 OK\r\ncontent-type: application/json\r\n\r\n{"login":"cbusillo"}\n'
	fi
elif [[ "${1:-}" == "api" && "${2:-}" == "user" ]]; then
	if [[ "${GH_TOKEN:-}" == "invalid-write-token" ]]; then
		printf 'Bad credentials\n' >&2
		exit 1
	elif [[ "${GH_TOKEN:-}" == "provider-failure-token" ]]; then
		printf "invalid character '<' looking for beginning of value\n" >&2
		exit 1
	elif [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'shiny-code-bot\n'
	else
		printf 'cbusillo\n'
	fi
elif [[ "${1:-}" == "rate-limited-command" ]]; then
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'HTTP/2.0 403 \\r\\ncontent-type: application/json\\r\\n\\r\\n{\"message\":\"API rate limit exceeded\"}\\n'
		printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
		exit 1
	fi
	printf 'active-success\n'
elif [[ "${1:-}" == "invalid-token-command" ]]; then
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'HTTP/2.0 401 \\r\\ncontent-type: application/json\\r\\n\\r\\n{\"message\":\"Bad credentials\"}\\n'
		printf 'X Failed to log in to github.com using token (GH_TOKEN)\n' >&2
		printf 'The token in GH_TOKEN is invalid.\n' >&2
		exit 1
	fi
	printf 'active-success\n'
elif [[ "${1:-}" == "api" && "${2:-}" == "graphql" ]]; then
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'bot:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
		printf 'HTTP/2.0 200 OK\r\n'
		printf 'content-type: application/json\r\n'
		printf '\r\n'
		printf '{"data":{"updateIssue":{"clientMutationId":"applied"}},"errors":[{"type":"RATE_LIMITED","message":"Rate limited"}]}\n'
		printf 'GraphQL mutation returned a rate-limit error.\n' >&2
		exit 1
	fi
	printf 'active:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
	printf 'active-success\n'
elif [[ "${1:-}" == "api" && "$*" == *"/repos/owner/repo/issues/1/comments"* ]]; then
	if [[ -n "${GH_TOKEN:-}" ]]; then
		cat >"${GH_ISSUE_STDIN_LOG}.bot"
		printf 'HTTP/2.0 403 \\r\\ncontent-type: application/json\\r\\n\\r\\n{\"message\":\"API rate limit exceeded\"}\\n'
		printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
		exit 1
	fi
	cat >"$GH_ISSUE_STDIN_LOG"
	printf '{"html_url":"https://github.com/owner/repo/issues/1#issuecomment-1"}\n'
elif [[ "${1:-}" == "secret-error-command" ]]; then
	printf 'transport failed token=synthetic-secret\n' >&2
	exit 1
elif [[ "${1:-}" == "active-command" ]]; then
	if [[ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}${CODEX_GITHUB_TOKEN:-}" ]]; then
		printf 'expected active auth without token env vars\n' >&2
		exit 1
	fi
	printf 'active-success\n'
elif [[ "${1:-}" == "pr" && "${2:-}" == "comment" ]]; then
	printf '%s\n' "${GH_TOKEN:-}" >"$GH_ISSUE_ENV_LOG"
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'bot:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
		printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
		exit 1
	fi
	printf 'active:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
	printf 'active-success\n'
elif [[ "${1:-}" == "-R" && "${3:-}" == "run" && "${4:-}" == "rerun" ]]; then
	printf '%s\n' "${GH_TOKEN:-}" >"$GH_ISSUE_ENV_LOG"
	if [[ -n "${GH_TOKEN:-}" ]]; then
		printf 'bot:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
		printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
		exit 1
	fi
	printf 'active:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
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
code_home_dir="$tmpdir/.chris-code"
codex_home_dir="$tmpdir/.codex"
mkdir -p "$code_home_dir" "$codex_home_dir"
printf 'CODEX_GITHUB_TOKEN=code-home-token\n' >"$code_home_dir/local.env"
printf 'CODEX_GITHUB_TOKEN=codex-home-token\n' >"$codex_home_dir/local.env"
generated_worktree="$tmpdir/.code/working/codex-skills/branches/review"
mkdir -p "$generated_worktree/github/scripts"
cp "$repo_root/github/scripts/gh-with-env-token" "$generated_worktree/github/scripts/gh-with-env-token"

: >"$env_log"
env -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN -u CODE_HOME -u CODEX_HOME \
	PATH="$tmpdir:$PATH" \
	HOME="$tmpdir" \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/path-gh" \
	"$generated_worktree/github/scripts/gh-with-env-token" auth status >/dev/null

grep -qx 'workspace-token' "$env_log"

: >"$env_log"
env -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN \
	PATH="$tmpdir:$PATH" \
	HOME="$tmpdir" \
	CODE_HOME="$code_home_dir" \
	CODEX_HOME="$codex_home_dir" \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/path-gh" \
	"$repo_root/github/scripts/gh-with-env-token" auth status >/dev/null

grep -qx 'code-home-token' "$env_log"

: >"$env_log"
env -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN -u CODE_HOME \
	PATH="$tmpdir:$PATH" \
	HOME="$tmpdir" \
	CODEX_HOME="$codex_home_dir" \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/path-gh" \
	"$repo_root/github/scripts/gh-with-env-token" auth status >/dev/null

grep -qx 'codex-home-token' "$env_log"

: >"$env_log"
env -u HOME -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN -u CODE_HOME -u CODEX_HOME \
	PATH="$tmpdir:$PATH" \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/path-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" auth status >/dev/null

grep -qx '' "$env_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_TOKEN=exhausted-token GITHUB_TOKEN=github-token CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" auth status

grep -qx 'codex-token' "$env_log"

cat >"$tmpdir/record-git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
if [[ "$1" == "commit" ]]; then
	printf 'author=%s <%s> committer=%s <%s>\n' \
		"${GIT_AUTHOR_NAME:-}" "${GIT_AUTHOR_EMAIL:-}" \
		"${GIT_COMMITTER_NAME:-}" "${GIT_COMMITTER_EMAIL:-}" \
		>>"$GH_ISSUE_ENV_LOG"
elif [[ "$1 $2 $3" == "remote get-url origin" ]]; then
	printf 'git@github.com:owner/repo.git\n'
elif [[ "$1 $2 $3" == "remote set-url origin" ]]; then
	printf 'remote=%s\n' "$4" >>"$GH_ISSUE_ENV_LOG"
elif [[ "$1" == "push" ]]; then
	printf 'askpass=%s prompt=%s token=%s\n' \
		"${GIT_ASKPASS:-}" "${GIT_TERMINAL_PROMPT:-}" \
		"${GIT_PUSH_AS_BOT_TOKEN:-}" >>"$GH_ISSUE_ENV_LOG"
fi
EOF
chmod +x "$tmpdir/record-git"

: >"$env_log"
PATH="$tmpdir:$PATH" GIT_COMMIT_AS_BOT_GIT="$tmpdir/record-git" GH_ISSUE_TEST_LOG="$log" \
	GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/git-commit-as-bot" -m "bot commit" >/dev/null

grep -q 'author=shiny-code-bot <chris@shinycomputers.com> committer=shiny-code-bot <chris@shinycomputers.com>' "$env_log"

: >"$env_log"
PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token GIT_PUSH_AS_BOT_GIT="$tmpdir/record-git" \
	GH_ISSUE_TEST_LOG="$log" \
	GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/git-push-as-bot" -u origin branch >/dev/null

grep -q '^remote=https://github.com/owner/repo.git$' "$env_log"
grep -q '^askpass=.* prompt=0 token=codex-token$' "$env_log"
grep -q '^remote=git@github.com:owner/repo.git$' "$env_log"

: >"$env_log"
PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_TOKEN=exhausted-token CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account token-command \
	>"$stdout_log" 2>"$stderr_log"

grep -qx 'codex-token' "$env_log"
grep -q 'Logged in to github.com account code-bot' "$stderr_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account rate-limited-command \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: rate-limited reads must not change actor without explicit authorization" >&2
	exit 1
fi

grep -q 'error: automation gh request was rate-limited; refusing to use active local gh auth' "$stderr_log"
grep -q 'GraphQL: API rate limit already exceeded' "$stderr_log"
grep -q 'gh auth status (automation token):' "$stderr_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" rate-limited-command \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'warning: automation gh request was rate-limited; explicitly authorized active-auth fallback; retrying with the active gh account' "$stderr_log"
grep -q "active gh account 'cbusillo'" "$stderr_log"
grep -qx 'active-success' "$stdout_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" -R owner/repo run rerun 123 --failed \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: write commands must not fall back to active gh auth by default" >&2
	exit 1
fi

grep -q 'error: automation gh request was rate-limited; refusing to use active local gh auth' "$stderr_log"
if grep -q '^active:-R owner/repo run rerun' "$log"; then
	echo "error: run rerun should not fall back to active gh auth" >&2
	exit 1
fi

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" pr comment 1 --body-file body.md \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'explicitly authorized active-auth fallback; retrying with the active gh account' "$stderr_log"
grep -qx 'active-success' "$stdout_log"

comment_json='{"body":"Line one\\n`literal` ${NOT_EXPANDED}"}'
printf '%s' "$comment_json" >"$tmpdir/expected-stdin"
printf '%s' "$comment_json" | PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" GH_ISSUE_STDIN_LOG="$stdin_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" api --method POST \
	/repos/owner/repo/issues/1/comments --input - \
	>"$stdout_log" 2>"$stderr_log"

cmp "$tmpdir/expected-stdin" "${stdin_log}.bot"
cmp "$tmpdir/expected-stdin" "$stdin_log"
grep -q 'explicitly authorized active-auth fallback; retrying with the active gh account' "$stderr_log"
grep -q '#issuecomment-1' "$stdout_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" api repos/owner/repo/issues -f title=test \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: api writes must not fall back to active gh auth without a bot token" >&2
	exit 1
fi

grep -q 'no automation gh token found; refusing to use active local gh auth' "$stderr_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" invalid-token-command \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: invalid automation auth must not change actor without explicit authorization" >&2
	exit 1
fi

grep -q 'error: automation gh authentication failed; refusing to use active local gh auth' "$stderr_log"
grep -q 'The token in GH_TOKEN is invalid' "$stderr_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" invalid-token-command \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'warning: automation gh authentication failed; explicitly authorized active-auth fallback; retrying with the active gh account' "$stderr_log"
grep -q "active gh account 'cbusillo'" "$stderr_log"
grep -qx 'active-success' "$stdout_log"

printf 'CODEX_GITHUB_TOKEN=invalid-write-token\n' >"$tmpdir/invalid-write.env"
: >"$log"
if env -u GH_TOKEN -u GITHUB_TOKEN -u CODEX_GITHUB_TOKEN \
	PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/invalid-write.env" \
	GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" issue edit 1 --title test \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: invalid write auth must fail during actor verification" >&2
	exit 1
fi

grep -q 'Bad credentials' "$stderr_log"
grep -q 'unable to verify the automation GitHub actor; refusing write' "$stderr_log"
if grep -q 'issue edit 1 --title test' "$log"; then
	echo "error: invalid write auth reached the GitHub mutation" >&2
	exit 1
fi

: >"$log"
if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" api graphql \
	-f 'query=mutation UpdateIssue { updateIssue(input: {}) { clientMutationId } }' \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: unknown-outcome writes must not replay under active auth" >&2
	exit 1
fi

grep -q '^bot:api graphql' "$log"
if grep -q '^active:api graphql' "$log"; then
	echo "error: unknown-outcome GraphQL mutation was replayed under active auth" >&2
	exit 1
fi
grep -q 'write outcome is unknown; refusing active-auth replay' "$stderr_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=codex-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" secret-error-command \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: synthetic transport failure should remain nonzero" >&2
	exit 1
fi

if grep -q 'synthetic-secret' "$stderr_log"; then
	echo "error: wrapper stderr leaked a synthetic secret" >&2
	exit 1
fi
grep -q '\[REDACTED\]' "$stderr_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account active-command \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: missing automation auth must not use active auth without explicit authorization" >&2
	exit 1
fi

grep -q 'error: no automation gh token found; refusing to use active local gh auth' "$stderr_log"

PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$repo_root/github/scripts/gh-with-env-token" --print-auth-account active-command \
	>"$stdout_log" 2>"$stderr_log"

grep -q 'warning: no automation gh token found; explicitly authorized active-auth fallback; using the active gh account' "$stderr_log"
grep -q "active gh account 'cbusillo'" "$stderr_log"
grep -q 'gh auth status (active gh auth):' "$stderr_log"
grep -qx 'active-success' "$stdout_log"

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=provider-failure-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" api --method POST /__validation_noop__ \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: provider failure during actor verification must refuse the write" >&2
	exit 1
fi
grep -q 'Network or provider failure (status=503): Unicorn! · GitHub — Sorry about that.' "$stderr_log"
grep -q 'unable to verify the automation GitHub actor; refusing write' "$stderr_log"
if grep -q "invalid character '<'" "$stderr_log"; then
	echo "error: actor verification leaked the gh HTML parse error" >&2
	exit 1
fi

if PATH="$tmpdir:$PATH" CODEX_SKILLS_ENV_FILE="$tmpdir/missing.env" \
	CODEX_GITHUB_TOKEN=provider-failure-token \
	GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	"$repo_root/github/scripts/gh-with-env-token" api user --jq .login \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: non-JSON provider response must remain nonzero" >&2
	exit 1
fi
grep -q 'GitHub returned a non-JSON provider response' "$stderr_log"
if grep -q "invalid character '<'" "$stderr_log"; then
	echo "error: legacy API classification leaked the raw HTML parse error" >&2
	exit 1
fi

cat >"$tmpdir/record-issue-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
payload=''
if [[ "$*" == *'--input -'* ]]; then payload="$(cat)"; fi
printf '%s | %s\n' "$*" "$payload" >>"$GH_ISSUE_TEST_LOG"
issue() {
	local number="$1"
	local state="${2:-open}"
	local reason="${3:-}"
	printf '{"id":90%s,"number":%s,"title":"Issue title","state":"%s","state_reason":%s,"html_url":"https://github.com/owner/repo/issues/%s","user":{"login":"shiny-code-bot"},"labels":[{"name":"plan"}],"assignees":[{"login":"shiny-code-bot"}],"milestone":{"number":7,"title":"Sprint 7"}}\n' \
		"$number" "$number" "$state" "$([[ -n "$reason" ]] && printf '"%s"' "$reason" || printf 'null')" "$number"
}
case "$*" in
	*'/user'*) printf '{"login":"shiny-code-bot"}\n' ;;
	*'/milestones?'*) printf '[{"number":7,"title":"Sprint 7"}]\n' ;;
	*'--method GET'*'/repos/owner/repo/issues?state=all'*) printf '[]\n' ;;
	*'--method POST'*'/repos/owner/repo/issues'*) issue 100 ;;
	*'--method PATCH'*'/repos/owner/repo/issues/42'*) issue 42 ;;
	*'--method POST'*'/repos/owner/repo/issues/42/labels'*) printf '[{"name":"enhancement"}]\n' ;;
	*'--method DELETE'*'/repos/owner/repo/issues/42/labels/plan'*) printf '[]\n' ;;
	*'--method POST'*'/repos/owner/repo/issues/42/assignees'*) issue 42 ;;
	*'--method DELETE'*'/repos/owner/repo/issues/42/assignees'*) issue 42 ;;
	*'--method GET'*'/repos/owner/repo/issues/42'*) issue 42 ;;
	*) printf 'unexpected gh args: %s\n' "$*" >&2; exit 1 ;;
esac
EOF
chmod +x "$tmpdir/record-issue-gh"

: >"$log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Body with `literal markdown` and $(do-not-run).\n' | \
	GH_ISSUE_GH="$tmpdir/record-issue-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" create "Issue title" --repo owner/repo \
	--label plan --assignee @me --milestone 'Sprint 7' \
	>"$stdout_log" 2>"$stderr_log"
assert_helper_envelope "$stdout_log" github.issue.create 'https://github.com/owner/repo/issues/100'
jq -e '.transport == "rest_api" and .operation_marker.kind == "request_fingerprint" and (.operation_marker.value | length) == 64 and (.operation_marker.operation_id | length) == 32' "$stdout_log" >/dev/null
python3 - "$log" <<'PY'
import json
import pathlib
import re
import sys

calls = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
create = next(
    line
    for line in calls
    if "--method POST" in line and "/repos/owner/repo/issues --input - |" in line
)
payload = json.loads(create.split(" | ", 1)[1])
assert payload["title"] == "Issue title", payload
assert payload["body"].startswith("Body with `literal markdown` and $(do-not-run).\n"), payload
assert re.search(r"<!-- github-skill-operation:[0-9a-f]{32} -->", payload["body"]), payload
assert payload["labels"] == ["plan"], payload
assert payload["assignees"] == ["shiny-code-bot"], payload
assert payload["milestone"] == 7, payload
PY

cat >"$tmpdir/create-503-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *'/user'* ]]; then
	printf '{"login":"shiny-code-bot"}\n'
elif [[ "$*" == *'--method GET'* && "$*" == *'/repos/owner/repo/issues?state=all'* ]]; then
	printf '[]\n'
else
	cat >/dev/null
	printf 'HTTP/2.0 503 Service Unavailable\r\ncontent-type: text/html\r\nx-github-request-id: CREATE-503\r\n\r\n<!DOCTYPE html><title>Unicorn! &middot; GitHub</title><p>Sorry about that.</p>\n'
	printf 'gh: HTTP 503\n' >&2
	exit 1
fi
EOF
chmod +x "$tmpdir/create-503-gh"

if printf 'Body\n' | GH_ISSUE_GH="$tmpdir/create-503-gh" \
	"$repo_root/github/scripts/gh-issue" create "Unknown outcome" --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: ambiguous REST create must fail closed" >&2
	exit 1
fi
assert_failure_envelope "$stdout_log" github.issue.create network_provider_failure create_issue unknown
jq -e '.request_id == "CREATE-503" and .reconciliation.required_before_retry == true and (.operation_marker.value | length) == 64 and (.operation_marker.operation_id | length) == 32' "$stdout_log" >/dev/null

: >"$log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Updated body with `literal markdown`.\n' | \
	GH_ISSUE_GH="$tmpdir/record-issue-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" edit 42 --repo owner/repo --title 'New title' \
	--add-label enhancement --remove-label plan --add-assignee octocat --remove-assignee @me --remove-milestone \
	>"$stdout_log" 2>"$stderr_log"
assert_helper_envelope "$stdout_log" github.issue.edit 'https://github.com/owner/repo/issues/42'
jq -e '.completed_steps == ["resolve_actor","edit_issue_fields","add_labels","remove_label","add_assignees","remove_assignees","read_after_write"]' "$stdout_log" >/dev/null
grep -q -- '--method PATCH.* /repos/owner/repo/issues/42' "$log"
grep -q -- '--method POST.* /repos/owner/repo/issues/42/labels' "$log"
grep -q -- '--method DELETE.* /repos/owner/repo/issues/42/labels/plan' "$log"
grep -q -- '--method POST.* /repos/owner/repo/issues/42/assignees' "$log"
grep -q -- '--method DELETE.* /repos/owner/repo/issues/42/assignees' "$log"
grep -q -- '--method GET.* /repos/owner/repo/issues/42' "$log"

cat >"$tmpdir/record-comment-helper-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
payload=''
if [[ "$*" == *'--input -'* ]]; then
	payload="$(cat)"
fi
printf '%s | %s\n' "$*" "$payload" >>"$GH_ISSUE_TEST_LOG"
write_body() {
	python3 -c 'import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.load(sys.stdin)["body"], encoding="utf-8")' \
		"$GH_ISSUE_ENV_LOG" <<<"$payload"
}
case "$*" in
	*'/user'*)
		printf '{"login":"shiny-code-bot"}\n'
		;;
	*'--method GET'*'/repos/owner/repo/issues/42/comments?'*)
		printf '[{"id":7,"html_url":"https://github.com/owner/repo/issues/42#issuecomment-7","user":{"login":"shiny-code-bot"},"created_at":"2026-07-16T12:00:00Z"}]\n'
		;;
	*'--method PATCH'*'/repos/owner/repo/issues/comments/7'*)
		write_body
		printf '{"id":7,"html_url":"https://github.com/owner/repo/issues/42#issuecomment-7","user":{"login":"shiny-code-bot"}}\n'
		;;
	*'--method POST'*'/repos/owner/repo/issues/42/comments'*)
		write_body
		printf '{"id":1,"html_url":"https://github.com/owner/repo/issues/42#issuecomment-1","user":{"login":"shiny-code-bot"}}\n'
		;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/record-comment-helper-gh"

: >"$log"
: >"$env_log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Issue comment with `literal markdown`.\n' | \
	GH_COMMENT_GH="$tmpdir/record-comment-helper-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-comment" issue 42 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"
grep -q '/user' "$log"
grep -q -- '--method POST' "$log"
grep -q '/repos/owner/repo/issues/42/comments' "$log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Issue comment with `literal markdown`.\n' >"$tmpdir/expected-comment-body"
assert_marked_body "$tmpdir/expected-comment-body" "$env_log"
assert_helper_envelope "$stdout_log" github.comment.issue 'https://github.com/owner/repo/issues/42#issuecomment-1'
jq -e '.comment_action == "created" and .actor == "shiny-code-bot"' "$stdout_log" >/dev/null

: >"$log"
: >"$env_log"
printf 'Updated PR comment.\n' | \
	GH_COMMENT_GH="$tmpdir/record-comment-helper-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-comment" pr 42 --repo owner/repo --edit-last --create-if-none \
	>"$stdout_log" 2>"$stderr_log"
grep -q -- '--method GET' "$log"
grep -q '/repos/owner/repo/issues/42/comments?per_page=100&page=1' "$log"
grep -q -- '--method PATCH' "$log"
grep -q '/repos/owner/repo/issues/comments/7' "$log"
printf 'Updated PR comment.\n' >"$tmpdir/expected-pr-comment-body"
cmp "$tmpdir/expected-pr-comment-body" "$env_log"
assert_helper_envelope "$stdout_log" github.comment.pr 'https://github.com/owner/repo/issues/42#issuecomment-7'
jq -e '.comment_action == "updated" and .comment.id == 7' "$stdout_log" >/dev/null

cat >"$tmpdir/record-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
payload=''
if [[ "$*" == *'--input -'* ]]; then
	payload="$(cat)"
fi
printf '%s | %s\n' "$*" "$payload" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	*'/user'*) printf '{"login":"shiny-code-bot"}\n' ;;
	*'--method GET'*'/repos/owner/repo/issues/41'*) printf '{"id":9041,"number":41,"html_url":"https://github.com/owner/repo/issues/41"}\n' ;;
	*'--method GET'*'/comments?'*) printf '[]\n' ;;
	*'--method GET'*'/repos/owner/repo/issues/'*)
		number=''
		for arg in "$@"; do
			case "$arg" in
			/repos/owner/repo/issues/*) number="${arg##*/}" ;;
			esac
		done
		printf '{"id":90%s,"number":%s,"title":"Issue title","state":"open","state_reason":null,"html_url":"https://github.com/owner/repo/issues/%s","user":{"login":"shiny-code-bot"},"labels":[],"assignees":[],"milestone":null}\n' \
			"$number" "$number" "$number"
		;;
	*'--method POST'*'/comments'*)
		printf '%s' "$payload" | jq -rj .body >"$GH_ISSUE_ENV_LOG"
		printf '{"id":1,"html_url":"https://github.com/owner/repo/issues/1#issuecomment-1","user":{"login":"shiny-code-bot"}}\n'
		;;
	*'--method PATCH'*'/repos/owner/repo/issues/'*)
		number=''
		for arg in "$@"; do
			case "$arg" in
			/repos/owner/repo/issues/*) number="${arg##*/}" ;;
			esac
		done
		state="$(printf '%s' "$payload" | jq -r .state)"
		reason="$(printf '%s' "$payload" | jq -r .state_reason)"
		printf '{"id":90%s,"number":%s,"title":"Issue title","state":"%s","state_reason":"%s","html_url":"https://github.com/owner/repo/issues/%s","user":{"login":"shiny-code-bot"},"labels":[],"assignees":[],"milestone":null}\n' \
			"$number" "$number" "$state" "$reason" "$number"
		;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
	;;
esac
EOF
chmod +x "$tmpdir/record-gh"

: >"$log"
: >"$env_log"
printf '%s' "Closing with \`literal markdown\`." | GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 9 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log"

grep -q '/repos/owner/repo/issues/9/comments' "$log"
grep -q -- '--method PATCH.* /repos/owner/repo/issues/9' "$log"
grep -q '"state": "closed".*"state_reason": "completed"' "$log"
printf '%s' "Closing with \`literal markdown\`." >"$tmpdir/expected-close-comment"
assert_marked_body "$tmpdir/expected-close-comment" "$env_log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/9'
jq -e '.completed_steps == ["post_close_comment", "close_issue"]' "$stdout_log" >/dev/null

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 90 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log" </dev/null

grep -q -- '--method PATCH.* /repos/owner/repo/issues/90' "$log"
if grep -q '/comments' "$log"; then
	echo "error: close without a comment must not call the comment REST endpoint" >&2
	exit 1
fi
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/90'
jq -e '.completed_steps == ["close_issue"]' "$stdout_log" >/dev/null

: >"$log"
: >"$env_log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 91 --repo owner/repo --comment caller-supplied \
	>"$stdout_log" 2>"$stderr_log" </dev/null

grep -q '/repos/owner/repo/issues/91/comments' "$log"
grep -q -- '--method PATCH.* /repos/owner/repo/issues/91' "$log"
printf '%s' 'caller-supplied' >"$tmpdir/expected-caller-comment"
assert_marked_body "$tmpdir/expected-caller-comment" "$env_log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/91'

: >"$log"
: >"$env_log"
printf 'Line one\nLine two\n\n' | GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 92 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"

printf 'Line one\nLine two\n\n' >"$tmpdir/expected-comment-bytes"
assert_marked_body "$tmpdir/expected-comment-bytes" "$env_log"
grep -q -- '--method PATCH.* /repos/owner/repo/issues/92' "$log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/92'

: >"$log"
: >"$env_log"
printf '%s' 'Closing with stdin body only.' | GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 10 --repo owner/repo \
	--comment "duplicate comment" --reason completed \
	>"$stdout_log" 2>"$stderr_log"

grep -q -- '--method PATCH.* /repos/owner/repo/issues/10' "$log"
printf '%s' 'Closing with stdin body only.' >"$tmpdir/expected-stdin-comment"
assert_marked_body "$tmpdir/expected-stdin-comment" "$env_log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/10'
if grep -q 'duplicate comment' "$log"; then
	echo "error: gh-issue close should prefer the stdin body over caller --comment" >&2
	exit 1
fi

: >"$log"
: >"$env_log"
printf '%s' 'Closing with stdin body only.' | GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 11 -Rowner/repo \
	-c"duplicate comment" --reason completed \
	>"$stdout_log" 2>"$stderr_log"

grep -q -- '--method PATCH.* /repos/owner/repo/issues/11' "$log"
assert_marked_body "$tmpdir/expected-stdin-comment" "$env_log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/11'
if grep -q -- '-cduplicate comment' "$log"; then
	echo "error: gh-issue close should strip attached caller -c passthrough" >&2
	exit 1
fi

cat >"$tmpdir/failing-close-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
payload=''
if [[ "$*" == *'--input -'* ]]; then payload="$(cat)"; fi
printf '%s | %s\n' "$*" "$payload" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	*'/user'*) printf '{"login":"shiny-code-bot"}\n' ;;
	*'--method GET'*'/repos/owner/repo/issues/12/comments?'*) printf '[]\n' ;;
	*'--method POST'*'/repos/owner/repo/issues/12/comments'*)
		printf '%s' "$payload" | jq -rj .body >"$GH_ISSUE_ENV_LOG"
		printf '{"id":12,"html_url":"https://github.com/owner/repo/issues/12#issuecomment-12","user":{"login":"shiny-code-bot"}}\n'
		;;
	*'--method PATCH'*'/repos/owner/repo/issues/12'*)
		printf 'HTTP/2.0 503 Service Unavailable\r\ncontent-type: application/json\r\nx-github-request-id: CLOSE-503\r\n\r\n{"message":"Service temporarily unavailable"}\n'
		printf 'gh: HTTP 503\n' >&2
		exit 1
		;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/failing-close-gh"

: >"$log"
: >"$env_log"
if printf '%s' 'Closing before a failing close command.' | GH_ISSUE_GH="$tmpdir/failing-close-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 12 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: gh-issue close should surface close failures" >&2
	exit 1
fi
assert_failure_envelope "$stdout_log" github.issue.close network_provider_failure close_issue unknown
jq -e '.completed_steps == ["post_close_comment"]' "$stdout_log" >/dev/null

grep -q '/repos/owner/repo/issues/12/comments' "$log"
grep -q -- '--method PATCH.* /repos/owner/repo/issues/12' "$log"
jq -e '.request_id == "CLOSE-503" and .reconciliation.strategy == "read_issue_and_compare_state"' "$stdout_log" >/dev/null

: >"$log"
: >"$env_log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 13 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'
Closing with a long streamed body.
EOF

grep -q '/repos/owner/repo/issues/13/comments' "$log"
grep -q -- '--method PATCH.* /repos/owner/repo/issues/13' "$log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/13'
python3 - "$stdout_log" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["completed_steps"] == ["post_close_comment", "close_issue"], payload
PY

cat >"$tmpdir/failing-large-comment-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
payload=''
if [[ "$*" == *'--input -'* ]]; then payload="$(cat)"; fi
printf '%s | %s\n' "$*" "$payload" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	*'/user'*) printf '{"login":"shiny-code-bot"}\n' ;;
	*'--method GET'*'/repos/owner/repo/issues/14/comments?'*) printf '[]\n' ;;
	*'--method POST'*'/repos/owner/repo/issues/14/comments'*)
		printf 'comment failed\n' >&2
		exit 1
		;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/failing-large-comment-gh"

: >"$log"
if GH_ISSUE_GH="$tmpdir/failing-large-comment-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 14 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'; then
Closing with a long streamed body.
EOF
	echo "error: gh-issue close should not close if the streamed comment fails" >&2
	exit 1
fi
assert_failure_envelope "$stdout_log" github.issue.close network_provider_failure post_close_comment unknown

grep -q '/repos/owner/repo/issues/14/comments' "$log"
if grep -q -- '--method PATCH.* /repos/owner/repo/issues/14' "$log"; then
	echo "error: gh-issue close should stop before close after comment failure" >&2
	exit 1
fi

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" reopen 15 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log" </dev/null
grep -q -- '--method PATCH.* /repos/owner/repo/issues/15' "$log"
grep -q '"state": "open".*"state_reason": "reopened"' "$log"
assert_helper_envelope "$stdout_log" github.issue.reopen 'https://github.com/owner/repo/issues/15'
jq -e '.completed_steps == ["reopen_issue"]' "$stdout_log" >/dev/null

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 16 --repo owner/repo --duplicate-of 41 \
	>"$stdout_log" 2>"$stderr_log" </dev/null
grep -q -- '--method GET.* /repos/owner/repo/issues/41' "$log"
grep -q -- '--method PATCH.* /repos/owner/repo/issues/16' "$log"
grep -q '"state_reason": "duplicate".*"duplicate_issue_id": 9041' "$log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/16'
jq -e '.completed_steps == ["resolve_duplicate_issue", "close_issue"]' "$stdout_log" >/dev/null

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 17 --repo owner/repo --reason 'not planned' \
	>"$stdout_log" 2>"$stderr_log" </dev/null
grep -q -- '--method PATCH.* /repos/owner/repo/issues/17' "$log"
grep -q '"state_reason": "not_planned"' "$log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/17'
jq -e '.completed_steps == ["close_issue"]' "$stdout_log" >/dev/null

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close https://github.com/owner/repo/issues/18 \
	>"$stdout_log" 2>"$stderr_log" </dev/null
grep -q -- '--method PATCH.* /repos/owner/repo/issues/18' "$log"
assert_helper_envelope "$stdout_log" github.issue.close 'https://github.com/owner/repo/issues/18'
jq -e '.completed_steps == ["close_issue"]' "$stdout_log" >/dev/null

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" edit https://github.com/owner/repo/issues/19 --title 'URL target edit' \
	>"$stdout_log" 2>"$stderr_log" </dev/null
grep -q -- '--method PATCH.* /repos/owner/repo/issues/19' "$log"
grep -q -- '--method GET.* /repos/owner/repo/issues/19' "$log"
assert_helper_envelope "$stdout_log" github.issue.edit 'https://github.com/owner/repo/issues/19'

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" reopen 'owner/repo#20' \
	>"$stdout_log" 2>"$stderr_log" </dev/null
grep -q -- '--method PATCH.* /repos/owner/repo/issues/20' "$log"
assert_helper_envelope "$stdout_log" github.issue.reopen 'https://github.com/owner/repo/issues/20'
jq -e '.completed_steps == ["reopen_issue"]' "$stdout_log" >/dev/null

: >"$log"
if GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-issue" close 21 --repo owner/repo --reason not_planned --duplicate-of 41 \
	>"$stdout_log" 2>"$stderr_log" </dev/null; then
	echo "error: gh-issue close must reject --reason with --duplicate-of" >&2
	exit 1
fi
assert_failure_envelope "$stdout_log" github.issue.close validation_error argument_parsing not_started
if [[ -s "$log" ]]; then
	echo "error: conflicting close modes must fail before calling GitHub" >&2
	exit 1
fi

cat >"$tmpdir/gh-noisy-json" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf "warning: no automation gh token found; explicitly authorized active-auth fallback; using the active gh account 'ok'\n" >&2
emit() {
	printf 'HTTP/2.0 200 \n'
	printf 'content-type: application/json\n'
	printf 'x-github-request-id: SNAPSHOT:123\n'
	printf 'x-ratelimit-limit: 5000\n'
	printf 'x-ratelimit-remaining: 4999\n'
	printf 'x-ratelimit-reset: 1784304000\n'
	printf 'x-ratelimit-used: 1\n'
	printf 'x-ratelimit-resource: core\n\n'
	printf '%s\n' "$1"
}
case "${1:-} ${2:-}" in
	'api --method')
		case "$*" in
			*'/pulls?'*'head='*) emit '[{"number":123,"title":"demo","state":"open","draft":false,"html_url":"https://github.com/owner/repo/pull/123","labels":[],"head":{"ref":"feature","sha":"abc","repo":{"full_name":"owner/repo"}},"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}]' ;;
			*'/pulls?'*) emit '[{"number":1,"title":"open","state":"open","draft":false,"html_url":"https://github.com/owner/repo/pull/1","labels":[],"head":{"ref":"feature","sha":"abc","repo":{"full_name":"owner/repo"}},"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}]' ;;
			*'/issues?'*) emit '[{"number":2,"title":"issue","state":"open","labels":[],"html_url":"https://github.com/owner/repo/issues/2","updated_at":"2026-07-17T00:00:00Z"}]' ;;
			*'/actions/runs?'*) emit '{"workflow_runs":[{"id":3,"name":"ci","display_title":"CI run","status":"completed","conclusion":"success","head_branch":"main","head_sha":"abc","event":"push","created_at":"2026-07-17T00:00:00Z","html_url":"https://github.com/owner/repo/actions/runs/3"}]}' ;;
			*'/repos/'*) emit '{"full_name":"owner/repo","default_branch":"main","delete_branch_on_merge":false,"html_url":"https://github.com/owner/repo"}' ;;
			*) emit '[]' ;;
		esac
		;;
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
		.github.openIssues[0].number == 2 and
		.github.openIssues[0].updatedAt == "2026-07-17T00:00:00Z" and
		.github.recentRuns[0].databaseId == 3 and
		.github.recentRuns[0].workflowName == "ci" and
		.github.diagnostics.degraded == true and
		([.github.diagnostics.degradedComponents[]] | index("openIssues")) != null and
		.github.diagnostics.components.openIssues.requestId == "SNAPSHOT:123" and
		.github.diagnostics.components.openIssues.quota.remaining == 4999 and
		.github.diagnostics.components.openIssues.actor == "ok" and
		.github.diagnostics.components.openIssues.expectedActor == "shiny-code-bot" and
		([.github.diagnostics.components.openIssues.diagnostics.degradedComponents[]] | index("actor")) != null and
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

cat >"$tmpdir/gh-snapshot-rate-limited" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
emit() {
	local status="$1"
	local body="$2"
	printf 'HTTP/2.0 %s \n' "$status"
	printf 'content-type: application/json\n'
	printf 'x-github-request-id: SNAPSHOT:RATE\n'
	printf 'x-ratelimit-limit: 5000\n'
	printf 'x-ratelimit-remaining: %s\n' "${3:-4999}"
	printf 'x-ratelimit-reset: 1784304999\n'
	printf 'x-ratelimit-used: 1\n'
	printf 'x-ratelimit-resource: core\n'
	if [[ "$status" -eq 429 ]]; then
		printf 'retry-after: 60\n'
	fi
	printf '\n%s\n' "$body"
}
case "${1:-} ${2:-}" in
	'api --method')
		case "$*" in
			*'/pulls?'*) emit 200 '[]' ;;
			*'/issues?'*)
				emit 429 '{"message":"API rate limit exceeded"}' 0
				exit 1
				;;
			*'/actions/runs?'*) emit 200 '{"workflow_runs":[]}' ;;
			*'/repos/'*) emit 200 '{"full_name":"owner/repo","default_branch":"main","delete_branch_on_merge":false}' ;;
			*) emit 200 '[]' ;;
		esac
		;;
	*) emit 200 '[]' ;;
esac
EOF
chmod +x "$tmpdir/gh-snapshot-rate-limited"

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-snapshot-rate-limited" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json |
	jq -e '
		.github.openIssues.error.cause == "secondary_rate_limited" and
		.github.openIssues.error.retryable == true and
		.github.diagnostics.degraded == true and
		([.github.diagnostics.degradedComponents[]] | index("openIssues")) != null and
		.github.diagnostics.components.openIssues.status == 429 and
		.github.diagnostics.components.openIssues.retryable == true and
		.github.diagnostics.components.openIssues.diagnostics.requests[0].retryAfter == 60 and
		(.github.recentRuns | type) == "array"
	' >/dev/null

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-snapshot-rate-limited" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" >"$tmpdir/rate-limited-snapshot.txt"
grep -q '^warning: open issues unavailable - ' "$tmpdir/rate-limited-snapshot.txt"

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
emit() {
	local status="$1"
	local body="$2"
	printf 'HTTP/2.0 %s \n' "$status"
	printf 'content-type: application/json\n'
	printf 'x-github-request-id: SNAPSHOT:DENIED\n'
	printf 'x-ratelimit-limit: 5000\n'
	printf 'x-ratelimit-remaining: 4998\n'
	printf 'x-ratelimit-reset: 1784304000\n'
	printf 'x-ratelimit-used: 2\n'
	printf 'x-ratelimit-resource: core\n\n'
	printf '%s\n' "$body"
}
case "${1:-} ${2:-}" in
	'api --method')
		case "$*" in
			*'/pulls?'*) emit 200 '[]' ;;
			*'/issues?'*) emit 200 '[]' ;;
			*'/actions/runs?'*) emit 200 '{"workflow_runs":[]}' ;;
			*'/repos/'*)
				emit 403 '{"message":"Resource not accessible by integration"}'
				exit 1
				;;
			*) emit 200 '[]' ;;
		esac
		;;
	*) emit 200 '[]' ;;
esac
EOF
chmod +x "$tmpdir/gh-repo-view-fails"

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-repo-view-fails" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --config "$snapshot_config" |
	grep -q 'warning: repositorySettings - Repository settings could not be read; do not treat configured expectations as verified.'

GITHUB_REPO_SNAPSHOT_GH="$tmpdir/gh-repo-view-fails" \
	GITHUB_REPO_SNAPSHOT_PR_HELPER="$tmpdir/missing-gh-pr.py" \
	"$repo_root/github/scripts/github-repo-snapshot.sh" --json --config "$snapshot_config" |
	jq -e '
		.github.repositorySettings.available == false and
		.github.repositorySettings.error.cause == "permission_denied" and
		.github.diagnostics.degraded == true and
		([.github.diagnostics.degradedComponents[]] | index("repositorySettings")) != null and
		.github.diagnostics.components.repositorySettings.status == 403 and
		.github.diagnostics.components.repositorySettings.requestId == "SNAPSHOT:DENIED"
	' >/dev/null

echo "ok validate-gh-issue"
