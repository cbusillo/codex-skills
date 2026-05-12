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

if PATH="$tmpdir:$PATH" GH_ISSUE_TEST_LOG="$log" \
	GH_ISSUE_GH="$tmpdir/rate-limited-gh" \
	"$repo_root/github/scripts/gh-issue" create "Rate limited" --repo owner/repo >"$stdout_log" 2>"$stderr_log" <<'EOF'
## Body
EOF
then
	echo "error: GH_ISSUE_GH override should surface the override failure" >&2
	exit 1
fi

if grep -q '^active:' "$log"; then
	echo "error: GH_ISSUE_GH override should not fall back to active gh" >&2
	exit 1
fi

cat >"$tmpdir/gh-with-env-token" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'bot:%s\n' "$*" >>"$GH_ISSUE_TEST_LOG"
printf 'GraphQL: API rate limit already exceeded for user ID 279560559.\n' >&2
exit 1
EOF
chmod +x "$tmpdir/gh-with-env-token"

mkdir -p "$tmpdir/github/scripts"
cp "$repo_root/github/scripts/gh-issue" "$tmpdir/github/scripts/gh-issue"
cp "$tmpdir/gh-with-env-token" "$tmpdir/github/scripts/gh-with-env-token"

: >"$log"
PATH="$tmpdir:$PATH" GH_ISSUE_TEST_LOG="$log" \
	"$tmpdir/github/scripts/gh-issue" create "Rate limited" --repo owner/repo >"$stdout_log" 2>"$stderr_log" <<'EOF'
## Body
EOF

grep -q '^bot:issue create' "$log"
grep -q '^active:issue create' "$log"
grep -q 'retrying with active gh auth' "$stderr_log"
grep -q 'https://github.com/owner/repo/issues/1' "$stdout_log"

echo "ok validate-gh-issue"
