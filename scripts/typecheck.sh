#!/usr/bin/env bash
set -euo pipefail

typecheck_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$typecheck_repo_root"
uv tool run --from ty==0.0.90 ty check --error-on-warning skills/jetbrains-inspection/scripts/jb-inspect.py
