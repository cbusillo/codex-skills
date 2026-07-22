#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Validate the machine-readable GitHub operation matrix."""

from __future__ import annotations

import argparse
import ast
import functools
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MATRIX = ROOT / "github/references/operation-matrix.toml"

REQUIRED_FIELDS = {
    "id",
    "entrypoint",
    "intent",
    "current_transport",
    "selected_transport",
    "endpoint_or_command",
    "quota_bucket",
    "actor_policy",
    "mutation_class",
    "idempotency",
    "retry_eligibility",
    "reconciliation_strategy",
    "retained_graphql_rationale",
    "source_refs",
    "test_refs",
}
OPTIONAL_FIELDS = {
    "current_endpoint_or_command",
    "current_quota_bucket",
    "migration_status",
}

ENUMS = {
    "current_transport": {
        "composite",
        "delegated_python",
        "gh_cli",
        "gh_cli_graphql",
        "gh_cli_wrapper",
        "git_remote",
        "local_git",
        "rest_api",
    },
    "selected_transport": {
        "composite",
        "delegated_python",
        "gh_cli",
        "gh_cli_graphql",
        "gh_cli_wrapper",
        "git_remote",
        "local_git",
        "rest_api",
    },
    "quota_bucket": {
        "delegated",
        "git_remote",
        "graphql",
        "local",
        "mixed",
        "rest_core",
        "search",
    },
    "current_quota_bucket": {
        "delegated",
        "git_remote",
        "graphql",
        "local",
        "mixed",
        "rest_core",
        "search",
    },
    "migration_status": {"planned"},
    "actor_policy": {
        "active_human_required",
        "automation_required",
        "automation_required_with_explicit_project_override",
        "automation_required_for_writes",
        "local_bot_identity",
        "split_identity_required",
    },
    "mutation_class": {
        "close",
        "comment",
        "create",
        "label_write",
        "local_git_write",
        "merge",
        "mixed",
        "passthrough",
        "project_write",
        "read",
        "relationship_write",
        "remote_git_write",
        "update",
    },
    "idempotency": {
        "conditional",
        "delegated",
        "idempotent",
        "non_idempotent",
        "read_only",
    },
    "retry_eligibility": {"conditional", "manual", "safe"},
    "reconciliation_strategy": {
        "body_file_write",
        "dedupe_then_create",
        "degraded_diagnosis",
        "degraded_snapshot",
        "delegated_evidence",
        "ensure_missing",
        "fail_closed_write",
        "fresh_read",
        "identity_env",
        "operation_marker",
        "paged_read",
        "project_recoverable_warning",
        "quota_snapshot",
        "read_after_write",
        "read_ref_after_delete",
        "relationship_read",
        "restore_remote_url",
        "sha_guarded_merge",
        "two_phase_compensating",
    },
}

ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
PLACEHOLDER_RE = re.compile(r"^(?:todo|tbd|n/?a|none|unknown)$", re.IGNORECASE)
PYTHON_REF_ALIASES = {"self-test": "run_self_tests"}


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_tests()

    matrix_path = args.matrix.resolve()
    repo_root = args.repo_root.resolve()
    errors = validate(matrix_path, repo_root)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1

    raw = load_toml(matrix_path)
    print(f"ok validate-operation-matrix ({len(raw.get('operations') or [])} operations)")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate github/references/operation-matrix.toml")
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--self-test", action="store_true", help="Run dependency-free function-level checks.")
    return parser.parse_args()


def validate(matrix_path: Path, repo_root: Path) -> list[str]:
    errors: list[str] = []
    try:
        raw = load_toml(matrix_path)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"unable to read TOML matrix {matrix_path}: {exc}"]

    operations = validate_schema(raw, repo_root, errors)
    if operations:
        validate_static_coverage(operations, repo_root, errors)
    return errors


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise TypeError("matrix root must be a TOML table")
    return data


