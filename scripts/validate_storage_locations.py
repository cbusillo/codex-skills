#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fail when tracked Markdown prose names a storage location nobody defines.

Task output goes where skills/references/repo-cleanup.md#task-artifacts-and-retention
says: the session scratchpad or temporary directory, or the location the host
names when it must outlive the session. A "task-private" folder is not one of
them, and agents told to use it invented one at a volume root. Prose is read the
same way as validate_role_words.py, so quoted identifiers pass.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "validate_role_words", Path(__file__).with_name("validate_role_words.py")
)
assert _spec and _spec.loader
role_words = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(role_words)

UNDEFINED = re.compile(r"\btask[- ]private\b", re.IGNORECASE)
# "task" at the end of a prose line carries to the next one, so a reflow that
# wraps the phrase still matches.
TRAILING_TASK = re.compile(r"\btask-?\s*$", re.IGNORECASE)


def findings(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    previous_number, previous = 0, ""
    for number, line in role_words.prose_lines(text):
        carried = TRAILING_TASK.search(previous) if number == previous_number + 1 else None
        previous_number, previous = number, line
        if carried:
            prefix = carried.group().strip()
            line = prefix + ("" if prefix.endswith("-") else " ") + line.lstrip()
        found.extend((number, match.group()) for match in UNDEFINED.finditer(line))
    return found


def main() -> int:
    failures = 0
    for path in role_words.tracked_markdown():
        for number, term in findings((role_words.ROOT / path).read_text(encoding="utf-8")):
            print(f"{path}:{number}: undefined storage location {term!r}")
            failures += 1
    if failures:
        print(
            f"{failures} undefined storage location(s); name the session "
            "scratchpad or the host's task-evidence location as described in "
            "skills/references/repo-cleanup.md#task-artifacts-and-retention.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
