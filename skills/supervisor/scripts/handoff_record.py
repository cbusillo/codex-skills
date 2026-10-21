#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import json
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

from account_choice import config_paths

ISSUE = re.compile(r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)")
LOGIN = re.compile(r"[A-Za-z0-9_.-]+")


def configured_issue(paths: list[Path]) -> tuple[str, Path] | None:
    for path in paths:
        if not path.exists():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"cannot read private supervisor config {path}") from error
        section = data.get("handoff")
        if section is None:
            continue
        if not isinstance(section, dict) or not isinstance(section.get("issue"), str):
            raise ValueError(f'[handoff] in {path} needs issue = "OWNER/REPO#NUMBER"')
        return section["issue"], path
    return None


def handoff_record(owner: str, paths: list[Path]) -> dict[str, str]:
    if not LOGIN.fullmatch(owner):
        raise ValueError("--owner takes the Director's GitHub login")
    configured = configured_issue(paths)
    if configured is None:
        return {"repository": f"{owner}/direction", "source": "default"}
    issue, path = configured
    match = ISSUE.fullmatch(issue)
    if match is None:
        raise ValueError(f"[handoff] issue in {path} must be OWNER/REPO#NUMBER")
    if match["owner"].lower() != owner.lower():
        raise ValueError(f"[handoff] issue {issue} is outside {owner}'s account; use an issue in your own repository")
    return {
        "issue": issue,
        "url": f"https://github.com/{match['owner']}/{match['repo']}/issues/{match['number']}",
        "source": str(path),
    }


def main(env: Mapping[str, str] = os.environ) -> None:
    parser = argparse.ArgumentParser(description="Print this Director's Supervisor handoff record.")
    parser.add_argument("--owner", required=True, help="the Director's GitHub login")
    parser.add_argument("--config", type=Path, help="private config; default skill-data/supervisor.toml")
    args = parser.parse_args()
    paths = [args.config] if args.config else config_paths(env, Path.home())
    try:
        print(json.dumps(handoff_record(args.owner, paths), indent=2))
    except ValueError as error:
        parser.exit(1, f"refused: {error}\n")


if __name__ == "__main__":
    main()
