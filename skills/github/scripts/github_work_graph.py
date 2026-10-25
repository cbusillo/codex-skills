# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

from typing import Any

import github_read

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
    return ("query WorkGraphTracks($owner: String!, $name: String!) {\n"
            f"  repository(owner: $owner, name: $name) {{\n{aliases}\n  }}\n}}")


def discover_tracks(reader: github_read.GitHubReader, owner: str, name: str, max_pages: int) -> list[dict[str, Any]]:
    found, cursor, expected, seen, cursors = [], None, None, set(), set()
    for page in range(max_pages):
        result = reader.graphql_json(LIST_QUERY, {"owner": owner, "name": name, "cursor": cursor},
            step=f"work_graph_list_{page + 1}")
        if not result.ok:
            raise github_read.GitHubReadError("Track discovery read failed", result=result,
                diagnostics=reader.diagnostics())
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
    reader: github_read.GitHubReader, owner: str, name: str = "direction", *, max_pages: int = 10,
) -> dict[str, Any]:
    tracks = discover_tracks(reader, owner, name, max_pages)
    nodes: dict[int, dict[str, Any]] = {}
    errors: list[str] = []
    for start in range(0, len(tracks), TRACKS_PER_QUERY):
        numbers = [track["number"] for track in tracks[start:start + TRACKS_PER_QUERY]]
        result = reader.graphql_json(detail_query(numbers), {"owner": owner, "name": name},
            step=f"work_graph_tracks_{start // TRACKS_PER_QUERY + 1}")
        data = result.body.get("data") if result.ok and isinstance(result.body, dict) else None
        repository = (data or {}).get("repository") or {}
        if not result.ok or not isinstance(result.body, dict) or result.body.get("errors"):
            errors.append(f"Track read failed for #{', #'.join(map(str, numbers))}")
        for number in numbers:
            node = repository.get(f"t{number}")
            if isinstance(node, dict) and node.get("state") == "OPEN":
                nodes[number] = node
    return {"tracks": tracks, "nodes": nodes, "errors": errors}
