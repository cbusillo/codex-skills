# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Presentation of the stranded Markdown report; no finding classification changes."""

from __future__ import annotations

import re
from collections import defaultdict

FINDING = re.compile(r"^- (\w+) \((age unknown|[0-9.]+h)\): (https://github\.com/\S+) — (.*)$")
PERSON = re.compile(r"\b(?:Chris|cbusillo|Director|Owner)\b", re.IGNORECASE)


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parse(report: str) -> tuple[str, str, list[dict]]:
    lines = report.splitlines()
    if (len(lines) < 3 or lines[0] != "Daily stranded-work sweep"
            or not lines[1].startswith("Observed: ") or not lines[2].startswith("Coverage: ")):
        raise ValueError("digest_report_invalid")
    rows: list[dict] = []
    for line in lines[3:]:
        match = FINDING.fullmatch(line)
        if match:
            kind, age, url, remainder = match.groups()
            actor = remainder.partition(". ")[0]
            rows.append({"kind": kind, "age": None if age == "age unknown" else float(age[:-1]),
                         "url": url, "title": url, "person": bool(PERSON.search(actor))})
        elif line.startswith("  Name: ") and rows:
            rows[-1]["title"] = line.removeprefix("  Name: ")
        elif line.startswith("- ") and not line.startswith("- Coverage gap:"):
            # Do not silently discard a changed collector format.
            raise ValueError("digest_report_invalid")
    count = re.search(r"; (\d+) candidates\.$", lines[2])
    if not count or int(count[1]) != len(rows):
        raise ValueError("digest_report_invalid")
    return lines[1].removeprefix("Observed: ")[:10], lines[2].split(";", 1)[0].removeprefix("Coverage: "), rows


def label(text: str, length: int) -> str:
    text = " ".join(text.split())
    text = re.sub(r"[\[\]()*`<>@]", "", text)
    return text if len(text) <= length else text[:length - 1] + "…"


def summary(report: str, full_list_url: str) -> str:
    date, coverage, rows = parse(report)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["kind"]].append(row)
    for group in groups.values():
        # Known oldest events first; unknown ages cannot outrank a known event.
        group.sort(key=lambda r: (r["age"] is None, -(r["age"] or 0), r["url"]))

    for name_length in (72, 48, 32, 20):
        lines = [f"Daily digest · {date} · {coverage}"]
        for kind, group in sorted(groups.items()):
            lines.append(f"{kind}: {len(group)}")
        waiting = [r for r in rows if r["person"]]
        lines.append("Waiting on Chris:" if waiting else "Waiting on Chris: none")
        lines.extend(f"- [{label(r['title'], name_length)}](<{r['url']}>)" for r in waiting)
        for kind, group in sorted(groups.items()):
            other = [r for r in group if not r["person"]][:5]
            if other:
                links = " ".join(f"[{i}](<{r['url']}>)" for i, r in enumerate(other, 1))
                lines.append(f"{kind} oldest: {links}")
        lines.append(f"[Full list](<{full_list_url}>)")
        result = "\n".join(lines)
        if units(result) < 2000:
            return result
    # An unbounded person queue cannot always fit. Keep every required item;
    # retain the runner's reconciliation boundary instead of dropping any.
    raise ValueError("digest_summary_too_large")


def archive(report: str, tracking_issue: str) -> str:
    """Use the existing bot comment transport, including uncertain-write recovery."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
    import github_comment

    target = re.fullmatch(r"([\w.-]+/[\w.-]+)#([0-9]+)", tracking_issue)
    if not target:
        raise ValueError("digest_tracking_issue_invalid")
    try:
        result = github_comment.comment("issue", int(target[2]), report, repo=target[1], dedupe_body=True)
    except github_comment.CommentError:
        raise ValueError("digest_archive_failed") from None
    url = result.get("url")
    if not isinstance(url, str) or not re.fullmatch(
            rf"https://github\.com/{re.escape(target[1])}/issues/{target[2]}#issuecomment-[0-9]+", url):
        raise ValueError("digest_archive_response_invalid")
    return url
