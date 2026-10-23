#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
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
    parser = argparse.ArgumentParser(description="Compare pinned read fields with generated OpenAPI.")
    parser.add_argument("command", choices=("check", "sync"))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ref", help="Historical ref with a published OpenAPI snapshot")
    source.add_argument("--openapi-file", type=Path, help="OpenAPI exported from a producer checkout")
    args = parser.parse_args(argv)
    source_label = "the supplied generated schema" if args.openapi_file else f"Launchplane {args.ref}"
    try:
        document = args.openapi_file.read_bytes() if args.openapi_file else fetch(args.ref)
        upstream = render_pin(json.loads(document))
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        print(f"unavailable: could not derive read fields from {source_label} ({type(exc).__name__}: {exc}). "
              "Generate OpenAPI from Launchplane source and pass --openapi-file.",
              file=sys.stderr)
        return 2
    if args.command == "check":
        if pin.read_bytes() == upstream:
            print(f"current: {pin.name} matches {source_label}")
            return 0
        print(
            f"drift: {pin.name} differs from {source_label}. Run "
            "`uv run skills/launchplane/scripts/sync-read-response-fields.py sync --openapi-file FILE`, "
            "add each new field to its read in BOUNDED_READ_RESPONSE_FIELDS, "
            "and name the Launchplane commit in the commit message.",
            file=sys.stderr,
        )
        return 1
    pin.write_bytes(upstream)
    print(f"synced: {pin.name} from {source_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
