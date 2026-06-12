#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Verify that a work brief stays grounded in collected evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


URL_RE = re.compile(r"https?://[^\s)>'\"]+")
GITHUB_ITEM_URL_RE = re.compile(r"https?://github\.com/([^/\s)]+/[^/\s)]+)/(?:issues|pull)/(\d+)\b")
QUALIFIED_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)\b")
BARE_REF_RE = re.compile(r"(?<![#\w/])#([1-9]\d*)\b")
STOPWORDS = {
    "about",
    "after",
    "before",
    "brief",
    "could",
    "evidence",
    "every",
    "failed",
    "source",
    "there",
    "these",
    "those",
    "would",
}


@dataclass(frozen=True)
class EvidenceIndex:
    urls: set[str]
    qualified_refs: set[str]
    bare_refs: set[str]
    source_notes: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a work brief against evidence JSON.")
    parser.add_argument("--evidence", required=True, type=Path, help="Evidence JSON file.")
    parser.add_argument("--brief", required=True, type=Path, help="Brief Markdown/text file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence = json.loads(args.evidence.read_text())
    brief = args.brief.read_text()
    errors = verify_brief(evidence, brief)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("ok verify-work-brief")
    return 0


def verify_brief(evidence: Any, brief: str) -> list[str]:
    index = build_evidence_index(evidence)
    errors: list[str] = []
    errors.extend(unsupported_urls(index, brief))
    errors.extend(unsupported_refs(index, brief))
    errors.extend(missing_source_notes(index, brief))
    return errors


def build_evidence_index(evidence: Any) -> EvidenceIndex:
    urls: set[str] = set()
    qualified_refs: set[str] = set()
    bare_refs: set[str] = set()
    source_notes: list[str] = []

    def visit(value: Any, repo_hint: str | None = None) -> None:
        nonlocal source_notes
        if isinstance(value, dict):
            next_repo = repo_hint
            raw_repo = value.get("repo") or value.get("repository")
            if isinstance(raw_repo, str) and "/" in raw_repo:
                next_repo = raw_repo
            raw_number = value.get("number")
            if isinstance(raw_number, int):
                bare_refs.add(str(raw_number))
                if next_repo:
                    qualified_refs.add(f"{next_repo}#{raw_number}")
            for key, item in value.items():
                if key in {"source_notes", "limitations"} and isinstance(item, list):
                    source_notes.extend(str(note) for note in item if str(note).strip())
                visit(item, next_repo)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, repo_hint)
            return
        if isinstance(value, str):
            for url in extract_urls(value):
                urls.add(url)
                match = GITHUB_ITEM_URL_RE.search(url)
                if match:
                    repo, number = match.groups()
                    bare_refs.add(number)
                    qualified_refs.add(f"{repo}#{number}")

    visit(evidence)
    return EvidenceIndex(urls=urls, qualified_refs=qualified_refs, bare_refs=bare_refs, source_notes=dedupe(source_notes))


def unsupported_urls(index: EvidenceIndex, brief: str) -> list[str]:
    errors: list[str] = []
    for url in sorted(extract_urls(brief) - index.urls):
        errors.append(f"unsupported URL not present in evidence: {url}")
    return errors


def unsupported_refs(index: EvidenceIndex, brief: str) -> list[str]:
    errors: list[str] = []
    for repo, number in sorted(QUALIFIED_REF_RE.findall(brief)):
        ref = f"{repo}#{number}"
        if ref not in index.qualified_refs:
            errors.append(f"unsupported issue/PR reference not present in evidence: {ref}")
    for number in sorted(set(BARE_REF_RE.findall(brief))):
        if number not in index.bare_refs:
            errors.append(f"unsupported issue/PR reference not present in evidence: #{number}")
    return errors


def missing_source_notes(index: EvidenceIndex, brief: str) -> list[str]:
    if not index.source_notes:
        return []
    normalized_brief = normalize_text(brief)
    if not any(marker in normalized_brief for marker in ("source", "limitation", "confidence", "caveat")):
        return ["brief must include a source limitation or confidence caveat"]
    errors: list[str] = []
    brief_words = set(normalized_brief.split())
    for note in index.source_notes:
        keywords = note_keywords(note)
        if keywords and len(brief_words & keywords) < min(2, len(keywords)):
            errors.append(f"source note not reflected in brief: {note}")
    return errors


def extract_urls(text: str) -> set[str]:
    return {match.group(0).rstrip(".,;:]") for match in URL_RE.finditer(text)}


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def note_keywords(note: str) -> set[str]:
    return {
        word
        for word in normalize_text(note).split()
        if len(word) >= 5 and word not in STOPWORDS and not word.isdigit()
    }


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
