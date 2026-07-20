#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.2",
#     "PyYAML==6.0.3",
# ]
# ///
"""Focused tests for validate_execution_environment.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("validate_execution_environment.py")
FIXTURE_SCRIPT_MARKER = "# /// " "script"
SPEC = importlib.util.spec_from_file_location("validate_execution_environment", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def python_script(requires_python: str = ">=3.12") -> str:
    return f'''#!/usr/bin/env python3
{FIXTURE_SCRIPT_MARKER}
# requires-python = "{requires_python}"
# dependencies = []
# ///
print("ok")
'''


def valid_root(root: Path) -> Path:
    write(root / ".python-version", "3.12\n")
    write(root / "uv.toml", 'required-version = ">=0.11.29,<1"\n')
    write(
        root / ".github/dependabot.yml",
        '''version: 2
updates:
  - package-ecosystem: github-actions
    directory: "/"
    open-pull-requests-limit: 5
    schedule:
      interval: weekly
''',
    )
    matrix_workflow = '''name: CI
on: [push]
jobs:
  test:
    name: Validate Python ${{ matrix.python-version }}
    runs-on: ubuntu-24.04
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.12", "3.14"]
    steps:
      - uses: astral-sh/setup-uv@1111111111111111111111111111111111111111
        with:
          cache-dependency-glob: "**/*.py"
          python-version: ${{ matrix.python-version }}
'''
    for workflow_name in MODULE.EXPECTED_PYTHON_MATRIX_WORKFLOWS:
        write(root / ".github/workflows" / workflow_name, matrix_workflow)
    write(
        root / ".github/workflows/update-pep723-dependencies.yml",
        '''name: Update
on: [workflow_dispatch]
jobs:
  update:
    runs-on: ubuntu-24.04
    steps:
      - uses: astral-sh/setup-uv@1111111111111111111111111111111111111111
        with:
          cache-dependency-glob: "**/*.py"
          python-version: "3.12"
''',
    )
    metadata = {
        "docs": {"executionEnvironment": "github/references/execution-environment.md"},
        "qualityGate": {
            "syntax": {
                "python": "uv run --python 3.12 python -m py_compile tool.py"
            }
        },
        "launchplane": {
            "mergeTrain": {
                "githubActionsRunner": {
                    **dict(MODULE.EXPECTED_LAUNCHPLANE_RUNNER),
                    "revisionEvidenceFields": dict(MODULE.EXPECTED_REVISION_EVIDENCE_FIELDS)
                }
            }
        },
    }
    write(root / ".github/github.json", json.dumps(metadata))
    write(root / "github/references/execution-environment.md", "# Policy\n")
    for relative_path, fragments in MODULE.EXPECTED_WRAPPER_FRAGMENTS.items():
        write(root / relative_path, "\n".join(fragments))
    script = root / "tool.py"
    write(script, python_script())
    return script


def assert_contains(violations: list[str], text: str) -> None:
    assert any(text in violation for violation in violations), violations


def test_valid_policy_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_dependabot_drift_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / ".github/dependabot.yml", "version: 2\nupdates: []\n")
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "exactly one github-actions",
        )


def test_runner_and_python_drift_fail() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        workflow = root / ".github/workflows/validate-skills.yml"
        content = workflow.read_text(encoding="utf-8")
        content = content.replace("ubuntu-24.04", "ubuntu-latest")
        content = content.replace('["3.12", "3.14"]', '["3.12"]')
        content = content.replace(
            "python-version: ${{ matrix.python-version }}",
            'python-version: "3.14"',
        )
        content = content.replace('cache-dependency-glob: "**/*.py"', 'cache-dependency-glob: "uv.lock"')
        write(workflow, content)
        violations = MODULE.validate_repository(root, python_paths=[script])
        assert_contains(violations, "ubuntu-24.04")
        assert_contains(violations, "Python matrix")
        assert_contains(violations, "matrix.python-version")
        assert_contains(violations, "cache-dependency-glob")


def test_version_and_pep_metadata_drift_fail() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / ".python-version", "3.14\n")
        write(root / "uv.toml", 'required-version = ">=0.11.30,<1"\n')
        write(script, python_script(">=3.13"))
        violations = MODULE.validate_repository(root, python_paths=[script])
        assert_contains(violations, "expected Python 3.12")
        assert_contains(violations, "required-version")
        assert_contains(violations, "requires-python")


def test_routing_evidence_drift_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        metadata_path = root / ".github/github.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        runner = metadata["launchplane"]["mergeTrain"]["githubActionsRunner"]
        runner["ref"] = "develop"
        runner["revisionEvidenceFields"] = {}
        write(metadata_path, json.dumps(metadata))
        violations = MODULE.validate_repository(root, python_paths=[script])
        assert_contains(violations, "runner ref")
        assert_contains(violations, "revisionEvidenceFields")


def test_wrapper_runtime_drift_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "github/scripts/gh-issue", "exec python3 github_issue.py\n")
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "execution-environment guard",
        )


TESTS = [
    test_valid_policy_passes,
    test_dependabot_drift_fails,
    test_runner_and_python_drift_fail,
    test_version_and_pep_metadata_drift_fail,
    test_routing_evidence_drift_fails,
    test_wrapper_runtime_drift_fails,
]


def main() -> int:
    for test in TESTS:
        test()
    print(f"execution-environment tests passed ({len(TESTS)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
