#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Conditional-cache adapter for plain JSON gh api GETs after wrapper auth."""
from __future__ import annotations

import json
import sys
import urllib.parse

import github_api
import github_http_cache


def endpoint(args):
    """Decline CLI formatting, pagination, input and header options unchanged."""
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
    # Credential and log surfaces retain the original uncached route.
    if any(part in url.path.split("/") for part in ("secrets", "tokens", "logs")):
        return None
    return path


def read(args, *, gh_cmd, actor, host):
    path = endpoint(args)
    if path is None:
        return None
    def transport(headers):
        return github_api._call_gh_uncached(
            "GET", path, gh_cmd=gh_cmd, gh_prefix_args=[], extra_headers=headers,
            operation="github.raw.read", actor=actor, expected_actor=actor, host=host,
            is_write=False,
        )
    return github_http_cache.request(
        transport, method="GET", path=path, body=None, headers={},
        api_version=github_api.DEFAULT_API_VERSION, host=host,
        actor=actor, expected_actor=actor, identity_scope=json.dumps([gh_cmd, actor]), enabled=True,
    )


def main():
    gh_cmd, actor, host, *args = sys.argv[1:]
    result = read(args, gh_cmd=gh_cmd, actor=actor, host=host)
    if result is None:
        return 125  # Unsupported; the shell wrapper runs its original command.
    if not result.ok:
        return github_api.emit_terminal(github_api.terminal_failure(
            result.failure, operation="github.raw.read", actor=result.actor,
            expected_actor=result.expected_actor, host=host, bucket=result.bucket,
            exit_code=1,
        ))
    if result.body is not None:
        print(result.body if isinstance(result.body, str) else json.dumps(result.body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
