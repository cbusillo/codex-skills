#!/usr/bin/env bash
set -euo pipefail

tls_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$tls_repo_root"
uv tool run --from ruff==0.17.1 ruff check --select S501,S502,S503,S504,S323 .
scripts/run-python-helper.sh scripts/check_insecure_tls.py
