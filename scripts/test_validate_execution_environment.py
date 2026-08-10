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
    write(root / "scripts/validate-skills.sh", helper_tests_manifest(["tool.py"]))
    return script


def helper_pytest_script(
    with_main_guard: bool = True,
    with_pytest_main: bool = True,
    propagate_pytest_exit: bool = True,
) -> str:
    lines = [
        "#!/usr/bin/env python3",
        FIXTURE_SCRIPT_MARKER,
        '# requires-python = ">=3.12"',
        '# dependencies = ["pytest==9.1.1"]',
        "# ///",
        "import pytest",
        "",
    ]
    if with_main_guard:
        if with_pytest_main:
            lines.append("if __name__ == '__main__':")
            if propagate_pytest_exit:
                lines.append("    raise SystemExit(pytest.main([__file__]))")
            else:
                lines.append("    pytest.main([__file__])")
        else:
            lines.extend(
                [
                    "if __name__ == '__main__':",
                    "    raise SystemExit(0)",
                ]
            )
    else:
        lines.append("print('ok')")
    return "\n".join(lines) + "\n"


def helper_tests_manifest(entries: list[str], skiplist: list[str] | None = None) -> str:
    helper_tests_lines = ["helper_tests=("]
    helper_tests_lines.extend(f"\t{entry}" for entry in entries)
    helper_tests_lines.append(")")
    helper_test_skiplist_lines = ["helper_test_skiplist=("]
    helper_test_skiplist_lines.extend(f"\t{entry}" for entry in skiplist or [])
    helper_test_skiplist_lines.append(")")
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "required_commands=(bash git gh jq node uv)",
            "uv run python --version",
            *helper_tests_lines,
            *helper_test_skiplist_lines,
        ]
    ) + "\n"


def assert_contains(violations: list[str], text: str) -> None:
    assert any(text in violation for violation in violations), violations


def test_valid_policy_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_valid_helper_pytest_entrypoint_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "tool.py", helper_pytest_script())
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_missing_main_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "tool.py", helper_pytest_script(with_main_guard=False))
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "must define a module-level if __name__ == '__main__': guard",
        )


def test_main_guard_without_pytest_main_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "tool.py", helper_pytest_script(with_pytest_main=False))
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "raises SystemExit(pytest.main(...))",
        )


def test_bare_pytest_main_call_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script(propagate_pytest_exit=False),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "raises SystemExit(pytest.main(...))",
        )


def test_nested_main_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script(with_main_guard=False)
            + "def run():\n"
            + "    if __name__ == '__main__':\n"
            + "        raise SystemExit(pytest.main([__file__]))\n",
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "must define a module-level if __name__ == '__main__': guard",
        )


def test_observed_value_format_handles_unknown_types() -> None:
    assert MODULE._format_observed_value(object()) == '"<object>"'


def test_missing_helper_tests_array_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "scripts/validate-skills.sh", "#!/usr/bin/env bash\n")
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helper_tests array could not be located",
        )


def test_empty_helper_tests_array_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "scripts/validate-skills.sh", helper_tests_manifest([]))
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helper_tests array is empty",
        )


def test_unterminated_helper_tests_array_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "scripts/validate-skills.sh",
            "#!/usr/bin/env bash\nhelper_tests=(\n\ttool.py\n",
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helper_tests array is not terminated",
        )


def test_syntax_invalid_helper_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "tool.py", "import pytest\nif True print('broken')\n")
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "cannot parse declared helper",
        )


def test_missing_declared_helper_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "scripts/validate-skills.sh",
            helper_tests_manifest(["missing-helper.py"]),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "cannot read declared helper",
        )


def test_skiplisted_pytest_cli_is_not_validated() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "skipped.py", helper_pytest_script(with_main_guard=False))
        write(
            root / "scripts/validate-skills.sh",
            helper_tests_manifest(["tool.py"], skiplist=["skipped.py"]),
        )
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
    test_valid_helper_pytest_entrypoint_passes,
    test_missing_main_guard_fails,
    test_main_guard_without_pytest_main_fails,
    test_bare_pytest_main_call_fails,
    test_nested_main_guard_fails,
    test_observed_value_format_handles_unknown_types,
    test_missing_helper_tests_array_fails,
    test_empty_helper_tests_array_fails,
    test_unterminated_helper_tests_array_fails,
    test_syntax_invalid_helper_fails,
    test_missing_declared_helper_fails,
    test_skiplisted_pytest_cli_is_not_validated,
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
