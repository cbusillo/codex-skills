# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ownership evidence for the planning claim command; no GitHub writes."""

from __future__ import annotations

from datetime import datetime
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any


MARKER = "github-plan:claim "
EMPTY_HOLDERS = {"none", "unassigned", "unclaimed", "not assigned", "n/a", "-", "nobody", "no one", "no-one"}


def no_wait_reason(reason: str, *, field: str) -> bool:
    """Recognize explicit absence of a wait without discarding wait clauses."""
    reason = reason.strip().casefold().rstrip(" .")
    if reason in {"none", "n/a", "nothing", "-", "no native issue blocker"}:
        return True
    if field == "Waiting for" and re.fullmatch(
        r"(?:none|nothing|n/a)[;.]\s*(?:(?:the )?next actor is an agent|an agent acts next|this is agent work)",
        reason,
    ):
        return True
    # Apart from the exact agent-next forms above, only Blocked by may contain
    # an explanatory sentence. Other semicolons and wrapped holds still refuse.
    if re.search(r"\b(?:wait(?:ing)?|await(?:ing)?|pending|parked|blocked|until|unless|except|but|after|requires?|needs?)\b", reason):
        return False
    if field == "Blocked by":
        return bool(re.fullmatch(r"(?:none|n/a|nothing|no native issue blocker)\.\s+[^;]+", reason))
    if field == "Waiting for":
        return bool(re.fullmatch(r"nothing for [\w -]+", reason))
    return False


def marker(claim: dict[str, str]) -> str:
    return "<!-- " + MARKER + json.dumps(claim, sort_keys=True) + " -->"


def records(text: str) -> list[dict[str, str]]:
    found = []
    # Format examples and incidental prose are not live ownership records.
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("<!-- " + MARKER)]
    for line in lines:
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


def same_owner(record: dict[str, str], claim: dict[str, str]) -> bool:
    return (all(record.get(key) == claim[key] for key in ("worker", "session", "branch"))
            and all(record.get(key) == claim.get(key)
                    for key in ("refresh_pr", "retained_handoff", "resume_from")))


def without_operation_marker(text: str) -> str:
    """Remove the helper's trailing transport marker, not handoff prose."""
    return re.sub(r"\n\s*<!-- github-skill-operation:[0-9a-f]+ -->\s*$", "", text).rstrip()


def legacy_release_worker(text: str) -> str | None:
    """Only a bare legacy directive releases; handoffs use exact claim IDs."""
    text = without_operation_marker("\n".join(text.splitlines()))
    match = re.fullmatch(r"Released by (\S+)", text)
    return match.group(1) if match else None


def released_claim_id(text: str) -> int | None:
    """Read an exact release directive; handoff narrative is not release state."""
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
    # A colon introduces an example/instruction rather than an authored directive.
    if preceding.endswith(":"):
        return None
    # A final line inside an unclosed code fence or raw HTML block is an example,
    # not a release. Quoted and indented directives never match the exact line.
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


def resumed_status(status: str, comments: list[dict[str, Any]], source_id: int | None) -> str:
    """Discard only an exact status marker superseded by its author's release."""
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    if source_id is None:
        return status
    source = next((c for c in comments if c.get("id") == source_id), None)
    if source is None:
        return status
    source_records = records(source.get("body") or "")
    if len(source_records) != 1 or source_records[0] not in records(status):
        return status
    record = source_records[0]
    author = (source.get("user") or {}).get("login")
    if not author:
        return status
    for release in comments[comments.index(source) + 1:]:
        if ((release.get("user") or {}).get("login") != author
                or released_claim_id(release.get("body") or "") != source_id):
            continue
        try:
            released_at = datetime.fromisoformat(release["created_at"])
            marker_at = datetime.fromisoformat(record["claimed_at"])
            source_at = datetime.fromisoformat(source.get("updated_at") or source["created_at"])
            if released_at <= max(marker_at, source_at):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        # Remove the marker and only its own identity assertions. Remaining
        # markers and unstructured ownership still go through the normal scan.
        status = "\n".join(line for line in status.splitlines()
                           if records(line) != [record])
        status = re.sub(rf"(?im)^\s*State:\s*Active;\s*owned by {re.escape(record['worker'])}\.?\s*$", "State: Active", status)
        for field, key in (("Worker", "worker"), ("Session", "session")):
            status = re.sub(rf"(?im)^\s*{field}:\s*{re.escape(record[key])}\.?\s*$", "", status)
        return status
    return status


