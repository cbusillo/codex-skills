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
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("validate_execution_environment.py")
FIXTURE_SCRIPT_MARKER = "# /// script"
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


def helper_pytest_script(with_main_guard: bool = True) -> str:
    return helper_script(
        "import pytest",
        "",
        *(("if __name__ == '__main__':", "    raise SystemExit(pytest.main([__file__]))") if with_main_guard else ("print('ok')",)),
        dependencies=("pytest==9.1.1",),
    )


def helper_script(*body_lines: str, dependencies: tuple[str, ...] = ()) -> str:
    return (
        "\n".join(
            [
                "#!/usr/bin/env python3",
                FIXTURE_SCRIPT_MARKER,
                '# requires-python = ">=3.12"',
                f"# dependencies = {json.dumps(dependencies)}",
                "# ///",
                *body_lines,
            ]
        )
        + "\n"
    )


def validate_script(content: str) -> list[str]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        script = valid_root(root)
        write(script, content)
        return MODULE.validate_repository(root, python_paths=[script])


def with_entry(statement: str) -> str:
    original = helper_pytest_script()
    modified = original.replace("    raise SystemExit(pytest.main([__file__]))", f"    {statement}")
    assert modified != original, statement
    return modified


def with_prefix(prefix: str) -> str:
    original = helper_pytest_script()
    guard = "if __name__ == '__main__':"
    modified = original.replace(guard, f"{prefix}\n\n{guard}")
    assert modified != original, prefix
    return modified


class ExecutionEnvironmentTests(unittest.TestCase):
    def test_valid_helpers_do_not_trigger_violations(self) -> None:
        for content in (
            python_script(),
            helper_pytest_script(),
            helper_script(
                "import importlib",
                'pytest = importlib.import_module("pytest")',
                "",
                "if __name__ == '__main__':",
                "    raise SystemExit(pytest.main([__file__]))",
            ),
            helper_script('module_name = "pytest"', 'message = f"module: {module_name}"'),
            helper_script("import importlib.util", 'importlib.util.find_spec("pytest")'),
            helper_script("import importlib", 'importlib.import_module("yaml")'),
            helper_script("import pytest_asyncio"),
            helper_script("from .pytest import helper"),
            with_prefix("from .sys import exit as stop\nstop(0)"),
            with_prefix("import sys\nstop = sys.exit\nstop = print\nstop('continuing')"),
            with_prefix("def stop_later():\n    raise SystemExit(0)"),
            with_entry('"""Run the helper tests."""\n    import os\n    raise SystemExit(pytest.main([__file__]))'),
            with_entry("import os\n    raise SystemExit(pytest.main([__file__]))"),
        ):
            with self.subTest(content=content):
                self.assertEqual(validate_script(content), [])

    def test_missing_or_bypassed_pytest_entrypoints_are_rejected(self) -> None:
        for content in (
            helper_pytest_script(with_main_guard=False),
            with_entry("raise SystemExit(0)"),
            with_entry("pytest.main([__file__])"),
            "#!/usr/bin/env python3\n"
            + FIXTURE_SCRIPT_MARKER
            + '\n# requires-python = ">=3.12"\n'
            + '# dependencies = ["pytest==9.1.1"]\n'
            + "# ///\n"
            + "import importlib\n"
            + "load_module = importlib.import_module\n"
            + "pytest = load_module('pytest')\n",
            helper_script("import importlib", 'pytest = importlib.import_module("pytest")'),
            helper_script("from importlib import import_module", 'pytest = import_module("pytest")'),
            helper_script('pytest = __import__("pytest")'),
            helper_script("import importlib", 'pytest = importlib.import_module(name="pytest")'),
            helper_script("import importlib", 'importlib.import_module("pytest.__main__")'),
            helper_script("import pytest.__main__"),
            "#!/usr/bin/env python3\n"
            + FIXTURE_SCRIPT_MARKER
            + '\n# requires-python = ">=3.12"\n'
            + '# dependencies = ["pytest==9.1.1"]\n'
            + "# ///\n"
            + "def test_example():\n"
            + "    assert True\n",
            "#!/usr/bin/env python3\n"
            + FIXTURE_SCRIPT_MARKER
            + '\n# requires-python = ">=3.12"\n'
            + "# dependencies = []\n"
            + "# ///\n"
            + "import pytest\n",
            with_entry("raise SystemExit(0)\n    raise SystemExit(pytest.main([__file__]))"),
            with_entry("if False:\n        raise SystemExit(pytest.main([__file__]))"),
            helper_pytest_script().replace(
                "if __name__ == '__main__':\n    raise SystemExit(pytest.main([__file__]))",
                "if __name__ == '__main__':\n    raise SystemExit(0)\n\nif __name__ == '__main__':\n    raise SystemExit(pytest.main([__file__]))",
            ),
            with_prefix("raise SystemExit(0)"),
            with_prefix("raise SystemExit"),
            with_prefix("import sys\nsys.exit(0)"),
            with_prefix("import os\nos._exit(0)"),
            with_entry('import os\n    "Not a leading documentation string."\n    raise SystemExit(pytest.main([__file__]))'),
            with_entry(
                '"First documentation string."\n    "Second documentation string."\n    raise SystemExit(pytest.main([__file__]))'
            ),
            with_entry('"No pytest entrypoint."'),
            helper_pytest_script(with_main_guard=False)
            + "def run():\n"
            + "    if __name__ == '__main__':\n"
            + "        raise SystemExit(pytest.main([__file__]))\n",
            with_entry("raise SystemExit(0, pytest.main([__file__]))"),
            with_entry("raise SystemExit(pytest.main([__file__]), 0)"),
            with_entry("raise SystemExit(code=pytest.main([__file__]))"),
            with_entry("raise SystemExit(int(pytest.main([__file__])))"),
            with_entry("raise SystemExit(pytest.main(['other.py']))"),
            with_entry("raise SystemExit(pytest.main([]))"),
            with_entry("raise SystemExit(pytest.main([__file__, 'other.py']))"),
            with_entry("raise SystemExit(pytest.main([__file__], plugins=[]))"),
            with_prefix("import sys\nstop = sys.exit\nstop(0)"),
            with_prefix("import os\nstop = os._exit\nstop(0)"),
            with_prefix("Stop = SystemExit\nraise Stop(0)"),
            with_prefix("import sys\nfirst = sys.exit\nsecond = first\nsecond(0)"),
            with_prefix("import sys as system\nstop = system.exit\nstop(0)"),
            with_prefix("from sys import exit as stop\nstop(0)"),
            with_prefix("from builtins import SystemExit as Stop\nraise Stop(0)"),
            with_prefix("import os.path\nos._exit(0)"),
            with_prefix("import builtins\nraise builtins.SystemExit"),
        ):
            with self.subTest(content=content):
                self.assertEqual(len(validate_script(content)), 1)

    def test_syntax_and_metadata_errors_are_rejected(self) -> None:
        for content in (
            python_script() + "if True print('broken')\n",
            python_script(">=3.13"),
        ):
            with self.subTest(content=content):
                self.assertEqual(len(validate_script(content)), 1)


if __name__ == "__main__":
    unittest.main()
