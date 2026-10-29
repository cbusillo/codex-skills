#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Report transcript activity and context for the ledger's explicit sessions."""

import argparse
import json
from pathlib import Path

from compact_check import snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--capacity-state", type=Path)
    args = parser.parse_args()
    results = snapshot(args.ledger, args.capacity_state)
    print(json.dumps(results, indent=2))
    return int(any("error" in result for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
