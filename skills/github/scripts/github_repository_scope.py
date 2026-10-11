#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared archived-repository scope for GitHub search and discovery."""

import argparse
import re
from collections.abc import Callable, Iterable
from typing import Any


def add_include_archived_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-archived", action="store_true",
        help="Include archived repositories in search results and repository inventories.",
    )


def search_query(query: str, *, include_archived: bool = False) -> str:
    if include_archived:
        return query
    # Apply this at the provider so totals and pagination use the same scope.
    # Normalize unquoted archive qualifiers rather than supplying contradictory
    # values; quoted issue text remains literal. The opt-in keeps caller scope.
    parts = re.split(r'("(?:\\.|[^"\\])*")', query)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r'(?<![\w:-])-?archived:(?:true|false)\b',
                              'archived:false', parts[index], flags=re.IGNORECASE)
    scoped = "".join(parts)
    return f"{scoped} archived:false".strip()


def includes_repository(metadata: dict[str, Any], *, include_archived: bool = False) -> bool:
    # REST calls use archived; gh repo list/view uses isArchived. Unknown state
    # remains the caller's coverage concern, rather than being called archived.
    archived = metadata.get("archived", metadata.get("isArchived"))
    return include_archived or archived is not True


def filter_named_repositories(
    names: Iterable[str], read_metadata: Callable[[str], dict[str, Any]], *, include_archived: bool = False,
) -> list[str]:
    if include_archived:
        return list(names)
    return [name for name in names if includes_repository(read_metadata(name))]
