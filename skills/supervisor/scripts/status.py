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
from capacity_retry import load_state, retry_status


def snapshot(ledger: Path, capacity_state: Path | None = None) -> list[dict]:
    state = load_state(capacity_state) if capacity_state else None
    results = []
    for entry in load_ledger(ledger):
        try:
            result = session_status(entry)
            result["idle_seconds"] = max(0, int(time.time() - result["mtime"]))
            if state is not None:
                result["capacity_retry"] = retry_status(entry, result, state)
            results.append(result)
        except (OSError, ValueError, TypeError) as error:
            results.append({"session_id": entry["session_id"], "error": str(error)})
    return results


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
