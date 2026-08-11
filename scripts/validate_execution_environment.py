#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.3",
#     "PyYAML==6.0.3",
# ]
# ///
"""Validate repository execution-environment policy and drift controls."""

from __future__ import annotations

import ast
import importlib.util
import json
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNNER = "ubuntu-24.04"
EXPECTED_PYTHON = "3.12"
EXPECTED_CURRENT_PYTHON = "3.14"
EXPECTED_REQUIRES_PYTHON = ">=3.12"
EXPECTED_UV_REQUIREMENT = ">=0.11.29,<1"
EXPECTED_CACHE_DEPENDENCY_GLOB = "**/*.py"
EXPECTED_POLICY_PATH = "github/references/execution-environment.md"
EXPECTED_HELPER_TESTS_PATH = "scripts/validate-skills.sh"
EXPECTED_PYTHON_MATRIX_WORKFLOWS = {
    "launchplane-train-validation.yml",
    "validate-skills.yml",
}
EXPECTED_PYTHON_MATRIX_EXPRESSION = "${{ matrix.python-version }}"
EXPECTED_LAUNCHPLANE_RUNNER = {
    "repo": "cbusillo/launchplane",
    "workflow": "merge-train-runner.yml",
    "ref": "main",
    "runnerMode": "controller",
    "mutateDefault": False,
}
EXPECTED_REVISION_EVIDENCE_FIELDS = {
    "runnerWorkflow": "workflow_run.head_sha",
    "candidate": "result.candidate.candidate_sha",
    "landing": "result.landing_plan.entries[].merge_commit_sha",
}
EXPECTED_WRAPPER_FRAGMENTS = {
    "github/scripts/gh-comment": (
        "command -v uv",
        'exec uv run --no-project --no-config --python 3.12 python "$script_dir/github_comment.py"',
    ),
    "github/scripts/gh-issue": (
        "command -v uv",
        'exec uv run --no-project --no-config --python 3.12 python "$script_dir/github_issue.py"',
    ),
    "github/scripts/gh-with-env-token": (
        "command -v uv",
        'uv run --no-project --no-config --python 3.12 python "$@"',
    ),
    "github/scripts/github-repo-snapshot.sh": (
        "GITHUB_REPO_SNAPSHOT_PYTHON",
        "python_command=(uv run --no-project --no-config --python 3.12 python)",
    ),
    "scripts/validate-skills.sh": (
        "required_commands=(bash git gh jq node uv)",
        "uv run python --version",
    ),
}


def load_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc


def _format_observed_value(value: object) -> str:
    if value is None:
        return "missing"
    return json.dumps(
        value,
        sort_keys=True,
        default=lambda item: f"<{type(item).__name__}>",
    )


