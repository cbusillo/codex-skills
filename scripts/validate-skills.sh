#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 github/scripts/validate-gh-plan.py
github/scripts/validate-gh-issue.sh
python3 skill-creator/scripts/validate-skill-behavior.py
uv run skill-creator/scripts/quick_validate.py chronicle
