from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

MARKER = "github-plan:claim "
ABANDONED_RELEASE_MARKER = "github-plan:abandoned-release "


def stamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Release evidence requires timezone-qualified timestamps")
    return parsed


def section_map(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body or ""))
    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def unquoted_text(text: str) -> str:
    from .waits import live_lines as visible_lines

    text = re.sub(r"(?im)^ {0,3}</?details\b[^>]*>[ \t]*$", "", text)
    lines = []
    quoted = False
    for _, line in visible_lines(text, prefix=""):
        if line.lstrip().startswith(">"):
            quoted = True
            continue
        if not line.strip() or re.match(r"^ {0,3}(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|<)", line):
            quoted = False
        if not quoted:
            lines.append(line)
    return "\n".join(lines)


def current_status(discussion: dict[str, Any], trusted: set[str], *, body_recorded_at: str | None = None,
                   preserve_markers: bool = False) -> str:
    def timestamp(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
            return parsed if parsed is not None and parsed.tzinfo is not None else None
        except ValueError:
            return None
    status = section_map(unquoted_text(discussion.get("body") or "")).get("Current Status")
    has_body_status = status is not None
    body_time = timestamp(body_recorded_at or discussion.get("updated_at"))
    for comment in discussion.get("comments", []):
        if str(comment.get("author") or "").casefold() not in trusted:
            continue
        found = section_map(unquoted_text(comment.get("body") or "")).get("Current Status")
        comment_time = timestamp(comment.get("created_at"))
        if found is not None and (not has_body_status or (comment_time is not None and (body_time is None or comment_time >= body_time))):
            status = found
            body_time = comment_time
            has_body_status = True
    return (status or "").strip() if preserve_markers else re.sub(r"<!--.*?-->", "", status or "", flags=re.S).strip()


def status_field(status: str, field: str) -> str:
    match = re.search(rf"(?mi)^\s*(?:[-*]\s+)?(?:[*]{{2}})?{re.escape(field)}(?:[*]{{2}})?:(?:[*]{{2}})?[ \t]*(.*)$", status)
    return match[1].strip() if match else ""


def live_lines(text: str) -> list[str]:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    return [line for line in text.splitlines() if line.strip().startswith("<!-- " + MARKER)]


def records(text: str) -> list[dict[str, str]]:
    found = []
    for line in (line.strip() for line in live_lines(text)):
        match = re.fullmatch(r"<!-- github-plan:claim (.*?) -->", line)
        if not match:
            raise ValueError("Malformed claim marker; preserve ownership for owner review")
        raw = match.group(1)
        record = json.loads(raw)
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) and record[key]
            for key in ("worker", "session", "branch", "claimed_at")
        ):
            raise ValueError("Malformed claim record; preserve ownership for owner review")
        found.append(record)
    return found


def without_operation_marker(text: str) -> str:
    return re.sub(r"\n\s*<!-- github-skill-operation:[0-9a-f]+(?: [^<>\r\n]*)? -->\s*$", "", text).rstrip()


def released_claim_id(text: str) -> int | None:
    first = text.splitlines()[:1]
    match = re.fullmatch(r"Released claim (\d+)\.?[ \t]*", first[0]) if first else None
    if match:
        return int(match.group(1))
    text = "\n".join(line if line.strip() else "" for line in text.splitlines())
    text = without_operation_marker(text)
    lines = text.splitlines()
    if len(lines) < 3 or lines[-2].strip():
        return None
    match = re.fullmatch(r"Released claim (\d+)\.?[ \t]*", lines[-1])
    if not match:
        return None
    preceding = text.rsplit("\n\n", 1)[0].rstrip()
    if preceding.endswith(":"):
        return None
    fence = None
    html_end = None
    for line in lines[:-1]:
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line):
                fence = None
            continue
        if html_end:
            if html_end in line.casefold():
                html_end = None
            continue
        if "<!--" in line:
            if "-->" not in line.split("<!--", 1)[1]:
                html_end = "-->"
            continue
        html = re.match(r" {0,3}<(pre|code|blockquote|details|script|style|textarea)[\s>]", line, re.IGNORECASE)
        if html and f"</{html.group(1).casefold()}>" not in line.casefold():
            html_end = f"</{html.group(1).casefold()}>"
            continue
        opener = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if opener:
            fence = opener.group(1)
    return int(match.group(1)) if not fence and not html_end else None


def exact_released_sources(comments: list[dict[str, Any]]) -> list[tuple[int, dict[str, str], int]]:
    found = []
    for index, source in enumerate(comments):
        parsed = records(source.get("body") or "")
        author = (source.get("user") or {}).get("login")
        if len(parsed) != 1 or not author or not isinstance(source.get("id"), int):
            continue
        for release_index in range(index + 1, len(comments)):
            release = comments[release_index]
            if ((release.get("user") or {}).get("login") != author
                    or released_claim_id(release.get("body") or "") != source["id"]):
                continue
            try:
                if stamp(release.get("created_at")) <= max(
                    stamp(parsed[0]["claimed_at"]), stamp(source.get("updated_at") or source.get("created_at")),
                ):
                    continue
            except (ValueError, TypeError):
                continue
            found.append((index, parsed[0], release_index))
            break
    return found


def released_actor(actor: str, *, effective_comments: list[dict[str, Any]], trusted: set[str]) -> bool:
    relevant = [index for index, comment in enumerate(effective_comments)
                if str((comment.get("user") or {}).get("login") or "").casefold() in trusted
                and any(actor.removeprefix("agent:") in {record["session"], record["worker"]}
                        for record in records(comment.get("body") or ""))]
    return bool(relevant and relevant[-1] in {index for index, _, _ in exact_released_sources(effective_comments)})


def has_release_receipt(text: str) -> bool:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    return any(line.startswith("<!-- " + ABANDONED_RELEASE_MARKER) for line in text.splitlines())
