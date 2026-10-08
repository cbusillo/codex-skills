#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline API-call fixture shared by the issue and comment regression suites."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import github_api


def with_call_stub(
    callback: Callable[..., github_api.ApiResult],
    test: Callable[[list[dict[str, Any]]], None],
    *,
    allow_retry: bool = False,
) -> None:
    calls: list[dict[str, Any]] = []

    def stub(method: str, path: str, body: Any = None, **kwargs: Any) -> github_api.ApiResult:
        calls.append({"method": method, "path": path, "body": body, "kwargs": kwargs})
        return callback(method, path, body, **kwargs)

    with tempfile.TemporaryDirectory() as temp_dir:
        def retry_policy() -> github_api.RetryPolicy:
            return github_api.RetryPolicy(
                max_wait_seconds=10.0,
                max_attempts=2 if allow_retry else 1,
                base_backoff_seconds=0.0,
                max_backoff_seconds=0.0,
                jitter_seconds=0.0,
                state_dir=Path(temp_dir),
            )

        with (
            patch.object(github_api, "call_gh", stub),
            patch.object(github_api, "default_retry_policy", retry_policy),
        ):
            test(calls)
