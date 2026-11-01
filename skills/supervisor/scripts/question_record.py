# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import question_delivery as delivery

from skills.direction.scripts import question_id
from skills.github.scripts import github_comment

VERSION = 1
COMMENT_LIMIT = 65536
ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)")
RECORD = re.compile(r"<!-- director-question:record (\{[^\n]*}) -->")
TEXT_FIELDS = ("issue", "decision", "basis", "recommendation", "comparison", "manual")
OPTION_FIELDS = ("answer", "changes", "cost", "next")
DiscussionReader = Callable[[str], list[dict[str, Any]]]


def validation_errors(record: Any) -> list[str]:
    if not isinstance(record, dict):
        return ["record must be an object"]
    errors = []
    if type(record.get("version")) is not int or record["version"] != VERSION:
        errors.append(f"version must be {VERSION}")
    for field in TEXT_FIELDS:
        if not isinstance(record.get(field), str) or not record[field].strip():
            errors.append(f"{field} must be nonempty text")
    if isinstance(record.get("issue"), str) and not ISSUE_URL.fullmatch(record["issue"]):
        errors.append("issue must be an exact GitHub issue URL")
    unknown = set(record) - {"version", "options", *TEXT_FIELDS}
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")
    options = record.get("options")
    if not isinstance(options, list) or len(options) < 2:
        errors.append("options must contain at least two choices")
    answers = []
    for index, option in enumerate(options if isinstance(options, list) else []):
        if not isinstance(option, dict):
            errors.append(f"options[{index}] must be an object")
            continue
        for field in OPTION_FIELDS:
            value = option.get(field)
            if field == "cost" and type(value) in (int, float) and value == 0:
                continue
            if not isinstance(value, str) or not value.strip():
                errors.append(f"options[{index}].{field} must be nonempty text (cost also accepts zero)")
        if set(option) - set(OPTION_FIELDS):
            errors.append(f"options[{index}] has unknown fields")
        answer = option.get("answer")
        if isinstance(answer, str) and answer.strip():
            key = answer.strip().casefold()
            if key in answers:
                errors.append(f"options[{index}].answer is duplicated")
            answers.append(key)
    if isinstance(record.get("recommendation"), str) and record["recommendation"].strip().casefold() not in answers:
        errors.append("recommendation must name one option's answer")
    return errors


