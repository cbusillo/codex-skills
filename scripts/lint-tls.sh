#!/usr/bin/env bash
set -euo pipefail

tls_repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$tls_repo_root"
tls_paths=()
while IFS= read -r -d '' tls_path; do
  tls_paths+=("$tls_path")
done < <(git ls-files -z -- '*.py' '*.pyi' '*.ipynb')
if [[ -n "${tls_paths[0]-}" ]]; then
  uv tool run --from ruff==0.17.1 ruff check --select S501,S502,S503,S504,S323 --force-exclude -- "${tls_paths[@]}"
fi
scripts/run-python-helper.sh scripts/check_insecure_tls.py
