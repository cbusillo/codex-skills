# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import functools
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import github_comment
import github_plan_wait

SKILLS = Path(__file__).resolve().parents[2]
COMMENT_LINK = github_plan_wait.COMMENT_LINK
CLIENT_ROLE = re.compile(r"\bClients?\b")
LOWERCASE_CLIENT = re.compile(r"\bclients?\b")
CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
WRITTEN_LOGIN = re.compile(r"(?<![\w/])@([a-z\d](?:[a-z\d-]*[a-z\d])?)(?![\w/-])", re.IGNORECASE)


class AskedPerson(NamedTuple):
    logins: frozenset[str]
    director: bool


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


@functools.cache
def address_book(owner: str) -> dict[str, frozenset[str]]:
    import github_unanswered_comments as radar

    identities = radar.people_identities({radar.normalize_login(owner)}, None)
    robots = {*identities["automation"], *identities["own_automation"]}
    book: dict[str, set[str]] = {}
    for login, names in identities["address_names"].items():
        if login in robots:
            continue
        for name in (login, *names):
            book.setdefault(name.casefold(), set()).add(login.casefold())
    return {name: frozenset(logins) for name, logins in book.items()}


def asked_person(owner: str, waiting_for: str) -> AskedPerson | None:
    import github_unanswered_comments as radar

    director = radar.current_director_wait(waiting_for, director_terms(owner), {owner.casefold()}) is not None
    asserted = radar.asserted_step(LOWERCASE_CLIENT.sub("", waiting_for))
    book = address_book(owner)
    known_logins = {login for logins in book.values() for login in logins}
    named = {login for name, logins in book.items()
             if github_plan_wait.names_recipient(asserted, [name], known_logins) for login in logins}
    named |= {login.casefold() for login in WRITTEN_LOGIN.findall(asserted)}
    if not (director or named or CLIENT_ROLE.search(asserted)):
        return None
    return AskedPerson(frozenset(named), director)


def person_on_wait(owner: str, entry: dict[str, Any]) -> AskedPerson | None:
    person = asked_person(owner, entry["waiting_for"])
    if person is None or (github_plan_wait.machine_checked(entry) and not person.director):
        return None
    return person


def person_waits(owner: str, wait: dict[str, Any]) -> list[tuple[dict[str, Any], AskedPerson]]:
    found = [(entry, person_on_wait(owner, entry)) for entry in wait["waits"]] if wait["state"] == "waiting" else []
    return [(entry, person) for entry, person in found if person is not None]


def attach_asks(owner: str, wait: dict[str, Any], ask_urls: list[str]) -> None:
    people = person_waits(owner, wait)
    if len(ask_urls) > len(people):
        raise ValueError(f"Each --ask-url belongs to one person wait, in order; {', '.join(ask_urls[len(people):])} has none. "
                         "Use --event, --not-before or --recheck for an event wait.")
    for (entry, _), url in zip(people, ask_urls):
        entry["ask_url"] = url
    github_plan_wait.validate(wait)


def configured_queue() -> str | None:
    from skills.direction.scripts import question_id

    return question_id.configured_queue()


def comment_mentions(comment: dict[str, Any]) -> set[str]:
    visible = CODE_SPAN.sub("", str(comment.get("body") or ""))
    return {mention.removeprefix("@").casefold() for mention in github_comment.live_mentions(visible)}


def comment_author(comment: dict[str, Any]) -> str:
    return str((comment.get("user") or {}).get("login") or "").casefold()


def comment_text(comment: dict[str, Any]) -> str:
    return str(comment.get("body") or "").lstrip().lstrip("*")


def is_director_record(comment: dict[str, Any], owner: str, trusted: set[str]) -> bool:
    import question_delivery

    author = comment_author(comment)
    return author == owner.casefold() or (
        author in trusted and question_delivery.oq.DECISION_HEADING.match(comment_text(comment)) is not None)


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


