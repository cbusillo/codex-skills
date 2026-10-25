# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Evidence-backed release of automation claims from closed sessions."""
from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from collections.abc import Callable
import json
import pathlib
import re
from typing import Any

import github_plan_claim as claim
import github_plan_termination as termination

COMMENT_URL = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")
RELEASE_MARKER = "github-plan:abandoned-release "
PENDING_MARKER = "github-plan:pending-release "
ISSUE_REFUSAL_SCOPE = "issue"


class IssueReleaseRefusal(ValueError):
    """This issue's retained work conflicts; closure of the source is still checked separately."""


def related_sessions_match(text: str, session: str) -> bool:
    """Compare explicit session fields outside fenced examples."""
    prose = claim.ownership_text(text, strip_quotes=False)
    fence = None
    for line in prose.splitlines():
        line = re.sub(r"^\s*(?:>\s*)+", "", line)
        opener = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line):
                fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        fields = (
            r"\b(?:Native[ \t_-]*)?(?:Session(?:[ \t_-]*ID)?|Thread[ \t_-]*ID)\b"
            r"(?:[ \t]*[:=][ \t]*|[ \t]+[—-][ \t]+)([^\s,;|]+)"
            r"|\|[ \t]*(?:Session(?:[ \t_-]*ID)?|Thread[ \t_-]*ID)[ \t]*\|[ \t]*([^\s|]+)"
        )
        for field in re.finditer(fields, line, re.IGNORECASE):
            value = (field.group(1) or field.group(2)).strip("()<>")
            if not re.fullmatch(rf"{re.escape(session)}\.?", value):
                return False
    return True


def stamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Release evidence requires timezone-qualified timestamps")
    return parsed


def source_record(source: dict[str, Any], *, legacy: bool = False,
                  legacy_branch: str | None = None, legacy_reason: str | None = None) -> dict[str, str]:
    parsed = claim.records(source.get("body") or "")
    if legacy:
        # Only the old bare claim format lacks identity. Never reinterpret a
        # structured, malformed structured, or session-bearing source as legacy.
        text = re.sub(r"<!-- github-skill-operation:[^<>]* -->", "", source.get("body") or "").strip()
        header = re.fullmatch(r"Claimed by ([^\s<>]+)", text)
        if parsed or header is None:
            raise ValueError("Legacy release requires a bare unstructured Claimed by worker comment")
        if not legacy_branch or re.search(r"\s|[<>]", legacy_branch):
            raise ValueError("Legacy release requires the verified original source branch")
        if not isinstance(legacy_reason, str) or not legacy_reason.strip() or any(c in legacy_reason for c in "\n\r"):
            raise ValueError("Legacy release requires why no native session ID exists")
        return {"worker": header[1], "session": f"legacy-claim-{source['id']}",
                "branch": legacy_branch, "claimed_at": source["created_at"],
                "legacy_reason": legacy_reason}
    if legacy_branch is not None or legacy_reason is not None:
        raise ValueError("Legacy identity arguments require --legacy")
    if len(parsed) != 1:
        raise ValueError("Abandoned release requires one structured source claim")
    return parsed[0]


def record_from_receipt(source: dict[str, Any], record: dict[str, str]) -> dict[str, str]:
    return source_record(source, legacy="legacy_reason" in record,
                         legacy_branch=record.get("branch") if "legacy_reason" in record else None,
                         legacy_reason=record.get("legacy_reason"))


def evidence_header(source_id: int, record: dict[str, str]) -> str:
    return f"Closed legacy claim {source_id}" if "legacy_reason" in record else f"Closed session {record['session']}"


