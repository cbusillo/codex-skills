#!/usr/bin/env bash
# Run this worktree's helper with its PEP 723 dependencies with a dependency-keyed environment, instead of retaining
# a script-path environment for every helper in every validation worktree.
set -euo pipefail
script=${1:?Usage: run-python-helper.sh SCRIPT [ARGS...]}
shift
exec uv run --no-project --with-requirements "$script" python "$script" "$@"
