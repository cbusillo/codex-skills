#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compare or report Launchplane agent/operator contract freshness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from launchplane_contract_freshness import (
    DEFAULT_ATTEMPTS,
    DEFAULT_RETRY_DELAY_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    EVIDENCE_SCHEMA_VERSION,
    FreshnessError,
    evaluate_freshness,
    report_maintenance_issue,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare or report Launchplane contract semantic freshness."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    compare.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    compare.add_argument(
        "--retry-delay", type=float, default=DEFAULT_RETRY_DELAY_SECONDS
    )

    report = subparsers.add_parser("report")
    report.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    report.add_argument("--evidence-file", type=Path, required=True)
    return parser


def _error_payload(code: str) -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "classification": "unknown",
        "retryable": False,
        "error": {"code": code},
        "summary": {"advisory_only": True, "runtime_authority": False},
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "compare":
            payload = evaluate_freshness(
                timeout=args.timeout,
                attempts=args.attempts,
                retry_delay=args.retry_delay,
            )
        else:
            if not args.repository:
                raise FreshnessError("missing_reporting_repository")
            try:
                raw_evidence: Any = json.loads(
                    args.evidence_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise FreshnessError("invalid_freshness_evidence") from exc
            if not isinstance(raw_evidence, dict):
                raise FreshnessError("invalid_freshness_evidence")
            payload = report_maintenance_issue(raw_evidence, repository=args.repository)
    except FreshnessError as exc:
        print(json.dumps(_error_payload(exc.code), sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
