#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Report transcript activity and context for the ledger's explicit sessions."""

import argparse
import json
import time
from pathlib import Path

from session_record import load_ledger, session_status


def snapshot(ledger: Path) -> list[dict]:
    results = []
    for entry in load_ledger(ledger):
        try:
            result = session_status(entry)
            result["idle_seconds"] = max(0, int(time.time() - result["mtime"]))
            results.append(result)
        except (OSError, ValueError, TypeError) as error:
            results.append({"session_id": entry["session_id"], "error": str(error)})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    results = snapshot(args.ledger)
    print(json.dumps(results, indent=2))
    return int(any("error" in result for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