def references_issue(text: str, number: int) -> bool:
    return bool(re.search(rf"(?i)(?:\bissue[-_ /#]?|\bgo[-_ ]?|\bagy[-_]|#|/issues/){number}(?!\d)", text)
                or re.search(rf"(?:^|/){number}[-_]", text))


def ownership_text(text: str, *, strip_quotes: bool = True) -> str:
    """Unwrap human-readable Markdown without changing machine directives."""
    text = "\n".join(text.splitlines())
    # Code content is literal Markdown: protect it while unwrapping other spans.
    code: list[str] = []

    def keep_code(match: re.Match[str]) -> str:
        code.append(match.group(2))
        return f"\x00{len(code) - 1}\x00"

    # Block contents are literal too, including tilde and unclosed fences.
    lines = text.splitlines()
    protected = []
    index = 0
    while index < len(lines):
        line = re.sub(r"^[ \t]*(?:>[ \t]?)+", "", lines[index])
        fence = re.match(r" {0,3}(`{3,}|~{3,})", line)
        html = re.match(r" {0,3}<(pre|code|blockquote|details|script|style|textarea)[\s>]", line, re.IGNORECASE)
        if not fence and not html:
            protected.append(lines[index])
            index += 1
            continue
        start = index
        html_end = f"</{html.group(1).casefold()}>" if html else None
        index += 1
        if not html_end or html_end not in line.casefold():
            while index < len(lines):
                closing = re.sub(r"^[ \t]*(?:>[ \t]?)+", "", lines[index])
                index += 1
                if (html_end and html_end in closing.casefold()
                        or fence and re.fullmatch(rf" {{0,3}}{re.escape(fence.group(1)[0])}{{{len(fence.group(1))},}}[ \t]*", closing)):
                    break
        code.append("\n".join(lines[start:index]))
        protected.append(f"\x00{len(code) - 1}\x00")
    text = "\n".join(protected)
    text = re.sub(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", keep_code, text, flags=re.DOTALL)
    # Keep paragraph and list boundaries, including hard-wrapped quoted prose.
    if strip_quotes:
        text = re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", text)
    # Retain link labels; destinations and optional titles are not holder prose.
    destination = r"(?:<[^>\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)"
    title = r'''(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?'''

    def reference_key(label: str) -> str:
        return " ".join(label.split()).casefold()

    reference_destination = r"(?:<[^>\n]*>|[^()\s]+(?:\([^()\s]*\)[^()\s]*)*)"
    definition = re.compile(rf" {{0,3}}\[([^]\n]+)]:[ \t]*{reference_destination}{title}[ \t]*")
    references: set[str] = set()
    lines = []
    block_start = True
    for line in text.splitlines():
        definition_match = definition.fullmatch(line) if block_start else None
        if definition_match:
            references.add(reference_key(definition_match.group(1)))
            lines.append("")
        else:
            lines.append(line)
            block_start = not line.strip()
    text = "\n".join(lines)
    text = re.sub(rf"\[([^][]*)]\([ \t]*{destination}{title}[ \t]*\)", r"\1", text)
    text = re.sub(r"\[([^][]+)]\[([^]\n]*)]",
                  lambda match: match.group(1) if reference_key(match.group(2) or match.group(1)) in references
                  else match.group(), text)
    text = re.sub(r"\[([^][]+)]",
                  lambda match: match.group(1) if reference_key(match.group(1)) in references else match.group(), text)
    # Paired delimiters only: underscores inside identity tokens are literal.
    # Repetition handles nested emphasis and links inside emphasized spans.
    emphasis = r"(?<!\w)(\*{1,3}|_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)"
    while True:
        unwrapped = re.sub(emphasis, r"\2", text, flags=re.DOTALL)
        if unwrapped == text:
            break
        text = unwrapped
    return re.sub(r"\x00(\d+)\x00", lambda match: code[int(match.group(1))], text)


def has_ownership_assertion(text: str, *, own_claim: dict[str, str] | None = None) -> bool:
    """Ignore explicit absence only at the assertion, never a whole status."""
    text = "\n".join(text.splitlines())
    if own_claim:
        # Match complete identity lines before splitting prose punctuation:
        # worker/session tokens may themselves contain dots or semicolons.
        for assertion, key in ((r"(?:State: Active;\s*)?(?:owned by|claimed by|Worker:)", "worker"),
                               (r"Session:", "session")):
            text = re.sub(rf"(?im)^\s*{assertion}\s+{re.escape(own_claim[key])}\.?[ \t]*$", "", text)
    text = ownership_text(text)
    # Exclude only resource prose with separately scoped evidence and approval
    # responsibilities, after formatting has been normalized.
    text = re.sub(
        r"(?im)^\s*(?:After these proposals,\s+)?"
        r"(?:Remaining |\d+ [\w-]+ and \d+ [\w-]+ )?(?:provider-only )?"
        r"(?:entries|records|resources)\b(?:\s+(?:would remain,|are))?\s+"
        r"owned by (?:(?!\b(?:owned|claimed) by\b)[\w -])+ for evidence and "
        r"(?:(?!\b(?:owned|claimed) by\b)[\w -])+ for (?:production )?disposition approval"
        r"(?:\.(?=\s|$)|(?=\n|$))",
        "", text,
    )
    # Structured markers were checked separately. Preserve logical paragraphs
    # when prose is hard-wrapped, while fields and list items stay independent.
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("<!-- " + MARKER))
    text = re.sub(r"(?i)(?<!\n)\n(?![ \t]*(?:\n|[-*+]\s|(?:re)?(?:owned|claimed) by\b|"
                  r"[*_`]*\w[\w /-]*:[*_`]*(?=[ \t]|$)|<!--|>))", " ", text)
    boundary = r"[;!?\n]|\.(?=\s|$)"
    subject = (
        r"(?:timing|(?:code )?changes?|(?:not-planned )?closures?|"
        r"(?:implementation |execution )?ownership|implementation|work|"
        r"(?:executing )?workers?|claims?|(?:this |the )?(?:issue|task))"
    )
    negative_subject = (
        rf"(?:no {subject}(?:\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+){subject})*"
        r"\s+(?:is|are|was|were|has been|have been)|no (?:implementation |execution )?ownership)\s*"
    )
    for match in re.finditer(r"owned by|claimed by|\bworker\s*:|\bsession\s*:", text, re.IGNORECASE):
        raw_prefix = re.split(boundary, text[:match.start()])[-1]
        raw_prefix = raw_prefix.lstrip(" -*+").rstrip()
        prefix = re.sub(r"^[^:]+:\s*", "", raw_prefix)
        ending = re.split(rf"({boundary})", text[match.end():], maxsplit=1)
        suffix = ending[0].strip()
        # Conditional or contrastive denials are not proof that nobody holds it.
        uncertain = (
            len(ending) > 1 and ending[1] == "?"
            or re.search(r"\b(?:if|unless|until|except|but|however|instead|rather|whether|"
                         r"other than|besides|apart from|save for)\b", raw_prefix + " " + suffix, re.IGNORECASE)
            or (prefix.casefold() != "no longer" and re.match(r"no\b", prefix, re.IGNORECASE)
                and re.search(r"\b(?:not|never|no longer)\s*$", prefix, re.IGNORECASE))
        )
        if not uncertain:
            empty_holder = suffix.lstrip(": ").casefold()
            if empty_holder in EMPTY_HOLDERS:
                continue
            if not match.group().rstrip().endswith(":"):
                negated = (
                    re.search(r"\b(?:not|never)(?:\s+(?:yet|currently))?(?:\s+been)?\s*$|"
                              r"\b(?:is|are|was|were|has|have|had)n['’]t(?:\s+been)?\s*$|\b(?:no longer|un|dis)\s*$",
                              prefix, re.IGNORECASE)
                    or re.fullmatch(negative_subject, prefix, re.IGNORECASE)
                )
                # A denial may name its subject, but trailing narrative can
                # describe a handoff or another holder without our keywords.
                if negated and re.fullmatch(r"(?:(?:this|the|a|an|any|another|current)\s+)?"
                                            r"[\w.-]+(?:\s+(?:worker|session|agent|sweep|pass|run|audit))?", suffix, re.IGNORECASE):
                    continue
        return True
    return False


