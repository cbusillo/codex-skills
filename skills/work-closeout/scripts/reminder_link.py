#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prepare a native session reminder; never write a reminder or open its URL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib
from urllib.parse import urlencode, quote
from uuid import UUID


def target_list(repo: Path, override: str | None, env: dict[str, str], home: Path) -> str:
    """First configured private field wins; invalid present config fails closed."""
    if override is not None:
        if not override.strip():
            raise ValueError("Target list must be non-empty")
        return override
    candidates = [repo / ".local/skill-data/work-closeout.toml"]
    candidates.extend(Path(env[key]) / "skill-data/work-closeout.toml"
                      for key in ("CODE_HOME", "CODEX_HOME") if env.get(key))
    candidates.append(home / ".code/skill-data/work-closeout.toml")
    for candidate in dict.fromkeys(candidates):
        if not candidate.exists():
            continue
        try:
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            reminders = data.get("reminders", {})
            if not isinstance(reminders, dict):
                raise ValueError("Invalid reminders configuration")
            if "list" not in reminders:
                continue
            name = reminders["list"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Target list must be a non-empty string")
            return name
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("Cannot read private reminder configuration") from exc
    raise ValueError("No target list configured; provide --list or private [reminders].list")


def require_unique_list(name: str, titles: list[str]) -> None:
    count = titles.count(name)
    if count != 1:
        raise ValueError(f"Target list has {count} exact matches; expected one. No default-list fallback.")


def verify_native_list(name: str) -> None:
    if sys.platform != "darwin":
        raise ValueError("Native list verification requires macOS; use the manual workflow on this platform")
    script = Path(__file__).with_name("reminder_list.swift")
    result = subprocess.run(["swift", str(script), name], capture_output=True, text=True,
                            timeout=45, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Native list verification failed")
    require_unique_list(name, json.loads(result.stdout))


def prepare(harness: str, directory: Path, session: str | None, prompt: str | None) -> dict[str, str]:
    if harness not in {"codex", "claude"}:
        raise ValueError("Unsupported harness")
    directory = directory.expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("Working directory must be a directory")
    if (session is None) == (prompt is None):
        raise ValueError("Choose exactly one of session ID or fresh-session prompt")
    if session is not None:
        session = str(UUID(session))
        argv = [harness, "resume" if harness == "codex" else "--resume", session]
        identity = session
    else:
        if prompt is None or not prompt.strip() or len(prompt) > 1000 or "\x00" in prompt:
            raise ValueError("Fresh-session prompt must contain 1–1000 characters without NUL")
        # End option parsing so even a prompt beginning with '-' remains data.
        argv = [harness, "--", prompt]
        identity = prompt
    command = shlex.join(argv)
    query = urlencode({"c": command, "d": str(directory)}, quote_via=quote, safe="")
    key = hashlib.sha256(json.dumps([harness, str(directory), "resume" if session is not None else "fresh", identity],
                                   ensure_ascii=False).encode()).hexdigest()
    return {"url": "iterm2:/command?" + query, "command": command,
            "directory": str(directory), "repository": directory.name,
            "marker": "session-reminder:v1:" + key,
            "fallback": "cd -- " + shlex.quote(str(directory)) + " && " + command}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=["codex", "claude"])
    parser.add_argument("--directory", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session-id")
    mode.add_argument("--prompt")
    parser.add_argument("--list", dest="list_name", help="One-off exact target-list override")
    parser.add_argument("--repo", type=Path, help="Repository config root; defaults to directory")
    parser.add_argument("--verify-list", action="store_true", help="Read-only native list lookup on macOS")
    args = parser.parse_args()
    try:
        result = prepare(args.harness, args.directory, args.session_id, args.prompt)
        result["list"] = target_list(args.repo or Path(result["directory"]), args.list_name,
                                     dict(os.environ), Path.home())
        if args.verify_list:
            verify_native_list(result["list"])
        result["list_verification"] = "unique" if args.verify_list else "manual_required"
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
