#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.3",
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


def helper_without_dependencies(*body_lines: str) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            FIXTURE_SCRIPT_MARKER,
            '# requires-python = ">=3.12"',
            "# dependencies = []",
            "# ///",
            *body_lines,
        ]
    ) + "\n"


def helper_tests_manifest(
    entries: list[str],
    skiplist: list[str] | None = None,
    explicit_validators: list[str] | None = None,
) -> str:
    helper_tests_lines = ["helper_tests=("]
    helper_tests_lines.extend(f"\t{entry}" for entry in entries)
    helper_tests_lines.append(")")
    explicit_validator_lines = ["explicit_helper_validators=("]
    explicit_validator_lines.extend(
        f"\t{entry}" for entry in explicit_validators or []
    )
    explicit_validator_lines.append(")")
    helper_test_skiplist_lines = ["helper_test_skiplist=("]
    helper_test_skiplist_lines.extend(f"\t{entry}" for entry in skiplist or [])
    helper_test_skiplist_lines.append(")")
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "required_commands=(bash git gh jq node uv)",
            "uv run python --version",
            *(f"uv run {entry}" for entry in explicit_validators or []),
            *helper_tests_lines,
            *explicit_validator_lines,
            *helper_test_skiplist_lines,
        ]
    ) + "\n"


def assert_contains(violations: list[str], text: str) -> None:
    assert any(text in violation for violation in violations), violations


def assert_pytest_entrypoint_rejected(statement: str) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "    raise SystemExit(pytest.main([__file__]))",
            f"    {statement}",
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "raises SystemExit(pytest.main(...))",
        )


def assert_module_prefix_rejected(*lines: str) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "if __name__ == '__main__':",
            "\n".join((*lines, "", "if __name__ == '__main__':")),
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "no direct module-level exit before it",
        )


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


def test_system_exit_with_leading_argument_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(0, pytest.main([__file__]))"
    )


def test_system_exit_with_trailing_argument_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(pytest.main([__file__]), 0)"
    )


def test_system_exit_with_keyword_argument_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(code=pytest.main([__file__]))"
    )


def test_system_exit_with_wrapped_pytest_call_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(int(pytest.main([__file__])))"
    )


def test_pytest_main_selecting_other_file_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(pytest.main(['other.py']))"
    )


def test_pytest_main_without_helper_file_fails() -> None:
    assert_pytest_entrypoint_rejected("raise SystemExit(pytest.main([]))")


def test_pytest_main_with_additional_selector_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(pytest.main([__file__, 'other.py']))"
    )


def test_pytest_main_with_keyword_arguments_fails() -> None:
    assert_pytest_entrypoint_rejected(
        "raise SystemExit(pytest.main([__file__], plugins=[]))"
    )


def test_declared_pytest_dependency_catches_aliased_dynamic_import() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            "#!/usr/bin/env python3\n"
            + FIXTURE_SCRIPT_MARKER
            + '\n# requires-python = ">=3.12"\n'
            + '# dependencies = ["pytest==9.1.1"]\n'
            + "# ///\n"
            + "import importlib\n"
            + "load_module = importlib.import_module\n"
            + "pytest = load_module('pytest')\n",
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_dynamic_pytest_import_without_dependency_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "import importlib",
                'pytest = importlib.import_module("pytest")',
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_bare_import_module_pytest_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "from importlib import import_module",
                'pytest = import_module("pytest")',
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_dunder_import_pytest_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies('pytest = __import__("pytest")'),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_keyword_dynamic_pytest_import_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "import importlib",
                'pytest = importlib.import_module(name="pytest")',
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_dynamic_pytest_submodule_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "import importlib",
                'importlib.import_module("pytest.__main__")',
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_static_pytest_submodule_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies("import pytest.__main__"),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_dynamic_pytest_import_with_entrypoint_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "import importlib",
                'pytest = importlib.import_module("pytest")',
                "",
                "if __name__ == '__main__':",
                "    raise SystemExit(pytest.main([__file__]))",
            ),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_pytest_string_without_import_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                'module_name = "pytest"',
                'message = f"module: {module_name}"',
            ),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_find_spec_pytest_probe_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "import importlib.util",
                'importlib.util.find_spec("pytest")',
            ),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_non_pytest_dynamic_import_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies(
                "import importlib",
                'importlib.import_module("yaml")',
            ),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_pytest_plugin_import_without_pytest_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies("import pytest_asyncio"),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_relative_pytest_import_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_without_dependencies("from .pytest import helper"),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_pytest_dependency_without_import_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            "#!/usr/bin/env python3\n"
            + FIXTURE_SCRIPT_MARKER
            + '\n# requires-python = ">=3.12"\n'
            + '# dependencies = ["pytest==9.1.1"]\n'
            + "# ///\n"
            + "def test_example():\n"
            + "    assert True\n",
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_pytest_import_without_dependency_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            "#!/usr/bin/env python3\n"
            + FIXTURE_SCRIPT_MARKER
            + '\n# requires-python = ">=3.12"\n'
            + "# dependencies = []\n"
            + "# ///\n"
            + "import pytest\n",
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "helpers that import or declare pytest",
        )


