#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Save session reminders through the shared native writer, or prepare a dry run."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from uuid import UUID
from zoneinfo import ZoneInfo

from reminder_link import prepare, target_list


def plan(harness: str, directory: Path, session: str | None, prompt: str | None,
         list_name: str, due: str, timezone: str, links: list[str],
         title: str | None = None, repeat_days: int | None = None) -> dict:
    link = prepare(harness, directory, session, prompt)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", due):
        raise ValueError("Use a local due time YYYY-MM-DDTHH:MM and a named --timezone")
    local = datetime.fromisoformat(due)
    if local.tzinfo is not None or local.second or local.microsecond:
        raise ValueError("Use a local due time YYYY-MM-DDTHH:MM and a named --timezone")
    zone = ZoneInfo(timezone)
    aware = local.replace(tzinfo=zone)
    # Refuse nonexistent and ambiguous wall times rather than guess a DST offset.
    if (aware.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) != local
            or aware.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset()):
        raise ValueError("Due time is nonexistent or ambiguous in the requested timezone")
    if repeat_days is not None and repeat_days < 1:
        raise ValueError("Repeat interval must be positive")
    for url in links:
        if not re.fullmatch(r"https://github[.]com/[\w.-]+/[\w.-]+/(issues|pull)/[1-9]\d*", url):
            raise ValueError("Links must be exact GitHub issue or PR URLs")
    provider = "Codex" if harness == "codex" else "Claude"
    identity = str(UUID(session)) if session else "fresh session"
    if title is None:
        reference = ", ".join("#" + url.rsplit("/", 1)[1] for url in links)
        title = f"{link['repository']}: {reference or identity} ({provider})"
    if not title.strip() or any(c in title for c in "\n\r\x00"):
        raise ValueError("Title must be one non-empty line")
    if not list_name.strip():
        raise ValueError("Exact list name is required")
    return {"list": list_name, "title": title,
            "notes": "\n".join([f"{provider}: {identity}", *dict.fromkeys(links)]),
            "url": link["url"], "marker": link["marker"],
            "due": local.isoformat(timespec="minutes"), "timezone": timezone,
            "repeat_days": repeat_days, "fallback": link["fallback"]}


def refusal(message: str, **extra) -> ValueError:
    return ValueError(json.dumps({"status": "refused", "message": message, "created": False, **extra},
                                 ensure_ascii=False, sort_keys=True))


def decide(request: dict, reminders: list[dict]) -> dict:
    """Pick the one item to change, or create; completed items are never targets."""
    matches = [r for r in reminders if r["same_target"] and not r["completed"]]
    if len(matches) > 1:
        raise refusal("Duplicate incomplete session reminders; no mutation")
    existing = matches[0]["id"] if matches else None
    if request.get("delete_id"):
        if existing is None or existing != request["delete_id"]:
            raise refusal("Delete ID must match the incomplete session in the exact list")
        return {"action": "delete", "id": existing}
    if existing is not None and request.get("update_id") != existing:
        raise refusal("Existing reminder found; explicit update ID and write authority required",
                      reminder_id=existing)
    if existing is None and request.get("update_id"):
        raise refusal("Update target not found; no replacement created")
    # The visible URL step finds the row by title, and a shown completed row would match too.
    if any(r["id"] != existing and r["title"] == request["title"] for r in reminders):
        raise refusal("Title is not unique in the target list, counting completed items; "
                      "pass a distinct --title. No UI edit")
    return {"action": "update", "id": existing} if existing else {"action": "create", "id": None}


def receive(process: subprocess.Popen) -> dict | None:
    try:
        return json.loads(process.stdout.readline())
    except json.JSONDecodeError:
        return None


def exchange(process: subprocess.Popen, request: dict) -> dict | None:
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    receipt = receive(process)
    if receipt is not None and receipt.get("status") == "lookup":
        try:
            process.stdin.write(json.dumps(decide(request, receipt["reminders"])) + "\n")
        finally:
            # Without a decision line the writer refuses and changes nothing.
            process.stdin.close()
        receipt = receive(process)
    process.wait()
    return receipt


def native(request: dict) -> dict:
    if sys.platform != "darwin":
        raise ValueError("Native saving requires macOS 14+; use the prepared manual fields")
    # The writer prints one lookup line, then waits for this side's target decision.
    with tempfile.TemporaryFile("w+") as errors, subprocess.Popen(
            ["swift", str(Path(__file__).with_suffix(".swift"))], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, text=True) as process:
        watchdog = threading.Timer(90, process.kill)
        watchdog.start()
        try:
            receipt = exchange(process, request)
        finally:
            watchdog.cancel()
        if receipt is None:
            errors.seek(0)
            raise ValueError(errors.read().strip() or "Native writer returned no receipt; read back before retrying")
    expected = "deleted" if request.get("delete_id") else "saved"
    if (process.returncode or receipt.get("status") != expected
            or (expected == "saved" and receipt.get("visible_url_verified") is not True)):
        raise ValueError(json.dumps(receipt, ensure_ascii=False))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=["codex", "claude"])
    parser.add_argument("--directory", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--session-id")
    mode.add_argument("--prompt")
    parser.add_argument("--list", dest="list_name")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--due", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--link", action="append", default=[])
    parser.add_argument("--title")
    parser.add_argument("--repeat-days", type=int)
    parser.add_argument("--apply", action="store_true", help="Save only under existing reminder-write authority")
    parser.add_argument("--preserve-existing", action="store_true",
                        help="With --update-id, change only Notes and URL; preserve scheduling and other fields")
    mutation = parser.add_mutually_exclusive_group()
    mutation.add_argument("--update-id", help="Explicitly authorized existing reminder ID; otherwise never edit existing items")
    mutation.add_argument("--delete-id", help="Remove only this exact incomplete session reminder under removal authority")
    args = parser.parse_args()
    try:
        if args.preserve_existing and not args.update_id:
            raise ValueError("--preserve-existing requires an explicit --update-id")
        name = target_list(args.repo or args.directory, args.list_name, dict(os.environ), Path.home())
        fields = plan(args.harness, args.directory, args.session_id, args.prompt,
                      name, args.due, args.timezone, args.link, args.title, args.repeat_days)
        if args.apply:
            # Fallback is for the caller, never sent to the native notes writer.
            request = {k: v for k, v in fields.items() if k != "fallback"}
            request["update_id"] = args.update_id
            request["delete_id"] = args.delete_id
            request["preserve_existing"] = args.preserve_existing
            print(json.dumps(native(request), ensure_ascii=False))
        else:
            print(json.dumps({"status": "dry_run", **fields,
                              "update_id": args.update_id,
                              "preserve_existing": args.preserve_existing}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
