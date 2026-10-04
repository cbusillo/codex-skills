#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

required_commands=(bash git gh jq node uv)
for required_command in "${required_commands[@]}"; do
	if ! command -v "$required_command" >/dev/null 2>&1; then
		printf 'error: required command is unavailable: %s\n' "$required_command" >&2
		exit 127
	fi
done

printf 'execution environment:\n'
printf '  '
bash --version | sed -n '1p'
printf '  '
git --version
printf '  '
gh --version | sed -n '1p'
printf '  jq '
jq --version
printf '  node '
node --version
printf '  '
uv --version
printf '  selected '
uv run python --version

uv run skills/github/scripts/validate-operation-matrix.py --self-test
uv run skills/github/scripts/validate-operation-matrix.py
uv run skills/skill-creator/scripts/validate-skill-behavior.py
uv run skills/skill-creator/scripts/validate-command-policy-simulator.py --self-test
uv run skills/skill-creator/scripts/validate-command-policy-simulator.py
uv run scripts/validate-public-safety.py --self-test
uv run scripts/validate-public-safety.py
uv run skills/launchplane/scripts/check-agent-operator-contract.py
uv run scripts/update_pep723_dependencies.py --check
uv run skills/skill-creator/scripts/quick_validate.py --self-test
uv run skills/skill-creator/scripts/validate-skill-repo.py

# --version does not initialize Node's ESM loader or the manual helper's
# builtins. Load them offline before parallel helpers compete for cold disk
# pages, keeping the symlink CLI regression's five-second deadline unchanged.
node --input-type=module -e 'await import("./skills/openai-docs/scripts/fetch-codex-manual.mjs")'

# Validators invoked by the dedicated commands above rather than by the helper loop.
explicit_helper_validators=(
	skills/github/scripts/validate-operation-matrix.py
	scripts/validate-public-safety.py
	skills/skill-creator/scripts/quick_validate.py
	skills/skill-creator/scripts/validate-command-policy-simulator.py
	skills/skill-creator/scripts/validate-skill-behavior.py
	skills/skill-creator/scripts/validate-skill-repo.py
)

# Files matching the helper names that are CLIs or fixtures requiring
# arguments or live context, so the loop below does not run them.
helper_test_skiplist=(
	skills/rollout-friction/scripts/validate_rollout_memory_llm_results.py
)

# Every tracked test_*.py or *validate*.py helper runs unless it is listed above,
# so a new helper cannot silently miss the gate and needs no list edit. The
# validators above scan the working tree, so they finish before this loop starts.
mapfile -t excluded_helper_tests < <(
	printf '%s\n' "${explicit_helper_validators[@]}" "${helper_test_skiplist[@]}" | sort
)
mapfile -t helper_tests < <(
	git ls-files --cached '*test_*.py' '*validate*.py' | sort | comm -23 - <(printf '%s\n' "${excluded_helper_tests[@]}")
)
helper_tests+=(skills/github/scripts/validate-gh-issue.sh)

if ((${#helper_tests[@]} == 0)); then
	printf 'error: no helper tests discovered\n' >&2
	exit 1
fi

# Helpers are independent, so run them in parallel and print each log only when
# it fails. VALIDATE_SKILLS_JOBS=1 restores serial execution.
helper_jobs="${VALIDATE_SKILLS_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"
helper_log_dir="$(mktemp -d)"
trap 'rm -rf "$helper_log_dir"' EXIT
export helper_log_dir
if ! printf '%s\0' "${helper_tests[@]}" | xargs -0 -n 1 -P "$helper_jobs" bash -c '
	log="$helper_log_dir/$(printf "%s" "$1" | tr "/" "_").log"
	started=$SECONDS
	case "$1" in
	*.py) run=(uv run "$1") ;;
	*) run=("$1") ;;
	esac
	if "${run[@]}" >"$log" 2>&1; then
		printf "ok %s (%ss)\n" "$1" "$((SECONDS - started))"
	else
		printf "FAILED %s\n" "$1"
		cat "$log"
		exit 1
	fi
' _; then
	printf 'error: one or more helper tests failed\n' >&2
	exit 1
fi
