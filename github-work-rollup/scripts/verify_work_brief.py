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
SHORT_REF_RE = re.compile(r"\b([A-Za-z0-9_.-]+)#(\d+)\b")
BARE_REF_RE = re.compile(r"(?<![#\w/])#([1-9]\d*)\b")
NATURAL_REF_RE = re.compile(r"\b(?:PR|pull request|issue)\s+#?([1-9]\d*)\b", re.IGNORECASE)
CAVEAT_MARKERS = ("source", "limitation", "confidence", "caveat", "incomplete", "partial")
FALSE_CAVEAT_RE = re.compile(r"\b(?:no|without)\s+(?:source\s+)?(?:limitations?|caveats?|gaps?)\b")
NOTE_KEYWORD_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "because",
    "but",
    "by",
    "can",
    "could",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "may",
    "not",
    "of",
    "or",
    "should",
    "so",
    "some",
    "the",
    "this",
    "to",
    "was",
    "were",
    "when",
    "with",
}


@dataclass(frozen=True)
class EvidenceIndex:
    urls: set[str]
    qualified_refs: set[str]
    short_refs: set[str]
    ambiguous_short_names: set[str]
    bare_refs: set[str]
    ambiguous_bare_refs: set[str]
    source_notes: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a work brief against evidence JSON.")
    parser.add_argument("--evidence", required=True, type=Path, help="Evidence JSON file.")
    parser.add_argument(
        "--plan-context",
        action="append",
        default=[],
        type=Path,
        help="Optional plan-context JSON file. May be repeated.",
    )
    parser.add_argument("--brief", required=True, type=Path, help="Brief Markdown/text file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence = json.loads(args.evidence.read_text())
    plan_context = [json.loads(path.read_text()) for path in args.plan_context]
    brief = args.brief.read_text()
    errors = verify_brief(evidence, brief, plan_context=plan_context)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("ok verify-work-brief")
    return 0


def verify_brief(evidence: Any, brief: str, *, plan_context: list[Any] | None = None) -> list[str]:
    index = build_evidence_index({"evidence": evidence, "plan_context": plan_context or []})
    errors: list[str] = []
    errors.extend(unsupported_urls(index, brief))
    errors.extend(unsupported_refs(index, brief))
    errors.extend(missing_source_notes(index, brief))
    return errors


def build_evidence_index(evidence: Any) -> EvidenceIndex:
    urls: set[str] = set()
    qualified_refs: set[str] = set()
    short_ref_candidates: set[str] = set()
    repos_by_short_name: dict[str, set[str]] = {}
    repos_by_bare_ref: dict[str, set[str]] = {}
    repo_unknown_bare_refs: set[str] = set()
    source_notes: list[str] = []

    def record_repo(repo: str) -> None:
        repos_by_short_name.setdefault(repo.rsplit("/", 1)[-1], set()).add(repo)

    def record_ref(repo: str, number: int | str) -> None:
        number_text = str(number)
        repos_by_bare_ref.setdefault(number_text, set()).add(repo)
        qualified_refs.add(f"{repo}#{number_text}")
        short_ref_candidates.add(f"{repo.rsplit('/', 1)[-1]}#{number_text}")

    def visit(value: Any, repo_hint: str | None = None) -> None:
        nonlocal source_notes
        if isinstance(value, dict):
            next_repo = repo_hint
            raw_repo = value.get("repo") or value.get("repository")
            if isinstance(raw_repo, str) and "/" in raw_repo:
                next_repo = raw_repo
                record_repo(raw_repo)
            raw_number = value.get("number")
            if isinstance(raw_number, int):
                if next_repo:
                    record_ref(next_repo, raw_number)
                else:
                    repo_unknown_bare_refs.add(str(raw_number))
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
                    record_repo(repo)
                    record_ref(repo, number)

    visit(evidence)
    ambiguous_short_names = {name for name, repos in repos_by_short_name.items() if len(repos) > 1}
    short_refs = {
        ref for ref in short_ref_candidates if ref.rsplit("#", 1)[0] not in ambiguous_short_names
    }
    ambiguous_bare_refs = {
        number for number, repos in repos_by_bare_ref.items() if len(repos) > 1
    }
    bare_refs = (set(repos_by_bare_ref) - ambiguous_bare_refs) | repo_unknown_bare_refs
    return EvidenceIndex(
        urls=urls,
        qualified_refs=qualified_refs,
        short_refs=short_refs,
        ambiguous_short_names=ambiguous_short_names,
        bare_refs=bare_refs,
        ambiguous_bare_refs=ambiguous_bare_refs,
        source_notes=dedupe(source_notes),
    )


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
    qualified_spans = {match.span() for match in QUALIFIED_REF_RE.finditer(brief)}
    for match in sorted(SHORT_REF_RE.finditer(brief), key=lambda item: item.group(0)):
        if any(start <= match.start() and match.end() <= end for start, end in qualified_spans):
            continue
        ref = match.group(0)
        repo_short = match.group(1)
        if repo_short in index.ambiguous_short_names:
            errors.append(f"ambiguous short repository reference; use owner/repo form: {ref}")
        elif ref not in index.short_refs:
            errors.append(f"unsupported issue/PR reference not present in evidence: {ref}")
    for number in sorted(set(BARE_REF_RE.findall(brief))):
        if number in index.ambiguous_bare_refs:
            errors.append(f"ambiguous bare issue/PR reference; use owner/repo form: #{number}")
        elif number not in index.bare_refs:
            errors.append(f"unsupported issue/PR reference not present in evidence: #{number}")
    for number in sorted(set(NATURAL_REF_RE.findall(brief))):
        if number in index.ambiguous_bare_refs:
            errors.append(f"ambiguous bare issue/PR reference; use owner/repo form: {number}")
        elif number not in index.bare_refs:
            errors.append(f"unsupported issue/PR reference not present in evidence: {number}")
    return errors


def missing_source_notes(index: EvidenceIndex, brief: str) -> list[str]:
    if not index.source_notes:
        return []
    normalized_brief = normalize_text(brief)
    if FALSE_CAVEAT_RE.search(normalized_brief):
        return ["brief contradicts evidence source limitations"]
    if not any(marker in normalized_brief for marker in CAVEAT_MARKERS):
        return ["brief must include a source limitation or confidence caveat"]
    missing_notes = [note for note in index.source_notes if not source_note_is_reflected(note, normalized_brief)]
    if missing_notes:
        return ["brief must reflect source note: " + missing_notes[0]]
    return []


def source_note_is_reflected(note: str, normalized_brief: str) -> bool:
    normalized_note = normalize_text(note)
    if grouped_source_note_is_reflected(normalized_note, normalized_brief):
        return True
    if normalized_note and normalized_note in normalized_brief:
        return True
    if any(phrase in normalized_brief for phrase in source_note_phrases(normalized_note)):
        return True
    keywords = source_note_keywords(normalized_note)
    if not keywords:
        return True
    required = min(len(keywords), 3)
    return sum(1 for keyword in keywords if keyword in normalized_brief) >= required


def grouped_source_note_is_reflected(normalized_note: str, normalized_brief: str) -> bool:
    if normalized_note.startswith("issues are disabled for "):
        return all(word in normalized_brief for word in ("issues", "disabled")) and any(
            word in normalized_brief for word in ("repo", "repos", "repositories")
        )
    if normalized_note.startswith("workflow collection for ") and "automation counts may be incomplete" in normalized_note:
        return all(word in normalized_brief for word in ("workflow", "counts", "incomplete")) and any(
            word in normalized_brief for word in ("cap", "capped", "reached", "partial")
        )
    if normalized_note.startswith("release collection for ") and "release counts may be incomplete" in normalized_note:
        return all(word in normalized_brief for word in ("release", "counts", "incomplete")) and any(
            word in normalized_brief for word in ("cap", "capped", "reached", "partial")
        )
    return False


def source_note_phrases(normalized_note: str) -> list[str]:
    words = normalized_note.split()
    return [" ".join(words[index : index + 4]) for index in range(max(len(words) - 3, 0))]


def source_note_keywords(normalized_note: str) -> list[str]:
    words = [word for word in normalized_note.split() if len(word) >= 5 and word not in NOTE_KEYWORD_STOPWORDS]
    return dedupe(words)[:5]


def extract_urls(text: str) -> set[str]:
    return {match.group(0).rstrip(".,;:]") for match in URL_RE.finditer(text)}


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


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
