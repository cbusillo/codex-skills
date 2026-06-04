#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
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


def test_accepts_minimal_scorecard() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    errors = module.validate_scorecard(minimal_scorecard())
    if errors:
        raise AssertionError(f"minimal scorecard should pass: {errors}")


def test_requires_all_active_skills() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill", "missing-skill"})
    errors = module.validate_scorecard(minimal_scorecard())
    if not any("missing active skills ['missing-skill']" in error for error in errors):
        raise AssertionError(f"missing skill should fail: {errors}")


def test_rejects_private_paths_and_tokens() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    card = minimal_scorecard()
    baseline = card["skills"]["demo-skill"]["checks"][0]["baseline"]
    baseline["notes"] = "artifact at /Users/example/.local/run with ghp_123456789012345678901234"
    errors = module.validate_scorecard(card)
    expected = ["private/local path", "token-shaped secret"]
    for fragment in expected:
        if not any(fragment in error for error in errors):
            raise AssertionError(f"missing {fragment!r}: {errors}")


def test_required_checks_cannot_be_not_run() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    card = minimal_scorecard()
    baseline = card["skills"]["demo-skill"]["checks"][0]["baseline"]
    baseline["outcome"] = "not_run"
    baseline["not_run_reason"] = "not_implemented"
    errors = module.validate_scorecard(card)
    if not any("required checks must not have outcome not_run" in error for error in errors):
        raise AssertionError(f"required not_run should fail: {errors}")


def test_exec_harness_scenarios_must_exist() -> None:
    module = load_module()
    set_active_skills(module, {"demo-skill"})
    card = minimal_scorecard()
    check = card["skills"]["demo-skill"]["checks"][0]
    check["kind"] = "exec_harness"
    check["mode"] = "fake_gh"
    check["scenario"] = "skill-creator/evaluations/missing.json"
    errors = module.validate_scorecard(card)
    if not any("scenario: missing" in error for error in errors):
        raise AssertionError(f"missing scenario should fail: {errors}")


def main() -> int:
    test_accepts_minimal_scorecard()
    test_requires_all_active_skills()
    test_rejects_private_paths_and_tokens()
    test_required_checks_cannot_be_not_run()
    test_exec_harness_scenarios_must_exist()
    print("ok test-validate-skill-scorecard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
