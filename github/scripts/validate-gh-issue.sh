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
elif [[ "${1:-}" == "api" && "${2:-}" == "user" ]]; then
	if [[ "${GH_TOKEN:-}" == "invalid-write-token" ]]; then
		printf 'Bad credentials\n' >&2
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

: >"$log"
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
assert_failure_envelope "$stdout_log" github.issue.create graphql_primary_rate_limited create_issue rejected

mkdir -p "$tmpdir/github/scripts"
cp "$repo_root/github/scripts/gh-issue" "$tmpdir/github/scripts/gh-issue"
cp "$repo_root/github/scripts/gh-with-env-token" "$tmpdir/github/scripts/gh-with-env-token"
cp "$repo_root/github/scripts/github_api.py" "$tmpdir/github/scripts/github_api.py"

: >"$log"
PATH="$tmpdir:$PATH" GH_ISSUE_TEST_LOG="$log" GH_TOKEN=bot-token \
	GH_ISSUE_ENV_LOG="$env_log" GH_WITH_ENV_TOKEN_GH="$tmpdir/env-gh" \
	GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK=1 \
	"$tmpdir/github/scripts/gh-issue" create "Rate limited" --repo owner/repo >"$stdout_log" 2>"$stderr_log" <<'EOF'
## Body
EOF

grep -q '^bot:issue create' "$log"
grep -q '^active:issue create' "$log"
grep -q 'GraphQL: API rate limit already exceeded' "$stderr_log"
grep -q 'explicitly authorized active-auth fallback; retrying with the active gh account' "$stderr_log"
assert_helper_envelope "$stdout_log" github.issue.create active-success

cat >"$tmpdir/record-edit-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2 $3" != "issue edit 42" || "$4" != "--body-file" || "$6 $7" != "--repo owner/repo" ]]; then
	printf 'unexpected gh args: %s\n' "$*" >&2
	exit 1
fi
cat "$5" >"$GH_ISSUE_TEST_LOG"
printf 'edited\n'
EOF
chmod +x "$tmpdir/record-edit-gh"

: >"$log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Updated body with `literal markdown`.\n' | GH_ISSUE_GH="$tmpdir/record-edit-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" edit 42 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"

# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Updated body with `literal markdown`.\n' >"$tmpdir/expected-edit-body"
cmp "$tmpdir/expected-edit-body" "$log"
assert_helper_envelope "$stdout_log" github.issue.edit edited

cat >"$tmpdir/record-comment-helper-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >"$GH_ISSUE_TEST_LOG"
if [[ "$4" != "--body-file" ]]; then
	printf 'missing --body-file: %s\n' "$*" >&2
	exit 1
fi
cat "$5" >"$GH_ISSUE_ENV_LOG"
printf 'https://github.com/owner/repo/issues/42#issuecomment-1\n'
EOF
chmod +x "$tmpdir/record-comment-helper-gh"

: >"$log"
: >"$env_log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Issue comment with `literal markdown`.\n' | \
	GH_COMMENT_GH="$tmpdir/record-comment-helper-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-comment" issue 42 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"
grep -q '^issue comment 42 --body-file .* --repo owner/repo$' "$log"
# shellcheck disable=SC2016 # literal Markdown backticks are intentional.
printf 'Issue comment with `literal markdown`.\n' >"$tmpdir/expected-comment-body"
cmp "$tmpdir/expected-comment-body" "$env_log"
assert_helper_envelope "$stdout_log" github.comment.issue 'https://github.com/owner/repo/issues/42#issuecomment-1'

: >"$log"
: >"$env_log"
printf 'Updated PR comment.\n' | \
	GH_COMMENT_GH="$tmpdir/record-comment-helper-gh" GH_ISSUE_TEST_LOG="$log" GH_ISSUE_ENV_LOG="$env_log" \
	"$repo_root/github/scripts/gh-comment" pr 42 --repo owner/repo --edit-last --create-if-none \
	>"$stdout_log" 2>"$stderr_log"
grep -q '^pr comment 42 --body-file .* --repo owner/repo --edit-last --create-if-none$' "$log"
printf 'Updated PR comment.\n' >"$tmpdir/expected-pr-comment-body"
cmp "$tmpdir/expected-pr-comment-body" "$env_log"
assert_helper_envelope "$stdout_log" github.comment.pr 'https://github.com/owner/repo/issues/42#issuecomment-1'

cat >"$tmpdir/record-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue close 9 --comment '*' --repo owner/repo --reason completed') printf 'closed\n' ;;
	'issue close 90 --repo owner/repo --reason completed') printf 'closed\n' ;;
	'issue close 91 --repo owner/repo --comment caller-supplied') printf 'closed\n' ;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/record-gh"

: >"$log"
printf '%s' "Closing with \`literal markdown\`." | GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 9 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log"

expected_close_line="issue close 9 --comment Closing with \`literal markdown\`. --repo owner/repo --reason completed"
grep -Fqx "$expected_close_line" "$log"
assert_helper_envelope "$stdout_log" github.issue.close closed

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 90 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log" </dev/null

grep -qx 'issue close 90 --repo owner/repo --reason completed' "$log"
assert_helper_envelope "$stdout_log" github.issue.close closed

: >"$log"
GH_ISSUE_GH="$tmpdir/record-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 91 --repo owner/repo --comment caller-supplied \
	>"$stdout_log" 2>"$stderr_log" </dev/null

grep -qx 'issue close 91 --repo owner/repo --comment caller-supplied' "$log"
assert_helper_envelope "$stdout_log" github.issue.close closed

