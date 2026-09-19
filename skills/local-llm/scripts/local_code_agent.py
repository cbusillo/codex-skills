#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Retired Every Code entry point; intentionally performs no execution."""

import sys


def main() -> int:
    print(
        "error: local_code_agent.py is retired with Every Code. "
        "For deliberately local agent execution, use local_codex_agent.py with "
        "an explicit --host and --model or locally configured --role. "
        "Arguments are not forwarded.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