def validate_schema(raw: dict[str, Any], repo_root: Path, errors: list[str]) -> list[dict[str, Any]]:
    if raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")

    operations = raw.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append("operations must be a non-empty array of tables")
        return []

    seen: dict[str, int] = {}
    valid_operations: list[dict[str, Any]] = []
    for index, operation in enumerate(operations, start=1):
        label = f"operations[{index}]"
        if not isinstance(operation, dict):
            errors.append(f"{label} must be a table")
            continue

        valid_operations.append(operation)
        missing = sorted(REQUIRED_FIELDS - operation.keys())
        if missing:
            errors.append(f"{label} missing required field(s): {', '.join(missing)}")
        unknown = sorted(set(operation) - REQUIRED_FIELDS - OPTIONAL_FIELDS)
        if unknown:
            errors.append(f"{label} has unsupported field(s): {', '.join(unknown)}")

        op_id = operation.get("id")
        if not isinstance(op_id, str) or not op_id.strip():
            errors.append(f"{label}.id must be a non-empty string")
        elif not ID_RE.fullmatch(op_id):
            errors.append(f"{label}.id is not stable dotted lowercase form: {op_id!r}")
        elif op_id in seen:
            errors.append(f"duplicate operation id {op_id!r} at operations[{seen[op_id]}] and {label}")
        else:
            seen[op_id] = index

        for field in sorted(REQUIRED_FIELDS - {"source_refs", "test_refs"}):
            value = operation.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{label}.{field} must be a non-empty string")
            elif PLACEHOLDER_RE.fullmatch(value.strip()):
                errors.append(f"{label}.{field} must not be a placeholder value")

        for field, allowed in ENUMS.items():
            value = operation.get(field)
            if isinstance(value, str) and value not in allowed:
                errors.append(f"{label}.{field} has unsupported value {value!r}")

        mutation_class = operation.get("mutation_class")
        idempotency = operation.get("idempotency")
        retry_eligibility = operation.get("retry_eligibility")
        reconciliation_strategy = operation.get("reconciliation_strategy")
        if mutation_class == "read" and idempotency != "read_only":
            errors.append(f"{label}.idempotency must be 'read_only' for read operations")
        elif idempotency == "read_only" and mutation_class != "read":
            errors.append(f"{label}.idempotency 'read_only' requires mutation_class 'read'")
        if retry_eligibility == "safe" and idempotency != "read_only":
            errors.append(f"{label}.retry_eligibility 'safe' requires read_only idempotency")
        if (
            retry_eligibility == "conditional"
            and idempotency == "non_idempotent"
            and reconciliation_strategy not in {"dedupe_then_create", "operation_marker"}
        ):
            errors.append(
                f"{label}.reconciliation_strategy must provide a stable create key "
                "for conditionally retried non-idempotent writes"
            )

        current_transport = operation.get("current_transport")
        selected_transport = operation.get("selected_transport")
        transport_changed = (
            isinstance(current_transport, str)
            and isinstance(selected_transport, str)
            and current_transport != selected_transport
        )
        migration_planned = operation.get("migration_status") == "planned"
        if transport_changed and not migration_planned:
            errors.append(f"{label}.migration_status must be 'planned' while transports differ")
        if transport_changed or migration_planned:
            for field in ("migration_status", "current_endpoint_or_command", "current_quota_bucket"):
                value = operation.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{label}.{field} is required for a planned migration")
        if migration_planned:
            current_endpoint = operation.get("current_endpoint_or_command")
            selected_endpoint = operation.get("endpoint_or_command")
            current_quota = operation.get("current_quota_bucket")
            selected_quota = operation.get("quota_bucket")
            if (
                not transport_changed
                and current_endpoint == selected_endpoint
                and current_quota == selected_quota
            ):
                errors.append(
                    f"{label}.migration_status is planned but transport, endpoint, and quota evidence are unchanged"
                )

        rationale = operation.get("retained_graphql_rationale")
        if isinstance(rationale, str):
            if "graphql" not in rationale.lower():
                errors.append(f"{label}.retained_graphql_rationale must explicitly mention GraphQL")
            if (
                operation.get("selected_transport") == "gh_cli_graphql"
                or operation.get("quota_bucket") == "graphql"
            ) and "no retained graphql" in rationale.lower():
                errors.append(f"{label}.retained_graphql_rationale contradicts GraphQL-selected transport")

        for field in ("source_refs", "test_refs"):
            validate_refs(label, field, operation.get(field), repo_root, errors)

    return valid_operations