cat >"$tmpdir/record-bytes-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2 $3" != "issue close 92" || "$4" != "--comment" ]]; then
	printf 'unexpected gh args: %s\n' "$*" >&2
	exit 1
fi
printf '%s' "$5" >"$GH_ISSUE_TEST_LOG"
printf 'closed\n'
EOF
chmod +x "$tmpdir/record-bytes-gh"

: >"$log"
printf 'Line one\nLine two\n\n' | GH_ISSUE_GH="$tmpdir/record-bytes-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 92 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"

printf 'Line one\nLine two\n\n' >"$tmpdir/expected-comment-bytes"
cmp "$tmpdir/expected-comment-bytes" "$log"
assert_helper_envelope "$stdout_log" github.issue.close closed

cat >"$tmpdir/record-comment-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue close 10 --comment '*' --repo owner/repo --reason completed') printf 'closed\n' ;;
	'issue close 11 --comment '*' -Rowner/repo --reason completed') printf 'closed\n' ;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/record-comment-gh"

: >"$log"
printf '%s' 'Closing with stdin body only.' | GH_ISSUE_GH="$tmpdir/record-comment-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 10 --repo owner/repo \
	--comment "duplicate comment" --reason completed \
	>"$stdout_log" 2>"$stderr_log"

grep -q '^issue close 10 --comment Closing with stdin body only\.[[:space:]]--repo owner/repo --reason completed$' "$log"
assert_helper_envelope "$stdout_log" github.issue.close closed
if grep -q -- '--comment' "$log"; then
	comment_count="$(grep -o -- '--comment' "$log" | wc -l | tr -d ' ')"
	if [[ "$comment_count" != "1" ]]; then
		echo "error: gh-issue close should strip caller --comment passthrough" >&2
		exit 1
	fi
fi

: >"$log"
printf '%s' 'Closing with stdin body only.' | GH_ISSUE_GH="$tmpdir/record-comment-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 11 -Rowner/repo \
	-c"duplicate comment" --reason completed \
	>"$stdout_log" 2>"$stderr_log"

grep -q '^issue close 11 --comment Closing with stdin body only\.[[:space:]]-Rowner/repo --reason completed$' "$log"
assert_helper_envelope "$stdout_log" github.issue.close closed
if grep -q -- '-cduplicate comment' "$log"; then
	echo "error: gh-issue close should strip attached caller -c passthrough" >&2
	exit 1
fi

cat >"$tmpdir/failing-close-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue close 12 --comment '*' --repo owner/repo')
		printf 'close failed\n' >&2
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
if printf '%s' 'Closing with one atomic close command.' | GH_ISSUE_GH="$tmpdir/failing-close-gh" GH_ISSUE_TEST_LOG="$log" \
	"$repo_root/github/scripts/gh-issue" close 12 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log"; then
	echo "error: gh-issue close should surface close failures" >&2
	exit 1
fi
assert_failure_envelope "$stdout_log" github.issue.close network_provider_failure close_issue unknown

grep -q '^issue close 12 --comment Closing with one atomic close command\.[[:space:]]--repo owner/repo$' "$log"
if grep -q '^issue comment ' "$log"; then
	echo "error: gh-issue close should not post a separate pre-close comment" >&2
	exit 1
fi

cat >"$tmpdir/large-close-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue comment 13 --body-file '*' --repo owner/repo') printf 'commented\n' ;;
	'issue close 13 --repo owner/repo --reason completed') printf 'closed\n' ;;
	*)
		printf 'unexpected gh args: %s\n' "$*" >&2
		exit 1
		;;
esac
EOF
chmod +x "$tmpdir/large-close-gh"

: >"$log"
GH_ISSUE_GH="$tmpdir/large-close-gh" GH_ISSUE_TEST_LOG="$log" \
	GH_ISSUE_CLOSE_COMMENT_ARG_MAX=8 \
	"$repo_root/github/scripts/gh-issue" close 13 --repo owner/repo --reason completed \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'
Closing with a long streamed body.
EOF

grep -q '^issue comment 13 --body-file .* --repo owner/repo$' "$log"
grep -qx 'issue close 13 --repo owner/repo --reason completed' "$log"
assert_helper_envelope "$stdout_log" github.issue.close $'commented\nclosed'
python3 - "$stdout_log" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["completed_steps"] == ["post_close_comment"], payload
PY
if grep -q -- '--comment' "$log"; then
	echo "error: large gh-issue close comments should not be inlined into argv" >&2
	exit 1
fi

cat >"$tmpdir/failing-large-comment-gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
case "$*" in
	'issue comment 14 --body-file '*' --repo owner/repo')
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
	GH_ISSUE_CLOSE_COMMENT_ARG_MAX=8 \
	"$repo_root/github/scripts/gh-issue" close 14 --repo owner/repo \
	>"$stdout_log" 2>"$stderr_log" <<'EOF'; then
Closing with a long streamed body.
EOF
	echo "error: gh-issue close should not close if the streamed comment fails" >&2
	exit 1
fi
assert_failure_envelope "$stdout_log" github.issue.close network_provider_failure post_close_comment unknown

grep -q '^issue comment 14 --body-file .* --repo owner/repo$' "$log"
if grep -q '^issue close 14' "$log"; then
	echo "error: gh-issue close should stop before close after comment failure" >&2
	exit 1
fi

cat >"$tmpdir/gh-noisy-json" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'warning: automation gh token failed; retrying with active gh auth; GitHub writes may appear as the active account\n' >&2
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
