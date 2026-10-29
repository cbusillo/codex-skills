# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Presentation of the stranded Markdown report; no finding classification changes."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

FINDING = re.compile(r"^- (\w+) \((age unknown|[0-9.]+h)\): (https://github\.com/\S+) — (.*)$")
PERSON = re.compile(r"\b(?:Chris|cbusillo|Director|Owner)\b", re.IGNORECASE)
TOOLING_PR = re.compile(
    r"Tooling PR: (https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*) additions=([0-9]+) deletions=([0-9]+) removed=(yes|no)"
)
TOOLING_SAMPLE_SIZE = 20


class DigestError(ValueError):
    pass


class SummaryTooLarge(DigestError):
    pass


@dataclass
class Presentation:
    content: str
    attachment: str | None = None


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parse(report: str) -> tuple[str, str, list[dict]]:
    lines = report.splitlines()
    if (len(lines) < 3 or lines[0] != "Daily stranded-work sweep"
            or not lines[1].startswith("Observed: ") or not lines[2].startswith("Coverage: ")):
        raise DigestError("digest_report_invalid")
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
            raise DigestError("digest_report_invalid")
    count = re.search(r"; (\d+) candidates\.$", lines[2])
    if not count or int(count[1]) != len(rows):
        raise DigestError("digest_report_invalid")
    return lines[1].removeprefix("Observed: ")[:10], lines[2].split(";", 1)[0].removeprefix("Coverage: "), rows


def label(text: str, length: int) -> str:
    text = " ".join(text.split())
    text = re.sub(r"[\[\]()*`<>@]", "", text)
    return text if len(text) <= length else text[:length - 1] + "…"


def tooling_measure(report: str) -> str:
    unavailable = next((line for line in report.splitlines() if line.startswith("Tooling PRs: unavailable;")), None)
    if unavailable:
        return unavailable
    evidence = [TOOLING_PR.fullmatch(line) for line in report.splitlines() if line.startswith("Tooling PR:")]
    if (len(evidence) != TOOLING_SAMPLE_SIZE or not all(evidence)
            or len({row[1].casefold() for row in evidence if row}) != TOOLING_SAMPLE_SIZE):
        return "Tooling PRs: unavailable; need 20 distinct tooling PR evidence rows."
    qualifying = sum(int(row[2]) <= int(row[3]) and row[4] == "yes" for row in evidence if row)
    return f"Tooling PRs: {qualifying}/{len(evidence)} ({qualifying / len(evidence):.0%}); net lines <= 0 and Removed:."


def header(report: str, date: str, coverage: str) -> list[str]:
    return [f"Daily digest · {date} · {coverage}", tooling_measure(report)]


def summary(report: str, full_list_url: str) -> str:
    date, coverage, rows = parse(report)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["kind"]].append(row)
    for group in groups.values():
        # Known oldest events first; unknown ages cannot outrank a known event.
        group.sort(key=lambda r: (r["age"] is None, -(r["age"] or 0), r["url"]))

    for name_length in (72, 48, 32, 20):
        lines = header(report, date, coverage)
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
    raise SummaryTooLarge("digest_summary_too_large")


def presentation(report: str, full_list_url: str) -> Presentation:
    try:
        return Presentation(summary(report, full_list_url))
    except SummaryTooLarge:
        date, coverage, rows = parse(report)
        counts: dict[str, int] = defaultdict(int)
        for row in rows:
            counts[row["kind"]] += 1
        lines = header(report, date, coverage)
        lines.extend(f"{kind}: {count}" for kind, count in sorted(counts.items()))
        waiting = sum(row["person"] for row in rows)
        lines.append(f"Waiting on Chris: {waiting} (all waits in attached full report)" if waiting else "Waiting on Chris: none")
        lines.append(f"[Full list](<{full_list_url}>)")
        content = "\n".join(lines)
        if units(content) >= 2000:
            raise SummaryTooLarge("digest_summary_too_large") from None
        return Presentation(content, report)


def archive(report: str, tracking_issue: str) -> str:
    """Use the existing bot comment transport, including uncertain-write recovery."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
    import github_comment

    target = re.fullmatch(r"([\w.-]+/[\w.-]+)#([0-9]+)", tracking_issue)
    if not target:
        raise DigestError("digest_tracking_issue_invalid")
    try:
        result = github_comment.comment("issue", int(target[2]), report, repo=target[1], dedupe_body=True)
    except github_comment.CommentError:
        raise DigestError("digest_archive_failed") from None
    url = result.get("url")
    if not isinstance(url, str) or not re.fullmatch(
            rf"https://github\.com/{re.escape(target[1])}/issues/{target[2]}#issuecomment-[0-9]+", url):
        raise DigestError("digest_archive_response_invalid")
    return url