def test_early_exit_before_pytest_entrypoint_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "    raise SystemExit(pytest.main([__file__]))",
                "    raise SystemExit(0)\n"
                "    raise SystemExit(pytest.main([__file__]))",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
        )


def test_dead_code_pytest_entrypoint_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "    raise SystemExit(pytest.main([__file__]))",
                "    if False:\n"
                "        raise SystemExit(pytest.main([__file__]))",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
        )


def test_later_duplicate_pytest_guard_does_not_bypass_early_exit() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "if __name__ == '__main__':\n"
                "    raise SystemExit(pytest.main([__file__]))",
                "if __name__ == '__main__':\n"
                "    raise SystemExit(0)\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    raise SystemExit(pytest.main([__file__]))",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
        )


def test_module_level_system_exit_call_before_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "if __name__ == '__main__':",
                "raise SystemExit(0)\n\nif __name__ == '__main__':",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "no direct module-level exit before it",
        )


def test_module_level_bare_system_exit_before_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "if __name__ == '__main__':",
                "raise SystemExit\n\nif __name__ == '__main__':",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "no direct module-level exit before it",
        )


def test_module_level_sys_exit_before_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "if __name__ == '__main__':",
                "import sys\nsys.exit(0)\n\nif __name__ == '__main__':",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "no direct module-level exit before it",
        )


def test_module_level_os_exit_before_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "tool.py",
            helper_pytest_script().replace(
                "if __name__ == '__main__':",
                "import os\nos._exit(0)\n\nif __name__ == '__main__':",
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "no direct module-level exit before it",
        )


def test_assigned_sys_exit_alias_before_guard_fails() -> None:
    assert_module_prefix_rejected("import sys", "stop = sys.exit", "stop(0)")


def test_assigned_os_exit_alias_before_guard_fails() -> None:
    assert_module_prefix_rejected("import os", "stop = os._exit", "stop(0)")


def test_assigned_system_exit_alias_before_guard_fails() -> None:
    assert_module_prefix_rejected("Stop = SystemExit", "raise Stop(0)")


def test_chained_exit_alias_before_guard_fails() -> None:
    assert_module_prefix_rejected(
        "import sys",
        "first = sys.exit",
        "second = first",
        "second(0)",
    )


def test_aliased_sys_module_exit_before_guard_fails() -> None:
    assert_module_prefix_rejected(
        "import sys as system",
        "stop = system.exit",
        "stop(0)",
    )


def test_imported_exit_alias_before_guard_fails() -> None:
    assert_module_prefix_rejected("from sys import exit as stop", "stop(0)")


def test_imported_system_exit_alias_before_guard_fails() -> None:
    assert_module_prefix_rejected(
        "from builtins import SystemExit as Stop",
        "raise Stop(0)",
    )


def test_dotted_os_import_preserves_direct_exit_detection() -> None:
    assert_module_prefix_rejected("import os.path", "os._exit(0)")


def test_bare_builtins_system_exit_before_guard_fails() -> None:
    assert_module_prefix_rejected("import builtins", "raise builtins.SystemExit")


def test_relative_import_does_not_create_exit_alias() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "if __name__ == '__main__':",
            "from .sys import exit as stop\n"
            "stop(0)\n"
            "\n"
            "if __name__ == '__main__':",
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_rebound_exit_alias_before_guard_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "if __name__ == '__main__':",
            "import sys\n"
            "stop = sys.exit\n"
            "stop = print\n"
            "stop('continuing')\n"
            "\n"
            "if __name__ == '__main__':",
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_nested_exit_before_guard_does_not_fail() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "if __name__ == '__main__':",
            "def stop_later():\n"
            "    raise SystemExit(0)\n"
            "\n"
            "if __name__ == '__main__':",
        )
        assert modified != original
        write(
            root / "tool.py",
            modified,
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_docstring_and_import_before_pytest_entrypoint_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "    raise SystemExit(pytest.main([__file__]))",
            '    """Run the helper tests."""\n'
            "    import os\n"
            "    raise SystemExit(pytest.main([__file__]))",
        )
        assert modified != original
        write(
            root / "tool.py",
            modified,
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_import_without_docstring_before_pytest_entrypoint_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "    raise SystemExit(pytest.main([__file__]))",
            "    import os\n"
            "    raise SystemExit(pytest.main([__file__]))",
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_string_after_import_before_pytest_entrypoint_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "    raise SystemExit(pytest.main([__file__]))",
            "    import os\n"
            '    "Not a leading documentation string."\n'
            "    raise SystemExit(pytest.main([__file__]))",
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
        )


def test_second_leading_string_before_pytest_entrypoint_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "    raise SystemExit(pytest.main([__file__]))",
            '    "First documentation string."\n'
            '    "Second documentation string."\n'
            "    raise SystemExit(pytest.main([__file__]))",
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
        )


