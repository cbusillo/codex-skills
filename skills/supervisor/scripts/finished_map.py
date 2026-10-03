#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Map explicit sessions to close-out candidates, never to automatic exit authority."""

import argparse
import json
from pathlib import Path

from session_record import load_ledger, session_status


def candidates(ledger: Path) -> list[dict]:
    results = []
    for entry in load_ledger(ledger):
        try:
            status = session_status(entry)
            status["candidate"] = (
                entry.get("supervisor_owned") is True and status["safe_verdict"]
            )
            status["handoff_url"] = entry.get("handoff_url")
            status["needs_verification"] = [
                "issue handoff",
                "process and tab identity",
                "input line",
                "pending prompt",
            ]
            results.append(status)
        except (OSError, ValueError, TypeError) as error:
            results.append(
                {
                    "session_id": entry["session_id"],
                    "candidate": False,
                    "error": str(error),
                }
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args()
    results = candidates(args.ledger)
    print(json.dumps(results, indent=2))
    return int(any("error" in result for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
