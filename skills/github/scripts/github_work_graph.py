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
import github_repository_scope
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
      nodes { number title url state milestone { number title state createdAt dueOn url } }
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
                    found.append(node)
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
    for track in tracks:
        number = track["number"]
        result = reader.graphql_json(
            detail_query([number]), {"owner": owner, "name": name}, step=f"work_graph_track_{number}"
        )
        data = result.body.get("data") if result.ok and isinstance(result.body, dict) else None
        repository = (data or {}).get("repository") or {}
        if not result.ok or not isinstance(result.body, dict):
            reason = result.failure.message if result.failure else f"HTTP {result.status}: invalid or unavailable response"
            errors.append(f"Track read failed for #{number}: {reason}")
        elif result.body.get("errors"):
            for error in result.body["errors"]:
                path = ".".join(map(str, error.get("path") or [])) or "query"
                errors.append(f"Track read failed at {path}: {error.get('message', 'GraphQL error')}")
        node = repository.get(f"t{number}")
        if isinstance(node, dict) and node.get("state") == "OPEN":
            nodes[number] = node
    return {"tracks": tracks, "nodes": nodes, "errors": errors}


@dataclass
class BatchRead:
    rows: list[dict[str, Any]]
    unread: dict[tuple[str, int], Exception]


@dataclass
class TrackInventory:
    actor: str | None
    issues: list[dict[str, Any]]
    seeds: dict[tuple[str, int], dict[str, Any]]
    complete: bool


