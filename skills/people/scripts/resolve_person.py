#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Resolve private local people entries without exposing the whole index."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
DETAIL_PREFIX = "people/"
SEPARATORS_RE = re.compile(r"[\s._-]+")
DETAIL_FILE_RE = re.compile(r"people/[a-z0-9][a-z0-9_-]*\.md")
SOURCE_SCOPE_KEY = "_people_source_scope"
DETAIL_BASE_LABEL_KEY = "_people_detail_base_label"
CONTACT_VALUE_KEYS = {
    "username",
    "handle",
    "user_id",
    "work",
    "personal",
    "mobile",
    "url",
    "bot_usernames",
    "bot_handles",
}
TRUST_VALUE_KEYS = {
    "level",
    "intent",
    "code",
    "authority",
    "handling",
    "review_posture",
}


class PeopleConfigError(Exception):
    pass


def default_code_home() -> tuple[Path, str]:
    code_home = os.environ.get("CODE_HOME", "").strip()
    if code_home:
        return Path(code_home).expanduser(), "$CODE_HOME"
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser(), "$CODEX_HOME"
    return Path.home() / ".code", "~/.code"


def default_global_index() -> tuple[Path, str]:
    home, label = default_code_home()
    return home / "skills" / ".local" / "people.yaml", label


def default_repo_index(repo_root: Path | None = None) -> Path:
    return (repo_root or Path.cwd()).expanduser() / ".local" / "people.yaml"


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    if text.startswith("@"):
        text = text[1:]
    text = SEPARATORS_RE.sub(" ", text.casefold()).strip()
    return text


def compact(value: object) -> str:
    return SEPARATORS_RE.sub("", normalize(value))


