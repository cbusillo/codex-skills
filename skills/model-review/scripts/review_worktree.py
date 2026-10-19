# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared classification of untracked scratch owned by the model-review helper."""

import re


def is_review_scratch(name: str | bytes) -> bool:
    if isinstance(name, bytes):
        name = name.decode("utf-8", errors="surrogateescape")
    return re.fullmatch(r"\.model-review-[A-Za-z0-9_]+/.*", name) is not None