def validate_refs(label: str, field: str, value: Any, repo_root: Path, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append(f"{label}.{field} must be a non-empty array")
        return
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{label}.{field} entries must be non-empty strings")
            continue
        path_text, separator, fragment = item.partition(":")
        path = repo_root / path_text
        if path_text and not path.exists():
            errors.append(f"{label}.{field} references missing path: {item}")
            continue
        if not separator or not fragment:
            continue
        if fragment.isdigit():
            line_number = int(fragment)
            if line_number < 1 or line_number > file_line_count(path):
                errors.append(f"{label}.{field} references missing line: {item}")
            continue
        python_fragment = PYTHON_REF_ALIASES.get(fragment, fragment)
        if path.suffix == ".py" and python_fragment not in python_symbols(path):
            errors.append(f"{label}.{field} references missing Python symbol: {item}")


@functools.cache
def file_line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


@functools.cache
def python_symbols(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    )


def validate_static_coverage(
    operations: list[dict[str, Any]],
    repo_root: Path,
    errors: list[str],
) -> None:
    entrypoints = {
        operation["entrypoint"]
        for operation in operations
        if isinstance(operation.get("entrypoint"), str)
    }

    require_entrypoint_commands(
        "github/scripts/github_api.py",
        extract_argparse_subcommands(repo_root / "github/scripts/github_api.py", errors),
        entrypoints,
        errors,
    )
    require_entrypoint_commands(
        "github/scripts/gh-pr.py",
        extract_argparse_subcommands(repo_root / "github/scripts/gh-pr.py", errors),
        entrypoints,
        errors,
    )
    require_entrypoint_commands(
        "github/scripts/gh-plan.py",
        extract_argparse_subcommands(repo_root / "github/scripts/gh-plan.py", errors),
        entrypoints,
        errors,
    )
    require_entrypoint_commands(
        "github/scripts/gh-issue",
        extract_argparse_subcommands(repo_root / "github/scripts/github_issue.py", errors),
        entrypoints,
        errors,
    )
    require_entrypoint_commands(
        "github/scripts/gh-comment",
        extract_argparse_argument_choices(
            repo_root / "github/scripts/github_comment.py",
            "kind",
            errors,
        ),
        entrypoints,
        errors,
    )

    for resource in sorted(extract_public_script_resources(repo_root / "github/SKILL.md", errors)):
        script = f"github/{resource}"
        if not any(entrypoint == script or entrypoint.startswith(f"{script} ") for entrypoint in entrypoints):
            errors.append(f"public github/SKILL.md resource is not represented in matrix: {script}")


def require_entrypoint_commands(
    script: str,
    commands: set[str],
    entrypoints: set[str],
    errors: list[str],
) -> None:
    for command in sorted(commands):
        expected = f"{script} {command}"
        if expected not in entrypoints:
            errors.append(f"missing operation matrix coverage for public command: {expected}")


def extract_argparse_subcommands(path: Path, errors: list[str]) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except OSError as exc:
        errors.append(f"unable to read {path}: {exc}")
        return set()
    except SyntaxError as exc:
        errors.append(f"unable to parse {path}: {exc}")
        return set()

    commands: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_parser":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        value = node.args[0].value
        if isinstance(value, str):
            commands.add(value)
    return commands


def extract_argparse_argument_choices(path: Path, argument: str, errors: list[str]) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except OSError as exc:
        errors.append(f"unable to read {path}: {exc}")
        return set()
    except SyntaxError as exc:
        errors.append(f"unable to parse {path}: {exc}")
        return set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant) or node.args[0].value != argument:
            continue
        choices = next((keyword.value for keyword in node.keywords if keyword.arg == "choices"), None)
        if not isinstance(choices, (ast.Tuple, ast.List, ast.Set)):
            continue
        values = {
            str(element.value)
            for element in choices.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        if values:
            return values
    errors.append(f"unable to find argparse choices for {argument} in {path}")
    return set()


def extract_shell_case_choices(path: Path, variable: str, errors: list[str]) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"unable to read {path}: {exc}")
        return set()
    commands = extract_shell_case_choices_from_text(text, variable)
    if not commands:
        errors.append(f"unable to find public case choices for ${variable} in {path}")
    return commands


