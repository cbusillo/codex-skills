#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record that a direction turn or weekly audit just finished on this machine.

The session-start hook in `hooks/direction_check_hook.py` reads this marker
and reminds the owner while a check is overdue. Run it at the end of a daily
turn with `turn`, or at the end of a weekly audit with `audit`; an audit
counts as a turn as well.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

MARKER_NAME = "direction-last-check.json"


def marker_path(env: Mapping[str, str] | None = None) -> Path:
    source: Mapping[str, str] = os.environ if env is None else env
    for name in ("CODE_HOME", "CODEX_HOME"):
        value = source.get(name)
        if value:
            return Path(value) / MARKER_NAME
    return Path(source.get("HOME", "~")).expanduser() / ".code" / MARKER_NAME


def mark(path: Path, kind: str, now: dt.datetime) -> dict[str, str]:
    try:
        current = json.loads(path.read_text())
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    stamp = now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    current["turn"] = stamp
    if kind == "audit":
        current["audit"] = stamp
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    return {key: value for key, value in current.items() if isinstance(value, str)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["turn", "audit"])
    args = parser.parse_args(argv)
    path = marker_path()
    written = mark(path, args.kind, dt.datetime.now(dt.timezone.utc))
    print(json.dumps({"ok": True, "marker": str(path), **written}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
