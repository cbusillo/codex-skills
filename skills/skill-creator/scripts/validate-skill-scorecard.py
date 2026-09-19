#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Validate the public-safe skill scorecard."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCORECARD = ROOT / "skill-creator" / "references" / "skill-scorecard.yaml"
IGNORED_SKILL_DIRS = {".disabled", ".git", ".local", ".system", ".code"}

KNOWN_STATUSES = {"active", "manual_only", "deprecated", "disabled"}
KNOWN_RISK_CLASSES = {
    "browser_ui",
    "command_execution",
    "command_policy",
    "docs_lookup",
    "external_api",
    "github_mutation",
    "github_read",
    "github_workflow",
    "local_context",
    "local_llm",
    "manual_only",
    "planning",
    "private_context",
    "public_safety",
    "python_workflow",
    "readiness_closeout",
    "script_helper",
    "security",
    "skill_authoring",
    "system_override",
}
KNOWN_KINDS = {
    "static_validator",
    "script_test",
    "exec_harness",
    "local_llm_advisory",
    "performance_probe",
}
KNOWN_MODES = {
    "ci",
    "fake_gh",
    "fake_responses_api",
    "script",
    "trusted_local_provider",
    "manual_review",
}
KNOWN_DIMENSIONS = {
    "advisory_behavior",
    "command_policy",
    "coverage",
    "external_api",
    "github_workflow",
    "helper_behavior",
    "metadata",
    "performance",
    "public_safety",
    "routing",
    "security",
}
KNOWN_GATES = {"required", "conditional", "advisory"}
KNOWN_OUTCOMES = {"pass", "fail", "skip", "not_run"}
KNOWN_NOT_RUN_REASONS = {
    "harness_unavailable",
    "local_endpoint_unavailable",
    "not_applicable",
    "not_implemented",
}
KNOWN_CI_DECISIONS = {"required_for_pr", "conditional_local", "advisory_local"}
EXPECTED_CI_PROMOTION = {
    "deterministic_exec_harness": ("conditional_local", "harness_unavailable"),
    "local_llm_advisory": ("advisory_local", "local_endpoint_unavailable"),
    "performance_probe": ("advisory_local", "harness_unavailable"),
    "script_test": ("required_for_pr", None),
    "static_validator": ("required_for_pr", None),
}
KNOWN_CI_PROMOTION_CLASSES = set(EXPECTED_CI_PROMOTION)
EXEC_HARNESS_PROMOTION_MODES = {
    "fake_gh": "deterministic_exec_harness",
    "fake_responses_api": "deterministic_exec_harness",
    "trusted_local_provider": "local_llm_advisory",
}
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SCRIPT_TEST_RATIONALE_RE = re.compile(r"\bno focused\b.*\bscript tests?\b", re.IGNORECASE)
PRIVATE_PATH_RE = re.compile(r"(^|/)(\.local|\.code)(/|$)|^/Users/|^~")
PRIVATE_HOST_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
TOKEN_SHAPES = (
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)


def active_skill_names(root: Path = ROOT) -> set[str]:
    names: set[str] = set()
    for skill_md in sorted(root.glob("*/SKILL.md")):
        if skill_md.parent.name in IGNORED_SKILL_DIRS:
            continue
        names.add(skill_md.parent.name)
    return names


def load_scorecard(path: Path = SCORECARD) -> Any:
    try:
        return yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return None
    except yaml.YAMLError as exc:
        return {"__yaml_error__": str(exc)}


def skill_has_script_resources(name: str, root: Path = ROOT) -> bool:
    skill_md = root / name / "SKILL.md"
    try:
        text = skill_md.read_text()
    except FileNotFoundError:
        return False
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return False
    parsed = yaml.safe_load(match.group(1))
    frontmatter = parsed if isinstance(parsed, dict) else {}
    resources = frontmatter.get("resources", [])
    if not isinstance(resources, list):
        return False
    return any(isinstance(resource, dict) and resource.get("kind") == "script" for resource in resources)


def public_safe_string_errors(path: str, value: str) -> list[str]:
    errors: list[str] = []
    if PRIVATE_PATH_RE.search(value):
        errors.append(f"{path}: must not contain private/local path {value!r}")
    if PRIVATE_HOST_RE.search(value):
        errors.append(f"{path}: must not contain private IP address")
    for pattern in TOKEN_SHAPES:
        if pattern.search(value):
            errors.append(f"{path}: must not contain token-shaped secret")
            break
    return errors


