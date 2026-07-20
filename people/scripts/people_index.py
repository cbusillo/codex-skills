#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Scoped writes for private people indexes."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


RESOLVER_PATH = Path(__file__).with_name("resolve_person.py")
SPEC = importlib.util.spec_from_file_location("resolve_person", RESOLVER_PATH)
assert SPEC is not None
resolve_person = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(resolve_person)


def target_index(args: argparse.Namespace) -> tuple[Path, str]:
    if args.index:
        return args.index.expanduser(), "explicit"
    if args.scope in {"global", "user"}:
        path, _label = resolve_person.default_global_index()
        return path, "global"
    return resolve_person.default_repo_index(args.repo_root), "repo"


def load_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "people": []}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise resolve_person.PeopleConfigError(
            f"invalid YAML in target people index: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise resolve_person.PeopleConfigError("people index must be a YAML mapping")
    if data.get("people") is None:
        data["people"] = []
    if not isinstance(data.get("people"), list):
        raise resolve_person.PeopleConfigError("people index field `people` must be a list")
    return data


def split_csv(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for item in value.split(","):
            text = item.strip()
            if text and text not in result:
                result.append(text)
    return result


def ensure_mapping(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        value = {}
        mapping[key] = value
    return value


def upsert_person(data: dict[str, Any], args: argparse.Namespace) -> tuple[str, list[str]]:
    people = data["people"]
    person_id = args.id
    changed: list[str] = []
    existing = None
    for person in people:
        if person.get("id") == person_id:
            existing = person
            break
    if existing is None:
        if not args.display_name:
            raise resolve_person.PeopleConfigError(
                "--display-name is required when creating a person"
            )
        existing = {"id": person_id, "display_name": args.display_name}
        people.append(existing)
        changed.extend(["id", "display_name"])
        action = "created"
    else:
        action = "updated"

    def set_string(field: str, value: str | None) -> None:
        if value is not None and existing.get(field) != value:
            existing[field] = value
            changed.append(field)

    set_string("display_name", args.display_name)
    set_string("preferred_reference", args.preferred_reference)
    set_string("github", args.github)

    aliases = split_csv(args.alias)
    if aliases:
        current = existing.get("aliases")
        if not isinstance(current, list):
            current = []
        merged = [item for item in current if isinstance(item, str) and item.strip()]
        for alias in aliases:
            if alias not in merged:
                merged.append(alias)
        if merged != current:
            existing["aliases"] = merged
            changed.append("aliases")

    roles = split_csv(args.role)
    if roles:
        relationship = ensure_mapping(existing, "relationship")
        current = relationship.get("roles")
        if not isinstance(current, list):
            current = []
        merged = [item for item in current if isinstance(item, str) and item.strip()]
        for role in roles:
            if role not in merged:
                merged.append(role)
        if merged != current:
            relationship["roles"] = merged
            changed.append("relationship.roles")

    if args.company or args.team or args.title:
        organization = ensure_mapping(existing, "organization")
        for field in ("company", "team", "title"):
            value = getattr(args, field)
            if value is not None and organization.get(field) != value:
                organization[field] = value
                changed.append(f"organization.{field}")

    if args.preferred_contact or args.timezone or args.mention_style:
        preferences = ensure_mapping(existing, "preferences")
        for field in ("preferred_contact", "timezone", "mention_style"):
            value = getattr(args, field)
            if value is not None and preferences.get(field) != value:
                preferences[field] = value
                changed.append(f"preferences.{field}")

    if args.details_file is not None:
        resolve_person.validate_detail_file(args.details_file, 0)
        if existing.get("details_file") != args.details_file:
            existing["details_file"] = args.details_file
            changed.append("details_file")

    resolve_person.validate_people_data(data)
    return action if changed else "unchanged", changed


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(temp_name).replace(path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write scoped private people index entries"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    upsert = subparsers.add_parser("upsert", help="Create or update one person")
    upsert.add_argument("--id", required=True, help="Stable lowercase person id")
    upsert.add_argument("--display-name", help="Human display name")
    upsert.add_argument("--preferred-reference", help="Preferred short reference")
    upsert.add_argument("--github", help="GitHub username without @")
    upsert.add_argument("--alias", action="append", help="Alias; repeat or comma-separate")
    upsert.add_argument("--role", action="append", help="Role; repeat or comma-separate")
    upsert.add_argument("--company", help="Organization company")
    upsert.add_argument("--team", help="Organization team")
    upsert.add_argument("--title", help="Organization title")
    upsert.add_argument("--preferred-contact", help="Preferred contact surface")
    upsert.add_argument("--timezone", help="Preferred timezone")
    upsert.add_argument("--mention-style", help="Preferred mention style")
    upsert.add_argument("--details-file", help="Relative people/<id>.md details file")
    upsert.add_argument(
        "--scope",
        choices=["global", "user", "repo"],
        default="global",
        help="Write target scope; defaults to global/user CODE_HOME storage",
    )
    upsert.add_argument("--index", type=Path, help="Explicit write target file")
    upsert.add_argument(
        "--repo-root",
        type=Path,
        help="Repository root used when --scope repo; default is cwd",
    )
    upsert.add_argument("--dry-run", action="store_true", help="Validate without writing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path, scope = target_index(args)
    try:
        data = load_document(path)
        action, changed = upsert_person(data, args)
        if not args.dry_run:
            write_atomic(path, data)
        payload = {
            "ok": True,
            "status": "dry_run" if args.dry_run else action,
            "scope": scope,
            "id": args.id,
            "changed_fields": changed,
        }
    except resolve_person.PeopleConfigError as exc:
        payload = {"ok": False, "status": "error", "scope": scope, "error": str(exc)}
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
