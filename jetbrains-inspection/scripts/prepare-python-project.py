#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import html
import os
import subprocess
import tempfile
from pathlib import Path


def sdk_name_for(venv: Path, home: Path) -> str:
    try:
        return f"~/{venv.relative_to(home).as_posix()}"
    except ValueError:
        return str(venv)


def render_project_files(repo: Path, module_name: str, test_roots: list[str]) -> dict[Path, str]:
    idea = repo / ".idea"
    sdk_name = html.escape(sdk_name_for(repo / ".venv", Path.home()), quote=True)
    safe_module_name = "".join(character if character.isalnum() or character in "-_" else "-" for character in module_name)
    module_file = idea / f"{safe_module_name}.iml"
    source_folders = "\n".join(
        f'      <sourceFolder url="file://$MODULE_DIR$/{html.escape(root, quote=True)}" isTestSource="true" />'
        for root in test_roots
    )
    module_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<module type="PYTHON_MODULE" version="4">
  <component name="NewModuleRootManager">
    <content url="file://$MODULE_DIR$">
      <excludeFolder url="file://$MODULE_DIR$/.mypy_cache" />
      <excludeFolder url="file://$MODULE_DIR$/.system" />
      <excludeFolder url="file://$MODULE_DIR$/.local" />
      <excludeFolder url="file://$MODULE_DIR$/.code" />
      <excludeFolder url="file://$MODULE_DIR$/.venv" />
{source_folders}
    </content>
    <orderEntry type="jdk" jdkName="{sdk_name}" jdkType="Python SDK" />
    <orderEntry type="sourceFolder" forTests="false" />
  </component>
</module>
'''
    modules_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectModuleManager">
    <modules>
      <module fileurl="file://$PROJECT_DIR$/.idea/{safe_module_name}.iml" filepath="$PROJECT_DIR$/.idea/{safe_module_name}.iml" />
    </modules>
  </component>
</project>
'''
    misc_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager" version="2" project-jdk-name="{sdk_name}" project-jdk-type="Python SDK" />
  <component name="PythonCompatibilityInspectionAdvertiser">
    <option name="version" value="3" />
  </component>
</project>
'''
    return {module_file: module_xml, idea / "modules.xml": modules_xml, idea / "misc.xml": misc_xml}


def atomic_write(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def ensure_ignored_outputs(repo: Path, paths: list[Path]) -> None:
    relative_paths = [str(path.relative_to(repo)) for path in paths]
    for path, relative_path in zip(paths, relative_paths, strict=True):
        tracked = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", relative_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if tracked.returncode == 0:
            raise RuntimeError(f"preparation output must not be tracked: {relative_path}")
        ignore_probe = f"{relative_path}/.prepare-check" if path.name == ".venv" else relative_path
        ignored = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--no-index", "-q", "--", ignore_probe],
            check=False,
        )
        if ignored.returncode != 0:
            raise RuntimeError(f"preparation output is not ignored: {relative_path}")


def validate_existing_venv(venv: Path, requested_python: str) -> None:
    if not venv.exists():
        return
    interpreter = venv / "bin" / "python"
    if not interpreter.is_file():
        raise RuntimeError(f"existing virtual environment is incomplete: {venv}")
    result = subprocess.run(
        [str(interpreter), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() != requested_python:
        raise RuntimeError(
            f"existing virtual environment uses Python {result.stdout.strip()}, expected {requested_python}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a worktree-local PyCharm project model.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--module-name", required=True)
    parser.add_argument("--test-root", action="append", default=[])
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    git_check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-dir"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if git_check.returncode != 0:
        parser.error(f"not a Git worktree: {repo}")
    for test_root in args.test_root:
        if not (repo / test_root).is_dir():
            parser.error(f"test root does not exist: {test_root}")

    project_files = render_project_files(repo, args.module_name, args.test_root)
    ensure_ignored_outputs(repo, [repo / ".venv", *project_files])
    validate_existing_venv(repo / ".venv", args.python)
    if not (repo / ".venv").exists():
        subprocess.run(["uv", "venv", "--python", args.python, ".venv"], cwd=repo, check=True)
    for path, content in project_files.items():
        atomic_write(path, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
