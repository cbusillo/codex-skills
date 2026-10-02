#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Fail when retired role words return in tracked Markdown prose.

The role words are defined in skills/references/role-words.md: Director,
Client, the admin permission, and "owner" only in GitHub's repository sense.
Code spans, fenced code, link targets, and HTML comments are not prose, so
quoted identifiers and helper-parsed literals pass.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RETIRED = re.compile(r"\b(?:owners?|operators?)(?:'s|s')?\b", re.IGNORECASE)

# GitHub's repository-owner sense and unrelated technical senses.
ALLOWED = re.compile(
    r"\b(?:repository|repo|organization|org|account|code)[- ]owners?(?:'s|s')?\b"
    r"|\bowner-only\b"
    r"|\bowners?/"
    r"|\bowner:"
    r"|\bowner\s+(?:or|and)\s+name\b",
    re.IGNORECASE,
)

FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"(`+).*?\1")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
HTML_COMMENT = re.compile(r"<!--.*?-->")
URL = re.compile(r"https?://\S+")

# The glossary names the retired words. Recorded evaluation evidence keeps the
# words it was written with. The root DIRECTION.md changes only through a
# code-owner-approved pull request, which converts it separately
# (cbusillo/direction#13 tracks it); drop it from this set once that lands.
EXCLUDED = re.compile(
    r"^evals/(?!README\.md$)|/evaluations/acceptance-"
    r"|^DIRECTION\.md$|^skills/references/role-words\.md$"
)


def prose_lines(text: str) -> Iterable[tuple[int, str]]:
    in_fence = False
    in_comment = False
    in_frontmatter = False
    for number, line in enumerate(text.splitlines(), start=1):
        if number == 1 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
                continue
            # Descriptions, purposes, and policy messages are prose; argv,
            # paths, and other metadata are not.
            if not re.match(r"^\s*(?:-\s+)?(?:description|purpose|message):", line):
                continue
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if in_comment:
            if "-->" not in line:
                continue
            line = line.split("-->", 1)[1]
            in_comment = False
        line = HTML_COMMENT.sub(" ", line)
        if "<!--" in line:
            line = line.split("<!--", 1)[0]
            in_comment = True
        line = INLINE_CODE.sub(" ", line)
        line = LINK_TARGET.sub("]", line)
        line = URL.sub(" ", line)
        yield number, ALLOWED.sub(" ", line)


def findings(text: str) -> list[tuple[int, str]]:
    return [
        (number, match.group(0))
        for number, line in prose_lines(text)
        for match in RETIRED.finditer(line)
    ]


def tracked_markdown() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    return sorted(
        path
        for path in output.split("\0")
        if path and not EXCLUDED.search(path)
    )


def main() -> int:
    failures = 0
    for path in tracked_markdown():
        for number, word in findings((ROOT / path).read_text(encoding="utf-8")):
            print(f"{path}:{number}: retired role word {word!r}")
            failures += 1
    if failures:
        print(
            f"{failures} retired role word(s); use Director, Client, or admin as "
            "defined in skills/references/role-words.md, or quote a legacy "
            "identifier as code.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
