#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Conditional-cache adapter for plain JSON gh api GETs after wrapper auth."""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
from typing import Callable

import github_api
import github_http_cache


def endpoint(args: list[str]) -> str | None:
    """Decline CLI formatting, pagination, input and download options unchanged."""
    if args[:1] != ["api"]:
        return None
    operands = []
    index = 1
    while index < len(args):
        arg = args[index]
        index += 1
        if arg in {"--method", "-X"}:
            if index >= len(args) or args[index] != "GET":
                return None
            index += 1
        elif arg.startswith("-"):
            return None
        else:
            operands.append(arg)
    if len(operands) != 1:
        return None
    path = operands[0]
    url = urllib.parse.urlsplit(path)
    if url.scheme or url.netloc or url.fragment or not github_http_cache.repository_from_path(path):
        return None
    if any(part in urllib.parse.unquote(url.path).split("/") for part in
           ("secrets", "tokens", "logs", "tarball", "zipball", "zip", "download")):
        return None
    return path


def response_body(raw: bytes) -> bytes:
    """Strip gh's include headers without decoding or changing body bytes."""
    while raw.startswith(b"HTTP/"):
        separators = [(raw.find(value), value) for value in (b"\r\n\r\n", b"\n\n") if value in raw]
        if not separators:
            return raw
        index, separator = min(separators)
        header, body = raw[:index], raw[index + len(separator):]
        parts = header.splitlines()[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if 100 <= status < 400 and body.lstrip(b"\r\n").startswith(b"HTTP/"):
            raw = body.lstrip(b"\r\n")
        else:
            return body
    return raw


def read(args: list[str], *, gh_cmd: str, actor: str, host: str,
         observer: Callable[[subprocess.CompletedProcess], None] | None = None) -> github_api.ApiResult | None:
    path = endpoint(args)
    if path is None:
        return None
    # The actual gh executable retains the wrapper's selected App credentials.
    return github_api.call_gh(
        "GET", path, gh_cmd=gh_cmd, gh_prefix_args=[], operation="github.raw.read",
        actor=actor, expected_actor=actor, host=host, response_observer=observer,
        cache_body_filter=lambda body: isinstance(body, (dict, list)),
    )


def main() -> int:
    gh_cmd, actor, host, *args = sys.argv[1:]
    replies = []
    result = read(args, gh_cmd=gh_cmd, actor=actor, host=host, observer=replies.append)
    if result is None:
        return 125  # Unsupported; the shell wrapper runs its original command.
    if replies:
        reply = replies[-1]
        if result.status != 304:
            # Preserve failures, non-JSON bodies and JSON bytes from gh exactly.
            sys.stdout.buffer.write(response_body(reply.stdout))
            sys.stderr.buffer.write(reply.stderr)
            if not result.ok and not reply.stderr and result.failure:
                print(f"gh: {result.failure.message} (HTTP {result.status})", file=sys.stderr)
            return reply.returncode if reply.returncode else int(not result.ok)
    if not result.ok:
        message = result.failure.message if result.failure else "GitHub read failed"
        print(f"gh: {message} (HTTP {result.status})", file=sys.stderr)
        return 1
    if result.body is not None:
        print(json.dumps(result.body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