def unasked_director_message(owner: str, waiting_for: str, queue: str | None) -> str:
    register = (f"register it with `uv run {SKILLS}/supervisor/scripts/oq.py QUESTION_URL --owner {owner} "
                f"--queue {queue} --register`, " if queue else "")
    return (f'The wait "{waiting_for}" is on the Director, and --ask-url links no numbered Director question'
            f"{f' registered in {queue}' if queue else ''}. Ask first: post a comment starting `Director question:` "
            "on the issue that states the step, your recommendation and what happens after it (the comment helpers "
            f"number it), {register}then record the wait with --ask-url QUESTION_URL. If the Director "
            "already set or agreed to this hold, link the Director's own comment or its `Director decision:` record "
            'instead. If the current step is a timer or someone else\'s, name that first and the Director after "then".')


def unasked_person_message(waiting_for: str, ask_url: str | None) -> str:
    finding = (f"{ask_url} does not @mention them and is not their own or the Director's words" if ask_url
               else "nothing links the comment that asks them")
    return (f'The wait "{waiting_for}" names a person, and {finding}. If they should be asked now, post a comment that '
            "@mentions them and record the wait with --ask-url COMMENT_URL. If nobody should be asked yet, record what is "
            'really in the way and name the person after "then": a native blocker, --event, --not-before, --recheck, or a '
            "Next actor for agent work.")


def ask_is_real(comment: dict[str, Any], person: AskedPerson, owner: str, trusted: set[str]) -> bool:
    if is_director_record(comment, owner, trusted):
        return True
    mentioned = comment_mentions(comment) - trusted
    return comment_author(comment) in person.logins or bool(mentioned & person.logins if person.logins else mentioned)


def read_ask_comment(url: str, read_comment: Callable[[str], Any]) -> dict[str, Any] | None:
    link = COMMENT_LINK.fullmatch(url)
    comment = read_comment(f"/repos/{link[1]}/issues/comments/{link[3]}")
    return comment if isinstance(comment, dict) and str(comment.get("html_url") or "").casefold() == url.casefold() else None


def ask_gap(repo: str, wait: dict[str, Any], *, read_comment: Callable[[str], Any],
            read_pages: Callable[[str], list[dict[str, Any]]]) -> str | None:
    owner = repo.split("/")[0]
    queue = configured_queue()
    trusted = github_plan_wait.authors(repo)
    registered = functools.cache(lambda: registered_question_ids(queue, read_pages))

    def director_asked(comment: dict[str, Any], url: str) -> bool:
        return is_director_record(comment, owner, trusted) or (is_numbered_question(comment, trusted) and (
            queue is None or COMMENT_LINK.fullmatch(url)[3] in registered()))

    for entry, person in person_waits(owner, wait):
        url = entry.get("ask_url")
        comment = read_ask_comment(url, read_comment) if url else None
        if url and comment is None:
            return f"The ask comment {url} could not be read. Fix the link or retry; a wait on a person needs a readable ask."
        if person.director:
            if comment is None or not director_asked(comment, url):
                return unasked_director_message(owner, entry["waiting_for"], queue)
            continue
        if comment is None or not ask_is_real(comment, person, owner, trusted):
            return unasked_person_message(entry["waiting_for"], url)
    return None


def ask_status(owner: str, wait: dict[str, Any], entry: dict[str, Any], *, trusted: set[str],
               read_thread: Callable[[str, int], list[dict[str, Any]] | None]) -> dict[str, Any] | None:
    person = person_on_wait(owner, entry)
    if person is None:
        return None
    status: dict[str, Any] = {"waiting_for": entry["waiting_for"], "ask_url": entry.get("ask_url"), "author": None, "replied_since": None}
    if status["ask_url"] is None and person.director and (
            link := COMMENT_LINK.search(f"{entry['waiting_for']} {entry['ends_when']} {wait['evidence']}")):
        status["ask_url"] = link[0]
    if status["ask_url"] is None:
        return status
    link = COMMENT_LINK.fullmatch(status["ask_url"])
    thread = read_thread(link[1], int(link[2])) or []
    position = next((index for index, row in enumerate(thread) if row["url"].casefold() == status["ask_url"].casefold()), None)
    if position is None:
        return status
    ask = thread[position]
    asked = person.logins | ({owner.casefold()} if person.director else set()) or (comment_mentions(ask) - trusted)
    return {**status, "author": ask["author"], "replied_since": any(row["author"].casefold() in asked for row in thread[position + 1:])}