def test_string_only_guard_body_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        original = helper_pytest_script()
        modified = original.replace(
            "    raise SystemExit(pytest.main([__file__]))",
            '    "No pytest entrypoint."',
        )
        assert modified != original
        write(root / "tool.py", modified)
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
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


def test_validate_skills_manifest_classifies_explicit_validators() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = repo_root / "scripts/validate-skills.sh"
    helper_tests, explicit_validators, skiplist = MODULE._load_helper_tests_manifest(manifest_path)
    assert "skill-creator/scripts/quick_validate.py" in explicit_validators
    assert "skill-creator/scripts/validate-command-policy-simulator.py" in explicit_validators
    assert "skill-creator/scripts/quick_validate.py" not in helper_tests
    assert "skill-creator/scripts/validate-command-policy-simulator.py" not in helper_tests
    assert set(helper_tests).isdisjoint(explicit_validators)
    assert set(helper_tests).isdisjoint(skiplist)
    assert set(explicit_validators).isdisjoint(skiplist)


def test_missing_explicit_helper_validator_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "scripts/validate-skills.sh",
            helper_tests_manifest(
                ["tool.py"],
                explicit_validators=["missing-explicit.py"],
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "missing-explicit.py: missing explicit helper validator",
        )


def test_helper_manifest_categories_must_be_disjoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "explicit.py", python_script())
        write(
            root / "scripts/validate-skills.sh",
            helper_tests_manifest(
                ["tool.py", "explicit.py"],
                explicit_validators=["explicit.py"],
            ),
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "explicit.py appears in multiple helper categories: helper_tests, explicit_helper_validators",
        )


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
    test_system_exit_with_leading_argument_fails,
    test_system_exit_with_trailing_argument_fails,
    test_system_exit_with_keyword_argument_fails,
    test_system_exit_with_wrapped_pytest_call_fails,
    test_pytest_main_selecting_other_file_fails,
    test_pytest_main_without_helper_file_fails,
    test_pytest_main_with_additional_selector_fails,
    test_pytest_main_with_keyword_arguments_fails,
    test_declared_pytest_dependency_catches_aliased_dynamic_import,
    test_dynamic_pytest_import_without_dependency_requires_entrypoint,
    test_bare_import_module_pytest_requires_entrypoint,
    test_dunder_import_pytest_requires_entrypoint,
    test_keyword_dynamic_pytest_import_requires_entrypoint,
    test_dynamic_pytest_submodule_requires_entrypoint,
    test_static_pytest_submodule_requires_entrypoint,
    test_dynamic_pytest_import_with_entrypoint_passes,
    test_pytest_string_without_import_passes,
    test_find_spec_pytest_probe_passes,
    test_non_pytest_dynamic_import_passes,
    test_pytest_plugin_import_without_pytest_passes,
    test_relative_pytest_import_passes,
    test_pytest_dependency_without_import_requires_entrypoint,
    test_pytest_import_without_dependency_requires_entrypoint,
    test_early_exit_before_pytest_entrypoint_fails,
    test_dead_code_pytest_entrypoint_fails,
    test_later_duplicate_pytest_guard_does_not_bypass_early_exit,
    test_module_level_system_exit_call_before_guard_fails,
    test_module_level_bare_system_exit_before_guard_fails,
    test_module_level_sys_exit_before_guard_fails,
    test_module_level_os_exit_before_guard_fails,
    test_assigned_sys_exit_alias_before_guard_fails,
    test_assigned_os_exit_alias_before_guard_fails,
    test_assigned_system_exit_alias_before_guard_fails,
    test_chained_exit_alias_before_guard_fails,
    test_aliased_sys_module_exit_before_guard_fails,
    test_imported_exit_alias_before_guard_fails,
    test_imported_system_exit_alias_before_guard_fails,
    test_dotted_os_import_preserves_direct_exit_detection,
    test_bare_builtins_system_exit_before_guard_fails,
    test_relative_import_does_not_create_exit_alias,
    test_rebound_exit_alias_before_guard_passes,
    test_nested_exit_before_guard_does_not_fail,
    test_docstring_and_import_before_pytest_entrypoint_passes,
    test_import_without_docstring_before_pytest_entrypoint_passes,
    test_string_after_import_before_pytest_entrypoint_fails,
    test_second_leading_string_before_pytest_entrypoint_fails,
    test_string_only_guard_body_fails,
    test_nested_main_guard_fails,
    test_observed_value_format_handles_unknown_types,
    test_missing_helper_tests_array_fails,
    test_empty_helper_tests_array_fails,
    test_unterminated_helper_tests_array_fails,
    test_syntax_invalid_helper_fails,
    test_missing_declared_helper_fails,
    test_skiplisted_pytest_cli_is_not_validated,
    test_validate_skills_manifest_classifies_explicit_validators,
    test_missing_explicit_helper_validator_fails,
    test_helper_manifest_categories_must_be_disjoint,
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
