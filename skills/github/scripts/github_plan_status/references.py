from __future__ import annotations

import re
from typing import Any


def pr_issue_reference(pull: dict[str, Any], number: int, *, repo: str,
                       local_references: bool = True, attestation: bool = False, record_kind: str = "issue") -> bool:
    title = pull.get("title") or ""
    body = pull.get("body") or ""
    path_kind = "pull" if record_kind == "pr" else "issues"
    issue_url = rf"https://github\.com/{re.escape(repo)}/{path_kind}/{number}"
    issue_reference = rf"(?:{re.escape(repo)}#{number}|{issue_url})(?!\d)"
    if local_references:
        issue_reference = rf"(?:#{number}|{issue_reference})(?!\d)"
    any_issue = r"(?:https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*(?:#[\w-]+)?|[\w.-]+/[\w.-]+#[1-9]\d*|#[1-9]\d*)"
    list_item = rf"(?:{any_issue}|<{any_issue}>|\[[^\]\n]+\]\({any_issue}(?:[ \t]+\"[^\"]*\")?\))"
    separator = r"(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+)"
    ownership_reference = (
        rf"(?i)(?<![\w])(?:__)?(?:refs?|fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed)|implement(?:s|ed|ing)?)"
        rf"(?:\*\*|__)?\s*:?(?:\*\*|__)?\s+(?:{list_item}{separator})*"
        rf"(?:{issue_reference}|<{issue_url}(?!\d)[^>]*>|"
        rf"\[[^\]\n]+\]\({issue_url}(?!\d)[^\n)]*\))"
    )
    link_text = title
    if attestation:
        link_text += "\n" + "\n".join(
            line for line in body.splitlines()
            if not (line.startswith("Code follow-ups recorded without starting implementation:")
                    and not re.search(ownership_reference, line))
        )
    return bool((repo and re.search(issue_url + r"(?!\d)", link_text, re.IGNORECASE))
                or re.search(ownership_reference, body)
                or (local_references and re.search(rf"(?<![\w/])#{number}(?!\d)", title)))


def linked_issues(repo: str, pull: dict[str, Any]) -> list[tuple[str, int]]:
    title = pull.get("title") or ""
    text = title + "\n" + (pull.get("body") or "")
    refs = [(match[1] or match[2] or repo, int(match[3])) for match in re.finditer(
        r"(?:https://github[.]com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)([1-9]\d*)\b", text)]
    return [(target, issue) for target, issue in refs if pr_issue_reference(
        pull, issue, repo=target, local_references=target.casefold() == repo.casefold())
        or titled_qualified_reference(title, target, issue)]


def titled_qualified_reference(title: str, repo: str, number: int) -> bool:
    return bool(re.search(rf"(?<![\w./-]){re.escape(repo)}#{number}(?!\d)", title, re.IGNORECASE))