def load_people(
    path: Path,
    *,
    source_scope: str = "explicit",
    detail_base_label: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    if not path.exists():
        return "no_index", []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PeopleConfigError(
            f"invalid YAML in {source_scope} people index: {exc}"
        ) from exc
    return "ok", annotate_people(
        validate_people_data(data),
        source_scope=source_scope,
        detail_base_label=detail_base_label,
    )


def load_scoped_people(
    *,
    scope: str = "auto",
    global_index: Path | None = None,
    repo_index: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    resolved_global_index, home_label = default_global_index()
    explicit_global_index = global_index is not None
    global_index = (global_index or resolved_global_index).expanduser()
    repo_index = (repo_index or default_repo_index(repo_root)).expanduser()
    requested_scope = "global" if scope == "user" else scope
    sources: dict[str, Any] = {
        "scope": scope,
        "global": {"status": "skipped", "count": 0},
        "repo": {"status": "skipped", "count": 0},
    }

    global_people: list[dict[str, Any]] = []
    repo_people: list[dict[str, Any]] = []

    if requested_scope in {"auto", "global"}:
        status, global_people = load_people(
            global_index,
            source_scope="global",
            detail_base_label=(
                "global/.local"
                if explicit_global_index
                else f"{home_label}/skills/.local"
            ),
        )
        sources["global"] = {"status": status, "count": len(global_people)}
    if requested_scope in {"auto", "repo"}:
        status, repo_people = load_people(
            repo_index,
            source_scope="repo",
            detail_base_label=".local",
        )
        sources["repo"] = {"status": status, "count": len(repo_people)}

    if requested_scope == "global":
        people = global_people
    elif requested_scope == "repo":
        people = repo_people
    else:
        people = merge_people(global_people, repo_people)

    if not people:
        return "no_index", [], sources
    return "ok", people, sources


def annotate_people(
    people: list[dict[str, Any]],
    *,
    source_scope: str,
    detail_base_label: str | None = None,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for person in people:
        copy = dict(person)
        copy[SOURCE_SCOPE_KEY] = source_scope
        if detail_base_label:
            copy[DETAIL_BASE_LABEL_KEY] = detail_base_label
        annotated.append(copy)
    return annotated


def merge_people(
    global_people: list[dict[str, Any]], repo_people: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for person in global_people:
        person_id = str(person.get("id"))
        positions[person_id] = len(merged)
        merged.append(person)
    for person in repo_people:
        person_id = str(person.get("id"))
        if person_id in positions:
            merged[positions[person_id]] = person
        else:
            positions[person_id] = len(merged)
            merged.append(person)
    return merged


def validate_people_data(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise PeopleConfigError("people index must be a YAML mapping")
    people = data.get("people")
    if people is None:
        people = []
    if not isinstance(people, list):
        raise PeopleConfigError("people index field `people` must be a list")
    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, person in enumerate(people):
        if not isinstance(person, dict):
            raise PeopleConfigError(f"people[{index}] must be a mapping")
        person_id = person.get("id")
        display_name = person.get("display_name")
        if not valid_person_id(person_id):
            raise PeopleConfigError(f"people[{index}].id must be a lowercase slug")
        if not isinstance(display_name, str) or not display_name.strip():
            raise PeopleConfigError(
                f"people[{index}].display_name must be a non-empty string"
            )
        if person_id in seen_ids:
            raise PeopleConfigError(f"people[{index}].id duplicates {person_id!r}")
        seen_ids.add(person_id)
        validate_detail_file(person.get("details_file"), index)
        cleaned.append(person)
    return cleaned


def valid_person_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value) is not None
    )


def validate_detail_file(value: object, index: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise PeopleConfigError(f"people[{index}].details_file must be a string")
    path = Path(value)
    if path.is_absolute() or not DETAIL_FILE_RE.fullmatch(value):
        raise PeopleConfigError(
            f"people[{index}].details_file must be a relative people/<id>.md path"
        )


def candidate_values(person: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []

    def add(source: str, raw: object) -> None:
        if raw is None:
            return
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                values.append((source, text))
        elif isinstance(raw, list):
            for item in raw:
                add(source, item)

    add("id", person.get("id"))
    add("person_ref", f"person:{person.get('id')}")
    add("display_name", person.get("display_name"))
    add("preferred_reference", person.get("preferred_reference"))
    add("alias", person.get("aliases"))

    contacts = person.get("contacts") or {}
    if isinstance(contacts, dict):
        for service, value in contacts.items():
            collect_contact_values(values, service, value)

    github = person.get("github")
    add("github", github)
    if isinstance(github, str) and github.strip():
        add("github", f"@{github.strip()}")
    return values


def collect_contact_values(
    values: list[tuple[str, str]], service: object, value: object
) -> None:
    source = str(service)

    def add(raw: object) -> None:
        if isinstance(raw, str) and raw.strip():
            values.append((source, raw.strip()))

    if isinstance(value, str):
        add(value)
    elif isinstance(value, dict):
        for key in CONTACT_VALUE_KEYS:
            raw = value.get(key)
            if isinstance(raw, list):
                for item in raw:
                    add(item)
            else:
                add(raw)
    elif isinstance(value, list):
        for item in value:
            collect_contact_values(values, service, item)


def public_person(
    person: dict[str, Any],
    *,
    matched_on: str | None = None,
    matched_value: str | None = None,
    include_detail: bool = True,
) -> dict[str, Any]:
    contacts = person.get("contacts") if isinstance(person.get("contacts"), dict) else {}
    preferences = (
        person.get("preferences") if isinstance(person.get("preferences"), dict) else {}
    )
    organization = (
        person.get("organization") if isinstance(person.get("organization"), dict) else {}
    )
    relationship = (
        person.get("relationship") if isinstance(person.get("relationship"), dict) else {}
    )
    github = first_string(person.get("github")) or first_string(
        nested_value(contacts, "github", "username")
    )
    result: dict[str, Any] = {
        "id": person.get("id"),
        "display_name": person.get("display_name"),
        "preferred_reference": person.get("preferred_reference"),
        "github": github,
        "company": organization.get("company"),
        "team": organization.get("team"),
        "title": organization.get("title"),
        "relationship": relationship.get("kind"),
        "roles": relationship.get("roles") or person.get("roles"),
        "preferred_contact": preferences.get("preferred_contact"),
        "timezone": preferences.get("timezone"),
        "mention_style": preferences.get("mention_style"),
        "trust": compact_trust(person.get("trust")),
    }
    if include_detail:
        result["details_file"] = detail_path(
            person.get("details_file"),
            detail_base_label=first_string(person.get(DETAIL_BASE_LABEL_KEY)),
        )
    if matched_on:
        result["matched_on"] = matched_on
    if matched_value:
        result["matched_value"] = matched_value
    return {key: value for key, value in result.items() if value not in (None, [], {})}


def nested_value(mapping: object, outer: str, inner: str) -> object:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(outer)
    if isinstance(value, dict):
        return value.get(inner)
    return None


def first_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            result = first_string(item)
            if result:
                return result
    return None


def compact_trust(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in TRUST_VALUE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            result[key] = item.strip()
        elif isinstance(item, list):
            cleaned = [entry.strip() for entry in item if isinstance(entry, str) and entry.strip()]
            if cleaned:
                result[key] = cleaned
    return result or None


def detail_path(value: object, *, detail_base_label: str | None = None) -> str | None:
    if isinstance(value, str) and value.strip():
        return f"{detail_base_label or '.local'}/{value.strip()}"
    return None


def resolve(
    query: str, people: list[dict[str, Any]], *, fuzzy: bool = False
) -> dict[str, Any]:
    q_norm = normalize(query)
    q_compact = compact(query)
    if not q_norm:
        return {"ok": True, "status": "not_found", "query": query, "candidates": []}

    tiers = [
        (
            "id",
            matches_for(
                people, lambda value: normalize(value) == q_norm, {"id", "person_ref"}
            ),
        ),
        (
            "contact",
            matches_for(
                people, lambda value: normalize(value) == q_norm, contact_sources()
            ),
        ),
        (
            "name",
            matches_for(
                people,
                lambda value: normalize(value) == q_norm,
                {"display_name", "preferred_reference", "alias"},
            ),
        ),
        ("compact", matches_for(people, lambda value: compact(value) == q_compact, None)),
    ]

    if fuzzy and len(q_compact) >= 5:
        fuzzy_matches: list[tuple[dict[str, Any], str, str]] = []
        for person in people:
            for source, value in candidate_values(person):
                value_compact = compact(value)
                if len(value_compact) >= 5 and (
                    value_compact.startswith(q_compact)
                    or q_compact.startswith(value_compact)
                ):
                    fuzzy_matches.append((person, source, value))
                    break
        tiers.append(("fuzzy", fuzzy_matches))

    for tier_name, matches in tiers:
        unique = unique_matches(matches)
        if len(unique) == 1:
            person, source, value = unique[0]
            return {
                "ok": True,
                "status": "matched",
                "query": query,
                "confidence": tier_name,
                "match": public_person(person, matched_on=source, matched_value=value),
                "candidates": [],
            }
        if len(unique) > 1:
            return {
                "ok": True,
                "status": "ambiguous",
                "query": query,
                "confidence": tier_name,
                "candidates": [
                    public_person(
                        person,
                        matched_on=source,
                        matched_value=value,
                        include_detail=False,
                    )
                    for person, source, value in unique
                ],
            }

    return {"ok": True, "status": "not_found", "query": query, "candidates": []}


def contact_sources() -> set[str]:
    return {"github", "email", "discord", "slack", "phone", "website"}


def matches_for(
    people: list[dict[str, Any]],
    matcher: Any,
    allowed_sources: set[str] | None,
) -> list[tuple[dict[str, Any], str, str]]:
    matches: list[tuple[dict[str, Any], str, str]] = []
    for person in people:
        for source, value in candidate_values(person):
            if allowed_sources is not None and source not in allowed_sources:
                continue
            if matcher(value):
                matches.append((person, source, value))
                break
    return matches


def unique_matches(
    matches: list[tuple[dict[str, Any], str, str]]
) -> list[tuple[dict[str, Any], str, str]]:
    seen: set[str] = set()
    unique: list[tuple[dict[str, Any], str, str]] = []
    for person, source, value in matches:
        person_id = str(person.get("id"))
        if person_id in seen:
            continue
        seen.add(person_id)
        unique.append((person, source, value))
    return unique


def run_self_test() -> None:
    sample = """
version: 1
people:
  - id: example-manager
    display_name: Example Manager
    preferred_reference: Example
    aliases: [Example, Example Manager, Exmaple]
    contacts:
      github:
        username: example-manager
  - id: example-reviewer
    display_name: Example Reviewer
    aliases: [Reviewer]
    contacts:
      github:
        username: example-reviewer
"""
    people = validate_people_data(yaml.safe_load(sample))
    assert resolve("@EXAMPLE-MANAGER", people)["match"]["id"] == "example-manager"
    assert resolve("exmaple", people)["match"]["id"] == "example-manager"
    assert resolve("Example", people)["status"] == "matched"
    assert resolve("unknown", people)["status"] == "not_found"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve a local private person identity"
    )
    parser.add_argument(
        "query", nargs="?", help="Name, alias, handle, or person:<id> to resolve"
    )
    parser.add_argument(
        "--index",
        type=Path,
        help="Read exactly one people index file; disables default scoped lookup",
    )
    parser.add_argument(
        "--scope",
        choices=["auto", "global", "user", "repo"],
        default="auto",
        help="People index scope to read; default auto loads global plus repo overlay",
    )
    parser.add_argument(
        "--global-index",
        type=Path,
        help="Override the global/user people index path for this lookup",
    )
    parser.add_argument(
        "--repo-index",
        type=Path,
        help="Override the repo-local people index path for this lookup",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used to find .local/people.yaml; default is cwd",
    )
    parser.add_argument(
        "--explain-sources",
        action="store_true",
        help="Include sanitized index-source statuses and counts in the JSON output",
    )
    parser.add_argument(
        "--fuzzy", action="store_true", help="Allow conservative non-write fuzzy matching"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when the query does not resolve exactly once",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="Run built-in resolver smoke tests"
    )
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        print(json.dumps({"ok": True, "status": "self_test_passed"}, sort_keys=True))
        return
    if not args.query:
        parser.error("query is required unless --self-test is used")

    try:
        sources = None
        if args.index:
            status, people = load_people(args.index.expanduser())
        else:
            status, people, sources = load_scoped_people(
                scope=args.scope,
                global_index=args.global_index,
                repo_index=args.repo_index,
                repo_root=args.repo_root,
            )
        if status == "no_index":
            payload = {
                "ok": True,
                "status": "no_index",
                "query": args.query,
                "candidates": [],
            }
        else:
            payload = resolve(args.query, people, fuzzy=args.fuzzy)
        if args.explain_sources and sources is not None:
            payload["sources"] = sources
    except PeopleConfigError as exc:
        payload = {"ok": False, "status": "error", "error": str(exc), "query": args.query}
        print(json.dumps(payload, sort_keys=True), file=sys.stdout)
        raise SystemExit(1) from exc

    print(json.dumps(payload, sort_keys=True))
    if args.strict and payload.get("status") != "matched":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