def load_pep723_module() -> ModuleType:
    path = ROOT / "scripts/update_pep723_dependencies.py"
    spec = importlib.util.spec_from_file_location("execution_environment_pep723", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load PEP 723 policy module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def discover_python_files(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(root / relative for relative in sorted(result.stdout.splitlines()) if relative)


def validate_python_metadata(paths: Sequence[Path]) -> list[str]:
    violations: list[str] = []
    module = load_pep723_module()
    try:
        scripts = module.load_script_metadata(paths)
    except module.DependencyPolicyError as exc:
        return [str(exc)]
    for script in scripts:
        requires_python = script.metadata.get("requires-python")
        if requires_python != EXPECTED_REQUIRES_PYTHON:
            violations.append(
                f"{script.path}: requires-python must be {EXPECTED_REQUIRES_PYTHON!r}, "
                f"not {requires_python!r}"
            )
    return violations


def validate_version_files(root: Path) -> list[str]:
    violations: list[str] = []
    python_path = root / ".python-version"
    try:
        python_version = python_path.read_text(encoding="utf-8").strip()
    except OSError:
        python_version = ""
    if python_version != EXPECTED_PYTHON:
        violations.append(
            f"{python_path}: expected Python {EXPECTED_PYTHON}, not {python_version or 'missing'}"
        )

    uv_path = root / "uv.toml"
    try:
        uv_config = tomllib.loads(uv_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        violations.append(f"{uv_path}: invalid or missing uv configuration: {exc}")
    else:
        required_version = uv_config.get("required-version")
        if required_version != EXPECTED_UV_REQUIREMENT:
            violations.append(
                f"{uv_path}: required-version must be {EXPECTED_UV_REQUIREMENT!r}, "
                f"not {_format_observed_value(required_version)}"
            )
    return violations


def validate_dependabot(root: Path) -> list[str]:
    path = root / ".github/dependabot.yml"
    try:
        config = load_yaml(path)
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(config, dict) or config.get("version") != 2:
        return [f"{path}: Dependabot config must use version 2"]
    updates = config.get("updates")
    if not isinstance(updates, list):
        return [f"{path}: Dependabot updates must be a list"]
    entries = [
        entry
        for entry in updates
        if isinstance(entry, dict) and entry.get("package-ecosystem") == "github-actions"
    ]
    if len(entries) != 1:
        return [f"{path}: expected exactly one github-actions update entry"]
    entry = entries[0]
    expected = {
        "directory": "/",
        "open-pull-requests-limit": 5,
    }
    violations: list[str] = []
    for key, expected_value in expected.items():
        observed_value = entry.get(key)
        if observed_value != expected_value:
            violations.append(
                f"{path}: github-actions {key} must be {expected_value!r}, "
                f"not {_format_observed_value(observed_value)}"
            )
    schedule = entry.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("interval") != "weekly":
        violations.append(f"{path}: github-actions schedule interval must be 'weekly'")
    return violations


def workflow_paths(root: Path) -> tuple[Path, ...]:
    workflow_root = root / ".github/workflows"
    return tuple(sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml"))))


def validate_workflows(root: Path) -> list[str]:
    violations: list[str] = []
    paths = workflow_paths(root)
    observed_names = {path.name for path in paths}
    for missing_name in sorted(EXPECTED_PYTHON_MATRIX_WORKFLOWS - observed_names):
        violations.append(f"{root / '.github/workflows' / missing_name}: required workflow is missing")
    for path in paths:
        try:
            workflow = load_yaml(path)
        except ValueError as exc:
            violations.append(str(exc))
            continue
        if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
            violations.append(f"{path}: workflow jobs must be a mapping")
            continue
        for job_name, job in workflow["jobs"].items():
            if not isinstance(job, dict):
                violations.append(f"{path}: job {job_name!r} must be a mapping")
                continue
            runner = job.get("runs-on")
            if runner is not None and runner != EXPECTED_RUNNER:
                violations.append(
                    f"{path}: job {job_name!r} must run on {EXPECTED_RUNNER!r}, "
                    f"not {_format_observed_value(runner)}"
                )
            uses_python_matrix = path.name in EXPECTED_PYTHON_MATRIX_WORKFLOWS
            if uses_python_matrix:
                strategy = job.get("strategy")
                matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
                python_versions = matrix.get("python-version") if isinstance(matrix, dict) else None
                if python_versions != [EXPECTED_PYTHON, EXPECTED_CURRENT_PYTHON]:
                    violations.append(
                        f"{path}: job {job_name!r} Python matrix must be "
                        f"[{EXPECTED_PYTHON!r}, {EXPECTED_CURRENT_PYTHON!r}]"
                    )
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                violations.append(f"{path}: job {job_name!r} steps must be a list")
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = step.get("uses")
                if not isinstance(uses, str) or not uses.startswith("astral-sh/setup-uv@"):
                    continue
                options = step.get("with")
                python_version = options.get("python-version") if isinstance(options, dict) else None
                expected_python_input = (
                    EXPECTED_PYTHON_MATRIX_EXPRESSION if uses_python_matrix else EXPECTED_PYTHON
                )
                if python_version != expected_python_input:
                    violations.append(
                        f"{path}: setup-uv in job {job_name!r} must set "
                        f"python-version to {expected_python_input!r}"
                    )
                if isinstance(options, dict) and any(
                    override in options for override in ("version", "version-file")
                ):
                    violations.append(
                        f"{path}: setup-uv in job {job_name!r} must resolve uv from uv.toml"
                    )
                cache_glob = (
                    options.get("cache-dependency-glob") if isinstance(options, dict) else None
                )
                if cache_glob != EXPECTED_CACHE_DEPENDENCY_GLOB:
                    violations.append(
                        f"{path}: setup-uv in job {job_name!r} must set "
                        f"cache-dependency-glob to {EXPECTED_CACHE_DEPENDENCY_GLOB!r}"
                    )
    return violations


def validate_metadata(root: Path) -> list[str]:
    path = root / ".github/github.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"{path}: invalid or missing metadata: {exc}"]
    violations: list[str] = []
    docs = config.get("docs")
    policy_path = docs.get("executionEnvironment") if isinstance(docs, dict) else None
    if policy_path != EXPECTED_POLICY_PATH:
        violations.append(
            f"{path}: docs.executionEnvironment must be {EXPECTED_POLICY_PATH!r}"
        )
    elif not (root / policy_path).is_file():
        violations.append(f"{root / policy_path}: execution-environment policy is missing")

    launchplane = config.get("launchplane")
    merge_train = launchplane.get("mergeTrain") if isinstance(launchplane, dict) else None
    runner = merge_train.get("githubActionsRunner") if isinstance(merge_train, dict) else None
    if not isinstance(runner, dict):
        violations.append(f"{path}: launchplane merge-train runner metadata is missing")
        runner = {}
    for key, expected_value in EXPECTED_LAUNCHPLANE_RUNNER.items():
        observed_value = runner.get(key)
        if observed_value != expected_value:
            violations.append(
                f"{path}: launchplane runner {key} must be {expected_value!r}, "
                f"not {_format_observed_value(observed_value)}"
            )
    evidence_fields = runner.get("revisionEvidenceFields") if isinstance(runner, dict) else None
    if evidence_fields != EXPECTED_REVISION_EVIDENCE_FIELDS:
        violations.append(
            f"{path}: launchplane runner revisionEvidenceFields must be "
            f"{EXPECTED_REVISION_EVIDENCE_FIELDS!r}"
        )
    quality_gate = config.get("qualityGate")
    syntax = quality_gate.get("syntax") if isinstance(quality_gate, dict) else None
    python_command = syntax.get("python") if isinstance(syntax, dict) else None
    expected_prefix = f"uv run --python {EXPECTED_PYTHON} python -m py_compile "
    if not isinstance(python_command, str) or not python_command.startswith(expected_prefix):
        violations.append(
            f"{path}: qualityGate.syntax.python must start with {expected_prefix!r}"
        )
    return violations


def validate_wrapper_runtime(root: Path) -> list[str]:
    violations: list[str] = []
    for relative_path, expected_fragments in EXPECTED_WRAPPER_FRAGMENTS.items():
        path = root / relative_path
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            violations.append(f"{path}: cannot read runtime wrapper: {exc}")
            continue
        for expected_fragment in expected_fragments:
            if expected_fragment not in content:
                violations.append(
                    f"{path}: missing execution-environment guard {expected_fragment!r}"
                )
    return violations


def _load_helper_tests_manifest(path: Path) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{path}: cannot read helper_tests manifest: {exc}") from exc
    lines = content.splitlines()
    try:
        start = next(
            index for index, line in enumerate(lines) if line.strip() == "helper_tests=("
        )
    except StopIteration:
        raise ValueError(f"{path}: helper_tests array could not be located")
    entries: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() == ")":
            break
        try:
            entries.extend(shlex.split(line, comments=True))
        except ValueError as exc:
            raise ValueError(f"{path}: invalid helper_tests entry: {exc}") from exc
    else:
        raise ValueError(f"{path}: helper_tests array is not terminated")
    if not entries:
        raise ValueError(f"{path}: helper_tests array is empty")
    return entries


def _imports_pytest(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "pytest" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            return True
    return False


def _declares_pytest_dependency(
    path: Path, source: str, pep723_module: ModuleType
) -> bool:
    try:
        script = pep723_module.parse_script(path, source)
    except (pep723_module.DependencyPolicyError, OSError, UnicodeError):
        return False
    if script is None:
        return False
    for value in script.dependencies:
        try:
            requirement = pep723_module.parse_requirement(value, require_pin=False)
        except pep723_module.DependencyPolicyError:
            continue
        if requirement.normalized_name == "pytest":
            return True
    return False


def _is_docstring_statement(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_pytest_main_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "main"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
    )


def _is_direct_pytest_exit_statement(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SystemExit"
        and any(_is_pytest_main_call(argument) for argument in node.exc.args)
    )


def _is_module_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if not isinstance(test.left, ast.Name) or test.left.id != "__name__":
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    if len(test.comparators) != 1:
        return False
    comparator = test.comparators[0]
    return isinstance(comparator, ast.Constant) and comparator.value == "__main__"


def _has_module_level_pytest_entrypoint(tree: ast.Module) -> bool:
    statement = next(
        (node for node in tree.body if _is_module_main_guard(node)),
        None,
    )
    if statement is None:
        return False
    first_executable_statement = next(
        (
            body_node
            for body_node in statement.body
            if not _is_docstring_statement(body_node)
            and not isinstance(body_node, (ast.Import, ast.ImportFrom))
        ),
        None,
    )
    return first_executable_statement is not None and _is_direct_pytest_exit_statement(
        first_executable_statement
    )


def validate_helper_tests(root: Path) -> list[str]:
    violations: list[str] = []
    pep723_module = load_pep723_module()
    manifest_path = root / EXPECTED_HELPER_TESTS_PATH
    try:
        helper_tests = _load_helper_tests_manifest(manifest_path)
    except ValueError as exc:
        return [str(exc)]

    for relative_path in helper_tests:
        path = root / relative_path
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            violations.append(f"{path}: cannot read declared helper: {exc}")
            continue
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path}: cannot parse declared helper: {exc}")
            continue
        uses_pytest = _imports_pytest(tree) or _declares_pytest_dependency(
            path, source, pep723_module
        )
        if uses_pytest and not _has_module_level_pytest_entrypoint(tree):
            violations.append(
                f"{path}: helpers that import or declare pytest must define a "
                "module-level if __name__ == '__main__': guard whose first "
                "executable statement raises SystemExit(pytest.main(...))"
            )
    return violations


def validate_repository(
    root: Path = ROOT, *, python_paths: Sequence[Path] | None = None
) -> list[str]:
    root = root.resolve()
    paths = tuple(python_paths) if python_paths is not None else discover_python_files(root)
    violations: list[str] = []
    violations.extend(validate_version_files(root))
    violations.extend(validate_dependabot(root))
    violations.extend(validate_workflows(root))
    violations.extend(validate_metadata(root))
    violations.extend(validate_wrapper_runtime(root))
    violations.extend(validate_helper_tests(root))
    violations.extend(validate_python_metadata(paths))
    return violations


def main() -> int:
    violations = validate_repository()
    if violations:
        print("execution-environment policy violations:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("ok execution-environment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
