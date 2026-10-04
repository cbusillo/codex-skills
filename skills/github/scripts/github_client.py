#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Read-only, repository-scoped Client identity for ranking and direction audits.

Only helper projections and private repository overlays are consumed. This
module grants no permissions and never interprets issue prose as instructions.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS.parents[2]) not in sys.path:
    sys.path.insert(0, str(SCRIPTS.parents[2]))
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def helper_json(script: Path, *args: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(["uv", "run", str(script), *args], capture_output=True,
                                text=True, timeout=30, check=False)
        value = json.loads(result.stdout) if result.returncode == 0 else None
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def local_repository(repo: str) -> Path | None:
    """Find a sibling primary checkout, verifying its origin before local reads."""
    try:
        result = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5, check=True)
        primary = Path(result.stdout.strip()).parent
        for candidate in (primary, primary.parent / repo.split("/")[-1]):
            origin = subprocess.run(["git", "-C", str(candidate), "remote", "get-url", "origin"],
                                    capture_output=True, text=True, timeout=5, check=False)
            remote = origin.stdout.strip().removesuffix(".git")
            if origin.returncode == 0 and remote.casefold() in {
                    "https://github.com/" + repo.casefold(), "git@github.com:" + repo.casefold(),
                    "ssh://git@github.com/" + repo.casefold()}:
                return candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def overlay_client(repo_root: Path | None) -> dict[str, Any]:
    if repo_root is None:
        return {"status": "unavailable", "source": "repo_people_overlay"}
    # Reuse the people schema and scope loader. Global identities never establish
    # who the Client of this repository is, and bot aliases are not human Clients.
    from skills.people.scripts.resolve_person import load_people, PeopleConfigError
    try:
        _, people = load_people(repo_root / ".local" / "people.yaml", source_scope="repo")
    except (OSError, PeopleConfigError):
        return {"status": "unavailable", "source": "repo_people_overlay"}
    clients = []
    for person in people:
        relationship = person.get("relationship") or {}
        roles = relationship.get("roles") or person.get("roles") or []
        if relationship.get("kind") != "client" and "client" not in roles:
            continue
        login = person.get("github") or ((person.get("contacts") or {}).get("github") or {}).get("username")
        if not isinstance(login, str) or not LOGIN.fullmatch(login):
            return {"status": "unavailable", "source": "repo_people_overlay"}
        clients.append(login.casefold())
    unique = set(clients)
    return ({"status": "recorded", "source": "repo_people_overlay", "login": clients[0]}
            if len(unique) == 1 else {"status": "none" if not clients else "ambiguous", "source": "repo_people_overlay"})


def recorded_client(repo: str) -> dict[str, Any]:
    context = helper_json(SCRIPTS.parents[1] / "launchplane/scripts/launchplane-context.py", "--repo", repo)
    mapping = ((context or {}).get("sections") or {}).get("repo_product_mapping") or {}
    if (context or {}).get("status") == "available" and mapping.get("status") == "available":
        products = {row.get("product_key") for row in mapping.get("repositories", [])
                    if str(row.get("repository") or "").casefold() == repo.casefold() and row.get("product_key")}
        if not products:
            return {"status": "none", "source": "launchplane"}
        if len(products) != 1:
            return {"status": "ambiguous", "source": "launchplane"}
        product = products.pop()
        response = helper_json(SCRIPTS.parents[1] / "launchplane/scripts/launchplane-write-action.py",
                               "product-profile-read", "--product", product)
        if (response or {}).get("status") in {"ok", "available"}:
            profile = (response or {}).get("result") or {}
            if not profile.get("repository"):
                return {"status": "unavailable", "source": "launchplane"}
            if (str(profile.get("repository") or "").casefold() != repo.casefold()
                    or profile.get("product") != product):
                return {"status": "ambiguous", "source": "launchplane"}
            login = profile.get("owner_github_login")
            if not login:
                return {"status": "none", "source": "launchplane"}
            if isinstance(login, str) and LOGIN.fullmatch(login):
                return {"status": "recorded", "source": "launchplane", "login": login.casefold()}
            return {"status": "unavailable", "source": "launchplane"}
    return overlay_client(local_repository(repo))


def is_client_issue(issue: dict[str, Any], client: dict[str, Any] | None, *, bot_logins: tuple[str, ...] = ()) -> bool:
    author = issue.get("author") or (issue.get("user") or {}).get("login")
    bots = {login.casefold() for login in bot_logins}
    return bool(client and client.get("status") == "recorded" and isinstance(author, str)
                and author.casefold() not in bots and not issue.get("author_is_bot")
                and (issue.get("user") or {}).get("type") != "Bot"
                and author.casefold() == client.get("login") and "pull_request" not in issue)
