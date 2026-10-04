#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prove a PEP 723 branch contains only automation and clean base merges."""

from __future__ import annotations

import argparse
import subprocess


AUTOMATION_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
AUTOMATION_TRAILER = "PEP723-Automation: true"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def check_branch(tip: str, base: str) -> None:
    """Reject edits, non-base merges, and merges with custom resolutions."""
    current = git("rev-parse", "--verify", f"{tip}^{{commit}}")
    base = git("rev-parse", "--verify", f"{base}^{{commit}}")
    while True:
        parents = git("show", "-s", "--format=%P", current).split()
        if len(parents) > 2:
            raise ValueError("branch contains an unsupported multi-parent merge")
        if len(parents) < 2:
            if (
                git("show", "-s", "--format=%ae", current) == AUTOMATION_EMAIL
                and AUTOMATION_TRAILER in git("show", "-s", "--format=%B", current).splitlines()
            ):
                return
            raise ValueError("branch contains a commit without the automation identity")
        first, second = parents
        ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", second, base])
        if ancestry.returncode == 1:
            raise ValueError("merge's second parent is outside base history")
        ancestry.check_returncode()
        # Exit status rejects conflicts; comparing trees rejects extra edits even
        # when the merge's author or message happens to look like automation.
        try:
            merged_tree = git("merge-tree", "--write-tree", first, second).splitlines()[0]
        except subprocess.CalledProcessError as exc:
            raise ValueError("merge cannot be reproduced without conflicts") from exc
        if merged_tree != git("rev-parse", f"{current}^{{tree}}"):
            raise ValueError("merge contains changes beyond a clean base merge")
        current = first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tip")
    parser.add_argument("base")
    args = parser.parse_args()
    try:
        check_branch(args.tip, args.base)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Refusing to replace automation branch: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