def extract_shell_case_choices_from_text(text: str, variable: str) -> set[str]:
    match = re.search(
        rf"case\s+\"\${re.escape(variable)}\"\s+in\n(?P<body>.*?)\n[ \t]*esac\b",
        text,
        re.DOTALL,
    )
    if not match:
        return set()

    commands: set[str] = set()
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("*)") or ")" not in line:
            continue
        choices = line.split(")", 1)[0]
        for choice in re.split(r"\s*\|\s*", choices):
            choice = choice.strip()
            if re.fullmatch(r"[a-z][a-z0-9_-]*", choice):
                commands.add(choice)
    return commands


def extract_public_script_resources(path: Path, errors: list[str]) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"unable to read {path}: {exc}")
        return set()
    return {
        match.group(1)
        for match in re.finditer(r"^\s+path:\s+(scripts/[^\s#]+)\s*$", text, re.MULTILINE)
    }


def run_self_tests() -> int:
    tests = [
        test_schema_accepts_minimal_operation,
        test_duplicate_ids_fail,
        test_invalid_enum_fails,
        test_read_operation_requires_read_only_idempotency,
        test_read_only_idempotency_requires_read_operation,
        test_safe_retry_requires_read_only_idempotency,
        test_conditional_non_idempotent_retry_requires_stable_key,
        test_graphql_rationale_is_required,
        test_planned_migration_requires_current_transport_evidence,
        test_same_transport_component_migration_is_allowed,
        test_planned_migration_requires_changed_evidence,
        test_missing_python_symbol_reference_fails,
        test_missing_line_reference_fails,
        test_shell_case_extractor_handles_pipe_choices,
        test_argparse_argument_choice_extractor,
        test_static_command_coverage_reports_missing_entrypoint,
    ]
    try:
        for test in tests:
            test()
    except AssertionError as exc:
        print(f"error: self-test failed: {exc}", file=sys.stderr)
        return 1
    print(f"ok validate-operation-matrix self-test ({len(tests)} tests)")
    return 0


def test_schema_accepts_minimal_operation() -> None:
    errors: list[str] = []
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [minimal_operation()]}, ROOT, errors)
    assert errors == [], errors


def test_duplicate_ids_fail() -> None:
    errors: list[str] = []
    operation = minimal_operation()
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation, dict(operation)]}, ROOT, errors)
    assert any("duplicate operation id" in error for error in errors), errors


def test_invalid_enum_fails() -> None:
    errors: list[str] = []
    operation = minimal_operation(retry_eligibility="maybe")
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("retry_eligibility" in error and "unsupported" in error for error in errors), errors


def test_read_operation_requires_read_only_idempotency() -> None:
    errors: list[str] = []
    operation = minimal_operation(idempotency="idempotent")
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("idempotency must be 'read_only'" in error for error in errors), errors


def test_read_only_idempotency_requires_read_operation() -> None:
    errors: list[str] = []
    operation = minimal_operation(mutation_class="create")
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("requires mutation_class 'read'" in error for error in errors), errors


def test_safe_retry_requires_read_only_idempotency() -> None:
    errors: list[str] = []
    operation = minimal_operation(
        mutation_class="update",
        idempotency="idempotent",
        retry_eligibility="safe",
    )
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("'safe' requires read_only" in error for error in errors), errors


def test_conditional_non_idempotent_retry_requires_stable_key() -> None:
    errors: list[str] = []
    operation = minimal_operation(
        mutation_class="create",
        idempotency="non_idempotent",
        retry_eligibility="conditional",
        reconciliation_strategy="fresh_read",
    )
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("stable create key" in error for error in errors), errors


