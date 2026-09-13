#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""File-backed GitHub transport for cleanup fixtures; never contacts a service.

Selected by GITHUB_API_GH or a fixture PATH shim. Unknown operations are recorded
and rejected, including writes outside the configured owning issue. This is a
provider simulation, not evidence of production authentication enforcement.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def response(argv: list[str], state: dict, body: dict) -> tuple[int, object, str, str]:
    """Return status, payload, method and normalized endpoint for one fake call."""
    if argv[:2] == ["repo", "view"]:
        repo = next((a for a in argv[2:] if "/" in a and not a.startswith("-")), state["repo"])
        value = state["repositories"].get(repo)
        if value is None:
            return 404, {"message": "Not found"}, "GET", f"repos/{repo}"
        return 200, {**value, "nameWithOwner": repo, "hasIssuesEnabled": value["has_issues"],
                     "viewerPermission": "WRITE", "defaultBranchRef": {"name": "main"}}, "GET", f"repos/{repo}"
    if not argv or argv[0] != "api":
        return 400, {"message": "Unsupported fixture operation"}, "UNKNOWN", "unsupported"
    method, endpoint = "GET", ""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in ("-X", "--method", "-H", "--header", "--input", "--hostname", "--jq", "-q"):
            if index + 1 >= len(argv):
                return 400, {"message": "Missing argument"}, method, endpoint
            if arg in ("-X", "--method"):
                method = argv[index + 1].upper()
            index += 2
        elif arg.startswith("-"):
            index += 1
        else:
            endpoint = arg
            index += 1
    endpoint = urlsplit(endpoint).path.lstrip("/")
    if method == "GET" and endpoint == "user":
        return 200, {"login": "fixture-bot"}, method, endpoint
    if method == "GET" and endpoint == "rate_limit":
        quota = {"limit": 5000, "remaining": 4999, "reset": 4102444800}
        return 200, {"resources": {"core": quota, "search": quota}, "rate": quota}, method, endpoint
    for repo, value in state["repositories"].items():
        if method == "GET" and endpoint == f"repos/{repo}":
            return 200, value, method, endpoint
    issue = state["issue"]
    issue_path = f"repos/{state['owner_repo']}/issues/{issue['number']}"
    if endpoint == issue_path and method == "GET":
        return 200, issue, method, endpoint
    if endpoint == issue_path + "/comments":
        if method == "GET":
            return 200, state["comments"], method, endpoint
        if method == "POST" and isinstance(body.get("body"), str):
            comment = {"id": len(state["comments"]) + 1, "body": body["body"],
                       "html_url": issue["html_url"] + f"#issuecomment-{len(state['comments']) + 1}",
                       "user": {"login": "fixture-bot"}, "author_association": "COLLABORATOR",
                       "created_at": "2026-09-13T00:00:00Z", "updated_at": "2026-09-13T00:00:00Z"}
            state["comments"].append(comment)
            return 201, comment, method, endpoint
    if method == "GET" and endpoint == f"repos/{state['owner_repo']}/issues":
        return 200, [issue], method, endpoint
    if method == "GET" and endpoint == "search/issues":
        return 200, {"total_count": 1, "incomplete_results": False, "items": [issue]}, method, endpoint
    if method == "GET" and endpoint.endswith(("/pulls", "/labels", "/sub_issues", "/dependencies/blocked_by", "/dependencies/blocking")):
        return 200, [], method, endpoint
    return 403, {"message": "Operation unavailable in this fixture"}, method, endpoint


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    state_path = Path(os.environ["CLEANUP_FIXTURE_PROVIDER_STATE"]).resolve(strict=True)
    state = json.loads(state_path.read_text())
    raw = sys.stdin.read() if "--input" in argv else ""
    body = json.loads(raw) if raw else {}
    status, payload, method, endpoint = response(argv, state, body)
    event = {"method": method, "endpoint": endpoint, "status": status}
    journal = state_path.with_suffix(".events.jsonl")
    descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (json.dumps(event) + "\n").encode())
    finally:
        os.close(descriptor)
    if method != "GET" and status < 300:
        temporary = state_path.with_suffix(".new")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(state_path)
    if "--include" in argv or "-i" in argv:
        print(f"HTTP/2 {status}")
        print("content-type: application/json")
        print("x-ratelimit-limit: 5000")
        print("x-ratelimit-remaining: 4999")
        print("x-ratelimit-reset: 4102444800\n")
    print(json.dumps(payload))
    return 0 if status < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
