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
import subprocess
import time
from typing import Any, Callable

try:
    from github_plan_status.references import pr_issue_reference as pr_issue_reference
except ModuleNotFoundError:
    from skills.github.scripts.github_plan_status.references import pr_issue_reference as pr_issue_reference


MARKER = "github-plan:claim "
EMPTY_HOLDERS = {"none", "unassigned", "unclaimed", "not assigned", "n/a", "-", "nobody", "no one", "no-one"}


class ClaimProofError(ValueError):
    """A source-proof refusal with a stable machine-readable reason."""

    def __init__(self, code: str, message: str, *, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def word_pattern(*words: str) -> str:
    return r"\b(?:" + "|".join(words) + r")\b"


# Words that make a clause a precondition, not a later step or a note. A lead
# clause may say "None before <step>"; a bare "None" note also keeps approvals.
PRECONDITION_WORDS = ("but", "except", "unless", "until", "if", "once", "after", "however", "pending",
                      "still", "then", "must", r"wait\w*", r"await\w*", "blocked", "parked",
                      r"requir\w*", r"need\w*", r"approv\w*", r"prerequisite\w*",
                      "outstanding", r"sign\w*[\s-]+off")
LEAD_HOLD_WORDS = word_pattern(*PRECONDITION_WORDS)
LATER_HOLD_WORDS = word_pattern(*PRECONDITION_WORDS, "before", "first")
HOLD_WORDS = word_pattern(*PRECONDITION_WORDS, "before", "first", r"authoriz\w*", "decision", r"accept\w*", "hold", "held", "gated", "blocker")


def no_wait_reason(reason: str, *, field: str) -> bool:
    """Recognize explicit absence of a wait without discarding wait clauses."""
    reason = reason.strip().casefold().rstrip(" .")
    if reason in {"none", "n/a", "nothing", "-", "no native issue blocker"}:
        return True
    # Parse the explicit absence separately: "blocker" in the no-native-blocker
    # prefix is not a hold, but the same word in its explanation is.
    explanation = None
    if field == "Blocked by":
        explanation = re.fullmatch(
            r"(?:(?:none|n/a|nothing)[.;]\s*|no native issue blocker(?:[.;]\s*|\s+))(.+)", reason,
        )
    elif field == "Waiting for":
        explanation = re.fullmatch(r"(?:none|nothing|n/a)[;.]\s*(.+)|nothing for ([\w -]+)", reason)
    if not explanation:
        return False
    note = next(group for group in explanation.groups() if group is not None)
    if re.search(HOLD_WORDS, note) or re.search(
        r"\b(?:review|depend\w*|clarification|director|owner|client)\b", note,
    ):
        return False
    # Check each action's subject, rather than allowing an agent mentioned in
    # a later clause to hide the person who must act first.
    for action in re.finditer(
        r"(?:^|[;.,]\s*)(?:(?:the )?next actor is ([^;.,]+)|"
        r"([^;.,]+?)\s+(?:acts next\b|answers?\b|to\s+[a-z][\w-]*)|"
        r"([^;.,]+?)['’]s\s+(?:answer|go-ahead)\b)", note,
    ):
        actor = next(group for group in action.groups() if group is not None)
        if not re.fullmatch(r"(?:(?:an?|the|executing|next)\s+)*(?:agent|supervisor|engineering)", actor):
            return False
    return True


def marker(claim: dict[str, str]) -> str:
    return "<!-- " + MARKER + json.dumps(claim, sort_keys=True).replace(">", "\\u003e") + " -->"


def live_lines(text: str) -> list[str]:
    # Format examples and incidental prose are not live ownership records.
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
        status = "\n".join(line for line in status.splitlines() if records(line) != [record])
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


def human_session_reference(text: str, position: int) -> bool:
    if not re.match(r"session[ \t]*:", text[position:], re.IGNORECASE):
        return False
    prefix = text[:position]
    return bool(re.search(
        r"\b\w+[ \t]+(?:administrator|browser|human)(?:[ \t]+|[ \t]*\n[ \t]*)\Z", prefix, re.IGNORECASE,
    )) and not bool(re.search(
        r"\b(?:Claimed by:?|Worker:|Owner:)\s+\S+\s+$", prefix, re.IGNORECASE,
    ))


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
        if human_session_reference(text, match.start()):
            continue
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


def released_successor_route(
    conflicts: list[dict[str, Any]], comments: list[dict[str, Any]], *, issue_repo: str,
) -> int | None:
    """Name the one released claim whose authored release retains every conflict.

    This is a diagnostic only. An explicit resume rechecks the entire source.
    """
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    candidates = set()
    for source_index, record, release_index in exact_released_sources(comments):
        source, release = comments[source_index], comments[release_index]
        body = release.get("body") or ""
        if released_claim_id(body.splitlines()[0]) != source["id"]:
            continue
        named = handoff_pr_numbers(body, issue_repo=issue_repo, target_repo=issue_repo)
        tied = [c for c in conflicts if c.get("source") == "open_pr" and c.get("number") in named
                or c.get("source") == "current_status" and c.get("record") == record]
        # Unstructured status text and branch artifacts are rechecked by the
        # successor route; any other holder keeps the original refusal.
        untied = [c for c in conflicts if c not in tied and not (
            c.get("source") == "current_status" and "text" in c
            or c.get("source") in {"local_branches", "remote_branches", "worktree"})]
        if named and tied and not untied:
            candidates.add(source["id"])
    return next(iter(candidates)) if len(candidates) == 1 else None


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


def attested_handoff_note(text: str, index: int, comments: list[dict[str, Any]]) -> bool:
    """A released session's closure evidence that names this exact note as its handoff accounts for it."""
    from github_plan_release import COMMENT_URL, receipt_for, related_sessions_match, stamp, validate_evidence
    header = re.match(r"Claimed by:?\s+\S+", text, re.IGNORECASE)
    if not header:
        return False
    note = comments[index]
    for source_index, record, release_index in exact_released_sources(comments):
        source, release = comments[source_index], comments[release_index]
        # Only a Supervisor or direction receipt carries closure evidence.
        receipt = receipt_for(release)
        author = (source.get("user") or {}).get("login")
        if (receipt is None or not source_index < index < release_index
                or (note.get("user") or {}).get("login") != author
                or not related_sessions_match(text, record["session"])):
            continue
        # Claim reads attach evidence fetched from any issue; otherwise use this issue's copy.
        evidence_ids = {int(m.group(3)) for m in COMMENT_URL.finditer(receipt["evidence_url"])}
        evidence = release.get("_release_evidence") or next((c for c in comments if c.get("id") in evidence_ids), None)
        if evidence is None or evidence.get("id") not in evidence_ids:
            continue
        try:
            validate_evidence(receipt, evidence, (release.get("user") or {}).get("login", ""))
        except (KeyError, ValueError, TypeError):
            continue
        handoffs = {int(m.group(3)) for line in (evidence.get("body") or "").splitlines()
                    if re.match(r"Handoff(?: context \([^)\n]*\))?: ", line)
                    for m in COMMENT_URL.finditer(line)}
        if note.get("id") not in handoffs:
            continue
        try:
            if not max(stamp(source.get("updated_at") or source.get("created_at")), stamp(record["claimed_at"])) < stamp(
                    note.get("created_at")) <= stamp(note.get("updated_at") or note.get("created_at")) < stamp(receipt["evidence_at"]):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if not has_ownership_assertion(text[header.end():], own_claim=record):
            return True
    return False


def discussion_evidence(
    status: str, comments: list[dict[str, Any]], claim: dict[str, str], *, resume_from: int | None = None,
    ended_claims: set[int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    conflicts: list[dict[str, Any]] = []
    # A legacy source has no native identity. Reusing its worker would look
    # like renewal and invalidate the release after posting the successor.
    from github_plan_release import receipts
    for release_comment, receipt in receipts(comments):
        if "legacy_reason" in receipt["source"] and re.fullmatch(re.escape(receipt["source"]["worker"]), claim["worker"], re.IGNORECASE):
            conflicts.append({"source": "legacy_worker_reuse", "id": release_comment["id"],
                              "worker": claim["worker"], "next_action": "Use a fresh worker token"})
    owned = []
    status = resumed_status(status, comments, resume_from)
    status_records = records(status)
    ended_records = [record for c in comments if c.get("id") in (ended_claims or set())
                     for record in records(c.get("body") or "")]
    for record in status_records:
        if record in ended_records:
            continue
        if same_owner(record, claim):
            owned.append(record)
        else:
            conflicts.append({"source": "current_status", "record": record})
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
        if comment.get("id") in (ended_claims or set()):
            continue
        parsed = records(text)
        author = (comment.get("user") or {}).get("login", "")
        comment_id = comment.get("id")
        if comment_id is not None and released_ids.get((comment_id, author), -1) > index:
            continue
        # A quoted historical header is not a claim by the quoting author.
        prose = ownership_text(text, strip_quotes=False)
        legacy = re.match(r"Claimed by:?\s+(\S+)", prose, re.IGNORECASE)
        if legacy and not parsed and has_ownership_assertion(text):
            if released_explanatory_note(text, index, comments) or attested_handoff_note(text, index, comments):
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


def expired_lease_sessions(comments: list[dict[str, Any]]) -> set[str]:
    import github_plan_lease
    released = {comments[i]["id"] for i, _, _ in exact_released_sources(comments)}
    sessions = {r["session"] for c in comments if c.get("id") not in released for r in records(c.get("body") or "")}
    return {session for session in sessions if github_plan_lease.state(session) == github_plan_lease.EXPIRED}


def ended_ownership(comments: list[dict[str, Any]], inventory: dict[str, Any],
                    expired_sessions: set[str] | frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    released = {comments[i]["id"]: comments[j]["id"] for i, _, j in exact_released_sources(comments)}
    ended = []
    for source in comments:
        parsed = records(source.get("body") or "")
        if len(parsed) != 1 or not isinstance(source.get("id"), int):
            continue
        record = parsed[0]
        if source["id"] in released:
            evidence = {"evidence": "authored_release", "release_id": released[source["id"]]}
        elif record["session"] in expired_sessions:
            evidence = {"evidence": "lease_expired"}
        else:
            continue
        paths = [pathlib.Path(t["path"]).resolve() for t in inventory["worktrees"] if t["branch"] == record["branch"]]
        ended.append({"claim_id": source["id"], **record, **evidence, "worktrees": [str(path) for path in paths]})
    return ended


def lease_takeovers(ended: list[dict[str, Any]]) -> str:
    return "".join(f"Lease takeover: claim {entry['claim_id']} (session {entry['session']}) had no recorded activity "
                   f"for {recorded_lease_minutes(entry['session']):g} minutes.\n\n"
                   for entry in ended if entry["evidence"] == "lease_expired")


def recorded_lease_minutes(session: str) -> float:
    import github_plan_lease
    lease = github_plan_lease.read(session)
    return github_plan_lease.DEFAULT_LEASE_MINUTES if lease is None else lease.minutes


def coordination(conflicts: list[dict[str, Any]], comments: list[dict[str, Any]], repo: str, number: int) -> list[dict[str, Any]]:
    def next_action(record):
        import github_plan_lease
        if github_plan_lease.state(record["session"]) == github_plan_lease.UNKNOWN:
            return ("This host has no lease for that session. Coordinate with it; if it has ended, a Supervisor or "
                    "direction session releases it with gh-plan.py release-claim. Preserve its artifacts")
        return ("Coordinate with this session or wait for its exact release; an unreleased claim frees itself "
                f"{recorded_lease_minutes(record['session']):g} minutes after the session's last activity. Preserve its artifacts")

    holders = []
    released_ids = {comments[i]["id"] for i, _, _ in exact_released_sources(comments)}
    for source in comments:
        for record in records(source.get("body") or ""):
            matching = [c for c in conflicts if c.get("id") == source.get("id") or c.get("record") == record
                        or c.get("session") == record["session"] or (
                            c.get("branch") == record["branch"] and source.get("id") not in released_ids)]
            if matching:
                holders.append({**record, "claim_url": handoff_url(repo, number, source.get("id")),
                                "next_action": next_action(record)})
    if not holders:
        holders = [{"worker": "unknown", "session": c.get("session", "unknown"),
                    "branch": c.get("branch", "unknown"), "work_record": f"https://github.com/{repo}/issues/{number}",
                    "next_action": "Identify the artifact's owning session on the work record and coordinate a handoff; preserve it"}
                   for c in conflicts]
    return holders


def ended_artifact_branches(ended: list[dict[str, Any]], comments: list[dict[str, Any]], pulls: list[dict[str, Any]]) -> set[str]:
    ended_ids = {entry["claim_id"] for entry in ended}
    branches = {entry["branch"] for entry in ended}
    for source in comments:
        for record in records(source.get("body") or ""):
            if source.get("id") not in ended_ids:
                branches.discard(record["branch"])
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref")
        authors = {(c.get("user") or {}).get("login") for c in comments
                   if c.get("id") in ended_ids and any(r["branch"] == branch for r in records(c.get("body") or ""))}
        if pull.get("state") != "closed" or (pull.get("user") or {}).get("login") not in authors:
            branches.discard(branch)
    return branches


def run_read(argv: list[str], *, cwd: pathlib.Path | None = None, deadline_at: float | None = None) -> str:
    timeout = min(30, max(0, deadline_at - time.time())) if deadline_at is not None else 30
    if timeout <= 0:
        raise subprocess.TimeoutExpired(argv, timeout)
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f"Ownership inventory failed: {argv[0]} {argv[1]} (exit {result.returncode})")
    return result.stdout


def local_inventory(repo: str, number: int, *, cwd: pathlib.Path | None = None,
                    deadline_at: float | None = None) -> dict[str, Any]:
    deadline_options = {"deadline_at": deadline_at} if deadline_at is not None else {}
    remote = run_read(["git", "remote", "get-url", "origin"], cwd=cwd, **deadline_options).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
        raise ValueError("Run claim from a checkout of the target repository")
    worktrees = []
    for block in run_read(["git", "worktree", "list", "--porcelain"], cwd=cwd, **deadline_options).strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        path = fields.get("worktree", "")
        worktrees.append({"branch": branch, "path": path})
    local = run_read(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=cwd, **deadline_options).splitlines()
    remote_branches = [
        line.split("\t", 1)[1].removeprefix("refs/heads/")
        for line in run_read(["git", "ls-remote", "--heads", "origin"], cwd=cwd, **deadline_options).splitlines()
    ]
    return {"worktrees": worktrees, "local_branches": local, "remote_branches": remote_branches, "issue": number}


def released_source(comments: list[dict[str, Any]], comment_id: int) -> dict[str, Any]:
    """Resolve one authored release without changing route-specific chronology."""
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    original = next((c for c in comments if c.get("id") == comment_id), None)
    if original is None:
        raise ClaimProofError("claim_resume_source_missing", "Resume source claim comment is missing")
    parsed = records(original.get("body") or "")
    if len(parsed) != 1:
        raise ClaimProofError("claim_resume_source_invalid", "Resume source must contain one structured claim record")
    author = (original.get("user") or {}).get("login")
    if not author:
        raise ClaimProofError("claim_resume_author_missing", "Resume source author is unavailable")
    source_index = comments.index(original)
    for comment in comments[source_index + 1:]:
        if (comment.get("user") or {}).get("login") != author:
            continue
        if (released_claim_id(comment.get("body") or "") == comment_id
                or legacy_release_worker(comment.get("body") or "") == parsed[0]["worker"]):
            return {"comment": original, "record": parsed[0], "author": author, "release": comment}
    raise ClaimProofError("claim_resume_source_unreleased", "Resume source claim has not been released by its author")


def handoff_pr_numbers(text: str, *, issue_repo: str, target_repo: str) -> set[int]:
    # Machine receipts describe checks, not the Supervisor's visible PR handoff.
    text = "\n".join(line for line in text.splitlines()
                     if not line.startswith("<!-- github-plan:abandoned-release "))
    qualified = rf"(?:https://github\.com/{re.escape(target_repo)}/pull/|(?<![\w/]){re.escape(target_repo)}#)([1-9]\d*)(?!\d)"
    numbers = {int(m.group(1)) for m in re.finditer(qualified, text, re.IGNORECASE)}
    if issue_repo.casefold() == target_repo.casefold():
        numbers.update(int(m.group(1)) for m in re.finditer(r"(?<![\w/#])#([1-9]\d*)(?!\d)", text))
        numbers.update(int(m.group(1)) for m in re.finditer(
            r"(?<![\w/#.-])PR([1-9]\d*)(?![\w-])", text, re.IGNORECASE))
    return numbers


def handoff_candidates(comments: list[dict[str, Any]], source: dict[str, Any]) -> list[dict[str, Any]]:
    """Authored exact-source handoffs in canonical discussion order."""
    from github_plan_release import effective_comments
    comments = effective_comments(comments)
    source_id, record, author = source["comment"]["id"], source["record"], source["author"]
    after_source = False
    exact_release_seen = False
    found = []
    for comment in comments:
        if comment.get("id") == source_id:
            after_source = True
            continue
        if not after_source or (comment.get("user") or {}).get("login") != author:
            continue
        text = comment.get("body") or ""
        exact_release_seen |= released_claim_id(text) == source_id
        first_line = text.splitlines()[:1]
        standalone = (first_line == [f"Handoff from {record['worker']}"]
                      and re.search(rf"\bclaim:? {source_id}(?!\d)", text)
                      and re.search(rf"(?<![\w-]){re.escape(record['session'])}(?![\w-])", text)
                      and not records(text))
        if exact_release_seen and (released_claim_id(first_line[0] if first_line else "") == source_id or standalone):
            found.append(comment)
    return found


def handoff_url(repo: str, number: int, comment_id: int) -> str:
    return f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}"


TRAIN_BRANCH_PREFIX = "launchplane/train/"
TRAIN_BATCH_MARKER = "<!-- launchplane-batch:"


def is_train_batch(pull: dict[str, Any]) -> bool:
    head, base = pull.get("head") or {}, pull.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name", "")
    return ((head.get("ref") or "").startswith(TRAIN_BRANCH_PREFIX) and TRAIN_BATCH_MARKER in (pull.get("body") or "")
            and bool(head_repo) and head_repo.casefold() == (base.get("repo") or {}).get("full_name", "").casefold())


def is_retained_work(pull: dict[str, Any], *, author: str, issue_repo: str, issue_number: int, target_repo: str,
                     include_closed_unmerged: bool = False) -> bool:
    if pull.get("state") != "open" and not (pull.get("state") == "closed" and (pull.get("merged_at") or include_closed_unmerged)):
        return False
    if (pull.get("user") or {}).get("login") != author:
        return False
    if any(((pull.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() != target_repo.casefold()
           for side in ("head", "base")):
        return False
    return pr_issue_reference(pull, issue_number, repo=issue_repo,
                              local_references=issue_repo.casefold() == target_repo.casefold(), attestation=True)


def retained_handoff(
    source: dict[str, Any], candidates: list[dict[str, Any]], pulls: list[dict[str, Any]], *,
    issue_repo: str, issue_number: int, target_repo: str, selected_id: int | None = None,
    usable: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any] | None:
    """Prefer the first usable qualified handoff, preserving a selected ID."""
    author = source["author"]
    qualified = [pull for pull in pulls if is_retained_work(pull, author=author, issue_repo=issue_repo,
                                                             issue_number=issue_number, target_repo=target_repo)]
    first = None
    for handoff in candidates:
        if selected_id is not None and handoff.get("id") != selected_id:
            continue
        named = handoff_pr_numbers(handoff.get("body") or "", issue_repo=issue_repo, target_repo=target_repo)
        retained = [pull for pull in qualified if pull["number"] in named]
        if retained:
            permitted = {source["record"]["branch"], *(pull["head"]["ref"] for pull in retained)}
            open_conflicts = [{"source": "open_pr", "number": pull["number"], "branch": pull["head"]["ref"],
                               "certainty": "not_in_source_handoff"}
                              for pull in pulls if pull.get("state") == "open"
                              and (pull.get("head") or {}).get("ref") in permitted
                              and (pull["number"] not in named or (pull.get("user") or {}).get("login") != author)]
            candidate = {"comment": handoff, "named": named, "qualified": retained,
                         "permitted": permitted, "open_conflicts": open_conflicts,
                         "url": handoff_url(issue_repo, issue_number, handoff["id"])}
            if first is None:
                first = candidate
            if not open_conflicts and (usable is None or usable(candidate)):
                return candidate
    if first is not None:
        return first
    if selected_id is not None:
        raise ClaimProofError("claim_handoff_changed", "Selected source handoff no longer qualifies its retained work")
    return None


def recorded_claim_branches(status: str, comments: list[dict[str, Any]]) -> set[str]:
    """A released claim leaves its PR branch as artifact evidence."""
    return {record["branch"] for text in [status, *(c.get("body") or "" for c in comments)]
            for record in records(text)}


def artifact_evidence(
    inventory: dict[str, Any], pulls: list[dict[str, Any]], number: int,
    claim: dict[str, str], *, own_record: bool, retained: str | None = None, repo: str = "",
    retained_branches: set[str] | None = None, retained_repo: str | None = None,
    inventory_repo: str | None = None,
    recorded_branches: set[str] | None = None,
    subject_pr: int | None = None,
    ended_branches: set[str] | None = None,
) -> list[dict[str, Any]]:
    conflicts = []
    local_references = inventory_repo is None or inventory_repo.casefold() == repo.casefold()
    def permitted(candidate_branch: str) -> bool:
        return (own_record and candidate_branch == claim["branch"]) or candidate_branch == retained or candidate_branch in (retained_branches or set())

    subject_branch = None
    if subject_pr is not None:
        subject = next((p for p in pulls if p["number"] == subject_pr), None)
        if subject is None or subject.get("state") != "open":
            raise ValueError("Claim requires the current open PR in the ownership inventory")
        subject_branch = (subject.get("head") or {}).get("ref")
        if not subject_branch:
            raise ValueError("PR source branch is unavailable")
        if any(((subject.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() != repo.casefold()
               for side in ("head", "base")):
            raise ValueError("PR-scoped work requires a same-repository head and base; use its canonical issue for a fork")
        # The PR and its remote head are the work record. Local source artifacts
        # still need owned/released provenance, even with nonnumeric names.
        for branch in inventory["local_branches"]:
            if branch == subject_branch and not permitted(branch):
                conflicts.append({"source": "local_branches", "branch": branch, "certainty": "current_or_stale"})
        for tree in inventory["worktrees"]:
            if tree["branch"] == subject_branch and not permitted(tree["branch"]):
                conflicts.append({"source": "worktree", **tree, "certainty": "current_or_stale"})

    for source in ("local_branches", "remote_branches"):
        for branch in inventory[source]:
            if (local_references and (references_issue(branch, number) or (subject_pr is not None and branch in (recorded_branches or set()))) and not permitted(branch) and branch not in (ended_branches or set())
                    and not (subject_pr is not None and source == "remote_branches" and branch == subject_branch)):
                conflicts.append({"source": source, "branch": branch, "certainty": "current_or_stale"})
    for tree in inventory["worktrees"]:
        if local_references and (references_issue(tree["branch"] + "/" + pathlib.Path(tree["path"]).name, number)
                                or subject_pr is not None and tree["branch"] in (recorded_branches or set())):
            if not permitted(tree["branch"]) and tree["branch"] not in (ended_branches or set()):
                conflicts.append({"source": "worktree", **tree, "certainty": "current_or_stale"})
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref", "")
        if pull["number"] == subject_pr or (is_train_batch(pull) and not permitted(branch)):
            continue
        if (pr_issue_reference(pull, number, repo=repo, local_references=local_references,
                               record_kind="pr" if subject_pr is not None else "issue")
                or branch in (recorded_branches or set())
                or (local_references and references_issue(branch, number))):
            if pull.get("state") == "closed":
                # Closed PRs are not open ownership evidence. Preserve any
                # unaccounted local/remote artifacts even for nonnumeric names.
                present = branch in inventory["local_branches"] or branch in inventory["remote_branches"] or any(
                    tree["branch"] == branch for tree in inventory["worktrees"]
                )
                if present and not permitted(branch) and branch not in (ended_branches or set()):
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
ADMISSIONS = ("live-breakage", "milestone", "own-project", "repeat-stop", "spare-capacity")
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/([1-9]\d*)")


def admission_link_target(link: str) -> tuple[str, int, int | None] | None:
    """The cited issue/PR and comment, keyed as GitHub resolves them, without broadening graph URLs."""
    parent, separator, fragment = link.partition("#")
    comment = re.fullmatch(r"issuecomment-([1-9]\d*)", fragment)
    if separator and not comment:
        return None
    match = ISSUE_URL.fullmatch(parent)
    return (match[1].casefold(), int(match[2]), int(comment[1]) if comment else None) if match else None


def admission_text(kind: str, links: list[str], *, unlinked: str | None = None) -> str:
    """Record the selected Order rule and its evidence; admission grants no authority."""
    detail = [unlinked] if unlinked else []
    if links:
        detail.append(", ".join(links))
    return f"Admission: {kind}" + ("; " + "; ".join(detail) if detail else "")
