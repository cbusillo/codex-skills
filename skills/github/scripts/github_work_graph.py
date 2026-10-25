# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import json
import re
import urllib.parse
from collections.abc import Callable
from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from typing import Any

import github_direction_next
import github_next_reads
import github_plan_graph
import github_read
from github_plan_graph import rest_author as graphql_author
from github_plan_graph import rest_comment as graphql_comment
from github_plan_graph import rest_related_issue as graphql_relationship

CHILD_LIMIT = 50
TRACK_COMMENTS = 100
CHILD_COMMENTS = 100
LABEL_LIMIT = 50
BLOCKER_LIMIT = 100
PR_LIMIT = 5
PR_COMMENTS = 30
TRACKS_PER_QUERY = 10

LIST_QUERY = """query WorkGraphTrackList($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: [OPEN], first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      nodes { number title }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

COMMENT = "url createdAt updatedAt databaseId body author { __typename login }"
BLOCKERS = f"""blockedBy(first: {BLOCKER_LIMIT}) {{ totalCount nodes {{
    number state url repository {{ nameWithOwner }} }} }}"""
TRACK_FIELDS = f"""number title url state stateReason body createdAt lastEditedAt
      author {{ __typename login }}
      {BLOCKERS}
      milestone {{ title }}
      labels(first: {LABEL_LIMIT}) {{ totalCount nodes {{ name }} }}
      comments(last: {TRACK_COMMENTS}) {{ totalCount nodes {{ {COMMENT} }} }}
      subIssues(first: {CHILD_LIMIT}) {{
        totalCount
        nodes {{
          number title url state stateReason body createdAt lastEditedAt
          author {{ __typename login }}
          {BLOCKERS}
          repository {{ nameWithOwner }}
          labels(first: {LABEL_LIMIT}) {{ totalCount nodes {{ name }} }}
          subIssuesSummary {{ total completed }}
          comments(last: {CHILD_COMMENTS}) {{ totalCount nodes {{ {COMMENT} }} }}
          closedByPullRequestsReferences(first: {PR_LIMIT}, includeClosedPrs: false) {{
            totalCount
            nodes {{
              number url state isDraft mergeable headRefOid
              comments(last: {PR_COMMENTS}) {{ totalCount nodes {{ {COMMENT} }} }}
            }}
          }}
        }}
      }}"""


def detail_query(numbers: list[int]) -> str:
    aliases = "\n".join(f"    t{number}: issue(number: {int(number)}) {{ {TRACK_FIELDS} }}" for number in numbers)
    return (
        "query WorkGraphTracks($owner: String!, $name: String!) {\n"
        f"  repository(owner: $owner, name: $name) {{\n{aliases}\n  }}\n}}"
    )


def discover_tracks(reader: github_read.GitHubReader, owner: str, name: str, max_pages: int) -> list[dict[str, Any]]:
    found, cursor, expected, seen, cursors = [], None, None, set(), set()
    for page in range(max_pages):
        result = reader.graphql_json(
            LIST_QUERY, {"owner": owner, "name": name, "cursor": cursor}, step=f"work_graph_list_{page + 1}"
        )
        if not result.ok:
            raise github_read.GitHubReadError("Track discovery read failed", result=result, diagnostics=reader.diagnostics())
        body = result.body
        if not isinstance(body, dict) or body.get("errors"):
            raise ValueError("Track discovery returned errors or invalid data")
        issues = body["data"]["repository"]["issues"]
        if expected is not None and issues["totalCount"] != expected:
            raise ValueError("open issue inventory changed during pagination; rebuild again")
        expected = issues["totalCount"]
        for node in issues["nodes"]:
            if node["number"] not in seen:
                seen.add(node["number"])
                if str(node["title"]).startswith("Track:"):
                    found.append({"number": node["number"], "title": node["title"]})
        if not issues["pageInfo"]["hasNextPage"]:
            if len(seen) != expected:
                raise ValueError("open issue inventory incomplete")
            return found
        cursor = issues["pageInfo"]["endCursor"]
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("pagination cursor did not advance")
        cursors.add(cursor)
    raise ValueError("open issue pagination exceeded page bound")


def read_tracks(
    reader: github_read.GitHubReader,
    owner: str,
    name: str = "direction",
    *,
    max_pages: int = 10,
) -> dict[str, Any]:
    tracks = discover_tracks(reader, owner, name, max_pages)
    nodes: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for start in range(0, len(tracks), TRACKS_PER_QUERY):
        numbers = [track["number"] for track in tracks[start : start + TRACKS_PER_QUERY]]
        result = reader.graphql_json(
            detail_query(numbers), {"owner": owner, "name": name}, step=f"work_graph_tracks_{start // TRACKS_PER_QUERY + 1}"
        )
        data = result.body.get("data") if result.ok and isinstance(result.body, dict) else None
        repository = (data or {}).get("repository") or {}
        if not result.ok or not isinstance(result.body, dict) or result.body.get("errors"):
            errors.append(f"Track read failed for #{', #'.join(map(str, numbers))}")
        for number in numbers:
            node = repository.get(f"t{number}")
            if isinstance(node, dict) and node.get("state") == "OPEN":
                nodes[number] = node
    return {"tracks": tracks, "nodes": nodes, "errors": errors}


@dataclass
class BatchRead:
    rows: list[dict[str, Any]]
    unread: dict[tuple[str, int], Exception]


def read_issues(
    reader: github_read.GitHubReader,
    refs: list[dict[str, Any]],
) -> BatchRead:
    fields = (
        github_plan_graph.ISSUE_FIELDS
        + " closedAt lastEditedAt"
        + " "
        + " ".join(github_plan_graph.connection_query(field) for field in github_plan_graph.CONNECTIONS)
    )
    rows: list[dict[str, Any]] = []
    unread: dict[tuple[str, int], Exception] = {}
    for start in range(0, len(refs), TRACKS_PER_QUERY):
        chunk = refs[start : start + TRACKS_PER_QUERY]
        selections = []
        for index, ref in enumerate(chunk):
            owner, name = ref["repo"].split("/", 1)
            selections.append(
                f"r{index}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) "
                f"{{ issue(number:{int(ref['number'])}) {{ {fields} }} }}"
            )
        result = reader.graphql_json(
            "query { " + " ".join(selections) + " rateLimit { cost remaining resetAt } }",
            {},
            step=f"work_graph_nodes_{start // TRACKS_PER_QUERY + 1}",
        )
        if not result.ok:
            raise github_read.GitHubReadError("Work graph batch unavailable", result=result, diagnostics=reader.diagnostics())
        body = result.body
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise github_read.GitHubReadShapeError("Work graph batch data unavailable")
        data = body["data"]
        errors = body.get("errors") or []
        for index, ref in enumerate(chunk):
            try:
                if any(
                    not isinstance(error.get("path"), list) or not error["path"] or error["path"][0] == f"r{index}"
                    for error in errors
                ):
                    raise github_read.GitHubReadShapeError("Work graph issue returned partial data")
                repository = data.get(f"r{index}")
                node = repository.get("issue") if isinstance(repository, dict) else None
                if not isinstance(node, dict):
                    raise github_read.GitHubReadShapeError(f"Work graph issue unavailable: {ref['repo']}#{ref['number']}")
                connections = {field: github_plan_graph.connection_page(node.get(field)) for field in github_plan_graph.CONNECTIONS}
                source = {**node, **{field: values[0] for field, values in connections.items()}}
                row = github_plan_graph.rest_issue(source)
                row["comments"] = connections["comments"][2]
                row["closed_at"] = node.get("closedAt")
                row["last_edited_at"] = node.get("lastEditedAt")
                row["_graph_comments_complete"] = (
                    connections["comments"][1] is None and len(connections["comments"][0]) == connections["comments"][2]
                )
                row["_graph_labels_complete"] = (
                    connections["labels"][1] is None and len(connections["labels"][0]) == connections["labels"][2]
                )
                row["_graph_truncated"] = [
                    key
                    for key, field in (("blocked_by", "blockedBy"), ("blocking", "blocking"), ("sub_issues", "subIssues"))
                    if connections[field][1] is not None or len(connections[field][0]) != connections[field][2]
                ]
                if (row["repo"].casefold(), row["number"]) != (ref["repo"].casefold(), ref["number"]):
                    raise github_read.GitHubReadShapeError("Work graph issue identity changed")
                rows.append(row)
            except (github_read.GitHubReadShapeError, KeyError, TypeError, ValueError, AttributeError) as exc:
                error = github_read.GitHubReadShapeError(str(exc))
                error.__cause__ = exc
                unread[(ref["repo"].casefold(), ref["number"])] = error
    return BatchRead(rows, unread)


def issue_path(repo: str, number: int) -> str:
    return f"/repos/{repo.casefold()}/issues/{number}"


def complete_issue_pages(row: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    base = issue_path(row["repo"], row["number"])
    collections = {
        field: values
        for field, values in row.get("_graph_relationships", {}).items()
        if field not in row.get("_graph_truncated", [])
    }
    if row.get("_graph_comments_complete"):
        collections["comments"] = row["_graph_comments"]
    pages: dict[str, list[dict[str, Any]]] = {}
    for field, values in collections.items():
        endpoint = f"dependencies/{field}" if field in {"blocked_by", "blocking"} else field
        for offset in range(0, len(values) + 1, 100):
            pages[f"{base}/{endpoint}?per_page=100&page={offset // 100 + 1}"] = copy.deepcopy(values[offset : offset + 100])
    return pages


def publish_issue_pages(reads: github_next_reads.NextReads, row: dict[str, Any], actor: str | None) -> None:
    for path, body in complete_issue_pages(row).items():
        with reads.lock:
            if path in reads.observations:
                continue
            future: Future[Any] = Future()
            reads.observations[path] = future
        future.set_result((actor, body))


def prefetch_issues(
    reader_for: Callable[[str], github_read.GitHubReader | None],
    refs: list[dict[str, Any]],
    reads: github_next_reads.NextReads,
) -> None:
    missing = {
        issue_path(ref["repo"], ref["number"]): ref
        for ref in refs
        if reads.observation(issue_path(ref["repo"], ref["number"])) is None
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for ref in list(missing.values())[:TRACKS_PER_QUERY]:
        groups.setdefault(ref["repo"].split("/", 1)[0].casefold(), []).append(ref)
    for chunk in groups.values():
        reader = reader_for(chunk[0]["repo"])
        if reader is None:
            continue
        owned: dict[str, tuple[dict[str, Any], Future[Any]]] = {}
        try:
            with reads.lock:
                for ref in chunk:
                    path = issue_path(ref["repo"], ref["number"])
                    if path not in reads.observations:
                        future: Future[Any] = Future()
                        reads.observations[path] = future
                        owned[path] = ref, future
            if not owned:
                continue

            def fetch(
                reader: github_read.GitHubReader = reader, owned: dict[str, tuple[dict[str, Any], Future[Any]]] = owned
            ) -> None:
                try:
                    batch = read_issues(reader, [ref for ref, _future in owned.values()])
                    values = {issue_path(row["repo"], row["number"]): row for row in batch.rows}
                    for path, (ref, future) in owned.items():
                        if path in values:
                            row = values[path]
                            publish_issue_pages(reads, row, reader.actor)
                            future.set_result((reader.actor, row))
                        else:
                            key = ref["repo"].casefold(), ref["number"]
                            future.set_exception(batch.unread.get(key, github_read.GitHubReadShapeError("Work graph issue missing")))
                except BaseException as exc:
                    for _ref, future in owned.values():
                        if not future.done():
                            future.set_exception(exc)
                    raise

            def settle_cancelled(job: Future[Any], owned: dict[str, tuple[dict[str, Any], Future[Any]]] = owned) -> None:
                if job.cancelled() or isinstance(job.exception(), CancelledError):
                    for _ref, future in owned.values():
                        if not future.done():
                            future.set_exception(CancelledError())

            job = reads.submit(("work_graph_batch", *owned), fetch)
            job.add_done_callback(settle_cancelled)
        except BaseException as exc:
            for _ref, future in owned.values():
                if not future.done():
                    future.set_exception(exc)
            raise


def read_discussion(
    issue: dict[str, Any],
    read_comments: Callable[[int], tuple[list[dict[str, Any]], bool]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    comments = issue.get("_graph_comments")
    complete = issue.get("_graph_comments_complete", True)
    if comments is None or (not complete and limit > len(comments)):
        if type(issue.get("comments")) is int and issue["comments"] == 0:
            comments, complete = [], True
        else:
            comments, complete = read_comments(limit + 1)
    return copy.deepcopy(comments[:limit]), complete and len(comments) <= limit


def read_direction_node(
    issue: dict[str, Any],
    *,
    config: dict[str, Any],
    focus: str | None,
    comment_limit: int,
    read_comments: Callable[[int], tuple[list[dict[str, Any]], bool]],
    read_relationships: Callable[[], tuple[dict[str, Any], list[str]]],
    resolve_wait: Callable[[list[dict[str, Any]]], dict[str, Any] | None],
) -> dict[str, Any]:
    base = {
        **github_direction_next.compact_list_issue(issue["repo"], issue),
        "plan_status": github_direction_next.next_plan_status(issue, config),
        "focus": focus,
        "milestone": github_direction_next.next_milestone_context(issue),
    }
    if issue.get("pull_request") is not None:
        return {"item": {**base, "exclusion": "pull_request"}}
    if issue.get("_graph_labels_complete") is False:
        return {"item": {**base, "exclusion": "unknown_dependencies", "detail": "Work graph status labels are incomplete"}}
    comments, complete = read_discussion(issue, read_comments, limit=comment_limit)
    try:
        wait = resolve_wait(comments) if complete else None
    except (ValueError, KeyError, TypeError):
        wait = None
    if wait is None:
        wait = {"version": 1, "state": "review", "evidence": "", "waits": []}
    static = github_direction_next.next_static_exclusion(issue, config=config, focus=focus)
    if static:
        node = {"item": static}
    else:
        relationships, truncated = read_relationships()
        node = github_direction_next.evaluate_direction_node(
            issue, config=config, focus=focus, relationships=relationships, truncated_relationships=truncated, structured_wait=wait
        )
    node["item"]["discussion"] = github_direction_next.discussion_snapshot(issue, comments, complete=complete)
    return node


def issue_repository_name(issue: dict[str, Any]) -> str | None:
    repo = issue.get("repo")
    if isinstance(repo, str) and re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        return repo
    repository = issue.get("repository")
    if isinstance(repository, str) and re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        return repository
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and re.fullmatch(r"[^/\s]+/[^/\s]+", full_name):
            return full_name
    for key in ("repository_url", "html_url", "url"):
        value = issue.get(key)
        if not isinstance(value, str):
            continue
        parts = [part for part in urllib.parse.urlparse(value).path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "repos":
            candidate = f"{parts[1]}/{parts[2]}"
            if re.fullmatch(r"[^/\s]+/[^/\s]+", candidate):
                return candidate
        if len(parts) >= 4 and parts[-2] == "issues":
            candidate = f"{parts[-4]}/{parts[-3]}"
            if re.fullmatch(r"[^/\s]+/[^/\s]+", candidate):
                return candidate
    return None


def compact_relationship_issue(issue: dict[str, Any], relationship: str) -> dict[str, Any]:
    repo = issue_repository_name(issue)
    number = issue.get("number")
    state = issue.get("state")
    if repo is None:
        raise ValueError(f"GitHub {relationship} response omitted the related issue repository")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError(f"GitHub {relationship} response omitted a valid related issue number")
    if not isinstance(state, str) or state.casefold() not in {"open", "closed"}:
        raise ValueError(f"GitHub {relationship} response returned an ambiguous related issue state")
    title = issue.get("title")
    return {
        "repo": repo,
        "number": number,
        "title": title if isinstance(title, str) else None,
        "state": state.casefold(),
        "url": issue.get("html_url") or f"https://github.com/{repo}/issues/{number}",
    }


def read_relationship_rows(
    repo: str,
    number: int,
    issue: dict[str, Any],
    read: Callable[[str, int], tuple[list[dict[str, Any]], bool]],
    *,
    limit: int = 100,
    fields: tuple[str, ...] = ("blocked_by", "blocking", "sub_issues"),
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    relationships: dict[str, list[dict[str, Any]]] = {}
    truncated: list[str] = []
    for field in fields:
        if "_graph_relationships" in issue:
            rows = issue["_graph_relationships"][field]
            cut = field in issue.get("_graph_truncated", []) or len(rows) > limit
        else:
            summary_key, total_key, endpoint = {
                "blocked_by": ("issue_dependencies_summary", "total_blocked_by", "dependencies/blocked_by"),
                "blocking": ("issue_dependencies_summary", "total_blocking", "dependencies/blocking"),
                "sub_issues": ("sub_issues_summary", "total", "sub_issues"),
            }[field]
            summary = issue.get(summary_key)
            total = summary.get(total_key) if isinstance(summary, dict) else None
            if type(total) is int and total == 0:
                relationships[field] = []
                continue
            rows, cut = read(f"/repos/{repo}/issues/{number}/{endpoint}", limit)
        relationships[field] = rows[:limit]
        if cut:
            truncated.append(field)
    return relationships, truncated


def read_relationships(
    repo: str, number: int, issue: dict[str, Any],
    read: Callable[[str, int], tuple[list[dict[str, Any]], bool]], *, limit: int = 100,
    fields: tuple[str, ...] = ("blocked_by", "blocking", "sub_issues"),
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    rows, truncated = read_relationship_rows(repo, number, issue, read, limit=limit, fields=fields)
    return {field: [compact_relationship_issue(row, field) for row in values]
            for field, values in rows.items()}, truncated


@dataclass
class NativeGraphRead:
    nodes: dict[tuple[str, int], dict[str, Any]]
    unavailable: list[str]
    truncated: bool


def read_native_graph(
    roots: list[dict[str, Any]], read: Callable[[str, int], tuple[list[dict[str, Any]], bool]], *,
    max_nodes: int = 500, relationship_limit: int = 2000,
) -> NativeGraphRead:
    records = {(row["repo"].casefold(), int(row["number"])): row for row in roots}
    unavailable: list[str] = []

    def read_edges(repo: str, number: int) -> list[tuple[str, int]]:
        related: list[tuple[str, int]] = []
        for field, endpoint in (("blocked_by", "dependencies/blocked_by"), ("sub_issues", "sub_issues")):
            source = f"{repo}#{number}:{endpoint}"
            try:
                fields, truncated = read_relationship_rows(repo, number, {}, read,
                    limit=relationship_limit, fields=(field,))
                if truncated:
                    unavailable.append(source)
                for row in fields[field]:
                    key = github_plan_graph.native_issue_key(row)
                    records.setdefault(key, row)
                    related.append(key)
            except ValueError:
                unavailable.append(source)
        return related

    graph = github_plan_graph.walk_native_issues(list(records), read_edges=read_edges, max_nodes=max_nodes)
    return NativeGraphRead({key: records[key] for key in graph.nodes}, sorted(set(unavailable)), not graph.complete)


def cached_reads(fetch: Callable[[list[str]], Any]) -> Callable[[list[str]], Any]:
    cache: dict[tuple[str, ...], Any] = {}

    def read(args: list[str]) -> Any:
        normalized = list(args)
        if len(normalized) >= 2 and normalized[0] == "api":
            path = github_next_reads.observation_key("/" + normalized[1].lstrip("/"))
            parsed = urllib.parse.urlsplit(path)
            query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True),
                                                  key=lambda pair: pair[0]))
            normalized[1] = urllib.parse.urlunsplit(parsed._replace(query=query)).lstrip("/")
            for index in range(2, len(normalized) - 1):
                if normalized[index] in {"--method", "-X"}:
                    normalized[index:index + 2] = ["--method", normalized[index + 1].upper()]
            if len(normalized) == 2 and normalized[1] != "graphql":
                normalized.extend(["--method", "GET"])
        key = tuple(normalized)
        if key not in cache:
            try:
                cache[key] = fetch(args)
            except Exception as exc:
                cache[key] = exc
        value = cache[key]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    return read


def repository_inventory(read: Callable[..., Any], *, installed: bool, limit: int,
                         affiliation: str = "owner,collaborator,organization_member") -> Any:
    return read("/installation/repositories" if installed else "/user/repos",
        query={} if installed else {"affiliation": affiliation},
        collection_key="repositories" if installed else None, bucket="rest_core",
        step_prefix="next_repository_inventory", limit=limit)


def issue_inventory(read: Callable[..., Any], repo: str, *, query: dict[str, Any], limit: int,
                    step_prefix: str, issue_only: bool = False) -> Any:
    return read(f"/repos/{repo}/issues", query=query, bucket="rest_core", step_prefix=step_prefix,
        limit=limit, issue_only=issue_only)


def ownership_inventory(read: Callable[..., Any], repo: str, *, limit: int) -> dict[str, Any]:
    _, pulls = read(f"/repos/{repo}/pulls", query={"state": "open", "sort": "created", "direction": "asc"},
        bucket="rest_core", step_prefix="next_queue_pulls", limit=limit + 1)
    _, branches = read(f"/repos/{repo}/branches", query={}, bucket="rest_core",
        step_prefix="next_queue_branches", limit=limit + 1)
    complete = len(pulls) <= limit and len(branches) <= limit
    return {"pulls": pulls[:limit], "complete": complete,
        "inventory": {"remote_branches": [branch["name"] for branch in branches[:limit]],
                      "local_branches": [], "worktrees": []},
        "coverage": {"pull_count": min(len(pulls), limit), "branch_count": min(len(branches), limit),
                     "truncated": not complete, "local_artifacts": "not_read"}}
