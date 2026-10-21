#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Keep the pinned Launchplane read response fields equal to Launchplane's.

The pin lists, for each bounded GET read in BOUNDED_READ_RESPONSE_FIELDS, the
top-level response fields Launchplane's published OpenAPI declares for that
route. The tests require each pinned set to fit the caller's allowlist, so a
field Launchplane adds is accepted, and projected only on purpose, before it
stops a read.

`sync [--ref REF]` rewrites the pin from Launchplane's OpenAPI at REF (default
`main`). Name the Launchplane commit in the commit that changes the pin.

`check [--ref REF]` exits 0 when the pin matches, 1 when it differs, and 2 when
Launchplane's OpenAPI could not be read or no longer describes a pinned route.
CI runs it so a drift fails the build.
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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_contract import BOUNDED_READ_RESPONSE_FIELDS, bounded_read_path  # noqa: E402
from launchplane_safety import safe_urlopen  # noqa: E402

UPSTREAM_REPOSITORY = "cbusillo/launchplane"
UPSTREAM_PATH = "frontend/generated/openapi-canonical.json"
PIN_PATH = Path(__file__).resolve().parent.parent / "references" / "launchplane-read-response-fields.json"
MAX_OPENAPI_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 30.0

Fetch = Callable[[str], bytes]


def upstream_url(ref: str) -> str:
    return (
        f"https://raw.githubusercontent.com/{UPSTREAM_REPOSITORY}/"
        f"{urllib.parse.quote(ref, safe='')}/{UPSTREAM_PATH}"
    )


def fetch_upstream(ref: str) -> bytes:
    request = urllib.request.Request(
        upstream_url(ref), headers={"User-Agent": "codex-skills-read-response-fields/1"}
    )
    with safe_urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body = response.read(MAX_OPENAPI_BYTES + 1)
    if len(body) > MAX_OPENAPI_BYTES:
        raise ValueError("upstream OpenAPI document is too large")
    return bytes(body)


def response_fields(openapi: dict[str, Any], path: str) -> list[str]:
    """Top-level properties of the route's 200 JSON response schema."""
    schemas = openapi["components"]["schemas"]
    schema = openapi["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    while "$ref" in schema:
        schema = schemas[schema["$ref"].removeprefix("#/components/schemas/")]
    properties = schema["properties"]
    if not isinstance(properties, dict) or not properties:
        raise ValueError(f"{path} declares no response fields")
    return sorted(properties)


def render_pin(openapi: dict[str, Any]) -> bytes:
    reads = {
        command: response_fields(openapi, bounded_read_path(command))
        for command in sorted(BOUNDED_READ_RESPONSE_FIELDS)
    }
    return (json.dumps(reads, indent=2, sort_keys=True) + "\n").encode()


def run(argv: list[str], *, fetch: Fetch = fetch_upstream, pin: Path = PIN_PATH) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "sync"))
    parser.add_argument("--ref", default="main", help="Launchplane git ref to compare or copy (default: main)")
    args = parser.parse_args(argv)
    try:
        upstream = render_pin(json.loads(fetch(args.ref)))
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        print(f"unavailable: could not derive read fields from {upstream_url(args.ref)} ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 2
    if args.command == "check":
        if pin.read_bytes() == upstream:
            print(f"current: {pin.name} matches Launchplane {args.ref}")
            return 0
        print(
            f"drift: {pin.name} differs from Launchplane {args.ref}. Run "
            "`uv run skills/launchplane/scripts/sync-read-response-fields.py sync`, "
            "add each new field to its read in BOUNDED_READ_RESPONSE_FIELDS, "
            "and name the Launchplane commit in the commit message.",
            file=sys.stderr,
        )
        return 1
    pin.write_bytes(upstream)
    print(f"synced: {pin.name} from Launchplane {args.ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
