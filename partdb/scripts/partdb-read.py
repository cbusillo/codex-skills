#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only Part-DB helper that resolves its target through private context."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "partdb.context.v1"


class PartdbError(RuntimeError):
    pass


def runtime_home() -> Path:
    return Path(os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or Path.home() / ".code")


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def context() -> tuple[Path, dict[str, Any]]:
    local = runtime_home() / "local-context.toml"
    try:
        with local.open("rb") as handle:
            configured = tomllib.load(handle)
        private_repo = Path(configured["docs"]["local_infra"]).expanduser()
    except (FileNotFoundError, KeyError, TypeError, tomllib.TOMLDecodeError, PermissionError, UnicodeDecodeError):
        raise PartdbError("private Part-DB context is not configured") from None
    provider = private_repo / "scripts" / "infra-context.py"
    try:
        result = subprocess.run(
            [sys.executable, str(provider), "partdb", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PartdbError("private Part-DB context provider failed; output redacted") from None
    if result.returncode:
        raise PartdbError("private Part-DB context provider failed; output redacted")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise PartdbError("private Part-DB context provider returned invalid JSON") from None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise PartdbError("private Part-DB context has an unsupported schema version")
    return private_repo, value


def environment(private_repo: Path, config: dict[str, Any]) -> tuple[str, str]:
    api = config.get("api")
    policy = config.get("policy")
    if not isinstance(api, dict) or not isinstance(policy, dict) or policy.get("allow_mutations") is not False:
        raise PartdbError("private Part-DB context is not read-only")
    base_key = api.get("base_url_env")
    token_key = api.get("read_token_env")
    env_file = api.get("env_file")
    if not all(isinstance(value, str) and value for value in (base_key, token_key, env_file)):
        raise PartdbError("private Part-DB context is incomplete")
    values = dict(os.environ)
    candidate = (private_repo / env_file).resolve()
    try:
        candidate.relative_to(private_repo.resolve())
    except ValueError:
        raise PartdbError("private Part-DB environment file is invalid") from None
    if candidate.is_file():
        try:
            lines = candidate.read_text().splitlines()
        except (PermissionError, UnicodeDecodeError):
            raise PartdbError("Part-DB read credentials are unavailable") from None
        for line in lines:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    base_url = values.get(base_key, "").rstrip("/")
    token = values.get(token_key, "")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not token:
        raise PartdbError("Part-DB read credentials are unavailable")
    return base_url, token


def request(base_url: str, token: str, path: str, query: dict[str, str] | None = None) -> Any:
    url = f"{base_url}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/ld+json", "Authorization": f"Bearer {token}"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise PartdbError(f"Part-DB request failed with HTTP {exc.code}; response body redacted") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        raise PartdbError("Part-DB read request failed; connection details redacted") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Part-DB helper")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("context-check")
    commands.add_parser("schema-probe")
    search = commands.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    locations = commands.add_parser("locations")
    locations.add_argument("--part-id", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        private_repo, config = context()
        if args.command == "context-check":
            environment(private_repo, config)
            print(json.dumps({"context_ready": True, "schema_version": SCHEMA_VERSION}, sort_keys=True))
            return 0
        base_url, token = environment(private_repo, config)
        if args.command == "schema-probe":
            schema = request(base_url, token, "/api/docs.json")
            paths = schema.get("paths") if isinstance(schema, dict) else None
            if not isinstance(paths, dict):
                raise PartdbError("Part-DB schema response has an unsupported shape")
            print(json.dumps({"parts_endpoint": "/api/parts" in paths, "schema_ready": True}, sort_keys=True))
            return 0
        if args.command == "search":
            result = request(base_url, token, "/api/parts", {"name": args.query, "itemsPerPage": str(args.limit)})
            members = result.get("hydra:member", result.get("member")) if isinstance(result, dict) else None
            if not isinstance(members, list):
                raise PartdbError("Part-DB parts response has an unsupported shape")
            print(json.dumps({"matches": [{"id": item.get("id"), "name": item.get("name")} for item in members if isinstance(item, dict)]}, sort_keys=True))
            return 0
        part = request(base_url, token, f"/api/parts/{args.part_id}")
        lots = part.get("partLots") if isinstance(part, dict) else None
        if not isinstance(lots, list):
            raise PartdbError("Part-DB part response has an unsupported lot shape")
        output = []
        for lot in lots:
            if isinstance(lot, dict):
                location = lot.get("storageLocation")
                output.append({"amount": lot.get("amount"), "location": location.get("name") if isinstance(location, dict) else None})
        print(json.dumps({"locations": output, "part_id": args.part_id}, sort_keys=True))
    except PartdbError as exc:
        fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
