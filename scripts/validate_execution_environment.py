#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.3",
# ]
# ///
"""Validate repository execution-environment invariants.

Only invariants that break real execution are checked here: every PEP 723
script agrees with the repository's minimum Python in `.python-version`, and
every helper test that uses pytest can run itself under `uv run`. Workflow,
wrapper, and config text is enforced where it executes, not by restating it.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]


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
        ["git", "-C", str(root), "ls-files", "--cached", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(root / relative for relative in sorted(result.stdout.splitlines()) if relative)


def is_helper_test(path: Path) -> bool:
    return path.name.startswith("test_") or "validate" in path.name


def expected_requires_python(root: Path) -> str | None:
    try:
        minimum = (root / ".python-version").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return f">={minimum}" if minimum else None


def validate_python_metadata(root: Path, paths: Sequence[Path]) -> list[str]:
    expected = expected_requires_python(root)
    if expected is None:
        return [f"{root / '.python-version'}: missing minimum Python version"]
    violations: list[str] = []
    module = load_pep723_module()
    try:
        scripts = module.load_script_metadata(paths)
    except module.DependencyPolicyError as exc:
        return [str(exc)]
    for script in scripts:
        requires_python = script.metadata.get("requires-python")
        if requires_python != expected:
            violations.append(
                f"{script.path}: requires-python must be {expected!r} to match "
                f".python-version, not {requires_python!r}"
            )
    return violations


def _is_pytest_module_name(value: object) -> bool:
    return isinstance(value, str) and (
        value == "pytest" or value.startswith("pytest.")
    )


def _is_dynamic_pytest_import(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    if isinstance(function, ast.Name):
        function_name = function.id
    elif isinstance(function, ast.Attribute):
        function_name = function.attr
    else:
        return False
    if function_name not in {"import_module", "__import__"}:
        return False
    if node.args:
        module_name = node.args[0]
    else:
        module_name = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "name"),
            None,
        )
    return (
        isinstance(module_name, ast.Constant)
        and _is_pytest_module_name(module_name.value)
    )


def _imports_pytest(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_pytest_module_name(alias.name) for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_pytest_module_name(node.module)
        ):
            return True
        elif _is_dynamic_pytest_import(node):
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
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "main"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and len(node.args) == 1
        and not node.keywords
    ):
        return False
    arguments = node.args[0]
    if not isinstance(arguments, ast.List) or len(arguments.elts) != 1:
        return False
    helper_path = arguments.elts[0]
    return isinstance(helper_path, ast.Name) and helper_path.id == "__file__"


def _is_direct_pytest_exit_statement(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SystemExit"
        and len(node.exc.args) == 1
        and not node.exc.keywords
        and _is_pytest_main_call(node.exc.args[0])
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


class _ExitAliases:
    def __init__(self) -> None:
        self.system_exit = {"SystemExit"}
        self.exit_functions: set[str] = set()
        self.sys_modules = {"sys"}
        self.os_modules = {"os"}
        self.builtins_modules: set[str] = set()


def _clear_exit_alias(name: str, aliases: _ExitAliases) -> None:
    aliases.system_exit.discard(name)
    aliases.exit_functions.discard(name)
    aliases.sys_modules.discard(name)
    aliases.os_modules.discard(name)
    aliases.builtins_modules.discard(name)


def _exit_alias_group(node: ast.AST, aliases: _ExitAliases) -> set[str] | None:
    if isinstance(node, ast.Name):
        for group in (
            aliases.system_exit,
            aliases.exit_functions,
            aliases.sys_modules,
            aliases.os_modules,
            aliases.builtins_modules,
        ):
            if node.id in group:
                return group
        return None
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
        return None
    if node.value.id in aliases.sys_modules and node.attr == "exit":
        return aliases.exit_functions
    if node.value.id in aliases.os_modules and node.attr == "_exit":
        return aliases.exit_functions
    if node.value.id in aliases.builtins_modules and node.attr == "SystemExit":
        return aliases.system_exit
    return None


def _update_exit_aliases(node: ast.stmt, aliases: _ExitAliases) -> None:
    if isinstance(node, ast.Import):
        for imported in node.names:
            module_name = imported.name.split(".", 1)[0]
            bound_name = imported.asname or module_name
            _clear_exit_alias(bound_name, aliases)
            if imported.name == "sys" or (
                imported.asname is None and module_name == "sys"
            ):
                aliases.sys_modules.add(bound_name)
            elif imported.name == "os" or (
                imported.asname is None and module_name == "os"
            ):
                aliases.os_modules.add(bound_name)
            elif imported.name == "builtins" or (
                imported.asname is None and module_name == "builtins"
            ):
                aliases.builtins_modules.add(bound_name)
        return
    if isinstance(node, ast.ImportFrom):
        for imported in node.names:
            bound_name = imported.asname or imported.name
            _clear_exit_alias(bound_name, aliases)
            if node.level != 0:
                continue
            if node.module == "sys" and imported.name == "exit":
                aliases.exit_functions.add(bound_name)
            elif node.module == "os" and imported.name == "_exit":
                aliases.exit_functions.add(bound_name)
            elif node.module == "builtins" and imported.name == "SystemExit":
                aliases.system_exit.add(bound_name)
        return

    if isinstance(node, ast.Assign):
        value = node.value
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        value = node.value
        targets = [node.target]
    elif isinstance(node, ast.AugAssign):
        value = None
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        value = None
        targets = node.targets
    else:
        return

    source_group = _exit_alias_group(value, aliases) if value is not None else None
    for target in targets:
        if not isinstance(target, ast.Name):
            continue
        _clear_exit_alias(target.id, aliases)
        if source_group is not None:
            source_group.add(target.id)


def _is_direct_module_exit_statement(
    node: ast.stmt, aliases: _ExitAliases
) -> bool:
    if isinstance(node, ast.Raise):
        exception = node.exc
        reference = exception.func if isinstance(exception, ast.Call) else exception
        return _exit_alias_group(reference, aliases) is aliases.system_exit
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    function = node.value.func
    return _exit_alias_group(function, aliases) is aliases.exit_functions


def _has_direct_module_exit(statements: list[ast.stmt]) -> bool:
    aliases = _ExitAliases()
    for statement in statements:
        if _is_direct_module_exit_statement(statement, aliases):
            return True
        _update_exit_aliases(statement, aliases)
    return False


def _guard_entrypoint_statement(body: list[ast.stmt]) -> ast.stmt | None:
    index = 1 if body and _is_docstring_statement(body[0]) else 0
    while index < len(body) and isinstance(body[index], (ast.Import, ast.ImportFrom)):
        index += 1
    return body[index] if index < len(body) else None


def _has_module_level_pytest_entrypoint(tree: ast.Module) -> bool:
    guard = next(
        (
            (index, node)
            for index, node in enumerate(tree.body)
            if _is_module_main_guard(node)
        ),
        None,
    )
    if guard is None:
        return False
    guard_index, statement = guard
    if not isinstance(statement, ast.If):
        return False
    if _has_direct_module_exit(tree.body[:guard_index]):
        return False
    first_executable_statement = _guard_entrypoint_statement(statement.body)
    return first_executable_statement is not None and _is_direct_pytest_exit_statement(
        first_executable_statement
    )



def validate_helper_tests(paths: Sequence[Path]) -> list[str]:
    violations: list[str] = []
    pep723_module = load_pep723_module()
    for path in paths:
        if not is_helper_test(path):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            violations.append(f"{path}: cannot read helper: {exc}")
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path}: cannot parse helper: {exc}")
            continue
        uses_pytest = _imports_pytest(tree) or _declares_pytest_dependency(
            path, source, pep723_module
        )
        if uses_pytest and not _has_module_level_pytest_entrypoint(tree):
            violations.append(
                f"{path}: helpers that import or declare pytest must define a "
                "module-level if __name__ == '__main__': guard with no direct "
                "module-level exit before it and whose first executable statement "
                "raises SystemExit(pytest.main(...))"
            )
    return violations


def validate_repository(
    root: Path = ROOT, *, python_paths: Sequence[Path] | None = None
) -> list[str]:
    root = root.resolve()
    paths = tuple(python_paths) if python_paths is not None else discover_python_files(root)
    violations: list[str] = []
    violations.extend(validate_helper_tests(paths))
    violations.extend(validate_python_metadata(root, paths))
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
