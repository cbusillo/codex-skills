#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Save plain or session reminders through the shared native writer, or prepare a dry run."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from reminder_link import prepare, target_list
from reminder_shortcut import SHORTCUT_NAME

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def due_time(due: str, timezone: str) -> datetime:
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
    return local


def plain_plan(title: str, notes: str, url: str | None, list_name: str,
               due: str, timezone: str, repeat: str | None = None,
               weekday: str | None = None) -> dict:
    local = due_time(due, timezone)
    if not title.strip() or any(c in title for c in "\n\r\x00"):
        raise ValueError("Title must be one non-empty line")
    if not list_name.strip() or "\x00" in notes:
        raise ValueError("Exact list name and valid notes are required")
    if url is not None:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError("Plain reminder URL must be an absolute HTTP or HTTPS URL")
        # Normalize each URL component before dry run, lookup and save.
        def escaped(value: str, safe: str) -> str:
            return quote(re.sub(r"%(?![0-9a-fA-F]{2})", "%25", value), safe=safe)

        if not parsed.hostname.isascii():
            raise ValueError("Use a browser-copied ASCII/punycode hostname; Unicode paths are supported")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        userinfo = escaped(parsed.netloc.rpartition("@")[0], ":!$&'()*+,;=%") + "@" if "@" in parsed.netloc else ""
        url = parsed._replace(netloc=userinfo + host + port,
                              path=escaped(parsed.path, "/:@!$&'()*+,;=%"),
                              query=escaped(parsed.query, "/?:@!$&'()*+,;=%"),
                              fragment=escaped(parsed.fragment, "/?:@!$&'()*+,;=%")).geturl()
    if repeat not in (None, "daily", "weekdays", "weekly", "monthly"):
        raise ValueError("Unsupported repeat pattern")
    if repeat == "weekly":
        if weekday not in WEEKDAYS or local.weekday() != WEEKDAYS.index(weekday):
            raise ValueError("Weekly repeats require --weekday matching the first due date")
    elif weekday is not None:
        raise ValueError("--weekday requires --repeat weekly")
    if repeat == "weekdays" and local.weekday() > 4:
        raise ValueError("Weekdays repeats require a weekday first due date")
    return {"kind": "plain", "list": list_name, "title": title, "notes": notes,
            "url": url or "", "marker": "", "due": due, "timezone": timezone,
            "repeat_days": None, "repeat": repeat,
            "weekday": WEEKDAYS.index(weekday) if weekday else None}


def plan(harness: str, directory: Path, session: str | None, prompt: str | None,
         list_name: str, due: str, timezone: str, links: list[str],
         title: str | None = None, repeat_days: int | None = None) -> dict:
    link = prepare(harness, directory, session, prompt)
    local = due_time(due, timezone)
    if repeat_days is not None and repeat_days < 1:
        raise ValueError("Repeat interval must be positive")
    for url in links:
        if not re.fullmatch(r"https://github[.]com/[\w.-]+/[\w.-]+/(issues|pull)/[1-9]\d*", url):
            raise ValueError("Links must be exact GitHub issue or PR URLs")
    provider = "Codex" if harness == "codex" else "Claude"
    identity = str(UUID(session)) if session else "fresh session"
    if title is None:
        reference = ", ".join("#" + url.rsplit("/", 1)[1] for url in links)
        # The due time keeps a session's next check-in distinct from its last.
        when = f"{local:%Y-%m-%d %H:%M}"
        if repeat_days is not None:
            when = f"every {repeat_days} days from {when}"
        title = f"{link['repository']}: {reference or identity} ({provider}) {when}"
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
        raise refusal("Duplicate incomplete reminders; no mutation")
    existing = matches[0]["id"] if matches else None
    if request.get("delete_id"):
        if existing is None or existing != request["delete_id"]:
            raise refusal("Delete ID must match the incomplete reminder in the exact list")
        return {"action": "delete", "id": existing}
    if existing is not None and request.get("update_id") != existing:
        raise refusal("Existing reminder found; explicit update ID and write authority required",
                      reminder_id=existing)
    if existing is None and request.get("update_id"):
        raise refusal("Update target not found; no replacement created")
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
            or (expected == "saved" and request.get("kind") == "plain"
                and receipt.get("saved_fields_verified") is not True)
            or (expected == "saved" and request.get("url")
                and receipt.get("visible_url_verified") is not True)):
        raise ValueError(json.dumps(receipt, ensure_ascii=False))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=["codex", "claude"])
    parser.add_argument("--directory", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plain", action="store_true", help="Create a general reminder without a session")
    mode.add_argument("--session-id")
    mode.add_argument("--prompt")
    parser.add_argument("--list", dest="list_name")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--due", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--link", action="append", default=[])
    parser.add_argument("--title")
    parser.add_argument("--notes", default="")
    parser.add_argument("--url", help="Optional HTTP(S) attachment for a plain reminder")
    parser.add_argument("--repeat", choices=["daily", "weekdays", "weekly", "monthly"])
    parser.add_argument("--weekday", choices=WEEKDAYS)
    parser.add_argument("--repeat-days", type=int)
    parser.add_argument("--apply", action="store_true", help="Save only under existing reminder-write authority")
    parser.add_argument("--preserve-existing", action="store_true",
                        help="With --update-id, change only Notes and URL; preserve scheduling and other fields")
    mutation = parser.add_mutually_exclusive_group()
    mutation.add_argument("--update-id", help="Explicitly authorized existing reminder ID; otherwise never edit existing items")
    mutation.add_argument("--delete-id", help="Remove only this exact incomplete reminder under removal authority")
    args = parser.parse_args()
    try:
        if args.preserve_existing and not args.update_id:
            raise ValueError("--preserve-existing requires an explicit --update-id")
        if args.plain:
            if (args.harness or args.directory or args.link or args.repeat_days is not None
                    or not args.title):
                raise ValueError("--plain requires --title and excludes session fields and --repeat-days")
            if not args.list_name and not args.repo:
                raise ValueError("--plain requires --list or --repo for private list selection")
            name = target_list(args.repo or Path.cwd(), args.list_name, dict(os.environ), Path.home())
            fields = plain_plan(args.title, args.notes, args.url, name, args.due,
                                args.timezone, args.repeat, args.weekday)
        else:
            if not args.harness or not args.directory:
                raise ValueError("Session reminders require --harness and --directory")
            if args.notes or args.url is not None or args.repeat or args.weekday:
                raise ValueError("--notes, --url, --repeat and --weekday require --plain")
            name = target_list(args.repo or args.directory, args.list_name, dict(os.environ), Path.home())
            fields = plan(args.harness, args.directory, args.session_id, args.prompt,
                          name, args.due, args.timezone, args.link, args.title, args.repeat_days)
        if args.apply:
            # Fallback is for the caller, never sent to the native notes writer.
            request = {k: v for k, v in fields.items() if k != "fallback"}
            request["update_id"] = args.update_id
            request["delete_id"] = args.delete_id
            request["preserve_existing"] = args.preserve_existing
            request["shortcut"] = SHORTCUT_NAME
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
