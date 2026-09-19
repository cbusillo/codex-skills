#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Focused tests for validate-skill-scorecard.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("validate-skill-scorecard.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_skill_scorecard", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate-skill-scorecard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def minimal_scorecard(skill_name: str = "demo-skill") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "owner_issue": 298,
        "normalization": {"score_scale": "0-1000"},
        "suites": {"static_repo_validation": {"command": "scripts/validate-skills.sh"}},
        "ci_promotion_decisions": {
            "script_test": {
                "decision": "required_for_pr",
                "ci_surface": "scripts/validate-skills.sh",
                "rationale": "Focused helper tests run in required CI.",
            }
        },
        "skills": {
            skill_name: {
                "status": "active",
                "risk_classes": ["script_helper"],
                "checks": [
                    {
                        "check_id": "demo-check",
                        "kind": "script_test",
                        "mode": "ci",
                        "dimension": "helper_behavior",
                        "gate": "required",
                        "baseline": {
                            "outcome": "pass",
                            "score": 1000,
                            "last_validated_ref": "main",
                        },
                    }
                ],
                "known_gaps": [],
            }
        },
    }


def set_active_skills(module: Any, names: set[str]) -> None:
    module.active_skill_names = lambda root=module.ROOT: names


def set_script_resources(module: Any, names: set[str]) -> None:
    module.skill_has_script_resources = lambda name, root=module.ROOT: name in names


def test_accepts_minimal_scorecard() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    errors = module.validate_scorecard(minimal_scorecard())
    if errors:
        raise AssertionError(f"minimal scorecard should pass: {errors}")


def test_requires_all_active_skills() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill", "missing-skill"})
    set_script_resources(module, set())
    errors = module.validate_scorecard(minimal_scorecard())
    if not any("missing active skills ['missing-skill']" in error for error in errors):
        raise AssertionError(f"missing skill should fail: {errors}")


def test_rejects_private_paths_and_tokens() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    baseline = card["skills"]["demo-skill"]["checks"][0]["baseline"]
    token = "ghp_" + "123456789012345678901234"
    baseline["notes"] = f"artifact at /Users/example/.local/run with {token}"
    errors = module.validate_scorecard(card)
    expected = ["private/local path", "token-shaped secret"]
    for fragment in expected:
        if not any(fragment in error for error in errors):
            raise AssertionError(f"missing {fragment!r}: {errors}")


def test_required_checks_cannot_be_not_run() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    baseline = card["skills"]["demo-skill"]["checks"][0]["baseline"]
    baseline["outcome"] = "not_run"
    baseline["not_run_reason"] = "not_implemented"
    errors = module.validate_scorecard(card)
    if not any("required checks must not have outcome not_run" in error for error in errors):
        raise AssertionError(f"required not_run should fail: {errors}")


def test_requires_promotion_decision_for_observed_class() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    card["ci_promotion_decisions"] = {}
    errors = module.validate_scorecard(card)
    if not any("missing observed classes ['script_test']" in error for error in errors):
        raise AssertionError(f"missing promotion decision should fail: {errors}")


def test_rejects_unobserved_promotion_decision() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    card["ci_promotion_decisions"]["static_validator"] = {
        "decision": "required_for_pr",
        "ci_surface": "scripts/validate-skills.sh",
        "rationale": "Static validators run in required CI.",
    }
    errors = module.validate_scorecard(card)
    if not any("unobserved classes ['static_validator']" in error for error in errors):
        raise AssertionError(f"unobserved promotion decision should fail: {errors}")


def test_local_promotion_decision_requires_not_run_reason() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "exec_harness"
    check["mode"] = "fake_gh"
    check["scenario"] = "skill-creator/evaluations/exec-harness/fake-gh-plan-index.json"
    card["ci_promotion_decisions"] = {
        "deterministic_exec_harness": {
            "decision": "conditional_local",
            "ci_surface": "scripts/validate-exec-harness-skills.sh <scenario.json>",
            "rationale": "Harness scenarios are opt-in until hermetic in CI.",
        }
    }
    errors = module.validate_scorecard(card)
    if not any("not_run_reason" in error for error in errors):
        raise AssertionError(f"missing not-run reason should fail: {errors}")


def test_required_promotion_decision_rejects_not_run_reason() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    card["ci_promotion_decisions"]["script_test"]["not_run_reason"] = "not_applicable"
    errors = module.validate_scorecard(card)
    if not any("required_for_pr decisions must not set not_run_reason" in error for error in errors):
        raise AssertionError(f"required decision not-run reason should fail: {errors}")


def test_promotion_decision_must_match_class() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    decision = card["ci_promotion_decisions"]["script_test"]
    decision["decision"] = "advisory_local"
    decision["not_run_reason"] = "harness_unavailable"
    errors = module.validate_scorecard(card)
    if not any("script_test must use required_for_pr" in error for error in errors):
        raise AssertionError(f"wrong class decision should fail: {errors}")