def render(record: dict[str, Any], *, legacy: bool = False) -> str:
    def text(value: Any) -> str:
        value = str(value) if legacy else " ".join(str(value).split())
        return value.replace("<!--", "&lt;!--")

    def sentence(value: Any) -> str:
        value = text(value)
        return value if value.endswith((".", "!", "?", ";")) else value + "."

    recommended = next(
        option for option in record["options"] if option["answer"].strip().casefold() == record["recommendation"].strip().casefold()
    )
    lines = [f"Director question: **{text(record['decision'])}**", ""]
    lines.extend(
        [text(record["basis"]), ""]
        if legacy
        else [
            f"- **Context:** {text(record['basis'])}",
            f"- **In simple terms:** {text(recommended['changes'])}",
            "- **Options:**",
        ]
    )
    for option in record["options"]:
        label = f"{text(option['answer'])}{' (recommended)' if option is recommended else ''}"
        lines.extend(
            [
                f"**{label}:** {text(option['changes'])}",
                f"Ongoing cost: {text(option['cost'])}. After this answer: {text(option['next'])}",
                "",
            ]
            if legacy
            else [
                f"  - **{label}:** {sentence(option['changes'])} "
                f"Ongoing cost: {sentence(option['cost'])} After this answer: {text(option['next'])}",
            ]
        )
    lines.extend(
        [
            f"Experienced developer: {text(record['comparison'])}",
            f"By hand: {text(record['manual'])}",
        ]
        if legacy
        else [
            f"- **A senior developer would:** {text(record['comparison'])}",
            f"- **Recommendation:** {text(recommended['answer'])}. {text(record['comparison'])}",
            f"- **You do:** {text(record['manual'])}",
            "",
        ]
    )
    lines.append(f"Owning issue: {record['issue']}")
    encoded = json.dumps(record, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    return "\n".join(lines) + f"\n\n<!-- director-question:record {encoded} -->\n"


def read_record(source: dict[str, Any]) -> dict[str, Any]:
    body = source["body"]
    markers = RECORD.findall(body)
    if len(markers) != 1:
        raise ValueError("source must contain exactly one structured question record")
    record = json.loads(markers[0])
    errors = validation_errors(record)
    if errors:
        raise ValueError("\n".join(errors))
    number = question_id.heading_id(body)
    if not number or github_comment.canonical_comment_body(body) != question_id.number_heading(render(record, legacy=True), number).strip():
        raise ValueError("source text disagrees with its record or has no number; read the source before repairing it")
    if record["issue"].casefold() != source["issue"].casefold():
        raise ValueError("record issue disagrees with its source")
    rendered = question_id.number_heading(render(record), number).split("<!-- director-question:record ", 1)[0].strip()
    return {
        "record": record,
        "author": source["author"],
        "question_id": number,
        "url": source["url"],
        "text": rendered.removeprefix("Director question: "),
    }


class QuestionStorage(Protocol):
    def create(self, issue: str, body: str) -> str: ...

    def read(self, url: str) -> dict[str, Any]: ...


class GitHubComments:
    def __init__(self, fetch: DiscussionReader) -> None:
        self.fetch = fetch

    @staticmethod
    def create(issue: str, body: str) -> str:
        match = ISSUE_URL.fullmatch(issue)
        if match is None:
            raise ValueError("invalid issue URL")
        result = github_comment.comment("issue", int(match[2]), body, repo=match[1], dedupe_body=True)
        return result["url"]

    def read(self, url: str) -> dict[str, Any]:
        source = next((c for c in self.fetch(delivery.source_item(url)) if c["url"] == url), None)
        if source is None:
            raise ValueError(f"source question is unavailable: {url}")
        match = delivery.COMMENT_URL.fullmatch(url)
        assert match is not None
        return {**source, "issue": f"https://github.com/{match[1]}/issues/{match[2]}"}


def create(record: Any, args: argparse.Namespace, storage: QuestionStorage, fetch: DiscussionReader) -> dict[str, Any]:
    errors = validation_errors(record)
    configured = question_id.configured_queue()
    queue = args.queue or configured
    if args.queue and configured and args.queue.casefold() != configured.casefold():
        errors.append("--queue disagrees with configured question queue")
    if queue and (not delivery.oq.ITEM.fullmatch(queue) or queue.split("/", 1)[0].casefold() != args.owner.casefold()):
        errors.append("queue must belong to the configured Director")
    body = render(record, legacy=True) if not errors else ""
    target = ISSUE_URL.fullmatch(record["issue"]) if body else None
    if body and target:
        try:
            github_comment.validate_question_mentions(body, target[1])
            github_comment.validate_planning_references(body, target[1])
        except ValueError as error:
            errors.append(str(error))
        if question_id.heading_id(body):
            errors.append("decision must not supply a question number; the confirmed source owns numbering")
        if len(body.encode()) + 128 > COMMENT_LIMIT:
            errors.append("record and rendered text exceed the GitHub comment limit")
    if errors:
        raise ValueError("Invalid Director question:\n" + "\n".join(f"- {error}" for error in errors))
    if args.dry_run:
        return {"text": render(record).split("<!-- director-question:record ", 1)[0].strip().removeprefix("Director question: "), "registration": "dry_run"}
    assert target is not None
    repo = target[1]
    actor = github_comment.authenticated_actor(
        gh_cmd=github_comment.DEFAULT_GH,
        operation="github.question.create",
        expected_actor=github_comment.EXPECTED_ACTOR,
        write_repository=repo,
    )
    trusted = {author.casefold() for author in [args.owner, *args.decision_author]}
    if actor.casefold() not in trusted:
        raise ValueError("question writer must be the Director or a configured --decision-author")
    with delivery.queue_lock():
        url = storage.create(record["issue"], body)
        try:
            source = storage.read(url)
            view = read_record(source)
            if source["author"].casefold() not in trusted or view["record"] != record:
                raise ValueError("saved question disagrees with the requested record or writer")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            raise ValueError(f"Question saved at {url}; source readback failed: {error}") from error
    if not queue:
        return {**view, "registration": "not_configured", "recipient": args.owner}
    request = SimpleNamespace(
        queue=queue, owner=args.owner, decision_author=args.decision_author, items=[url], register=True, ack=None, delivery_url=None
    )
    try:
        report = delivery.run_queue(request, fetch)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise ValueError(f"Question saved at {url}; queue registration failed: {error}") from error
    if not report["complete"]:
        raise ValueError(f"Question saved at {url}; queue registration is incomplete: {json.dumps(report)}")
    row = next((q for q in report["questions"] if q["url"] == url), None)
    if not isinstance(row, dict):
        return {**view, "registration": "answered", "queue": queue}
    if not row["registration_url"]:
        raise ValueError(f"Question saved at {url}; registration readback is missing")
    return {**view, "registration": row["registration_url"], "queue": queue}


def main(argv: list[str], fetch: DiscussionReader) -> int:
    parser = argparse.ArgumentParser(description="Create or read one structured Director question")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("create")
    new.add_argument("--record-file", type=Path, required=True)
    new.add_argument("--owner", required=True)
    new.add_argument("--decision-author", action="append", default=delivery.oq.default_decision_authors())
    new.add_argument("--queue")
    new.add_argument("--dry-run", action="store_true")
    show = commands.add_parser("show-question")
    show.add_argument("url")
    args = parser.parse_args(argv)
    storage = GitHubComments(fetch)
    try:
        if args.command == "show-question":
            print(json.dumps(read_record(storage.read(args.url))))
            return 0
        result = create(json.loads(args.record_file.read_text()), args, storage, fetch)
        print(result["text"])
        if result.get("url"):
            print(f"\nOriginal question: {result['url']}")
        print(f"Registration: {result['registration']}")
        if result.get("recipient"):
            print(f"Next actor: present the link to {result['recipient']}.")
        return 0
    except github_comment.CommentError as error:
        print(
            json.dumps(
                github_comment.terminal_failure(error, "github.question.create", expected_actor=github_comment.EXPECTED_ACTOR)
            ),
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