def track_inventory(
    reader: github_read.GitHubReader | None,
    read: Callable[..., tuple[str, list[dict[str, Any]]]],
    repo: str,
    *,
    limit: int,
    milestone: int | None = None,
) -> TrackInventory:
    if reader is not None and milestone is None:
        owner, name = repo.split("/", 1)
        nodes = discover_tracks(reader, owner, name, (limit + 99) // 100)
        refs = [
            {
                "repo": repo,
                "number": node["number"],
                "title": node["title"],
                "html_url": node["url"],
                "state": node["state"].lower(),
                "milestone": github_plan_graph.rest_milestone(node.get("milestone")),
            }
            for node in nodes
        ]
        return TrackInventory(reader.actor, refs, {}, True)
    query: dict[str, Any] = {"state": "open", "sort": "created", "direction": "asc"}
    if milestone is not None:
        query["milestone"] = milestone
    actor, rows = read(
        f"/repos/{repo}/issues", query=query, bucket="rest_core", step_prefix="next_plan_issues", limit=limit + 1, issue_only=True
    )
    issues = rows[:limit]
    return TrackInventory(
        actor, issues, {(repo.casefold(), row["number"]): {**row, "repo": repo} for row in issues}, len(rows) <= limit
    )


def repository_inventory(
    read: Callable[..., tuple[str, list[dict[str, Any]]]],
    *,
    installed: bool,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = "/installation/repositories" if installed else "/user/repos"
    _, rows = read(
        path,
        query={} if installed else {"affiliation": "owner,collaborator,organization_member"},
        collection_key="repositories" if installed else None,
        bucket="rest_core",
        step_prefix="next_repository_inventory",
        limit=limit + 1,
    )
    return rows[:limit], {
        "source": path,
        "repository_limit": limit,
        "inventory_observed_count": len(rows),
        "inventory_count": min(len(rows), limit),
        "inventory_truncated": len(rows) > limit,
        "complete": len(rows) <= limit,
    }


def live_breakage_inventory(
    read: Callable[..., tuple[str, list[dict[str, Any]]]],
    *,
    owner: str,
    installed: bool,
    repo_limit: int,
    issue_limit: int,
    include_archived: bool = False,
    map_reads: Callable[..., list[Any]] | None = None,
    reader: github_read.GitHubReader | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context: dict[str, Any] = {
        "source": "current_owner_visible_label_inventory",
        "complete": False,
        "scope": "owner repositories accessible to the configured actor",
        "repositories": [],
    }
    try:
        repositories, inventory = repository_inventory(read, installed=installed, limit=repo_limit)
    except ValueError as exc:
        return [], {**context, "error": str(exc)}
    context.update(complete=inventory["complete"], repository_inventory=inventory)
    if inventory["inventory_truncated"]:
        context.update(
            error=f"Repository inventory reached --repo-limit {repo_limit}; "
            f"at least {inventory['inventory_observed_count']} repositories were observed. Raise --repo-limit to establish live priority.",
            recommended_next_action="increase_repo_limit",
        )
    eligible = []
    for repository in repositories:
        name = repository.get("full_name")
        if not isinstance(name, str) or re.fullmatch(r"[\w.-]+/[\w.-]+", name) is None:
            context["complete"] = False
            continue
        if (
            name.split("/", 1)[0].casefold() == owner.casefold()
            and github_repository_scope.includes_repository(repository, include_archived=include_archived)
            and not repository.get("disabled")
            and repository.get("has_issues") is not False
        ):
            eligible.append(repository)

    def read_repository(repository: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        name = repository["full_name"]
        try:
            _, rows = read(
                f"/repos/{name}/issues",
                query={"state": "open", "labels": github_direction_next.LIVE_BREAKAGE_LABEL},
                bucket="rest_core",
                step_prefix="next_live_breakage_issues",
                limit=issue_limit + 1,
                issue_only=True,
            )
            seen = set()
            for row in rows:
                number = row.get("number")
                if (
                    type(number) is not int
                    or number <= 0
                    or number in seen
                    or (issue_repository_name(row) or "").casefold() != name.casefold()
                ):
                    raise ValueError("live-breakage issue identity is incomplete")
                seen.add(number)
            return [{**row, "repo": name} for row in rows[:issue_limit]], {
                "repo": name,
                "count": min(len(rows), issue_limit),
                "complete": len(rows) <= issue_limit,
            }
        except ValueError as exc:
            return [], {"repo": name, "complete": False, "error": str(exc)}

    if reader is None:
        results = map_reads(read_repository, eligible) if map_reads is not None else [read_repository(row) for row in eligible]
    else:
        chunks = [eligible[start : start + TRACKS_PER_QUERY] for start in range(0, len(eligible), TRACKS_PER_QUERY)]

        def read_chunk(chunk: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
            return read_live_breakage_batch(reader, [row["full_name"] for row in chunk], issue_limit)

        batches = map_reads(read_chunk, chunks) if map_reads is not None else [read_chunk(chunk) for chunk in chunks]
        results = [result for batch in batches for result in batch]
    issues = []
    for rows, coverage in results:
        issues.extend(rows)
        context["repositories"].append(coverage)
        context["complete"] &= coverage["complete"]
    context["count"] = len(issues)
    return issues, context


def read_live_breakage_batch(
    reader: github_read.GitHubReader, repositories: list[str], limit: int
) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
    rows: dict[str, dict[int, dict[str, Any]]] = {name: {} for name in repositories}
    coverage: dict[str, dict[str, Any]] = {name: {"repo": name, "complete": False} for name in repositories}
    cursors: dict[str, str | None] = dict.fromkeys(repositories)
    totals: dict[str, int] = {}
    seen_cursors: dict[str, set[str]] = {name: set() for name in repositories}
    while cursors:
        names = list(cursors)
        selections = []
        for index, name in enumerate(names):
            owner, project = name.split("/", 1)
            selections.append(
                f"r{index}: repository(owner:{json.dumps(owner)}, name:{json.dumps(project)}) {{ "
                f"issues(states:[OPEN], labels:[{json.dumps(github_direction_next.LIVE_BREAKAGE_LABEL)}], "
                f"first:{min(100, limit - len(rows[name]))}, after:{json.dumps(cursors[name])}, "
                "orderBy:{field:CREATED_AT,direction:ASC}) { totalCount "
                "nodes { number title state url repository { nameWithOwner } } pageInfo { hasNextPage endCursor } } }"
            )
        result = reader.graphql_json("query { " + " ".join(selections) + " }", {}, step="work_graph_live_breakage")
        if not result.ok:
            raise github_read.GitHubReadError("Live-breakage batch unavailable", result=result, diagnostics=reader.diagnostics())
        body = result.body
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise github_read.GitHubReadShapeError("Live-breakage batch data unavailable")
        errors = body.get("errors") or []
        for index, name in enumerate(names):
            try:
                if any(not isinstance(error, dict) or not error.get("path") or error["path"][0] == f"r{index}" for error in errors):
                    raise github_read.GitHubReadShapeError("Live-breakage inventory returned partial data")
                repository = body["data"].get(f"r{index}")
                nodes, cursor, total = github_plan_graph.connection_page(
                    repository.get("issues") if isinstance(repository, dict) else None
                )
                if name in totals and totals[name] != total:
                    raise github_read.GitHubReadShapeError("Live-breakage inventory changed during pagination")
                totals[name] = total
                for node in nodes:
                    row = github_plan_graph.rest_related_issue(node)
                    number = row["number"]
                    if (
                        type(number) is not int
                        or number <= 0
                        or row["repo"].casefold() != name.casefold()
                        or github_plan_graph.native_issue_key(row) != (name.casefold(), number)
                        or row["state"] != "open"
                        or number in rows[name]
                        or len(rows[name]) >= limit
                    ):
                        raise github_read.GitHubReadShapeError("Live-breakage issue identity or page is incomplete")
                    rows[name][number] = row
                if cursor is not None:
                    if cursor in seen_cursors[name]:
                        raise github_read.GitHubReadShapeError("Live-breakage cursor did not advance")
                    seen_cursors[name].add(cursor)
                    if len(rows[name]) < limit:
                        cursors[name] = cursor
                        continue
                coverage[name]["complete"] = cursor is None and len(rows[name]) == total
                del cursors[name]
            except (github_read.GitHubReadShapeError, KeyError, TypeError, ValueError, AttributeError) as exc:
                coverage[name]["error"] = str(exc)
                del cursors[name]
    return [(list(rows[name].values()), {**coverage[name], "count": len(rows[name])}) for name in repositories]


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
    repo: str,
    number: int,
    issue: dict[str, Any],
    read: Callable[[str, int], tuple[list[dict[str, Any]], bool]],
    *,
    limit: int = 100,
    fields: tuple[str, ...] = ("blocked_by", "blocking", "sub_issues"),
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    rows, truncated = read_relationship_rows(repo, number, issue, read, limit=limit, fields=fields)
    return {field: [compact_relationship_issue(row, field) for row in values] for field, values in rows.items()}, truncated


@dataclass
class NativeGraphRead:
    nodes: dict[tuple[str, int], dict[str, Any]]
    unavailable: list[str]
    truncated: bool


def read_native_graph(
    roots: list[dict[str, Any]],
    read: Callable[[str, int], tuple[list[dict[str, Any]], bool]],
    *,
    max_nodes: int = 500,
    relationship_limit: int = 2000,
) -> NativeGraphRead:
    records = {(row["repo"].casefold(), int(row["number"])): row for row in roots}
    unavailable: list[str] = []

    def read_edges(repo: str, number: int) -> list[tuple[str, int]]:
        related: list[tuple[str, int]] = []
        for field, endpoint in (("blocked_by", "dependencies/blocked_by"), ("sub_issues", "sub_issues")):
            source = f"{repo}#{number}:{endpoint}"
            try:
                fields, truncated = read_relationship_rows(repo, number, {}, read, limit=relationship_limit, fields=(field,))
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
            query = urllib.parse.urlencode(
                sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True), key=lambda pair: pair[0])
            )
            normalized[1] = urllib.parse.urlunsplit(parsed._replace(query=query)).lstrip("/")
            for index in range(2, len(normalized) - 1):
                if normalized[index] in {"--method", "-X"}:
                    normalized[index : index + 2] = ["--method", normalized[index + 1].upper()]
            if len(normalized) == 2 and normalized[1] != "graphql":
                normalized.extend(["--method", "GET"])
        key = tuple(normalized)
        if key not in cache:
            try:
                cache[key] = fetch(args)
            except Exception as exc:
                cache[key] = exc
                raise
        value = cache[key]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    return read


def ownership_inventory(read: Callable[..., Any], repo: str, *, limit: int) -> dict[str, Any]:
    _, pulls = read(
        f"/repos/{repo}/pulls",
        query={"state": "open", "sort": "created", "direction": "asc"},
        bucket="rest_core",
        step_prefix="next_queue_pulls",
        limit=limit + 1,
    )
    _, branches = read(f"/repos/{repo}/branches", query={}, bucket="rest_core", step_prefix="next_queue_branches", limit=limit + 1)
    complete = len(pulls) <= limit and len(branches) <= limit
    return {
        "pulls": pulls[:limit],
        "complete": complete,
        "inventory": {"remote_branches": [branch["name"] for branch in branches[:limit]], "local_branches": [], "worktrees": []},
        "coverage": {
            "pull_count": min(len(pulls), limit),
            "branch_count": min(len(branches), limit),
            "truncated": not complete,
            "local_artifacts": "not_read",
        },
    }