def test_local_promotion_decision_must_use_class_not_run_reason() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "exec_harness"
    check["mode"] = "trusted_local_provider"
    check["scenario"] = "skill-creator/evaluations/exec-harness/local-llm-sibling-skill-routing.json"
    card["ci_promotion_decisions"] = {
        "local_llm_advisory": {
            "decision": "advisory_local",
            "ci_surface": "scripts/validate-exec-harness-skills.sh <local-llm-scenario.json>",
            "not_run_reason": "harness_unavailable",
            "rationale": "Local LLM checks require a trusted local endpoint.",
        }
    }
    errors = module.validate_scorecard(card)
    if not any("local_llm_advisory must use local_endpoint_unavailable" in error for error in errors):
        raise AssertionError(f"wrong not-run reason should fail: {errors}")


def test_advisory_promotion_decision_requires_not_run_reason() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "performance_probe"
    check["mode"] = "script"
    card["ci_promotion_decisions"] = {
        "performance_probe": {
            "decision": "advisory_local",
            "ci_surface": "uv run skill-creator/scripts/collect_exec_harness_performance.py --latest 10",
            "rationale": "Performance probes are advisory local evidence.",
        }
    }
    errors = module.validate_scorecard(card)
    if not any("performance_probe.not_run_reason" in error for error in errors):
        raise AssertionError(f"advisory missing not-run reason should fail: {errors}")


def test_promotion_decision_requires_surface_and_rationale() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    decision = card["ci_promotion_decisions"]["script_test"]
    decision["ci_surface"] = ""
    decision.pop("rationale")
    errors = module.validate_scorecard(card)
    expected = ["script_test.ci_surface", "script_test.rationale"]
    for fragment in expected:
        if not any(fragment in error for error in errors):
            raise AssertionError(f"missing {fragment!r}: {errors}")


def test_promotion_decision_keys_must_be_sorted() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "static_validator"
    card["ci_promotion_decisions"] = {
        "static_validator": {
            "decision": "required_for_pr",
            "ci_surface": "scripts/validate-skills.sh",
            "rationale": "Static validators run in required CI.",
        },
        "script_test": {
            "decision": "required_for_pr",
            "ci_surface": "scripts/validate-skills.sh",
            "rationale": "Focused helper tests run in required CI.",
        },
    }
    errors = module.validate_scorecard(card)
    if not any("ci_promotion_decisions: keys must be sorted" in error for error in errors):
        raise AssertionError(f"unsorted promotion decisions should fail: {errors}")


def test_exec_harness_scenarios_must_exist() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "exec_harness"
    check["mode"] = "fake_gh"
    check["scenario"] = "skill-creator/evaluations/missing.json"
    errors = module.validate_scorecard(card)
    if not any("scenario: missing" in error for error in errors):
        raise AssertionError(f"missing scenario should fail: {errors}")


def test_exec_harness_checks_must_use_harness_modes() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, set())
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "exec_harness"
    check["mode"] = "ci"
    check["scenario"] = "skill-creator/evaluations/exec-harness/fake-gh-plan-index.json"
    card["ci_promotion_decisions"] = {}
    errors = module.validate_scorecard(card)
    if not any("exec_harness checks must use" in error for error in errors):
        raise AssertionError(f"invalid exec harness mode should fail: {errors}")


def test_script_bearing_skills_require_test_or_rationale() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    set_script_resources(module, {"demo-skill"})
    card = minimal_scorecard()
    card["skills"]["demo-skill"]["checks"][0]["kind"] = "static_validator"
    card["ci_promotion_decisions"] = {
        "static_validator": {
            "decision": "required_for_pr",
            "ci_surface": "scripts/validate-skills.sh",
            "rationale": "Static validators run in required CI.",
        }
    }
    errors = module.validate_scorecard(card)
    if not any("no focused script tests rationale" in error for error in errors):
        raise AssertionError(f"missing script-test rationale should fail: {errors}")

    card["skills"]["demo-skill"]["known_gaps"] = [
        "No focused script tests yet because this helper requires a live local provider."
    ]
    errors = module.validate_scorecard(card)
    if errors:
        raise AssertionError(f"explicit script-test rationale should pass: {errors}")


def main() -> int:
    test_accepts_minimal_scorecard()
    test_requires_all_active_skills()
    test_rejects_private_paths_and_tokens()
    test_required_checks_cannot_be_not_run()
    test_requires_promotion_decision_for_observed_class()
    test_rejects_unobserved_promotion_decision()
    test_local_promotion_decision_requires_not_run_reason()
    test_required_promotion_decision_rejects_not_run_reason()
    test_promotion_decision_must_match_class()
    test_local_promotion_decision_must_use_class_not_run_reason()
    test_advisory_promotion_decision_requires_not_run_reason()
    test_promotion_decision_requires_surface_and_rationale()
    test_promotion_decision_keys_must_be_sorted()
    test_exec_harness_scenarios_must_exist()
    test_exec_harness_checks_must_use_harness_modes()
    test_script_bearing_skills_require_test_or_rationale()
    print("ok test-validate-skill-scorecard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
