#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Thin CLI for the portable private ordinary-agent client."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from launchplane_ordinary_agent_client import (
    OrdinaryAgentClient,
    OrdinaryAgentClientError,
    PrivateStateStore,
)


def _json_file(path: str) -> dict[str, object]:
    candidate = Path(path).expanduser()
    try:
        info = candidate.lstat()
        if (
            info.st_uid != os.getuid()
            or not candidate.is_file()
            or candidate.is_symlink()
            or info.st_mode & 0o077
        ):
            raise OrdinaryAgentClientError("unsafe_input_file")
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise OrdinaryAgentClientError("invalid_input_file") from None
    if not isinstance(value, dict):
        raise OrdinaryAgentClientError("invalid_input_file")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Use a privately claimed Launchplane ordinary-agent credential."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Launchplane service URL (HTTPS, or loopback HTTP for local use).",
    )
    parser.add_argument(
        "--state-dir",
        help="Private state directory; defaults to an OS-appropriate home state directory.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "command",
        choices=(
            "enroll-propose",
            "enroll-status",
            "claim",
            "session-propose",
            "session-status",
            "session-cancel",
            "job-admit",
            "job-status",
        ),
    )
    parser.add_argument(
        "--input-file",
        help="Private input file for nonsecret enrollment/session/job intent.",
    )
    parser.add_argument(
        "--alias", help="Named private enrollment, session, or job record."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        client = OrdinaryAgentClient(
            args.url,
            state=None if args.state_dir is None else PrivateStateStore(args.state_dir),
            timeout=args.timeout,
        )
        if args.command == "enroll-propose":
            result = client.propose_enrollment(
                None if not args.input_file else _json_file(args.input_file),
                alias=args.alias,
            )
        elif args.command == "enroll-status":
            result = client.enrollment_status(alias=args.alias)
        elif args.command == "claim":
            result = client.claim(alias=args.alias)
        elif args.command == "session-propose":
            result = client.propose_session(
                None if not args.input_file else _json_file(args.input_file),
                alias=args.alias,
            )
        elif args.command == "session-status":
            result = client.session_status(alias=args.alias)
        elif args.command == "session-cancel":
            result = client.cancel_session(alias=args.alias)
        elif args.command == "job-admit":
            result = client.admit_job(
                None if not args.input_file else _json_file(args.input_file),
                alias=args.alias,
            )
        else:
            result = client.job_status(alias=args.alias)
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except OrdinaryAgentClientError as exc:
        detail: dict[str, object] = {"code": exc.code}
        if exc.retry_after_seconds is not None:
            detail["retry_after_seconds"] = exc.retry_after_seconds
        if exc.trace_id:
            detail["trace_id"] = exc.trace_id
        print(
            json.dumps({"status": "error", "error": detail}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