def exact_released_sources(comments: list[dict[str, Any]]) -> list[tuple[int, dict[str, str], int]]:
    """Bind history to one authored, timestamp-proved structured release."""
    from github_plan_release import stamp
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


def released_status_lines(status: str, comments: list[dict[str, Any]]) -> dict[str, str]:
    """Share the exact historical-field match with live-session checks."""
    return {
        line: record["session"]
        for _, record, _ in exact_released_sources(comments)
        for line in status.splitlines()
        if line.rstrip(" \t") == (
            f"Session: {record['worker']} / {record['session']}; "
            "executing claim released in the closeout comment."
        )
    }


def current_ownership_text(
    status: str, comments: list[dict[str, Any]], *, repo: str | None, number: int | None,
    issue_url: str | None = None,
) -> str:
    """Remove only the demonstrated historical and other-issue assertions."""
    if repo is None or number is None:
        return status
    # This sentence quotes a replaced field, not a present Session field.
    status = re.sub(
        r'(The previous )"Session: [\w.-]+"'
        r'( line was stale and made the next claim refuse as ambiguous ownership '
        r'\([^\n]+\); this status replaces it\.)', r'\1previous holder\2', status,
    )
    released_lines = released_status_lines(status, comments)
    status = "\n".join(line for line in status.splitlines() if line not in released_lines)
    identities = {(repo.casefold(), number)}
    canonical = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)", issue_url or "")
    if canonical:
        identities.add((canonical.group(1).casefold(), int(canonical.group(2))))

    def other_issue(match: re.Match[str]) -> str:
        if (match.group(1).casefold(), int(match.group(2))) in identities:
            return match.group()
        return match.group().replace(match.group(3), "")

    status = re.sub(
        r"(?m)^Blocked by: https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+) "
        r"\((actively owned by [\w.-]+)\)(?=\.(?:\s|$)|\s*$)", other_issue, status,
    )
    return status