def validate_string_tree(value: Any, path: str = "scorecard") -> list[str]:
    errors: list[str] = []
    if isinstance(value, str):
        errors.extend(public_safe_string_errors(path, value))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(validate_string_tree(item, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, item in value.items():
            errors.extend(validate_string_tree(item, f"{path}.{key}"))
    return errors


def require_mapping(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be a mapping")
        return {}
    return value


def require_list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return []
    return value


def validate_enum(
    value: Any,
    *,
    path: str,
    known: set[str],
    errors: list[str],
) -> str | None:
    if not isinstance(value, str) or value not in known:
        errors.append(f"{path}: must be one of {sorted(known)}")
        return None
    return value


def validate_kebab(value: Any, *, path: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not KEBAB_RE.match(value):
        errors.append(f"{path}: must be kebab-case")
        return None
    return value


def validate_baseline(value: Any, path: str, gate: str | None) -> list[str]:
    errors: list[str] = []
    baseline = require_mapping(value, path, errors)
    outcome = validate_enum(
        baseline.get("outcome"), path=f"{path}.outcome", known=KNOWN_OUTCOMES, errors=errors
    )

    score = baseline.get("score")
    if score is not None and (
        not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 1000
    ):
        errors.append(f"{path}.score: must be an integer from 0 to 1000")

    for key in ("duration_ms_p50", "duration_ms_p95", "duration_ms_budget", "command_count", "tool_call_count", "token_estimate"):
        if key in baseline:
            value = baseline[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"{path}.{key}: must be a non-negative integer")

    if outcome == "not_run":
        validate_enum(
            baseline.get("not_run_reason"),
            path=f"{path}.not_run_reason",
            known=KNOWN_NOT_RUN_REASONS,
            errors=errors,
        )
        if gate == "required":
            errors.append(f"{path}: required checks must not have outcome not_run")

    notes = baseline.get("notes")
    if notes is not None and not isinstance(notes, str):
        errors.append(f"{path}.notes: must be a string")

    ref = baseline.get("last_validated_ref")
    if ref is not None and not isinstance(ref, str):
        errors.append(f"{path}.last_validated_ref: must be a string")

    return errors


def validate_check(value: Any, path: str) -> tuple[str | None, list[str]]:
    errors: list[str] = []
    check = require_mapping(value, path, errors)
    check_id = validate_kebab(check.get("check_id"), path=f"{path}.check_id", errors=errors)
    validate_enum(check.get("kind"), path=f"{path}.kind", known=KNOWN_KINDS, errors=errors)
    validate_enum(check.get("mode"), path=f"{path}.mode", known=KNOWN_MODES, errors=errors)
    validate_enum(
        check.get("dimension"), path=f"{path}.dimension", known=KNOWN_DIMENSIONS, errors=errors
    )
    gate = validate_enum(check.get("gate"), path=f"{path}.gate", known=KNOWN_GATES, errors=errors)

    scenario = check.get("scenario")
    kind = check.get("kind")
    if kind == "exec_harness":
        if check.get("mode") not in EXEC_HARNESS_PROMOTION_MODES:
            errors.append(
                f"{path}.mode: exec_harness checks must use {sorted(EXEC_HARNESS_PROMOTION_MODES)}"
            )
        if not isinstance(scenario, str) or not scenario:
            errors.append(f"{path}.scenario: exec_harness checks must name a scenario path")
        elif PRIVATE_PATH_RE.search(scenario):
            errors.append(f"{path}.scenario: must be a public relative path")
        elif not (ROOT / scenario).exists():
            errors.append(f"{path}.scenario: missing {scenario}")
    elif scenario is not None and not isinstance(scenario, str):
        errors.append(f"{path}.scenario: must be a string")

    if "model_family" in check and not isinstance(check["model_family"], str):
        errors.append(f"{path}.model_family: must be a string")

    errors.extend(validate_baseline(check.get("baseline"), f"{path}.baseline", gate))
    return check_id, errors


def ci_promotion_class(check: dict[str, Any]) -> str | None:
    kind = check.get("kind")
    mode = check.get("mode")
    if kind == "exec_harness":
        if not isinstance(mode, str):
            return None
        return EXEC_HARNESS_PROMOTION_MODES.get(mode)
    if kind in {"static_validator", "script_test", "local_llm_advisory", "performance_probe"}:
        return kind
    return None


def observed_ci_promotion_classes(skills: dict[str, Any]) -> set[str]:
    classes: set[str] = set()
    for skill in skills.values():
        if not isinstance(skill, dict):
            continue
        checks = skill.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            promotion_class = ci_promotion_class(check)
            if promotion_class:
                classes.add(promotion_class)
    return classes


def validate_ci_promotion_decisions(value: Any, skills: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decisions = require_mapping(value, "scorecard.ci_promotion_decisions", errors)
    decision_keys = set(decisions)
    observed = observed_ci_promotion_classes(skills)

    missing = sorted(observed - decision_keys)
    extra = sorted(decision_keys - KNOWN_CI_PROMOTION_CLASSES)
    unobserved = sorted((decision_keys & KNOWN_CI_PROMOTION_CLASSES) - observed)
    if missing:
        errors.append(f"scorecard.ci_promotion_decisions: missing observed classes {missing}")
    if extra:
        errors.append(f"scorecard.ci_promotion_decisions: unknown classes {extra}")
    if unobserved:
        errors.append(f"scorecard.ci_promotion_decisions: unobserved classes {unobserved}")

    if list(decisions) != sorted(decisions):
        errors.append("scorecard.ci_promotion_decisions: keys must be sorted")

    for name, raw_decision in decisions.items():
        path = f"scorecard.ci_promotion_decisions.{name}"
        if name not in KNOWN_CI_PROMOTION_CLASSES:
            continue

        decision_entry = require_mapping(raw_decision, path, errors)
        decision = validate_enum(
            decision_entry.get("decision"),
            path=f"{path}.decision",
            known=KNOWN_CI_DECISIONS,
            errors=errors,
        )
        expected_decision, expected_not_run_reason = EXPECTED_CI_PROMOTION[name]
        if decision is not None and decision != expected_decision:
            errors.append(f"{path}.decision: {name} must use {expected_decision}")

        for key in ("ci_surface", "rationale"):
            field_value = decision_entry.get(key)
            if not isinstance(field_value, str) or not field_value.strip():
                errors.append(f"{path}.{key}: must be a non-empty string")

        not_run_reason = decision_entry.get("not_run_reason")
        if expected_not_run_reason is None:
            if not_run_reason is not None:
                errors.append(f"{path}.not_run_reason: required_for_pr decisions must not set not_run_reason")
        else:
            actual_not_run_reason = validate_enum(
                not_run_reason,
                path=f"{path}.not_run_reason",
                known=KNOWN_NOT_RUN_REASONS,
                errors=errors,
            )
            if actual_not_run_reason is not None and actual_not_run_reason != expected_not_run_reason:
                errors.append(f"{path}.not_run_reason: {name} must use {expected_not_run_reason}")

    return errors


def validate_skill_entry(name: str, value: Any, path: str) -> list[str]:
    errors: list[str] = []
    if not KEBAB_RE.match(name):
        errors.append(f"{path}: skill key must be kebab-case")
    skill = require_mapping(value, path, errors)
    validate_enum(skill.get("status"), path=f"{path}.status", known=KNOWN_STATUSES, errors=errors)

    risk_classes = require_list(skill.get("risk_classes"), f"{path}.risk_classes", errors)
    if not risk_classes:
        errors.append(f"{path}.risk_classes: must not be empty")
    for index, risk_class in enumerate(risk_classes):
        validate_enum(
            risk_class,
            path=f"{path}.risk_classes[{index}]",
            known=KNOWN_RISK_CLASSES,
            errors=errors,
        )

    checks = require_list(skill.get("checks"), f"{path}.checks", errors)
    seen_ids: set[str] = set()
    previous_id = ""
    for index, raw_check in enumerate(checks):
        check_id, check_errors = validate_check(raw_check, f"{path}.checks[{index}]")
        errors.extend(check_errors)
        if check_id:
            if check_id in seen_ids:
                errors.append(f"{path}.checks[{index}].check_id: duplicate {check_id}")
            seen_ids.add(check_id)
            if check_id < previous_id:
                errors.append(f"{path}.checks: check_id values must be sorted")
            previous_id = check_id

    known_gaps = skill.get("known_gaps", [])
    gaps = require_list(known_gaps, f"{path}.known_gaps", errors)
    for index, gap in enumerate(gaps):
        if not isinstance(gap, str) or not gap.strip():
            errors.append(f"{path}.known_gaps[{index}]: must be a non-empty string")

    has_script_test = any(
        isinstance(check, dict) and check.get("kind") == "script_test" for check in checks
    )
    if skill_has_script_resources(name) and not has_script_test:
        gap_text = "\n".join(gap for gap in gaps if isinstance(gap, str))
        if SCRIPT_TEST_RATIONALE_RE.search(gap_text) is None:
            errors.append(
                f"{path}.known_gaps: script-bearing skills without script_test checks must include a no focused script tests rationale"
            )

    return errors


def validate_scorecard(data: Any, *, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    if data is None:
        return [f"{SCORECARD.relative_to(root)}: missing scorecard"]
    if isinstance(data, dict) and "__yaml_error__" in data:
        return [f"{SCORECARD.relative_to(root)}: invalid YAML: {data['__yaml_error__']}"]

    card = require_mapping(data, "scorecard", errors)
    schema_version = card.get("schema_version")
    if schema_version != 1:
        errors.append("scorecard.schema_version: must be 1")

    if card.get("owner_issue") != 298:
        errors.append("scorecard.owner_issue: must be 298")

    for key in ("normalization", "suites"):
        require_mapping(card.get(key), f"scorecard.{key}", errors)

    raw_skills = card.get("skills")
    skills = require_mapping(raw_skills, "scorecard.skills", errors)
    if isinstance(raw_skills, dict):
        errors.extend(validate_ci_promotion_decisions(card.get("ci_promotion_decisions"), skills))
    skill_names = set(skills)
    active_names = active_skill_names(root)
    missing = sorted(active_names - skill_names)
    extra = sorted(skill_names - active_names)
    if missing:
        errors.append(f"scorecard.skills: missing active skills {missing}")
    if extra:
        errors.append(f"scorecard.skills: unknown skills {extra}")

    previous_name = ""
    for name, value in skills.items():
        if name < previous_name:
            errors.append("scorecard.skills: skill keys must be sorted")
        previous_name = name
        errors.extend(validate_skill_entry(name, value, f"scorecard.skills.{name}"))

    errors.extend(validate_string_tree(card))
    return errors


def main() -> int:
    errors = validate_scorecard(load_scorecard())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("ok validate-skill-scorecard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
