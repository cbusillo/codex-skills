#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import NamedTuple

FULL_SUITE_EXIT = 3
TRAIN_BRANCH_PREFIX = "launchplane/train/"
VALIDATION_CONTROL_FILES = (
    "scripts/validate-skills.sh",
    "scripts/run-python-helper.sh",
    "scripts/select_validation_tests.py",
    "uv.toml",
    ".python-version",
)
CODE_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".cjs", ".ts"}
DATA_SUFFIXES = {".json", ".toml", ".yaml", ".yml"}
REPOSITORY_SCANNER = re.compile(r"scripts/validate[\w-]*\.py")
FILE_NAME_TOKEN = re.compile(r"[\w.-]+")
IDENTIFIER_TOKEN = re.compile(r"\w+")


class Selection(NamedTuple):
    helpers: list[str] | None
    reason: str


class FullSuiteRequired(Exception):
    pass


def select_helpers(event_name: str, event: dict, repo: Path, helpers: list[str]) -> Selection:
    try:
        changed = pull_request_changes(event_name, event, repo)
    except FullSuiteRequired as reason:
        return Selection(None, str(reason))
    owned_patterns = code_owned_patterns(repo)
    for path in changed:
        if requires_every_helper(path, owned_patterns):
            return Selection(None, f"{path} changes validation or can land outside the merge train")
    affected = reaching_files(repo, changed)
    selected = [helper for helper in helpers if helper in affected or REPOSITORY_SCANNER.fullmatch(helper)]
    return Selection(selected, f"{len(selected)} of {len(helpers)} helpers reach {len(changed)} changed paths")


def pull_request_changes(event_name: str, event: dict, repo: Path) -> list[str]:
    if event_name != "pull_request":
        raise FullSuiteRequired(f"{event_name or 'local'} validation runs every helper")
    try:
        head = event["pull_request"]["head"]
        head_ref, head_sha = str(head["ref"]), str(head["sha"])
    except (KeyError, TypeError) as error:
        raise FullSuiteRequired("the pull request event has no head") from error
    if head_ref.startswith(TRAIN_BRANCH_PREFIX):
        raise FullSuiteRequired("merge-train candidates validate every helper")
    if git(repo, "rev-parse", "HEAD^2") != head_sha:
        raise FullSuiteRequired("the checkout is not this pull request's merge commit")
    return [path for path in git(repo, "diff", "--no-renames", "--name-only", "-z", "HEAD^1", "HEAD").split("\0") if path]


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise FullSuiteRequired(f"git {args[0]} failed") from error


def code_owned_patterns(repo: Path) -> list[str]:
    owners = repo / ".github" / "CODEOWNERS"
    lines = owners.read_text(encoding="utf-8").splitlines() if owners.is_file() else []
    return [line.split()[0] for line in lines if line.strip() and not line.lstrip().startswith("#")]


def requires_every_helper(path: str, owned_patterns: list[str]) -> bool:
    if path.startswith(".github/") or path in VALIDATION_CONTROL_FILES:
        return True
    return any(matches_owned_pattern(path, pattern) for pattern in owned_patterns)


def matches_owned_pattern(path: str, pattern: str) -> bool:
    anchored = pattern.startswith("/")
    pattern = pattern.strip("/")
    if path == pattern or path.startswith(f"{pattern}/") or fnmatch(path, pattern):
        return True
    return not anchored and fnmatch(path, f"*/{pattern}")


def reaching_files(repo: Path, changed: list[str]) -> set[str]:
    callers = callers_by_file(repo, changed)
    reached = set(changed)
    pending = list(changed)
    while pending:
        for caller in callers[pending.pop()] - reached:
            reached.add(caller)
            pending.append(caller)
    return reached


def callers_by_file(repo: Path, changed: list[str]) -> defaultdict[str, set[str]]:
    tracked = [path for path in git(repo, "ls-files", "-z").split("\0") if path]
    files_by_name = names_of(set(tracked) | set(changed))
    callers: defaultdict[str, set[str]] = defaultdict(set)
    for caller in tracked:
        for name in dependency_names(caller, code_text(repo / caller)) & files_by_name.keys():
            for target in files_by_name[name] - {caller}:
                callers[target].add(caller)
    return callers


def names_of(paths: set[str]) -> defaultdict[str, set[str]]:
    files_by_name: defaultdict[str, set[str]] = defaultdict(set)
    for path in paths:
        file = PurePosixPath(path)
        files_by_name[file.name].add(path)
        if file.suffix == ".py":
            files_by_name[file.stem].add(path)
    return files_by_name


def code_text(path: Path) -> str:
    if path.suffix not in CODE_SUFFIXES and path.suffix:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if path.suffix or text.startswith("#!") else ""


def dependency_names(caller: str, text: str) -> set[str]:
    if not text:
        return set()
    if PurePosixPath(caller).suffix != ".py" or is_helper(caller):
        return mentioned_names(text)
    return imported_names(text) | {name for name in mentioned_names(text) if PurePosixPath(name).suffix in DATA_SUFFIXES}


def is_helper(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") or "validate" in name


def imported_names(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set(IDENTIFIER_TOKEN.findall(text))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(part for alias in node.names for part in alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
    return names


def mentioned_names(text: str) -> set[str]:
    file_names = {token.strip(".-") for token in FILE_NAME_TOKEN.findall(text)}
    return file_names | set(IDENTIFIER_TOKEN.findall(text))


def main() -> int:
    helpers = [line for line in sys.stdin.read().splitlines() if line]
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    selection = select_helpers(os.environ.get("GITHUB_EVENT_NAME", ""), event, Path.cwd(), helpers)
    print(f"validation selection: {selection.reason}", file=sys.stderr)
    if selection.helpers is None:
        return FULL_SUITE_EXIT
    print("\n".join(selection.helpers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
