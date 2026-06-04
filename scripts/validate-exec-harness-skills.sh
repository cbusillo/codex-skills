#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
harness="${CODE_EXEC_HARNESS:-$repo_root/../code/tools/code-exec-harness/harness.py}"

if [[ ! -f "$harness" ]]; then
	cat >&2 <<EOF
error: Every Code exec harness not found: $harness
Set CODE_EXEC_HARNESS to /path/to/code/tools/code-exec-harness/harness.py.
EOF
	exit 1
fi

if (($# == 0)); then
	cat >&2 <<'EOF'
usage: scripts/validate-exec-harness-skills.sh <scenario.json> [...]

Runs opt-in Every Code exec-harness scenarios against this skills checkout.
The harness is intentionally not part of validate-skills.sh because it depends
on a sibling Every Code checkout, a code binary, and sometimes local/auth setup.
EOF
	exit 2
fi

python3 "$harness" "$@" --skill-root "$repo_root"
