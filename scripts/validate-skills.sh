#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python3 github/scripts/validate-gh-plan.py
github/scripts/validate-gh-issue.sh
python3 skill-creator/scripts/validate-skill-behavior.py
uv run skill-creator/scripts/quick_validate.py --self-test
uv run skill-creator/scripts/validate-skill-repo.py
uv run skill-creator/scripts/quick_validate.py chronicle

helper_tests=(
	babysit-pr/scripts/test_gh_pr_watch.py
	codex-issue-digest/scripts/test_collect_issue_digest.py
	google-seo/scripts/test_google_search_console.py
	github-work-rollup/scripts/test_github_work_rollup.py
	infra-ops/scripts/test_npmplus_ops.py
	people/scripts/test_resolve_person.py
	jetbrains-inspection/tests/test_jb_inspect.py
	skill-creator/scripts/test_validate_skill_repo.py
	rollout-friction/scripts/validate_analyze_rollouts.py
	rollout-friction/scripts/validate_extract_rollout_memory.py
	rollout-friction/scripts/validate_prepare_rollout_memory_long_context_review.py
	rollout-friction/scripts/validate_reduce_rollout_memory_reviews.py
	rollout-friction/scripts/validate_review_rollout_memory_batches.py
	rollout-friction/scripts/validate_run_rollout_memory_long_context_matrix.py
	rollout-friction/scripts/validate_summarize_rollout_memory_reviews.py
	rollout-friction/scripts/validate_validate_rollout_memory_llm_results.py
)

for helper_test in "${helper_tests[@]}"; do
	uv run "$helper_test"
done
