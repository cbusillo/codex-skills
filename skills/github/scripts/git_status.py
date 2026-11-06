# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import NamedTuple


class Change(NamedTuple):
    status: str
    paths: tuple[str, ...]


def read_status(
    git: Callable[..., bytes | str],
    *,
    untracked: bool = True,
    ignored: bool = False,
    allow_review_scratch: bool = False,
) -> list[Change]:
    raw = git(
        "status", "--porcelain=v1", "-z", f"--untracked-files={'all' if untracked else 'no'}", *(["--ignored"] if ignored else [])
    )
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if raw and not raw.endswith("\0"):
        raise ValueError("Incomplete Git status")
    entries = iter(raw.split("\0")[:-1])
    changes = []
    for entry in entries:
        if len(entry) < 4 or entry[2] != " ":
            raise ValueError("Invalid Git status entry")
        status, path = entry[:2], entry[3:]
        paths = (path,)
        if "R" in status or "C" in status:
            original = next(entries, "")
            if not original:
                raise ValueError("Incomplete Git rename or copy")
            paths += (original,)
        if allow_review_scratch and status == "??" and re.fullmatch(r"\.model-review-[A-Za-z0-9_]+/.*", path):
            continue
        changes.append(Change(status, paths))
    return changes
