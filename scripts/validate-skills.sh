#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

uv run github/scripts/validate-gh-plan.py
github/scripts/validate-gh-issue.sh
uv run skill-creator/scripts/validate-skill-behavior.py
uv run skill-creator/scripts/validate-command-policy-simulator.py --self-test
uv run skill-creator/scripts/validate-command-policy-simulator.py
uv run skill-creator/scripts/validate-skill-scorecard.py
uv run scripts/validate-public-safety.py --self-test
uv run scripts/validate-public-safety.py
uv run skill-creator/scripts/quick_validate.py --self-test
uv run skill-creator/scripts/validate-skill-repo.py
uv run skill-creator/scripts/quick_validate.py chronicle

helper_tests=(
	babysit-pr/scripts/test_gh_pr_watch.py
	google-seo/scripts/test_bing_webmaster.py
	google-seo/scripts/test_google_search_console.py
	github-work-rollup/scripts/test_github_work_rollup.py
	github-work-rollup/scripts/test_synthesize_work_brief.py
	github-work-rollup/scripts/test_verify_work_brief.py
	github/scripts/test_github_work_evidence.py
	infra-ops/scripts/test_npmplus_ops.py
	local-llm/scripts/validate_local_code_agent.py
	local-llm/scripts/validate_lm_studio_api.py
	people/scripts/test_resolve_person.py
	scripts/test_validate_public_safety.py
	jetbrains-inspection/tests/test_jb_inspect.py
	skill-creator/scripts/test_collect_exec_harness_performance.py
	skill-creator/scripts/test_validate_skill_repo.py
	skill-creator/scripts/test_validate_skill_scorecard.py
	skill-creator/scripts/validate-command-policy-simulator.py
	rollout-friction/scripts/validate_analyze_rollouts.py
	rollout-friction/scripts/validate_classify_auto_review_ledger.py
	rollout-friction/scripts/validate_cluster_rollout_episodes.py
	rollout-friction/scripts/validate_extract_rollout_memory.py
	rollout-friction/scripts/validate_prepare_rollout_memory_long_context_review.py
	rollout-friction/scripts/validate_reduce_rollout_memory_reviews.py
	rollout-friction/scripts/validate_review_rollout_memory_batches.py
	rollout-friction/scripts/validate_run_rollout_memory_long_context_matrix.py
	rollout-friction/scripts/validate_segment_rollout_episodes.py
	rollout-friction/scripts/validate_summarize_rollout_memory_reviews.py
	rollout-friction/scripts/validate_validate_rollout_memory_llm_results.py
)

# Files matching these names are CLIs or fixtures that require arguments/live
# context, not standalone helper tests. Keep the skip list explicit so newly
# added test_*.py or validate_*.py files do not silently miss validation.
helper_test_skiplist=(
	github/scripts/validate-gh-plan.py
	rollout-friction/scripts/validate_rollout_memory_llm_results.py
	scripts/validate-public-safety.py
	skill-creator/scripts/validate-skill-behavior.py
	skill-creator/scripts/validate-skill-repo.py
	skill-creator/scripts/validate-skill-scorecard.py
)

mapfile -t discovered_helper_tests < <(
	git ls-files --cached --others --exclude-standard '*test_*.py' '*validate_*.py' '*validate-*.py' | sort
)

declared_helper_tests="$(printf '%s\n' "${helper_tests[@]}" "${helper_test_skiplist[@]}" | sort)"
discovered_helper_tests_text="$(printf '%s\n' "${discovered_helper_tests[@]}")"
if [[ "$declared_helper_tests" != "$discovered_helper_tests_text" ]]; then
	printf 'helper test list is out of date\n' >&2
	printf 'declared + skipped:\n%s\n' "$declared_helper_tests" >&2
	printf 'discovered:\n%s\n' "$discovered_helper_tests_text" >&2
	exit 1
fi

for helper_test in "${helper_tests[@]}"; do
	uv run "$helper_test"
done
