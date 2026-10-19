#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Keep the pinned merge-train controller reply schema equal to Launchplane's.

`sync [--ref REF]` replaces the pin with Launchplane's published file at REF
(default `main`), byte for byte, after checking this skill can enforce it.
Name the Launchplane commit in the commit that changes the pin.

`check [--ref REF]` exits 0 when the pin matches, 1 when it differs, and 2 when
Launchplane's file could not be read. CI runs it so a drift fails the build.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_reply_schema import (  # noqa: E402
    CONTROLLER_REPLY_SCHEMA_PATH,
    check_schema,
)
from launchplane_safety import safe_urlopen  # noqa: E402

UPSTREAM_REPOSITORY = "cbusillo/launchplane"
UPSTREAM_PATH = "contracts/merge-train-controller-reply.schema.json"
MAX_SCHEMA_BYTES = 256 * 1024
TIMEOUT_SECONDS = 15.0

Fetch = Callable[[str], bytes]


def upstream_url(ref: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{UPSTREAM_REPOSITORY}/"
        f"{urllib.parse.quote(ref, safe='')}/{UPSTREAM_PATH}"
    )


def fetch_upstream(ref: str) -> bytes:
    request = urllib.request.Request(
        upstream_url(ref), headers={"User-Agent": "codex-skills-merge-train-reply-schema/1"}
    )
    with safe_urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_SCHEMA_BYTES + 1)
    if len(body) > MAX_SCHEMA_BYTES:
        raise ValueError("upstream schema is too large")
    return bytes(body)


def _enforceable(body: bytes) -> bytes:
    check_schema(json.loads(body))
    return body


def run(argv: list[str], *, fetch: Fetch = fetch_upstream, pin: Path = CONTROLLER_REPLY_SCHEMA_PATH) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "sync"))
    parser.add_argument("--ref", default="main", help="Launchplane git ref to compare or copy (default: main)")
    args = parser.parse_args(argv)
    try:
        upstream = fetch(args.ref)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"unavailable: could not read {upstream_url(args.ref)} ({type(exc).__name__})", file=sys.stderr)
        return 2
    if args.command == "check":
        if pin.read_bytes() == upstream:
            print(f"current: {pin.name} matches Launchplane {args.ref}")
            return 0
        print(
            f"drift: {pin.name} differs from Launchplane {args.ref}. Run "
            "`uv run skills/launchplane/scripts/sync-merge-train-reply-schema.py sync` "
            "and name the Launchplane commit in the commit message.",
            file=sys.stderr,
        )
        return 1
    try:
        pin.write_bytes(_enforceable(upstream))
    except ValueError as exc:
        print(f"refused: Launchplane's schema cannot be enforced here ({exc})", file=sys.stderr)
        return 1
    print(f"synced: {pin.name} from Launchplane {args.ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
