#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Validate tracked files for public-safety secret leaks."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    rule: str
    excerpt: str


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github-classic-token", re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b")),
    ("github-fine-grained-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "credentialed-url",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", re.IGNORECASE),
    ),
)

ALLOWLIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"github_pat_(?:xxx|example)\b"),
    re.compile(r"ghp_example\b"),
    re.compile(r"sk-\[A-Za-z0-9"),
    re.compile(r"sk-ant-\[A-Za-z0-9"),
    re.compile(r"AKIA\[0-9A-Z\]"),
    re.compile(r"xox\[baprs\]"),
    re.compile(r"PRIVATE KEY-----\.\*"),
)

def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / raw.decode() for raw in result.stdout.split(b"\0") if raw]


def is_allowed(path: Path, line: str) -> bool:
    return any(pattern.search(line) for pattern in ALLOWLIST_PATTERNS)


def scan_file(path: Path) -> list[Finding]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(errors="ignore")
    findings: list[Finding] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if is_allowed(path, line):
            continue
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        path=path.relative_to(ROOT),
                        line_number=line_number,
                        rule=rule,
                        excerpt=line.strip()[:160],
                    )
                )
                break
    return findings


def validate(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        if path.is_file():
            findings.extend(scan_file(path))
    return findings


def self_test() -> None:
    safe = "GH_TOKEN=github_pat_xxx"
    unsafe = "GH_TOKEN=github_pat_" + "A" * 80
    assert is_allowed(ROOT / ".env.example", safe)
    assert not is_allowed(ROOT / "README.md", unsafe)
    assert SECRET_PATTERNS[1][1].search(unsafe)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("ok validate-public-safety self-test")
        return 0

    findings = validate(tracked_files())
    if findings:
        for finding in findings:
            print(
                f"{finding.path}:{finding.line_number}: {finding.rule}: {finding.excerpt}",
                file=sys.stderr,
            )
        return 1
    print("ok validate-public-safety")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
