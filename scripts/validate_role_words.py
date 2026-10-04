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

RETIRED = re.compile(
    r"\b(?:owners?|operators?)(?:'s|s')?\b|\bpolicy[- ]administrators?\b",
    re.IGNORECASE,
)

# GitHub's repository-owner sense and unrelated technical senses.
QUALIFIER = r"(?:repository|repo|organization|org|account|code)"
ALLOWED = re.compile(
    rf"\b{QUALIFIER}[- ]owners?(?:'s|s')?\b"
    r"|\bowner-only\b"
    r"|\bowners?/"
    r"|\bowner:"
    r"|\bowner\s+(?:or|and)\s+name\b",
    re.IGNORECASE,
)
# A qualifier or "policy" at a line ending carries over to wrapped prose.
TRAILING_QUALIFIER = re.compile(rf"\b(?:{QUALIFIER}|policy)(?:-)?\s*$", re.IGNORECASE)

BLOCKQUOTE = re.compile(r"^\s*(?:>\s*)+")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
FRONTMATTER_PROSE = re.compile(r"^(\s*)(?:-\s+)?(?:description|purpose|message):")
FRONTMATTER_KEY = re.compile(r"^\s*(?:-\s+)?[\w-]+:(?:\s|$)")
INLINE_CODE = re.compile(r"(`+).*?\1")
LINK_TARGET = re.compile(r"]\([^)]*\)")
URL = re.compile(r"https?://\S+")

# The glossary names the retired words. Recorded evaluation evidence keeps the
# words it was written with.
EXCLUDED = re.compile(
    r"^evals/(?!README\.md$)|/evaluations/acceptance-|^skills/references/role-words\.md$"
)


def strip_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove HTML comment text, carrying an open comment to the next line."""
    kept: list[str] = []
    while line:
        if in_comment:
            end = line.find("-->")
            if end < 0:
                return " ".join(kept), True
            line = line[end + 3 :]
            in_comment = False
        else:
            start = line.find("<!--")
            if start < 0:
                kept.append(line)
                break
            kept.append(line[:start])
            line = line[start + 4 :]
            in_comment = True
    return " ".join(kept), in_comment


def prose_lines(text: str) -> Iterable[tuple[int, str]]:
    fence = ""
    fence_in_blockquote = False
    in_comment = False
    in_frontmatter = False
    prose_indent: int | None = None
    previous = ""
    for number, line in enumerate(text.splitlines(), start=1):
        quote_prefix = BLOCKQUOTE.match(line)
        if not fence or fence_in_blockquote:
            line = BLOCKQUOTE.sub("", line)
        if number == 1 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
                continue
            # Descriptions, purposes, and policy messages are prose, including
            # their folded continuation lines; argv, paths, and other metadata
            # are not.
            indent = len(line) - len(line.lstrip())
            key = FRONTMATTER_PROSE.match(line)
            if key:
                prose_indent = len(key.group(1))
            elif (
                prose_indent is None
                or indent <= prose_indent
                or FRONTMATTER_KEY.match(line)
            ):
                prose_indent = None
                continue
        opening = FENCE.match(line)
        if fence:
            if opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence):
                fence = ""
            continue
        if opening:
            fence = opening.group(1)
            fence_in_blockquote = quote_prefix is not None
            previous = ""
            continue
        line, in_comment = strip_comments(line, in_comment)
        line = INLINE_CODE.sub(" ", line)
        line = LINK_TARGET.sub("]", line)
        line = URL.sub(" ", line)
        carried = TRAILING_QUALIFIER.search(previous)
        previous = line
        if carried:
            prefix = carried.group().strip()
            separator = "" if prefix.endswith("-") else " "
            line = prefix + separator + line.lstrip()
        yield number, ALLOWED.sub(" ", line)


def findings(text: str) -> list[tuple[int, str]]:
    return [
        (number, match.group())
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