def _check_activity(comments, source, cutoff, record):
    source_at = stamp(source.get("updated_at") or source.get("created_at"))
    if max(source_at, stamp(record["claimed_at"])) >= cutoff:
        raise ValueError("Source claim changed after the cited session-ended evidence")
    for comment in comments:
        if comment.get("id") == source.get("id"):
            continue
        text = comment.get("body") or ""
        # Shared bot identity is not session identity. Only explicit ownership
        # activity can be attributed to this native session.
        parsed = claim.records(text)
        matching = any(r["session"] == record["session"] or
                       ("legacy_reason" in record and r["worker"] == record["worker"])
                       for r in parsed)
        if "legacy_reason" in record:
            matching |= bool(re.match(rf"Claimed by:?\s+{re.escape(record['worker'])}(?:\s|$)",
                                      claim.ownership_text(text, strip_quotes=False), re.IGNORECASE))
        elif not parsed:
            # Structured claims carry session, worker and branch identity;
            # their header alone cannot attribute activity to the source.
            matching |= bool(re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", text))
        if matching and stamp(comment.get("updated_at") or comment.get("created_at")) >= cutoff:
            raise ValueError("Source session shows ownership activity after the cited evidence")


def check_retained_pr(pull, head_commit, *, repo, number, actor, ended, subject_pr=None, source_branch=None, allow_closed_unmerged=False):
    """One retained-work guard shared by write preflight and read-only planning."""
    subject = subject_pr is not None and pull.get("number") == subject_pr
    if (not subject or (pull.get("head") or {}).get("ref") == source_branch) and stamp(head_commit["commit"]["committer"]["date"]) >= stamp(ended):
        raise ValueError("Retained PR has commit activity after the cited session-ended evidence")
    if subject:
        if (pull.get("state") not in {"open", "closed"}
                or any(((pull.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() != repo.casefold()
                       for side in ("head", "base"))):
            raise IssueReleaseRefusal("PR claim's work record must remain a same-repository open or closed PR")
        return
    # A closed PR from an unrelated task can share a reused branch name.
    # Its commit activity was checked above; it is not this claim's handoff.
    if (allow_closed_unmerged and pull.get("state") == "closed"
            and all(((pull.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() == repo.casefold()
                    for side in ("head", "base"))
            and not claim.pr_issue_reference(pull, number, repo=repo, attestation=True)):
        return
    if ((pull.get("user") or {}).get("login") != actor
            or pull.get("state") != "open" and not pull.get("merged_at") and not (allow_closed_unmerged and pull.get("state") == "closed")
            or any(((pull.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() != repo.casefold() for side in ("head", "base"))
            or not claim.pr_issue_reference(pull, number, repo=repo, attestation=True, record_kind="pr" if subject_pr is not None else "issue")):
        raise IssueReleaseRefusal("Retained PR must independently link the canonical issue and belong to the source automation identity")


@dataclass(frozen=True)
class ReleaseFacts:
    """Source and closure facts parsed once within one fresh mutation phase."""
    source: dict[str, Any]
    evidence: dict[str, Any]
    record: dict[str, str]
    cutoff: datetime
    terminated: dict[str, Any] | None
    actor: str
    role: str
    releaser_session: str


def parse_release(source: dict[str, Any], evidence: dict[str, Any], *, actor: str,
                  role: str, releaser_session: str, legacy: bool = False,
                  legacy_branch: str | None = None, legacy_reason: str | None = None) -> ReleaseFacts:
    """Validate exact source identity and closure before reading retained work."""
    record = source_record(source, legacy=legacy, legacy_branch=legacy_branch, legacy_reason=legacy_reason)
    if not actor or (source.get("user") or {}).get("login") != actor:
        raise ValueError("Release must use the source claim's configured automation identity")
    if role not in {"supervisor", "direction"} or not releaser_session or releaser_session == record["session"]:
        raise ValueError("Abandoned release requires a distinct direction or Supervisor native session")
    if (evidence.get("user") or {}).get("login") != actor:
        raise ValueError("Session-ended evidence must be authored by the configured automation identity")
    text = evidence.get("body") or ""
    terminated = termination.evidence(text, record["session"])
    if terminated is not None and terminated["session_id"] == releaser_session:
        raise ValueError("Abandoned release requires a distinct native source session")
    if terminated is None and (text.splitlines()[:1] != [evidence_header(source["id"], record)] or "Safe to exit: yes" not in text.splitlines()):
        raise ValueError("Evidence must be a closed-session attestation naming the exact native session and Safe to exit: yes")
    if legacy and (f"Worker: {record['worker']}" not in text.splitlines()
                   or f"Source branch: {record['branch']}" not in text.splitlines()
                   or f"Native session unavailable: {record['legacy_reason']}" not in text.splitlines()):
        raise ValueError("Legacy attestation must verify the worker, source branch and missing native identity reason")
    ended = re.findall(r"(?m)^Ended at: (\S+)$", text)
    if len(ended) != 1:
        raise ValueError("Closed-session attestation must record the verified Ended at timestamp")
    cutoff = stamp(ended[0])
    if terminated is not None and (stamp(terminated["ended_at"]) != cutoff
            or stamp(terminated["checked_at"]) > stamp(evidence["created_at"])):
        raise ValueError("Termination checks must precede the attestation and match its Ended at time")
    if cutoff > stamp(evidence.get("created_at")):
        raise ValueError("Session-ended timestamp is newer than its attestation")
    return ReleaseFacts(source, evidence, record, cutoff, terminated, actor, role, releaser_session)


def worker_named_pull(facts: ReleaseFacts, comments: list[dict[str, Any]], number: int, *, repo: str) -> bool:
    worker = rf"(?<![\w-]){re.escape(facts.record['worker'])}(?![\w-])"
    pull = rf"(?:(?<![\w/#&])#{number}|https://github\.com/{re.escape(repo)}/pull/{number})(?!\d)"
    later = comments[comments.index(facts.source) + 1:]
    return any((comment.get("user") or {}).get("login") == facts.actor
               and stamp(comment.get("updated_at") or comment.get("created_at")) < facts.cutoff
               and re.search(worker, body := comment.get("body") or "")
               and re.search(pull, body, re.IGNORECASE)
               for comment in later)


def check_related(record: dict[str, str], comment: dict[str, Any], actor: str, cutoff: datetime) -> None:
    """Bind an unstructured follow-up to its source worker, native ID and closure."""
    if ((comment.get("user") or {}).get("login") != actor or claim.records(comment.get("body") or "")
            or not re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", comment.get("body") or "")):
        raise ValueError("Related release must name an unstructured ownership follow-up of this exact source worker")
    if not related_sessions_match(comment.get("body") or "", record["session"]):
        raise ValueError("Related ownership follow-up names a different native session; obtain that session's own closure evidence")
    if stamp(comment.get("updated_at") or comment.get("created_at")) >= cutoff:
        raise ValueError("Related ownership follow-up is newer than the session-ended evidence")


def _observed_branches(inventory):
    return (set(inventory["local_branches"]) | set(inventory["remote_branches"])
            | {tree["branch"] for tree in inventory["worktrees"]})


def validate_source_release(facts: ReleaseFacts, comments: list[dict[str, Any]], inventory: dict[str, Any], *,
                            retained_prs: list[str], related: list[dict[str, Any]]) -> set[pathlib.Path]:
    """Check source activity and all known source/artifact paths before PR reads."""
    record, cutoff, terminated = facts.record, facts.cutoff, facts.terminated
    legacy = "legacy_reason" in record
    if legacy and (related or retained_prs):
        raise ValueError("Legacy releases do not support related releases or structured retained-PR handoffs")
    _check_activity(comments, facts.source, cutoff, record)
    known_paths = {pathlib.Path(tree["path"]).resolve() for tree in inventory["worktrees"]
                   if tree["branch"] == record["branch"]}
    if terminated is not None:
        if record["branch"] in _observed_branches(inventory) and record["branch"] not in {a["branch"] for a in terminated["retained_artifacts"]}:
            raise ValueError("Termination evidence must retain the claimed source branch")
        artifact_paths = {pathlib.Path(a["worktree"]).resolve() for a in terminated["retained_artifacts"] if a.get("worktree") is not None}
        if not known_paths.issubset(artifact_paths):
            raise ValueError("Termination evidence must name every preserved source/retained worktree")
        known_paths.update(artifact_paths)
    for comment in related:
        check_related(record, comment, facts.actor, cutoff)
    return known_paths


def validate_retained_release(facts: ReleaseFacts, inventory: dict[str, Any], *,
                              retained_branches: set[str], checked_paths: set[pathlib.Path]) -> None:
    """Check only newly discovered retained paths, after PR metadata is available."""
    paths = {pathlib.Path(tree["path"]).resolve() for tree in inventory["worktrees"]
             if tree["branch"] in retained_branches} - checked_paths
    terminated = facts.terminated
    if terminated is not None:
        known_branches = _observed_branches(inventory)
        observed_branches = known_branches | retained_branches
        artifacts = terminated["retained_artifacts"]
        if (facts.record["branch"] not in known_branches and facts.record["branch"] in retained_branches
                and facts.record["branch"] not in {a["branch"] for a in artifacts}):
            raise ValueError("Termination evidence must retain the claimed source branch")
        if any(a["branch"] not in observed_branches for a in artifacts if a.get("worktree") is None):
            raise ValueError("Branch-only retained artifact is absent from repository inventory")
        artifact_paths = {pathlib.Path(a["worktree"]).resolve() for a in artifacts if a.get("worktree") is not None}
        if not paths.issubset(artifact_paths):
            raise ValueError("Termination evidence must name every preserved source/retained worktree")


def validate_release(facts: ReleaseFacts, comments: list[dict[str, Any]], inventory: dict[str, Any], *,
                     retained_prs: list[str], related: list[dict[str, Any]],
                     retained_branches: set[str] | None = None) -> None:
    """Compatibility validation when every retained branch is already available."""
    checked_paths = validate_source_release(facts, comments, inventory, retained_prs=retained_prs, related=related)
    validate_retained_release(facts, inventory, retained_branches=retained_branches or set(), checked_paths=checked_paths)


def render_release(facts: ReleaseFacts, *, evidence_url: str, retained_prs: list[str],
                   related: list[dict[str, Any]], checked_prs: list[str] | None = None) -> str:
    """Render only already validated facts; this performs no reads or validation."""
    source, evidence, record = facts.source, facts.evidence, facts.record
    cutoff, terminated, role = facts.cutoff, facts.terminated, facts.role
    releaser_session = facts.releaser_session
    receipt = {"source_id": source["id"], "source": record, "source_updated_at": source.get("updated_at") or source["created_at"],
               "evidence_url": evidence_url, "evidence_at": cutoff.isoformat(),
               "evidence_updated_at": evidence.get("updated_at") or evidence["created_at"], "role": role,
               "releaser_session": releaser_session, "retained_prs": retained_prs,
               "checked_prs": checked_prs or [],
               "related_ids": [c["id"] for c in related]}
    if terminated is not None:
        receipt["termination"] = terminated
    return (f"Released claim {source['id']}\n\n"
            f"The {role} reconciles the next action on the work record.\n"
            f"[Session-ended evidence]({evidence_url})\n"
            + ("Retained PR handoff: " + ", ".join(retained_prs) + "\n" if retained_prs else "")
            + ("Retained artifacts (resume; preserve uncommitted work):\n"
               + ("\n".join(f"- {a.get('worktree') or 'no local worktree'} (branch {a['branch']})" for a in terminated["retained_artifacts"]) or "None; no source branch or worktree was created.")
               + "\n" if terminated is not None else "")
            + "\n<!-- " + RELEASE_MARKER + json.dumps(receipt, sort_keys=True).replace(">", "\\u003e") + " -->")


def prepare_release(
    source: dict[str, Any], evidence: dict[str, Any], comments: list[dict[str, Any]],
    inventory: dict[str, Any], *, actor: str, role: str, releaser_session: str,
    evidence_url: str, retained_prs: list[str], related: list[dict[str, Any]],
    checked_prs: list[str] | None = None, retained_branches: set[str] | None = None,
    legacy: bool = False, legacy_branch: str | None = None, legacy_reason: str | None = None,
) -> str:
    """Compatibility composition for callers that already have all phase inputs."""
    facts = parse_release(source, evidence, actor=actor, role=role, releaser_session=releaser_session,
        legacy=legacy, legacy_branch=legacy_branch, legacy_reason=legacy_reason)
    validate_release(facts, comments, inventory, retained_prs=retained_prs, related=related,
                     retained_branches=retained_branches)
    return render_release(facts, evidence_url=evidence_url, retained_prs=retained_prs,
                          related=related, checked_prs=checked_prs)


def pending_body(body: str) -> str:
    """Stage preservation evidence without an operative release or receipt."""
    pending = body.replace("Released claim ", "Pending release of claim ", 1).replace(
        "<!-- " + RELEASE_MARKER, "<!-- " + PENDING_MARKER)
    if receipt_for(pending_comment := {"body": pending}) is not None or claim.released_claim_id(pending):
        raise ValueError("Pending release must contain no operative release authority")
    if pending_receipt_for(pending_comment) != receipt_for({"body": body}):
        raise ValueError("Pending release must preserve its exact proposed receipt")
    return pending


def pending_receipt_for(comment: dict[str, Any]) -> dict[str, Any] | None:
    """Read staging evidence for recovery only; this is never release authority."""
    body = comment.get("body") or ""
    header = re.match(r"Pending release of claim ([1-9]\d*)\n", body)
    if header is None:
        return None
    proposed = body.replace(header[0], f"Released claim {header[1]}\n", 1).replace(
        "<!-- " + PENDING_MARKER, "<!-- " + RELEASE_MARKER)
    receipt = receipt_for({"body": proposed})
    return receipt if receipt is not None and receipt["source_id"] == int(header[1]) else None


def receipt_for(comment: dict[str, Any]) -> dict[str, Any] | None:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", comment.get("body") or "")
    lines = [line for line in text.splitlines() if line.startswith("<!-- " + RELEASE_MARKER)]
    if len(lines) != 1 or not lines[0].endswith(" -->"):
        return None
    try:
        receipt = json.loads(lines[0][len("<!-- " + RELEASE_MARKER):-len(" -->")])
        if not isinstance(receipt, dict) or not all(key in receipt for key in (
            "source_id", "source", "evidence_url", "evidence_at", "evidence_updated_at", "role", "releaser_session", "checked_prs")):
            return None
        if (type(receipt["source_id"]) is not int or not isinstance(receipt["source"], dict)
                or not all(isinstance(receipt["source"].get(k), str) and receipt["source"][k] for k in ("worker", "session", "branch", "claimed_at"))
                or receipt["role"] not in {"supervisor", "direction"} or not receipt["releaser_session"]
                or receipt["releaser_session"] == receipt["source"]["session"]
                or not isinstance(receipt["evidence_url"], str)
                or not isinstance(receipt["checked_prs"], list)
                or not all(isinstance(url, str) for url in receipt["checked_prs"])):
            return None
        if "legacy_reason" in receipt["source"] and (
                not isinstance(receipt["source"]["legacy_reason"], str)
                or not receipt["source"]["legacy_reason"].strip()):
            return None
        stamp(receipt["evidence_at"])
        stamp(receipt["evidence_updated_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return receipt


def receipts(comments: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(c, r) for c in comments if (r := receipt_for(c)) is not None]


def without_release(comment: dict[str, Any]) -> dict[str, Any]:
    # Keep any ownership assertions: an invalid receipt is not a release,
    # rather than a permanent error or a way to hide a structured claim.
    return {**comment, "body": "\n".join(line for line in (comment.get("body") or "").splitlines()
        if not line.startswith("Released claim ") and not line.startswith("<!-- " + RELEASE_MARKER))}


def validate_evidence(receipt: dict[str, Any], evidence: dict[str, Any], author: str) -> None:
    text = evidence.get("body") or ""
    record = receipt["source"]
    if "legacy_reason" in record and any(line not in text.splitlines() for line in (
            f"Worker: {record['worker']}", f"Source branch: {record['branch']}",
            f"Native session unavailable: {record['legacy_reason']}")):
        raise ValueError("Legacy evidence no longer attests this source identity")
    ended = re.findall(r"(?m)^Ended at: (\S+)$", text)
    terminated = termination.evidence(text, record["session"], apply_policy=False)
    if terminated != receipt.get("termination"):
        raise ValueError("Termination evidence no longer matches the release")
    if ((evidence.get("user") or {}).get("login") != author
            or (terminated is None and (text.splitlines()[:1] != [evidence_header(receipt["source_id"], receipt["source"])]
                                       or "Safe to exit: yes" not in text.splitlines()))
            or len(ended) != 1 or stamp(ended[0]) != stamp(receipt["evidence_at"])
            or stamp(evidence.get("updated_at") or evidence.get("created_at")) != stamp(receipt["evidence_updated_at"])):
        raise ValueError("Closed-session evidence changed or no longer attests this release")


def exact_source_release(source_id: int, candidate: dict[str, Any], comments: list[dict[str, Any]]) -> bool:
    """Apply the shared exact-release guard to one canonical candidate pair."""
    pair = [c for c in comments if c.get("id") in {source_id, candidate["id"]}]
    return len(pair) == 2 and bool(claim.exact_released_sources(pair))


@dataclass(frozen=True)
class _ReleasedSourceContext:
    source_id: int
    author: str
    source: dict[str, str]
    ended_at: datetime

    @property
    def session(self) -> str:
        return self.source["session"]


def _released_source_contexts(comments: list[dict[str, Any]]) -> tuple[_ReleasedSourceContext, ...]:
    """Select only confirmed canonical authority, never a worker's posting time."""
    comments = effective_comments(comments)
    result = []
    for released, receipt in receipts(comments):
        if ("_release_evidence" not in released or "legacy_reason" in receipt["source"]
                or receipt.get("termination") is not None
                or not exact_source_release(receipt["source_id"], released, comments)):
            continue
        source = next(c for c in comments if c.get("id") == receipt["source_id"])
        result.append(_ReleasedSourceContext(source["id"], source["user"]["login"],
                                            receipt["source"], stamp(receipt["evidence_at"])))
    return tuple(result)


def followup_release(source: dict[str, Any], comments: list[dict[str, Any]], *, actor: str,
                     session: str, ended: datetime) -> dict[str, Any] | None:
    """Consume an authored exact release using the producer's relationship facts.

    Worker releases precede their own end. A later release needs an earlier
    validated parent receipt selecting this follow-up through related_ids;
    that receipt may attest a later end than the verified one, as its producer
    checked activity through that time. Explanatory prose and the hand claim's
    position confer no authority.
    """
    if (source.get("user") or {}).get("login") != actor or claim.records(source.get("body") or ""):
        return None
    index = comments.index(source)
    for release_index in range(index + 1, len(comments)):
        released = comments[release_index]
        if ((released.get("user") or {}).get("login") != actor
                or claim.released_claim_id(released.get("body") or "") != source["id"]
                or not related_sessions_match(released.get("body") or "", session)):
            continue
        if stamp(released["created_at"]) <= stamp(source.get("updated_at") or source["created_at"]):
            continue
        if stamp(released.get("updated_at") or released["created_at"]) <= ended:
            return released
        prior = comments[:release_index]
        for confirmed, receipt in receipts(prior):
            related_ids = receipt.get("related_ids", [])
            if ("_release_evidence" not in confirmed
                    or (confirmed.get("user") or {}).get("login") != actor
                    or receipt["source"]["session"] != session
                    or not isinstance(related_ids, list) or source["id"] not in related_ids):
                continue
            # Check this canonical source/receipt pair independently: an older
            # ordinary release of the same source must not shadow its receipt.
            if not exact_source_release(receipt["source_id"], confirmed, prior):
                continue
            try:
                check_related(receipt["source"], source, actor, stamp(receipt["evidence_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            return released
    return None


def verified_comments(comments: list[dict[str, Any]],
                      read_evidence: Callable[[str, int, int], dict[str, Any] | None]) -> list[dict[str, Any]]:
    """Validate one canonical discussion phase; a missing evidence read returns None.

    Readers translate only 403/404 to None. Transport, page and shape failures
    propagate. This cache lives only for this phase, including invalid evidence.
    """
    if not isinstance(comments, list) or any(not isinstance(c, dict) for c in comments):
        raise ValueError("canonical discussion must contain comment objects")
    comments = effective_comments([{k: v for k, v in c.items() if k != "_release_evidence"} for c in comments])
    cache = {}
    for release_comment, receipt in receipts(comments):
        match = COMMENT_URL.fullmatch(receipt["evidence_url"])
        evidence, valid = None, False
        if match:
            repo, number, identifier = match.groups()
            if receipt["evidence_url"] not in cache:
                cache[receipt["evidence_url"]] = read_evidence(repo, int(number), int(identifier))
            evidence = cache[receipt["evidence_url"]]
            if evidence is not None:
                if (not isinstance(evidence, dict) or type(evidence.get("id")) is not int
                        or not isinstance(evidence.get("body"), str)
                        or not isinstance(evidence.get("user"), dict)
                        or not isinstance(evidence.get("created_at"), str)
                        or not isinstance(evidence.get("issue_url"), str)):
                    raise ValueError("canonical release evidence has an invalid comment shape")
                try:
                    if (evidence["id"] != int(identifier)
                            or evidence["issue_url"] != f"https://api.github.com/repos/{repo}/issues/{number}"):
                        raise ValueError("release evidence belongs to another canonical comment")
                    validate_evidence(receipt, evidence, (release_comment.get("user") or {}).get("login", ""))
                    valid = True
                except (KeyError, TypeError, ValueError):
                    pass
        comments = [({**c, "_release_evidence": evidence} if valid else without_release(c))
                    if c.get("id") == release_comment.get("id") else c for c in comments]
    return comments


def effective_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invalid receipt facts restore ownership within a well-formed discussion.

    This does not convert malformed canonical API shapes or failed evidence
    reads into missing records; verified_comments propagates those failures.
    """
    result = []
    for comment in comments:
        text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", comment.get("body") or "")
        if not any(line.startswith("<!-- " + RELEASE_MARKER) for line in text.splitlines()):
            result.append(comment)
            continue
        receipt = receipt_for(comment)
        try:
            if receipt is None:
                raise ValueError("Invalid receipt")
            source = next((c for c in comments if c.get("id") == receipt["source_id"]), None)
            if (source is None or record_from_receipt(source, receipt["source"]) != receipt["source"]
                    or not (source.get("user") or {}).get("login")
                    or (source.get("user") or {}).get("login") != (comment.get("user") or {}).get("login")
                    or claim.released_claim_id(comment.get("body") or "") != source["id"]):
                raise ValueError("Source no longer matches receipt")
            _check_activity(comments, source, stamp(receipt["evidence_at"]), receipt["source"])
        except (KeyError, TypeError, ValueError):
            comment = without_release(comment)
        result.append(comment)
    return result
