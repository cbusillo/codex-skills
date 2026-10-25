# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import github_identity
import github_plan_wait

SKILLS = Path(__file__).resolve().parents[2]
COMMENT_LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/[1-9][0-9]*#issuecomment-([1-9][0-9]*)")
QUEUE = re.compile(r"[\w.-]+/[\w.-]+#[1-9][0-9]*")


for scripts in ("github-work-rollup/scripts", "supervisor/scripts"):
    if str(SKILLS / scripts) not in sys.path:
        sys.path.append(str(SKILLS / scripts))


def director_terms(owner: str) -> list[str]:
    import github_unanswered_comments as radar

    names = radar.people_identities({radar.normalize_login(owner)}, None)["director_names"]
    return [owner, *names, *radar.DIRECTOR_ROLE_WORDS]


def waits_on_director(owner: str, wait: dict[str, Any]) -> list[str]:
    import github_unanswered_comments as radar

    terms = director_terms(owner)
    return [entry["waiting_for"] for entry in wait["waits"]
            if radar.current_director_wait(entry["waiting_for"], terms, {owner.casefold()})]


def configured_queue() -> str | None:
    import account_choice

    for path in account_choice.config_paths(os.environ, Path.home()):
        if not path.exists():
            continue
        try:
            section = tomllib.loads(path.read_text(encoding="utf-8")).get("questions")
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"cannot read private supervisor config {path}") from error
        if section is None:
            continue
        queue = section.get("queue") if isinstance(section, dict) else None
        if not isinstance(queue, str) or not QUEUE.fullmatch(queue):
            raise ValueError(f'[questions] in {path} needs queue = "OWNER/REPO#NUMBER"')
        return queue
    return None


def comment_links(wait: dict[str, Any]) -> list[tuple[str, str, str]]:
    found = {match[0]: (match[0], match[1], match[2]) for match in COMMENT_LINK.finditer(json.dumps(wait))}
    return list(found.values())


def comment_author(comment: dict[str, Any]) -> str:
    return str((comment.get("user") or {}).get("login") or "").casefold()


def comment_text(comment: dict[str, Any]) -> str:
    return str(comment.get("body") or "").lstrip().lstrip("*")


def is_director_record(comment: dict[str, Any], owner: str, trusted: set[str]) -> bool:
    import question_delivery

    author = comment_author(comment)
    return author == owner.casefold() or (
        author in trusted and comment_text(comment).startswith(question_delivery.oq.DECISION_PREFIXES))


def is_numbered_question(comment: dict[str, Any], trusted: set[str]) -> bool:
    import question_delivery

    text = comment_text(comment)
    return (comment_author(comment) in trusted and text.startswith(question_delivery.oq.QUESTION_PREFIXES)
            and question_delivery.question_id(text) is not None)


def registered_question_ids(queue: str, read_pages: Callable[[str], list[dict[str, Any]]]) -> set[str]:
    import question_delivery

    queue_repo, number = queue.split("#")
    comments = [{"author": comment_author(comment), "body": comment.get("body"), "url": comment.get("html_url")}
                for comment in read_pages(f"/repos/{queue_repo}/issues/{number}/comments")]
    records = question_delivery.records(comments, github_plan_wait.authors(queue_repo))
    return {question_delivery.COMMENT_URL.fullmatch(record["question_url"])[3]
            for record in records if record["event"] == "registered"}


def unasked_message(owner: str, director_waits: list[str], queue: str | None, unreadable: list[str]) -> str:
    waits = "; ".join(f'"{text}"' for text in director_waits)
    bot = github_identity.automation_login() or "TRUSTED_BOT"
    register = (f"register it with `uv run {SKILLS}/supervisor/scripts/oq.py QUESTION_URL --owner {owner} "
                f"--decision-author {bot} --queue {queue} --register`, " if queue else "")
    unread = f" These linked comments could not be read; fix the link or retry: {', '.join(unreadable)}." if unreadable else ""
    return (f"The wait {waits} is on the Director, and it links no numbered Director question"
            f"{f' registered in {queue}' if queue else ''}. Ask first: post a comment starting `Director question:` "
            "on the issue that states the step, your recommendation and what happens after it (the comment helpers "
            f"number it), {register}then record the wait again with QUESTION_URL in --evidence. If the Director "
            "already set or agreed to this hold, link the Director's own comment or its `Director decision:` record "
            'instead. If the current step is a timer or someone else\'s, name that first and the Director after "then".'
            + unread)


def director_question_gap(repo: str, wait: dict[str, Any], *, read_comment: Callable[[str], dict[str, Any] | None],
                          read_pages: Callable[[str], list[dict[str, Any]]]) -> str | None:
    owner = repo.split("/")[0]
    director_waits = waits_on_director(owner, wait) if wait["state"] == "waiting" else []
    if not director_waits:
        return None
    queue = configured_queue()
    trusted = github_plan_wait.authors(repo)
    registered: set[str] | None = None
    unreadable = []
    for url, comment_repo, comment_id in comment_links(wait):
        comment = read_comment(f"/repos/{comment_repo}/issues/comments/{comment_id}")
        if comment is None:
            unreadable.append(url)
            continue
        if is_director_record(comment, owner, trusted):
            return None
        if not is_numbered_question(comment, trusted):
            continue
        if queue is None:
            return None
        if registered is None:
            registered = registered_question_ids(queue, read_pages)
        if comment_id in registered:
            return None
    return unasked_message(owner, director_waits, queue, unreadable)