def released_explanatory_note(
    text: str, index: int, comments: list[dict[str, Any]],
) -> bool:
    """An exact source reference accounts for that session's earlier note only."""
    from github_plan_release import receipt_for, related_sessions_match, stamp
    header = re.match(r"Claimed by (\S+); structured claim #(\d+) was confirmed and read back\.", text)
    if not header:
        return False
    for source_index, record, release_index in exact_released_sources(comments):
        source, note, release = comments[source_index], comments[index], comments[release_index]
        if (not source_index < index < release_index or source["id"] != int(header.group(2))
                or record["worker"] != header.group(1)
                or (note.get("user") or {}).get("login") != (source.get("user") or {}).get("login")
                or not related_sessions_match(text, record["session"])):
            continue
        try:
            receipt = receipt_for(release)
            cutoff = stamp(release.get("created_at"))
            if receipt is not None:
                cutoff = min(cutoff, stamp(receipt["evidence_at"]))
            if not max(stamp(source.get("created_at")), stamp(source.get("updated_at") or source.get("created_at")),
                       stamp(record["claimed_at"])) < stamp(note.get("created_at")) <= stamp(
                note.get("updated_at") or note.get("created_at")
            ) < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        # A shared bot and worker alias cannot attribute a note across sessions.
        same_worker = [r for c in comments[:release_index] for r in records(c.get("body") or "")
                       if r["worker"] == record["worker"]]
        if any(r["session"] != record["session"] for r in same_worker):
            continue
        if not has_ownership_assertion(text[header.end():], own_claim=record):
            return True
    return False


