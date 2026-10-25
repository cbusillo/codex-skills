#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared archived-repository scope for GitHub search and discovery."""

import argparse
import re
import shlex
import sys
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def add_include_archived_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Include archived repositories in search results and repository inventories.",
    )


def search_query(query: str, *, include_archived: bool = False, preserve_archive_qualifiers: bool = False) -> str:
    if include_archived:
        return query
    # Apply this at the provider so totals and pagination use the same scope.
    # Normalize unquoted archive qualifiers rather than supplying contradictory
    # values; quoted issue text remains literal. The opt-in keeps caller scope.
    parts = re.split(r'("(?:\\.|[^"\\])*")', query)
    if preserve_archive_qualifiers and any(
        re.search(r"(?<![\w:-])-?archived:(?:true|false)\b", part, re.IGNORECASE) for part in parts[::2]
    ):
        return query
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r"(?<![\w:-])-?archived:(?:true|false)\b", "archived:false", parts[index], flags=re.IGNORECASE)
    scoped = "".join(parts)
    return f"{scoped} archived:false".strip()


def includes_repository(metadata: dict[str, Any], *, include_archived: bool = False) -> bool:
    # REST calls use archived; gh repo list/view uses isArchived. Unknown state
    # remains the caller's coverage concern, rather than being called archived.
    archived = metadata.get("archived", metadata.get("isArchived"))
    return include_archived or archived is not True


def filter_named_repositories(
    names: Iterable[str],
    read_metadata: Callable[[str], dict[str, Any]],
    *,
    include_archived: bool = False,
) -> list[str]:
    if include_archived:
        return list(names)
    return [name for name in names if includes_repository(read_metadata(name))]


def scope_cli_search(args: list[str]) -> list[str]:
    query = []
    scoped = []
    include_archived = False
    index = 0
    boolean_flags = {"--archived", "--draft", "--locked", "--merged", "--web", "-w", "--help", "-h"}
    while index < len(args):
        argument = args[index]
        index += 1
        if argument == "--":
            scoped.extend([argument, *args[index:]])
            query.extend(args[index:])
            break
        if argument == "--include-archived":
            include_archived = True
            continue
        scoped.append(argument)
        if argument.startswith("--archived"):
            include_archived = True
        if argument.startswith("-"):
            if argument in boolean_flags or argument.startswith("--no-") or "=" in argument:
                continue
            if (argument.startswith("--") or len(argument) == 2) and index < len(args):
                scoped.append(args[index])
                index += 1
            continue
        query.append(argument)
    raw_query = " ".join(query)
    if search_query(raw_query, include_archived=include_archived, preserve_archive_qualifiers=True) != raw_query:
        scoped.append(search_query(""))
    return scoped


def scope_api_search(args: list[str], *, include_archived: bool) -> list[str]:
    from github_api import parse_gh_api_args

    parsed = parse_gh_api_args(args)
    if parsed is None or parsed.sent_method != "GET" or include_archived:
        return args
    endpoint = urlsplit(parsed.endpoint)
    if endpoint.path.strip("/") != "search/issues":
        return args
    fields = [value for value in parsed.fields if value.startswith("q=")]
    if fields:
        scoped = list(args)
        for index, argument in enumerate(args):
            field = re.fullmatch(r"(-i*[fF]=?|--(?:raw-)?field=)?(q=.*)", argument, re.DOTALL)
            if field is None or field[2] not in fields:
                continue
            if field[1] is None and (index == 0 or not re.fullmatch(r"-i*[fF]|--(?:raw-)?field", args[index - 1])):
                continue
            query = field[2][2:]
            scoped[index] = f"{field[1] or ''}q={search_query(query, preserve_archive_qualifiers=True)}"
        return scoped
    parameters: list[tuple[str, str]] = parse_qsl(endpoint.query, keep_blank_values=True)
    if not any(key == "q" for key, _ in parameters):
        return args
    scoped_parameters = [
        (key, search_query(value, preserve_archive_qualifiers=True) if key == "q" else value) for key, value in parameters
    ]
    if scoped_parameters == parameters:
        return args
    scoped_endpoint = urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, urlencode(scoped_parameters), endpoint.fragment))
    return [scoped_endpoint if argument == parsed.endpoint else argument for argument in args]


def scope_gh_arguments(args: list[str]) -> list[str]:
    include_archived = args[:1] == ["--include-archived"]
    if include_archived:
        args = args[1:]
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] == "--":
            index += 1
            break
        index += 2 if args[index] in {"-R", "--repo", "--hostname"} else 1
    if args[index : index + 2] in (["search", "issues"], ["search", "prs"]):
        prefix = ["--include-archived"] if include_archived else []
        return args[: index + 2] + scope_cli_search(prefix + args[index + 2 :])
    if args[index : index + 1] == ["api"]:
        return args[: index + 1] + scope_api_search(args[index + 1 :], include_archived=include_archived)
    return args


if __name__ == "__main__":
    print(shlex.join(scope_gh_arguments(sys.argv[1:])))
