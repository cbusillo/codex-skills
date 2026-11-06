#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from pathlib import Path


def split_child_parents(review_dir: Path) -> set[str]:
    parents: set[str] = set()
    for prompt_path in review_dir.glob("batch-*-*.prompt.json"):
        stem = prompt_path.name.removesuffix(".prompt.json").removeprefix("batch-")
        parent, _sep, _suffix = stem.partition("-")
        if parent:
            parents.add(parent)
    return parents