def test_graphql_rationale_is_required() -> None:
    errors: list[str] = []
    operation = minimal_operation(retained_graphql_rationale="REST only.")
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("retained_graphql_rationale" in error and "GraphQL" in error for error in errors), errors


def test_planned_migration_requires_current_transport_evidence() -> None:
    errors: list[str] = []
    operation = minimal_operation(current_transport="gh_cli_wrapper", selected_transport="rest_api")
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("migration_status" in error and "required" in error for error in errors), errors
    assert any("current_endpoint_or_command" in error and "required" in error for error in errors), errors
    assert any("current_quota_bucket" in error and "required" in error for error in errors), errors


def test_same_transport_component_migration_is_allowed() -> None:
    errors: list[str] = []
    operation = minimal_operation(
        current_transport="composite",
        selected_transport="composite",
        endpoint_or_command="REST issue reads plus retained Project GraphQL",
        quota_bucket="mixed",
        migration_status="planned",
        current_endpoint_or_command="gh issue list plus retained Project GraphQL",
        current_quota_bucket="mixed",
    )
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert errors == [], errors


def test_planned_migration_requires_changed_evidence() -> None:
    errors: list[str] = []
    operation = minimal_operation(
        migration_status="planned",
        current_endpoint_or_command="GET /fixture",
        current_quota_bucket="rest_core",
    )
    validate_schema({"schema_version": SCHEMA_VERSION, "operations": [operation]}, ROOT, errors)
    assert any("transport, endpoint, and quota evidence are unchanged" in error for error in errors), errors


def test_missing_python_symbol_reference_fails() -> None:
    errors: list[str] = []
    validate_refs(
        "operations[1]",
        "source_refs",
        ["github/scripts/gh-pr.py:not_a_real_symbol"],
        ROOT,
        errors,
    )
    assert any("missing Python symbol" in error for error in errors), errors


def test_missing_line_reference_fails() -> None:
    errors: list[str] = []
    validate_refs(
        "operations[1]",
        "source_refs",
        ["github/scripts/gh-comment:99999"],
        ROOT,
        errors,
    )
    assert any("missing line" in error for error in errors), errors


def test_shell_case_extractor_handles_pipe_choices() -> None:
    text = 'if true; then\n  case "$kind" in\n    issue|pr) ;;\n    *) exit 2 ;;\n  esac\nfi\n'
    assert extract_shell_case_choices_from_text(text, "kind") == {"issue", "pr"}


def test_argparse_argument_choice_extractor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "helper.py"
        path.write_text(
            "import argparse\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('kind', choices=('issue', 'pr'))\n",
            encoding="utf-8",
        )
        errors: list[str] = []
        assert extract_argparse_argument_choices(path, "kind", errors) == {"issue", "pr"}
        assert errors == [], errors


def test_static_command_coverage_reports_missing_entrypoint() -> None:
    errors: list[str] = []
    require_entrypoint_commands(
        "github/scripts/example",
        {"one", "two"},
        {"github/scripts/example one"},
        errors,
    )
    assert errors == ["missing operation matrix coverage for public command: github/scripts/example two"], errors


def minimal_operation(**overrides: Any) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "id": "test.operation",
        "entrypoint": "github/scripts/gh-pr.py view",
        "intent": "Fixture operation.",
        "current_transport": "rest_api",
        "selected_transport": "rest_api",
        "endpoint_or_command": "GET /fixture",
        "quota_bucket": "rest_core",
        "actor_policy": "automation_required",
        "mutation_class": "read",
        "idempotency": "read_only",
        "retry_eligibility": "safe",
        "reconciliation_strategy": "fresh_read",
        "retained_graphql_rationale": "No retained GraphQL transport; fixture rationale.",
        "source_refs": ["github/scripts/gh-pr.py:1"],
        "test_refs": ["github/scripts/validate-operation-matrix.py:self-test"],
    }
    operation.update(overrides)
    return operation


if __name__ == "__main__":
    raise SystemExit(main())