def discussion_evidence(
    status: str, comments: list[dict[str, Any]], claim: dict[str, str], *, resume_from: int | None = None,
    repo: str | None = None, number: int | None = None, issue_url: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Old claims remain ambiguous until explicitly released; age is never a lease."""
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    conflicts: list[dict[str, Any]] = []
    owned = []
    status = resumed_status(status, comments, resume_from)
    status_records = records(status)
    for record in status_records:
        if same_owner(record, claim):
            owned.append(record)
        else:
            conflicts.append({"source": "current_status", "record": record})
    ownership_status = current_ownership_text(status, comments, repo=repo, number=number, issue_url=issue_url)
    # A matching marker or complete legacy identity accounts only for that
    # owner's assertions; it must not hide a second holder in the same status.
    own_claim = claim if owned or all(claim[key] in status for key in ("worker", "session", "branch")) else None
    if owned:
        # The helper copies these intent/history lines from its claim comment.
        # Ignore only exact recorded lines, never an arbitrary Next action or
        # another assertion added beside the matching marker.
        intent_lines = {
            line for comment in comments
            if any(same_owner(record, claim) for record in records(comment.get("body") or ""))
            for line in (comment.get("body") or "").splitlines()
            if line.startswith(("Next action: ", "Wait resolution: "))
        }
        ownership_status = "\n".join(line for line in ownership_status.splitlines() if line not in intent_lines)
    if has_ownership_assertion(ownership_status, own_claim=own_claim):
        conflicts.append({"source": "current_status", "text": status, "certainty": "ambiguous"})
    released: dict[tuple[str, str], int] = {}
    legacy_released: dict[tuple[str, str], int] = {}
    released_ids: dict[tuple[int, str], int] = {}
    for index, comment in enumerate(comments):
        worker = legacy_release_worker(comment.get("body") or "")
        if worker is not None:
            author = (comment.get("user") or {}).get("login", "")
            legacy_worker = ownership_text(worker, strip_quotes=False)
            prior_sessions: set[str | int] = set()
            for prior_index, prior in enumerate(comments[:index]):
                if (prior.get("user") or {}).get("login", "") != author:
                    continue
                prior_text = prior.get("body") or ""
                parsed_prior = records(prior_text)
                prior_sessions.update(
                    record["session"] for record in parsed_prior
                    if ownership_text(record["worker"], strip_quotes=False) == legacy_worker
                )
                if not parsed_prior:
                    prior_prose = ownership_text(prior_text, strip_quotes=False)
                    header = re.match(r"Claimed by:?\s+(\S+)", prior_prose, re.IGNORECASE)
                    if header and header.group(1) == legacy_worker and has_ownership_assertion(prior_text):
                        sessions = [
                            session.strip() for session in re.findall(r"(?im)\bSession:[ \t]*([^\n]+)", prior_prose)
                            if session.strip() and session.strip().casefold().strip("().,;<> \t")
                            not in EMPTY_HOLDERS | {"unknown", "tbd", "session", "session-id", "not recorded",
                                                   "unavailable", "pending", "?", "—", "–"}
                        ]
                        # Missing identity cannot establish that two claims are
                        # the same session; exact comment IDs remain recoverable.
                        prior_sessions.update(sessions or [prior_index])
            # A reused token cannot release a different native session.
            if len(prior_sessions) <= 1:
                released[worker, author] = index
                legacy_released[legacy_worker, author] = index
        release_id = released_claim_id(comment.get("body") or "")
        if release_id is not None:
            author = (comment.get("user") or {}).get("login", "")
            released_ids[release_id, author] = index
    for index, comment in enumerate(comments):
        text = comment.get("body") or ""
        parsed = records(text)
        author = (comment.get("user") or {}).get("login", "")
        comment_id = comment.get("id")
        if comment_id is not None and released_ids.get((comment_id, author), -1) > index:
            continue
        # A quoted historical header is not a claim by the quoting author.
        prose = ownership_text(text, strip_quotes=False)
        legacy = re.match(r"Claimed by:?\s+(\S+)", prose, re.IGNORECASE)
        if legacy and not parsed and has_ownership_assertion(text):
            if released_explanatory_note(text, index, comments):
                continue
            worker = legacy.group(1)
            if legacy_released.get((worker, author), -1) > index:
                continue
            if (worker != claim["worker"] or any(claim[key] not in text for key in ("session", "branch"))
                    or has_ownership_assertion(text, own_claim=claim)):
                conflicts.append({"source": "comment", "id": comment.get("id"), "text": text,
                                  "certainty": "current_or_stale"})
            else:
                owned.append({**claim, "claimed_at": comment.get("created_at") or claim["claimed_at"], "_legacy": "yes"})
        for record in parsed:
            if released.get((record["worker"], author), -1) > index:
                continue
            if same_owner(record, claim):
                owned.append(record)
            else:
                conflicts.append({"source": "comment", "id": comment.get("id"), "record": record,
                                  "certainty": "current_or_stale"})
    return conflicts, owned


def run_read(argv: list[str], *, cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(f"Ownership inventory failed: {argv[0]} {argv[1]} (exit {result.returncode})")
    return result.stdout


def local_inventory(repo: str, number: int, *, cwd: pathlib.Path | None = None) -> dict[str, Any]:
    remote = run_read(["git", "remote", "get-url", "origin"], cwd=cwd).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
        raise ValueError("Run claim from a checkout of the target repository")
    worktrees = []
    for block in run_read(["git", "worktree", "list", "--porcelain"], cwd=cwd).strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        path = fields.get("worktree", "")
        worktrees.append({"branch": branch, "path": path})
    local = run_read(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=cwd).splitlines()
    remote_branches = [
        line.split("\t", 1)[1].removeprefix("refs/heads/")
        for line in run_read(["git", "ls-remote", "--heads", "origin"], cwd=cwd).splitlines()
    ]
    sessions: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {"codex": {"status": "unavailable", "reason": "no CLI peer inventory"}}
    if shutil.which("claude"):
        try:
            sessions = json.loads(run_read(["claude", "agents", "--json"], cwd=pathlib.Path.home()))
            if not isinstance(sessions, list) or any(
                not isinstance(s, dict) or not isinstance(s.get("sessionId"), str)
                or not isinstance(s.get("cwd"), str) for s in sessions
            ):
                raise ValueError("Invalid Claude session inventory")
            coverage["claude"] = {"status": "available", "source": "claude agents --json",
                                  "scope": "native local active-session registry"}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            coverage["claude"] = {"status": "unavailable", "reason": str(exc)}
            sessions = []
    else:
        coverage["claude"] = {"status": "unavailable", "reason": "Claude CLI not installed"}
    return {"worktrees": worktrees, "local_branches": local, "remote_branches": remote_branches,
            "sessions": sessions, "session_coverage": coverage, "issue": number}


def retained_branch(comments: list[dict[str, Any]], comment_id: int) -> str:
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    original = next((c for c in comments if c.get("id") == comment_id), None)
    if original is None:
        raise ValueError("Resume source claim comment is missing")
    parsed = records(original.get("body") or "")
    if len(parsed) != 1:
        raise ValueError("Resume source must contain one structured claim record")
    author = (original.get("user") or {}).get("login")
    if not author:
        raise ValueError("Resume source author is unavailable")
    source_index = comments.index(original)
    for comment in comments[source_index + 1:]:
        if (comment.get("user") or {}).get("login") != author:
            continue
        if (released_claim_id(comment.get("body") or "") == comment_id
                or legacy_release_worker(comment.get("body") or "") == parsed[0]["worker"]):
            return parsed[0]["branch"]
    raise ValueError("Resume source claim has not been released by its author")


def handoff_pr_numbers(text: str, *, issue_repo: str, target_repo: str) -> set[int]:
    # Machine receipts describe checks, not the Supervisor's visible PR handoff.
    text = "\n".join(line for line in text.splitlines()
                     if not line.startswith("<!-- github-plan:abandoned-release "))
    qualified = rf"(?:https://github\.com/{re.escape(target_repo)}/pull/|(?<![\w/]){re.escape(target_repo)}#)([1-9]\d*)(?!\d)"
    numbers = {int(m.group(1)) for m in re.finditer(qualified, text, re.IGNORECASE)}
    if issue_repo.casefold() == target_repo.casefold():
        numbers.update(int(m.group(1)) for m in re.finditer(r"(?<![\w/#])#([1-9]\d*)(?!\d)", text))
    return numbers


def retained_handoff(
    comments: list[dict[str, Any]], source_id: int, handoff_id: int,
    pulls: list[dict[str, Any]], target_number: int | None, *, issue_repo: str,
    issue_number: int, target_repo: str,
) -> set[str]:
    """Bind retained PR identities to the exact author-released handoff."""
    source_branch = retained_branch(comments, source_id)
    source = next(c for c in comments if c.get("id") == source_id)
    author = source["user"]["login"]
    handoff = next((c for c in comments if c.get("id") == handoff_id), None)
    if handoff is None or (handoff.get("user") or {}).get("login") != author:
        raise ValueError("Refresh handoff must be a comment by the source claim author")
    source_index, handoff_index = comments.index(source), comments.index(handoff)
    if handoff_index <= source_index or not any(
        (c.get("user") or {}).get("login") == author
        and released_claim_id(c.get("body") or "") == source_id
        for c in comments[source_index + 1:handoff_index + 1]
    ):
        raise ValueError("Refresh requires an exact source-claim release before or in the handoff")

    handoff_text = handoff.get("body") or ""
    source_record = records(source["body"])[0]
    source_claim_match = re.search(rf"\bclaim:? {source_id}(?!\d)", handoff_text)
    source_session_match = re.search(rf"(?<![\w-]){re.escape(source_record['session'])}(?![\w-])", handoff_text)
    standalone = (handoff_text.splitlines()[:1] == [f"Handoff from {source_record['worker']}"]
                  and source_claim_match and source_session_match
                  and not records(handoff_text))
    named = handoff_pr_numbers(handoff_text, issue_repo=issue_repo, target_repo=target_repo)
    # A PR-only refresh can bind directly to the released source's own branch.
    # Split branches and ordinary successor work still need the explicit
    # session-bearing handoff. Never reinterpret a malformed canonical header.
    source_pr_refresh = (target_number is not None
                         and not re.match(r"(?i)^\s*Handoff\s+from\b", handoff_text)
                         and not records(handoff_text)
                         and any(pull["number"] == target_number
                                 and pull["number"] in named
                                 and pull.get("state") == "open"
                                 and (pull.get("head") or {}).get("ref") == source_branch
                                 for pull in pulls))
    # An embedded release proves release, not the identity of a split handoff.
    first_line_release = released_claim_id(handoff_text.splitlines()[0] if handoff_text else "")
    if not (first_line_release == source_id or standalone or source_pr_refresh):
        raise ValueError("Refresh handoff must identify the exact released source claim")
    # The exact authored release above owns release state. The separate handoff
    # binds source identity and retained PRs, not natural-language conditions.
    direct_source_refresh = source_pr_refresh and not (first_line_release == source_id or standalone)
    permitted = {source_branch}
    target_found = False
    attested = False
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref", "")
        if pull["number"] not in named:
            continue
        if direct_source_refresh and branch != source_branch:
            continue
        if pull.get("state") != "open" and not (pull.get("state") == "closed" and pull.get("merged_at")):
            continue
        if (pull.get("user") or {}).get("login") != author:
            continue
        if ((pull.get("head") or {}).get("repo") or {}).get("full_name", "").casefold() != target_repo.casefold():
            continue
        if ((pull.get("base") or {}).get("repo") or {}).get("full_name", "").casefold() != target_repo.casefold():
            continue
        # Classify only the issue reference, independently of branch evidence
        # or lifecycle state (open/merged admission was checked above).
        if not pr_issue_reference(pull, issue_number, repo=issue_repo,
                                  local_references=issue_repo.casefold() == target_repo.casefold(), attestation=True):
            continue
        permitted.add(branch)
        attested = True
        target_found |= pull["number"] == target_number and pull.get("state") == "open"
    if target_number is not None and not target_found:
        raise ValueError("Refresh target must be an open same-repository PR linked to the issue and attested in the released handoff")
    if target_number is None and not attested:
        raise ValueError("Retained handoff must attest a same-repository open or merged PR linked to the issue")
    return permitted


def recorded_claim_branches(status: str, comments: list[dict[str, Any]]) -> set[str]:
    """A released claim leaves its PR branch as artifact evidence."""
    return {record["branch"] for text in [status, *(c.get("body") or "" for c in comments)]
            for record in records(text)}


def pr_issue_reference(pull: dict[str, Any], number: int, *, repo: str,
                       local_references: bool = True, attestation: bool = False) -> bool:
    """Ownership needs implementation evidence; named handoffs may attest links."""
    title = pull.get("title") or ""
    body = pull.get("body") or ""
    issue_url = rf"https://github\.com/{re.escape(repo)}/issues/{number}"
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
    # Preserve the existing independent-link rule for source-authored retained
    # handoffs, including exclusion of explicitly unstarted follow-up links.
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


def artifact_evidence(
    inventory: dict[str, Any], pulls: list[dict[str, Any]], number: int,
    claim: dict[str, str], *, own_record: bool, retained: str | None = None, repo: str = "",
    retained_branches: set[str] | None = None, retained_repo: str | None = None,
    inventory_repo: str | None = None,
    recorded_branches: set[str] | None = None,
    status_sessions: set[str] | None = None,
) -> list[dict[str, Any]]:
    conflicts = []
    local_references = inventory_repo is None or inventory_repo.casefold() == repo.casefold()
    def permitted(candidate_branch: str) -> bool:
        return (own_record and candidate_branch == claim["branch"]) or candidate_branch == retained or candidate_branch in (retained_branches or set())

    for source in ("local_branches", "remote_branches"):
        for branch in inventory[source]:
            if local_references and references_issue(branch, number) and not permitted(branch):
                conflicts.append({"source": source, "branch": branch, "certainty": "current_or_stale"})
    for tree in inventory["worktrees"]:
        if local_references and references_issue(tree["branch"] + "/" + pathlib.Path(tree["path"]).name, number):
            if not permitted(tree["branch"]):
                conflicts.append({"source": "worktree", **tree, "certainty": "current_or_stale"})
    paths = {str(pathlib.Path(t["path"]).resolve()) for t in inventory["worktrees"]}
    retained_paths = {str(pathlib.Path(t["path"]).resolve()) for t in inventory["worktrees"]
                      if t["branch"] == retained or t["branch"] in (retained_branches or set())}
    for session in inventory["sessions"]:
        if session.get("sessionId") == claim["session"]:
            continue
        cwd = str(pathlib.Path(session.get("cwd") or "/").resolve())
        if (session.get("sessionId") in (status_sessions or set())
                or any(cwd == path or pathlib.Path(cwd).is_relative_to(path) for path in retained_paths)
                or (local_references and cwd in paths and references_issue((session.get("name") or "") + "/" + pathlib.Path(cwd).name, number))):
            conflicts.append({"source": "claude_session", "session": session.get("sessionId"),
                              "state": session.get("state") or session.get("status")})
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref", "")
        if (pr_issue_reference(pull, number, repo=repo, local_references=local_references)
                or branch in (recorded_branches or set())
                or (local_references and references_issue(branch, number))):
            if pull.get("state") == "closed":
                # Closed PRs are not open ownership evidence. Preserve any
                # unaccounted local/remote artifacts even for nonnumeric names.
                present = branch in inventory["local_branches"] or branch in inventory["remote_branches"] or any(
                    tree["branch"] == branch for tree in inventory["worktrees"]
                )
                if present and not permitted(branch):
                    conflicts.append({"source": "closed_pr_artifacts", "number": pull["number"], "branch": branch})
                continue
            same_repo = not retained_repo or all(
                ((pull.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() == retained_repo.casefold()
                for side in ("head", "base")
            )
            if not permitted(branch) or not same_repo:
                conflicts.append({"source": "open_pr", "number": pull["number"], "branch": branch})
    return conflicts


# The kinds of work the overall Order admits; a claim names one with --admission.
ADMISSIONS = ("live-breakage", "milestone", "repeat-stop", "spare-capacity")
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/([1-9]\d*)")


def admission_track(
    start: tuple[str, int], owner: str, *, read_issue: Any, read_parent: Any, read_blocking: Any,
    max_nodes: int = 50,
) -> tuple[dict[str, Any] | None, bool]:
    """The milestone Track this issue reaches through parents and the issues it blocks.

    Returns the Track and whether the walk was complete. A Track is an issue
    titled `Track:` with a milestone in OWNER/direction, the graph the weekly
    audit counts as milestone work.
    """
    direction = f"{owner}/direction".casefold()
    pending = [start]
    seen: set[tuple[str, int]] = set()
    while pending:
        repo, number = pending.pop(0)
        key = (repo.casefold(), number)
        if key in seen:
            continue
        if len(seen) >= max_nodes:
            return None, False
        seen.add(key)
        issue = read_issue(repo, number)
        milestone = (issue.get("milestone") or {}).get("title")
        if key[0] == direction and str(issue.get("title") or "").startswith("Track:") and milestone:
            return {"url": f"https://github.com/{repo}/issues/{number}", "title": issue.get("title"),
                    "milestone": milestone}, True
        for linked in [read_parent(repo, number), *read_blocking(repo, number)]:
            match = ISSUE_URL.fullmatch(str((linked or {}).get("html_url") or ""))
            if match:
                pending.append((match[1], int(match[2])))
    return None, True


def admission_text(kind: str, links: list[str], *, track: dict[str, Any] | None = None, unlinked: str | None = None) -> str:
    """The claim comment's admission line."""
    detail = []
    if track:
        detail.append(f"reaches {track['url']} ({track['title']}, milestone `{track['milestone']}`)")
    if unlinked:
        detail.append(f"not linked into a milestone Track graph: {unlinked}")
    if links:
        detail.append(", ".join(links))
    return f"Admission: {kind}" + ("; " + "; ".join(detail) if detail else "")
