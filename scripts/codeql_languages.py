#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Select PR CodeQL lanes from the complete Git diff; other events scan all."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

LANGUAGES = ("actions", "python", "javascript-typescript", "swift")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
SCAN_CONTROL_FILES = {
    ".github/workflows/codeql.yml",
    "scripts/codeql_languages.py",
}


def languages_for_paths(paths: list[str]) -> set[str]:
    selected: set[str] = set()
    for raw_path in paths:
        if raw_path in SCAN_CONTROL_FILES or raw_path.startswith(".github/codeql/"):
            return set(LANGUAGES)
        path = PurePosixPath(raw_path)
        suffix = path.suffix.lower()
        name = path.name.lower()
        if (
            raw_path.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}
        ) or (raw_path.startswith(".github/actions/") and name in {"action.yml", "action.yaml"}):
            selected.add("actions")
        if suffix in {".py", ".pyi", ".pyw"} or name in {
            "pyproject.toml", "uv.lock", "uv.toml", ".python-version",
            "pipfile", "pipfile.lock", "poetry.lock", "setup.cfg",
        } or (name.startswith("requirements") and suffix in {".txt", ".in"}):
            selected.add("python")
        if suffix in {
            ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
            ".es", ".es6", ".html", ".htm", ".vue", ".svelte", ".hbs",
        } or name in {
            "package.json", "package-lock.json", "npm-shrinkwrap.json",
            "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
        } or name.startswith("tsconfig") and suffix == ".json":
            selected.add("javascript-typescript")
        if suffix == ".swift" or name == "package.resolved":
            selected.add("swift")
    return selected


def select_languages(event_name: str, event: dict, repo: Path) -> tuple[set[str], str]:
    if event_name != "pull_request":
        return set(LANGUAGES), "full scan for non-PR event"
    try:
        pull_request = event["pull_request"]
        base = pull_request["base"]["sha"]
        head = pull_request["head"]["sha"]
        if not isinstance(base, str) or not isinstance(head, str):
            raise ValueError("missing commit SHA")
        if COMMIT_SHA.fullmatch(base) is None or COMMIT_SHA.fullmatch(head) is None:
            raise ValueError("invalid commit SHA")
        diff = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}", "--"],
            cwd=repo, check=True, capture_output=True, timeout=60,
        )
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        # Missing history or malformed input may cost a full scan, never coverage.
        return set(LANGUAGES), "full scan because the PR diff is unavailable"
    paths = [os.fsdecode(path) for path in diff.stdout.split(b"\0") if path]
    return languages_for_paths(paths), f"complete PR diff: {len(paths)} changed paths"


def matrix_for_languages(selected: set[str]) -> dict:
    return {"include": [
        {
            "language": language,
            "scan": language in selected,
            "runner": "macos-15" if language == "swift" and language in selected else "ubuntu-24.04",
            "build_mode": "manual" if language == "swift" else "none",
        }
        for language in LANGUAGES
    ]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-path", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path, default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()
    # Invalid event JSON fails the job instead of reporting a successful omission.
    event = json.loads(args.event_path.read_text()) if args.event_path else {}
    selected, reason = select_languages(args.event_name, event, args.repo)
    matrix = json.dumps(matrix_for_languages(selected), separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"matrix={matrix}\n")
    print(json.dumps({"selected": sorted(selected), "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
