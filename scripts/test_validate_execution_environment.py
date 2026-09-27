#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.3",
# ]
# ///
"""Focused tests for validate_execution_environment.py."""

from __future__ import annotations

import importlib.util
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
    script = root / "test_tool.py"
    write(script, python_script())
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
        write(root / "test_tool.py", modified)
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
        write(root / "test_tool.py", modified)
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
        write(root / "test_tool.py", helper_pytest_script())
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_missing_main_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "test_tool.py", helper_pytest_script(with_main_guard=False))
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "must define a module-level if __name__ == '__main__': guard",
        )


def test_main_guard_without_pytest_main_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(root / "test_tool.py", helper_pytest_script(with_pytest_main=False))
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "raises SystemExit(pytest.main(...))",
        )


def test_bare_pytest_main_call_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
            helper_without_dependencies("import pytest_asyncio"),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_relative_pytest_import_passes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "test_tool.py",
            helper_without_dependencies("from .pytest import helper"),
        )
        assert MODULE.validate_repository(root, python_paths=[script]) == []


def test_pytest_dependency_without_import_requires_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
        write(root / "test_tool.py", modified)
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
        write(root / "test_tool.py", modified)
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
            root / "test_tool.py",
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
            root / "test_tool.py",
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
        write(root / "test_tool.py", modified)
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
        write(root / "test_tool.py", modified)
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
        write(root / "test_tool.py", modified)
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
        write(root / "test_tool.py", modified)
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "first executable statement raises SystemExit(pytest.main(...))",
        )


def test_nested_main_guard_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(
            root / "test_tool.py",
            helper_pytest_script(with_main_guard=False)
            + "def run():\n"
            + "    if __name__ == '__main__':\n"
            + "        raise SystemExit(pytest.main([__file__]))\n",
        )
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "must define a module-level if __name__ == '__main__': guard",
        )


def test_syntax_invalid_helper_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(script, "import pytest\nif True print('broken')\n")
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "cannot parse helper",
        )


def test_requires_python_disagreeing_with_python_version_fails() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(script, python_script(">=3.13"))
        assert_contains(
            MODULE.validate_repository(root, python_paths=[script]),
            "requires-python must be '>=3.12'",
        )


def main() -> int:
    tests = [
        candidate
        for name, candidate in globals().items()
        if name.startswith("test_") and callable(candidate)
    ]
    for test in tests:
        test()
    print(f"execution-environment tests passed ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
