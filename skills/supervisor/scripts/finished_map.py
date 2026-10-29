#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Map explicit sessions to close-out candidates, never to automatic exit authority."""

import argparse
import json
from pathlib import Path

from compact_check import SessionAbsent, candidates, closeout_request

__all__ = ["SessionAbsent", "candidates", "closeout_request"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--session-id", help="verify the exact native session named by a turn-end notice")
    args = parser.parse_args()
    try:
        results = candidates(args.ledger, args.session_id)
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    print(json.dumps(results, indent=2))
    return int(any("error" in result for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
