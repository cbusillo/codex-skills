# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import base64
import datetime as dt
import pathlib
import re
import json
import subprocess
import sys
import time
from collections import Counter
from urllib.parse import quote
from typing import Any

import direction_audit as audit
import direction_next
from skills.github.scripts.github_plan_status import next_actor
from skills.github.scripts.github_plan_status.discussion import status_field
from skills.direction.scripts.question_id import heading_id
from skills.github.scripts import github_identity, github_plan_wait, github_plan_trigger, github_read, ready_handoff, github_request_usage

# The existing Supervisor helpers use sibling imports when called as scripts.
sys.path.insert(0, str(audit.REPO_ROOT / "skills/supervisor/scripts"))
sys.path.insert(0, str(audit.REPO_ROOT / "skills/github-work-rollup/scripts"))
import oq
import landed_worktrees
import github_unanswered_comments as attention

UTC = dt.timezone.utc
QUESTION = re.compile(r"\bQ[1-9][0-9]*\b")
PREFIXES = oq.QUESTION_PREFIXES
COMMENT_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+#issuecomment-\d+(?!\d)")
SUPERVISOR_SETTLEMENT = re.compile(
    r"^(?:Supervisor answer to (Q[1-9][0-9]*)\b"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*[.:][ \t]*"
    r"(?i:(?:yes|no)(?=[,.:!?]|$)|this is already decided\b)"
    r"|(Q[1-9][0-9]*) withdrawn by the Supervisor\b)"
)


def supervisor_settled_questions(comments: list[dict], trusted: set[str]) -> set[str]:
    settlements: dict[str, list[dt.datetime]] = {}
    for comment in comments:
        if login(comment) not in trusted:
            continue
        heading = question_body(comment).partition("\n")[0].replace("*", "")
        match = SUPERVISOR_SETTLEMENT.match(heading)
        when = stamp(comment.get("created_at"))
        if match and when:
            settlements.setdefault(match[1] or match[2], []).append(when)
    settled = set()
    for comment in comments:
        question = heading_id(question_body(comment))
        asked = stamp(comment.get("created_at"))
        if question and asked and any(when > asked for when in settlements.get(question, [])):
            settled.add(comment.get("html_url") or comment["url"])
    return settled


def is_decision(body: str) -> bool:
    return oq.DECISION_HEADING.match(body) is not None
DUE = re.compile(r"(?im)^\s*(?:[-*] )?\**Due\**:\s*(\d{4}-\d{2}-\d{2}T[^\s*]+)")
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:https://github.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)(\d+)\b", re.I)


DATE = (r"(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))?"
        r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4})")
REGISTER_FIELD = re.compile(r"(?m)^[-*]\s+\**([A-Za-z][\w ]*?)\**:\**\s*(.*(?:\n[ \t]+\S.*)*)")
CLAUSE_END = re.compile(r";|\.\s")
# Repository metadata names its operational register under this docs key.
METADATA = ".github/github.json"
REGISTER_KEY = "openOperationalIssues"


def calendar(text: str) -> dt.datetime | None:
    """A timestamp, or the start of a dated day in UTC."""
    text = text.replace(",", "").replace(".", "")
    if re.match(r"\d{4}-", text):
        return stamp(text if "T" in text else text + "T00:00:00+00:00")
    for form in ("%B %d %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(text, form).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def supervisor_next_action(body: str | None) -> str | None:
    status = audit.github_direction_next.section_map(body or "").get("Current Status", "")
    try:
        actor = next_actor.read(status)
    except ValueError:
        return None
    return status_field(status, "Next action") if actor == "agent:supervisor" else None


def register_entries(text: str) -> list[dict]:
    """Entries of a Markdown operational register: `### Title` and `- **Field**: value` bullets."""
    entries = []
    for block in re.split(r"(?m)^(?=### )", text)[1:]:
        title, _, rest = block.partition("\n")
        fields = {m[1].strip().casefold(): " ".join(m[2].split()) for m in REGISTER_FIELD.finditer(rest)}
        if fields:
            entries.append({"title": title[4:].strip(), "fields": fields})
    return entries


def anchor(title: str) -> str:
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", title.strip().casefold()))


def register_findings(register: dict, *, now: dt.datetime, owner_terms: tuple[str, ...]) -> tuple[list[dict], list[dict]]:
    """Register entries whose dated next check passed or whose current action waits on the owner."""
    findings, gaps = [], []
    base = f"https://github.com/{register['repo']}/blob/{register['default_branch']}/{register['path']}"
    names = "|".join(re.escape(term) for term in owner_terms)
    waits_on_owner = re.compile(rf"^(?:the\s+)?(?:{names})\b|\bwait(?:s|ing)?\s+(?:on|for)\s+(?:the\s+)?(?:{names})\b", re.I)
    for entry in register_entries(register["text"]):
        url = f"{base}#{anchor(entry['title'])}"
        check = entry["fields"].get("next check")
        if check is None:
            gaps.append({"source": url, "reason": "operational_register_entry_without_next_check"})
            continue
        # The trigger is the first clause; later clauses cite dated history.
        first = CLAUSE_END.split(check, maxsplit=1)[0]
        dates = [d for d in map(calendar, re.findall(DATE, first)) if d]
        due = min(dates) if dates else None
        action = entry["fields"].get("current action", "")
        common = {"repo": register["repo"], "title": entry["title"], "url": url,
                  "last_checked": entry["fields"].get("last checked"), "next_check": check}
        if due and due <= now:
            findings.append({**common, "kind": "overdue_operational_check", "since": due,
                             "next_actor": "Repository agent",
                             "detail": f"{entry['title']}: dated next check passed. Next check: {check}"})
        if owner_terms and waits_on_owner.search(CLAUSE_END.split(action, maxsplit=1)[0].strip()):
            findings.append({**common, "kind": "operational_check_waits_on_owner", "since": None,
                             "next_actor": "Director",
                             "detail": f"{entry['title']}: current action waits on the owner. Current action: {action}"})
    return findings, gaps


def stamp(value: Any) -> dt.datetime | None:
    value = audit._parse_time(value)
    return value if value and value.tzinfo else None


def login(row: dict) -> str:
    return str((row.get("user") or {}).get("login") or row.get("author") or "").casefold()


def queue_comment(row: dict) -> dict:
    return {**row, "author": login(row), "url": row.get("html_url") or row.get("url")}


def labels(row: dict) -> set[str]:
    return {name.casefold() for name in audit.github_direction_next.normalize_labels(row.get("labels"))}


def question_body(row: dict) -> str:
    return str(row.get("body") or "").strip().lstrip("*")


def merged_link(record: dict) -> tuple[bool, dict] | None:
    """Newest completing default-branch merge, otherwise newest incidental merge."""
    linked = []
    for pull in record.get("linked_pulls", []):
        refs = {((m[1] or m[2] or pull.get("_repository", record["repo"])).casefold(), int(m[3]))
                for m in CLOSING.finditer(pull.get("body") or "")}
        default = pull.get("_default_branch", record.get("default_branch"))
        if stamp(pull.get("merged_at")) and default and (pull.get("base") or {}).get("ref") == default:
            linked.append(((record["repo"].casefold(), record["item"]["number"]) in refs, pull))
    return max(linked, key=lambda pair: (pair[0], stamp(pair[1]["merged_at"]))) if linked else None


def has_status(source: dict) -> bool:
    """Existing Current Status prose or a live structured wait/status record."""
    body = source.get("body") or ""
    return any(k.casefold() == "current status" and v.strip()
               for k, v in audit.github_direction_next.section_map(body).items()) or bool(github_plan_wait.live_lines(body))


def merged_review_due(record: dict, merged_at: dt.datetime | None, now: dt.datetime, trusted: set[str]) -> bool:
    """Only a post-merge status review earns the fourteen-day quiet period."""
    reviews = []
    item = record["item"]
    if login(item) in trusted and has_status(item):
        reviews.append(stamp(record.get("status_body_updated_at")))
    current = record.get("comments", [])
    for comment in current:
        if login(comment) in trusted and has_status(comment):
            reviews.append(stamp(comment.get("updated_at") or comment.get("created_at")))
    reviewed = max((when for when in reviews if when), default=None)
    if reviewed is None or merged_at is None or reviewed <= merged_at:
        return True
    # Later discussion is activity only after an actual post-merge review;
    # labels and generic issue updated_at cannot establish or renew that review.
    activity = [reviewed, *(stamp(c.get("updated_at") or c.get("created_at")) for c in current
                            if login(c) in trusted)]
    latest = max(when for when in activity if when)
    return now >= latest + dt.timedelta(days=14)


def report(records: list[dict], *, now: dt.datetime, age_hours: float, trusted: set[str],
           owners: set[str], gaps: list[dict] | None = None, director_names: tuple[str, ...] = (),
           delivery_view: dict | None = None, related_comments: list[dict] | None = None,
           registers: list[dict] | None = None, worktrees: list[dict] | None = None,
           supervisor_bodies: list[dict] | None = None) -> dict:
    """Interpret retained API facts. A finding requests review, never authorizes a write."""
    findings: list[dict] = []
    needs_status = []
    gaps = list(gaps or [])
    trusted = {name.casefold() for name in trusted} | {name.casefold() for name in owners}
    owners = {name.casefold() for name in owners}
    comments = [c for r in records if not r.get("trigger_only") for c in r.get("comments", [])] + (related_comments or [])
    question_comments = sorted(
        [{**c, "author": login(c), "url": c.get("html_url") or c.get("url")} for c in comments],
        key=lambda c: (c.get("created_at") or "", c["id"]),
    )
    question_view = {}
    for owner in owners:
        for row in oq.questions(question_comments, owner, trusted):
            if row["url"] not in question_view or row["status"] == "answered":
                question_view[row["url"]] = row
    decisions = [c for c in comments if login(c) in owners or
                 (login(c) in trusted and is_decision(question_body(c)))]
    supervisor_settled = supervisor_settled_questions(comments, trusted)
    deliveries = {row["url"]: row for row in (delivery_view or {}).get("questions", [])
                  if row["url"] not in supervisor_settled}
    if delivery_view is not None and delivery_view.get("complete") is not True:
        gaps.append({"source": "question_delivery", "reason": "queue_view_incomplete",
                     **{k: delivery_view.get(k, []) for k in
                        ("missing_sources", "untrusted_sources", "untrusted_records", "unnumbered_sources")}})

    def add(kind, record, source, since, actor, detail, *, evidence=None, immediate=False):
        since = since if isinstance(since, dt.datetime) else stamp(since)
        hours = max(0.0, (now - since).total_seconds() / 3600) if since else None
        if not immediate and hours is not None and hours < age_hours:
            return
        title = record["item"].get("title") or detail.partition(". ")[0]
        if kind in {"unanswered_question", "registered_question"}:
            question = next((c for c in comments if (c.get("html_url") or c.get("url")) == source), None)
            if question:
                title = question_body(question).partition("\n")[0]
        findings.append({"kind": kind, "repo": record["repo"], "number": record["item"]["number"],
                         "title": title,
                         "url": source, "since": since.isoformat() if since else None,
                         "age_hours": round(hours, 2) if hours is not None else None,
                         "age_basis": "recorded_event" if since else "unknown",
                         "next_actor": actor, "detail": detail, "evidence": evidence or [],
                         "review_required": True})

    def promise_completed(promise: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
        heading = question_body(promise).partition("\n")[0]
        own_id = (QUESTION.match(heading.partition(":")[2].strip().lstrip("*"))
                  if heading.startswith(PREFIXES) else QUESTION.search(heading))
        qids = {own_id[0]} if own_id else set()
        url = promise.get("html_url") or promise.get("url")
        asked = stamp(promise.get("created_at"))
        if asked is None:
            return False
        return any((at := stamp(c.get("created_at"))) is not None and at > asked
                   and not question_body(c).startswith(PREFIXES)
                   and ((url and url in (c.get("body") or "")) or
                        bool(qids & set(QUESTION.findall(question_body(c).partition("\n")[0])[:1]))) for c in candidates)

    open_questions = {c["html_url"] for r in records if not r.get("trigger_only") and r["item"].get("state") == "open"
                      for c in r.get("comments", []) if c.get("html_url") and login(c) in trusted
                      and question_view.get(c["html_url"], {}).get("status") == "needs_review"
                      and c["html_url"] not in supervisor_settled}
    open_questions |= {url for url, row in deliveries.items()
                       if row.get("registration_url") and row.get("status") != "unread_source"}
    decision_urls = {c.get("html_url") for c in decisions}
    director_terms = ["Director", *sorted(owners), *director_names]
    receiver_terms = ("Supervisor", "Director", "Owner", *owners, *director_names)

    for record in records + (supervisor_bodies or []):
        item, repo = record["item"], record["repo"]
        url = item.get("html_url") or f"https://github.com/{repo}/issues/{item['number']}"
        if item.get("state") == "open" and "pull_request" not in item:
            if routed := supervisor_next_action(item.get("body")):
                add("supervisor_next_action", record, url, record.get("status_body_updated_at"), "Supervisor / triage session",
                    "Current Status routes the next action to the Supervisor. Do the Supervisor-only step, "
                    "or name the next agent and its concrete step.", evidence=[routed])
    for record in records:
        item, repo = record["item"], record["repo"]
        url = item.get("html_url") or f"https://github.com/{repo}/issues/{item['number']}"
        trigger = record.get("reopen_trigger")
        if item.get("state") == "closed" and "pull_request" not in item and trigger and record.get("trigger_fired") is True:
            add("reopen_trigger_fired", record, url, None, "Repository agent / Supervisor",
                f"Structured {trigger['kind']} reopen trigger fired; review and reopen the closed issue.",
                evidence=[{"trigger": trigger}])
        if record.get("trigger_only"):
            continue
        current = record.get("comments", [])
        open_item = item.get("state") == "open"
        for comment in current:
            if login(comment) not in trusted:
                continue
            body = question_body(comment)
            question = question_view.get(comment.get("html_url"), {})
            if (open_item and question.get("status") == "needs_review"
                    and comment.get("html_url") not in supervisor_settled):
                delivery = deliveries.get(comment["html_url"])
                add("unanswered_question", record, comment["html_url"], comment.get("created_at"),
                    "Director / Supervisor presenter", "Owner/Director question has no matching recorded decision in covered discussions.",
                    evidence=[{"delivery": delivery.get("delivery"), "registration_url": delivery.get("registration_url"),
                               "delivery_url": delivery.get("delivery_url"), "resurface": delivery.get("resurface")}]
                    if delivery else [])
            # Read existing explicit due fields, and the retained Q249 relative-date promise.
            due = DUE.search(body)
            due_at = stamp(due[1]) if due else None
            if is_decision(body) and re.search(r"Measured after one day", body, re.I):
                start = stamp(comment.get("created_at"))
                due_at = start + dt.timedelta(days=1) if start else None
            if due or due_at:
                if not due_at:
                    gaps.append({"source": comment.get("html_url"), "reason": "promise_due_time_unknown"})
                elif due_at <= now:
                    # Only a linked completion record from a trusted receiver settles the promise.
                    completions = [c for c in comments if login(c) in trusted and
                                   re.search(r"\b(?:report|comparison|promise)\b.{0,80}\b(?:posted|complete[sd]?|delivered)\b", c.get("body") or "", re.I | re.S)]
                    if not promise_completed(comment, completions):
                        add("overdue_promise", record, comment["html_url"], due_at.isoformat(),
                            "Supervisor", "Recorded due time passed without a linked completion in covered discussions.")

        if not open_item:
            continue
        if "pull_request" in item:
            pull = record.get("pull")
            if not pull:
                continue
            if ready_handoff.latest_ready(pull, current, trusted):
                handoffs = [c for c in current if login(c) in trusted and (c.get("body") or "").splitlines() and
                            ready_handoff.handoff_line((c.get("body") or "").splitlines()[0]) == ready_handoff.READY]
                latest = max(handoffs, key=lambda c: (c.get("updated_at") or c["created_at"], c["id"]))
                # Current-head ready is a routing request. The enqueue view is evidence only;
                # the sweep cannot infer service custody from GitHub alone.
                policy = record.get("train_policy") or {}
                target = next((t for t in policy.get("targets", [])
                               if t.get("baseBranch") == (pull.get("base") or {}).get("ref")), None)
                since = latest.get("updated_at") or latest.get("created_at")
                if pull.get("mergeable") is False or pull.get("mergeable_state") == "dirty":
                    add("conflicting_ready_handoff", record, latest["html_url"], since, "Repository agent / Supervisor",
                        "Current-head Ready now conflicts with its base, so the train cannot admit it. Refresh the branch and hand off again.",
                        immediate=True)
                elif target is None:
                    add("ready_handoff_routing_unknown", record, latest["html_url"], since,
                        "Supervisor", "Current-head Ready remains open; active policy confirms no target for this base branch. Supervisor resolves routing." if policy.get("status") in {"enrolled", "not_enrolled"} else
                        "Current-head Ready remains open; active train policy is unavailable. No routing conclusion is inferred.")
                else:
                    # Since launchplane#3834 the train admits a current-head Ready itself,
                    # so the enqueue label is no signal either way; only landing is.
                    add("aged_ready_handoff", record, latest["html_url"], since,
                        "Supervisor / train recovery owner",
                        "Current-head Ready has not landed; read the train's exclusion or deferral reason for this head. "
                        + ("Its enqueue label is set." if target["readyLabel"].casefold() in labels(item) else "It has no enqueue label."))
            if "merge-blocked" in labels(item):
                events = [e for e in record.get("timeline", []) if e.get("event") == "labeled" and
                          (e.get("label") or {}).get("name") == "merge-blocked"]
                event = events[-1] if events and record.get("timeline_complete", True) else {}
                add("aged_train_hold", record, url, event.get("created_at"), "Supervisor / train recovery owner",
                    "Existing merge-blocked view remains set; inspect the train's recorded reason and current head.")
            continue
        try:
            wait = github_plan_wait.current(item, current, trusted)
        except (ValueError, TypeError):
            gaps.append({"source": url, "reason": "structured_wait_unreadable"})
            wait = {"state": "review", "waits": []}
        status = audit.github_direction_next.section_map(item.get("body") or "").get("Current Status", "")
        if migration := next_actor.migration(status, wait):
            needs_status.append({"repo": repo, "number": item["number"], "url": url, "next_action": migration})
        if wait["state"] == "waiting":
            kinds = {w["waiting_for"]: attention.current_director_wait(w["waiting_for"], director_terms, owners)
                     for w in wait["waits"]}
            director = [text for text, kind in kinds.items() if kind]
            director_spoke = any(c in decisions for c in current)
            receivers = [w["waiting_for"] for w in wait["waits"] if w["waiting_for"] not in director and any(
                re.search(r"\b" + re.escape(name) + r"\b", w["waiting_for"], re.I) for name in receiver_terms)]
            source = github_plan_wait.current_source(item, current, trusted) or item
            since = None if source is item else source.get("updated_at") or source.get("created_at")
            linked = (any(c.get("html_url") in open_questions for c in current)
                      or bool(set(COMMENT_LINK.findall(json.dumps(wait))) & (open_questions | decision_urls)))
            if director and not linked and not (director_spoke and all(kinds[t] == "director_hold" for t in director)):
                add("unasked_director_wait", record, source.get("html_url") or url, since,
                    "Supervisor asks the Director",
                    "Wait on the Director has no open Director question. Post one on this issue with the exact step "
                    "and what happens after it, and register it.",
                    evidence=wait["waits"])
            if receivers:
                add("aged_structured_wait", record, source.get("html_url") or url, since, "; ".join(receivers),
                    "Recorded receiver still has an unresolved wait. Body-only marker age is unknown; issue activity cannot reset the wait clock.",
                    evidence=wait["waits"])
        blockers = record.get("blockers")
        if ("plan:blocked" in labels(item) and wait["state"] == "clear" and blockers
                and all(b.get("state") == "closed" and b.get("state_reason") == "completed" for b in blockers)):
            closed = max((b.get("closed_at") or "" for b in blockers), default="")
            add("stale_blocked_view", record, url, closed, "Supervisor / landing reconciler",
                "Blocked view remains after all recorded native blockers completed and structured wait is clear.",
                evidence=[b.get("html_url") for b in blockers])
        selected = merged_link(record)
        if selected:
            completing, pull = selected
            if completing or merged_review_due(record, stamp(pull["merged_at"]), now, trusted):
                add("open_after_completing_merge" if completing else "merged_link_needs_review", record, url,
                    pull["merged_at"], "Supervisor / landing reconciler",
                    "Linked PR merged while issue remains open; completing scope is explicit." if completing else
                    "Linked PR merged while issue remains open; Refs/partial scope is not proof of completion. Review the existing finish line.",
                    evidence=[pull.get("html_url"), {"wait_state": wait["state"], "waits": wait["waits"]}], immediate=True)

    owner_terms = ("owner", "Director", *owners, *director_names)
    for register in registers or []:
        rows, register_gaps = register_findings(register, now=now, owner_terms=owner_terms)
        gaps.extend(register_gaps)
        for row in rows:
            add(row["kind"], {"repo": row["repo"], "item": {"number": None, "title": row["title"]}}, row["url"], row["since"],
                row["next_actor"], row["detail"], evidence=[{"last_checked": row["last_checked"], "next_check": row["next_check"]}])
    by_repo: dict[str, list[dict]] = {}
    for row in worktrees or []:
        since = stamp(row.get("since"))
        if since is None or (now - since).total_seconds() / 3600 >= age_hours:
            by_repo.setdefault(row["repo"], []).append(row)
    for repo, rows in sorted(by_repo.items()):
        # One finding per repository keeps the digest short; evidence names every worktree.
        rows.sort(key=lambda r: r.get("since") or "")
        names = ", ".join(r["task"] for r in rows[:5]) + (f" and {len(rows) - 5} more" if len(rows) > 5 else "")
        dirty = sum(1 for r in rows if r["dirty"])
        add("finished_worktree", {"repo": repo, "item": {"number": None, "title": f"{len(rows)} landed-worktree candidates in {repo}"}},
            f"https://github.com/{repo}", rows[0].get("since"), "Next repository worktree creation",
            f"Source landed or was closed; ownership and continuing use remain unverified: {names}. The next catalog worktree creation checks these "
            "through guarded retirement; retained exceptions need their reported proof or content resolved."
            + (f" {dirty} have local changes to preserve first." if dirty else ""),
            evidence=[{key: r[key] for key in ("task", "branch", "basis", "dirty", "pull")} for r in rows], immediate=True)
    reported_questions = {row["url"] for row in findings if row["kind"] == "unanswered_question"}
    for row in deliveries.values():
        if not row.get("registration_url") or row["url"] in reported_questions or row.get("status") == "unread_source":
            continue
        target = re.fullmatch(r"https://github.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)#issuecomment-\d+", row["url"])
        if target:
            context = {"repo": target[1], "item": {"number": int(target[2])}}
            add("registered_question", context, row["url"], row.get("created_at"),
                "Supervisor / Director", "Registered question remains unresolved outside the selected open-record inventory.",
                evidence=[{"registration_url": row["registration_url"], "delivery": row.get("delivery"),
                           "delivery_url": row.get("delivery_url"), "resurface": row.get("resurface")}])
    findings.sort(key=lambda row: (row["since"] or "", row["repo"], row["number"] or 0, row["kind"], row["url"]))
    routed = Counter(row["repo"] for row in findings if row["kind"] == "supervisor_next_action")
    return {"read_only": True, "sampled_at": now.isoformat(), "age_threshold_hours": age_hours,
            "complete": not gaps, "findings": findings, "coverage_gaps": gaps, "needs_status": needs_status,
            "supervisor_next_action_by_repo": dict(sorted(routed.items(), key=lambda pair: (-pair[1], pair[0]))),
            "scope_notes": ["Decisions and completion records outside the selected repositories/history are unverified.",
                            "Unscheduled promises without a parseable due time are not ageable; no promise state is invented.",
                            "GitHub enqueue/blocked labels are existing helper views, not train custody authority; service-only holds are outside coverage."]}


def has_removed_statement(body: str) -> bool:
    for _, line in github_plan_wait.live_lines(body.replace("**", ""), prefix=""):
        match = re.search(r"^[ \t]*(?:[-*][ \t]+)?Removed:[ \t]*(\S.*?)\s*$", line, re.I)
        if match is None:
            continue
        if match[1].casefold().rstrip(".") not in {"none", "nothing", "n/a"}:
            return True
    return False


def tooling_measure(
    records: list[dict], *, owner: str | None, since: dt.datetime, now: dt.datetime,
    fetch: Any, incomplete: bool, trusted: set[str],
) -> dict:
    result: dict = {"status": "unavailable", "rows": [], "reason": "inventory incomplete"}
    if incomplete or not owner:
        return result
    try:
        direction_repo = f"{owner}/direction"
        text = audit.merged_direction(direction_repo, fetch=fetch)
        if not text:
            raise audit.AuditError("merged overall direction unavailable")
        own_label = audit.own_project_label(direction_repo)
        own_projects = audit.own_project_patterns(text, own_label=own_label)
        tracked, errors = audit.tracked_capacity_issues(direction_repo, text, since, fetch=fetch)
        if errors:
            raise audit.AuditError("milestone classification incomplete")
        candidates = []
        for record in records:
            item = record["item"]
            if record.get("trigger_only") or item.get("state") != "closed" or "pull_request" not in item:
                continue
            if audit.capacity_rank(record["repo"], item, own_projects=own_projects, tracked_issues=tracked,
                                   own_label=own_label, own_issues=frozenset()) != "tooling":
                continue
            reference = item["pull_request"]
            if "merged_at" not in reference:
                reference = fetch(["api", f"repos/{record['repo']}/pulls/{item['number']}"])
            merged = stamp(reference.get("merged_at"))
            if "merged_at" not in reference or (reference["merged_at"] is not None and merged is None):
                raise audit.AuditError("PR merge time unavailable")
            if merged and since <= merged <= now:
                candidates.append((merged, record))
        candidates.sort(key=lambda pair: (pair[0], pair[1]["repo"], pair[1]["item"]["number"]), reverse=True)
        for merged, record in candidates:
            repo, item = record["repo"], record["item"]
            pull = fetch(["api", f"repos/{repo}/pulls/{item['number']}"])
            if audit.github_plan_claim.is_train_batch(pull):
                continue
            if not isinstance(pull.get("labels"), list):
                raise audit.AuditError("PR labels unavailable")
            if audit.capacity_rank(repo, pull, own_projects=own_projects, tracked_issues=tracked,
                                   own_label=own_label, own_issues=frozenset()) != "tooling":
                continue
            pull["_native_issue_refs"] = audit.native_pull_issue_refs(repo, item["number"], fetch=fetch)
            if audit.capacity_rank(repo, pull, own_projects=own_projects, tracked_issues=tracked,
                                   own_label=own_label, own_issues=frozenset()) != "tooling":
                continue
            own_issues = set()
            for ref in audit.pull_issue_refs(repo, pull):
                linked = fetch(["api", f"repos/{ref[0]}/issues/{ref[1]}"])
                if not isinstance(linked.get("labels"), list):
                    raise audit.AuditError("linked issue labels unavailable")
                if own_label.casefold() in labels(linked):
                    own_issues.add(ref)
                    break
            if audit.capacity_rank(repo, pull, own_projects=own_projects, tracked_issues=tracked,
                                   own_label=own_label, own_issues=own_issues) != "tooling":
                continue
            if any(type(pull.get(key)) is not int or pull[key] < 0 for key in ("additions", "deletions")):
                raise audit.AuditError("PR line counts unavailable")
            url = f"https://github.com/{repo}/pull/{item['number']}"
            statement_authors = trusted | {owner.casefold(), login(pull)}
            statements = [(url, pull.get("body") or ""),
                          *((comment["html_url"], comment.get("body") or "") for comment in record["comments"]
                            if login(comment) in statement_authors)]
            removed = [source for source, body in statements if has_removed_statement(body)]
            result["rows"].append({"url": url, "merged_at": merged.isoformat(),
                                   "net_lines": pull["additions"] - pull["deletions"], "removed": removed})
            if len(result["rows"]) == 20:
                qualifying = sum(row["net_lines"] <= 0 and bool(row["removed"]) for row in result["rows"])
                return {**result, "status": "complete", "reason": None, "qualifying": qualifying,
                        "share": qualifying / len(result["rows"]), "classification_source": direction_repo}
        result["reason"] = "fewer than 20 tooling merges in selected history"
    except audit.AuditError as exc:
        result["reason"] = str(exc)
    except (KeyError, TypeError, ValueError, AttributeError, OSError, ImportError):
        result["reason"] = "PR evidence or classification unavailable"
    return result


def collect(repos: list[str], *, reader, now: dt.datetime, age_hours: float = 2,
            history_days: int = 30, max_requests: int = 800, max_pages: int = 20,
            quota_reserve: int = 250, owner: str | None = None, director_names: tuple[str, ...] = (),
            question_queue: str | None = None, policy_reader=None, director: str | None = None,
            worktree_root: pathlib.Path | None = None, apply_next: bool = False) -> dict:
    """Reuse the direction audit's paged inventory with one global bounded read budget."""
    gaps: list[dict] = []
    records: list[dict] = []
    cache: dict[str, Any] = {}
    budget = max_requests
    reads = 0
    policy_reads = 0
    policies = {}
    closed_pr_inventory_complete = True
    director = director.casefold() if director else None
    repos = [repo.casefold() for repo in repos]
    bots: set[str] = set()
    for name in (github_identity.automation_login(), *github_identity.configured_bot_logins()):
        if isinstance(name, str) and name:
            bots.add(name)

    def fetch(args):
        nonlocal reads, budget
        path = args[1]
        cache_key = json.dumps(args) if path == "graphql" else path
        if cache_key not in cache:
            if reads >= budget:
                raise audit.AuditError("request_budget_exhausted")
            reads += 1
            try:
                if path == "graphql":
                    parameters = dict(argument.split("=", 1) for argument in args[3::2])
                    query = parameters.pop("query")
                    if "number" in parameters:
                        parameters["number"] = int(parameters["number"])
                    response = reader.graphql_json(query, parameters, step="stranded_tooling_links")
                    if not response.ok:
                        cause = getattr(getattr(response, "failure", None), "cause", None)
                        if cause in {"quota_reserve", "deadline_exceeded", "cancelled"}:
                            budget = reads
                        raise audit.AuditError(cause or "native PR links unavailable")
                    cache[cache_key] = response.body
                else:
                    cache[cache_key] = reader.get_json(path, step="stranded_inventory")
            except (github_read.GitHubReadError, github_read.GitHubReadShapeError, TimeoutError) as exc:
                cause = getattr(getattr(getattr(exc, "result", None), "failure", None), "cause", None)
                if cause in {"quota_reserve", "deadline_exceeded", "cancelled"} or isinstance(exc, TimeoutError):
                    budget = reads
                raise audit.AuditError(cause or type(exc).__name__) from exc
        return cache[cache_key]

    def listing(path, *, stop=None):
        try:
            rows, cut = audit.fetch_paginated(path, fetch=fetch, max_pages=max_pages, stop=stop)
            if cut:
                gaps.append({"source": path, "reason": "page_limit"})
            if any(not isinstance(row, dict) for row in rows):
                gaps.append({"source": path, "reason": "invalid_listing_item"})
            return [row for row in rows if isinstance(row, dict)]
        except audit.AuditError as exc:
            gaps.append({"source": path, "reason": str(exc)})
            return []

    def one(path):
        try:
            value = fetch(["api", path])
            if not isinstance(value, dict):
                raise audit.AuditError("unexpected_response_shape")
            return value
        except audit.AuditError as exc:
            gaps.append({"source": path, "reason": str(exc)})
            return {}

    applied_actions = []
    allowed = (director or owner or "").casefold()
    if apply_next and not allowed:
        raise audit.AuditError("Applying next records requires --owner or --stranded-owner")
    fresh = github_read.GitHubReader(gh_cmd=reader.gh_cmd, operation="github.direction.stranded_work",
                                    deadline_at=reader.deadline_at) if apply_next else reader

    def fresh_one(path):
        nonlocal reads
        if reads >= budget:
            raise ValueError("request_budget_exhausted")
        reads += 1
        return fresh.get_json(path, step="next_recheck")

    def fresh_listing(path, *, stop=None):
        rows, page_cut = audit.fetch_paginated(path, fetch=lambda args: fresh_one(args[1]), max_pages=max_pages, stop=stop)
        if page_cut:
            raise ValueError("next_recheck_page_limit")
        return rows

    def prepare_source(repo, next_item, comments):
        source_record = {"repo": repo, "item": next_item, "comments": comments}
        status_body_time(source_record, source_reader=fresh)
        return source_record["item"]

    def apply_record(action_record):
        action_item, repo = action_record["item"], action_record["repo"]
        if (not apply_next or action_record.get("archived") or "pull_request" in action_item
                or repo.split("/")[0].casefold() != allowed):
            return
        try:
            trusted = bots | {allowed}
            status_body_time(action_record)
            changed = False
            for action in direction_next.actions(action_item, action_record["comments"], trusted=trusted, now=now, one=one, listing=listing):
                with direction_next.deadline(reader.deadline_at):
                    applied = direction_next.apply(repo, action_item["number"], action, trusted=trusted, now=now,
                                                   one=fresh_one, listing=fresh_listing, prepare_source=prepare_source)
                changed |= applied
                applied_actions.append({"repo": repo, "number": action_item["number"], "kind": action["kind"], "applied": applied})
            if changed:
                action_item.update(fresh_one(f"repos/{repo}/issues/{action_item['number']}"))
                action_record["comments"] = fresh_listing(f"repos/{repo}/issues/{action_item['number']}/comments")
                status_body_time(action_record, source_reader=fresh)
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, github_read.GitHubReadError,
                github_read.GitHubReadShapeError, direction_next.github_issue.IssueError, direction_next.github_comment.CommentError) as action_error:
            gaps.append({"source": f"{repo}#{action_item['number']}", "reason": "next_action_failed", "detail": str(action_error)})

    def check_scheduled(repo):
        if not apply_next or repo.split("/")[0].casefold() != allowed:
            return
        try:
            workflows = []
            for page in range(1, max_pages + 1):
                payload = one(f"repos/{repo}/actions/workflows?per_page=100&page={page}")
                if not isinstance(payload.get("workflows"), list) or type(payload.get("total_count")) is not int:
                    raise ValueError("Workflow inventory unavailable")
                workflows.extend(payload["workflows"])
                if len(workflows) >= payload["total_count"]:
                    break
            else:
                raise ValueError("Workflow inventory page limit")
            for workflow in workflows:
                if workflow.get("state") != "active":
                    continue
                payload = one(f"repos/{repo}/actions/workflows/{workflow['id']}/runs?event=schedule&per_page=2")
                if not isinstance(payload.get("workflow_runs"), list):
                    raise ValueError("Scheduled run inventory unavailable")
                for pair in direction_next.scheduled_failures(payload["workflow_runs"]):
                    query = quote(f'repo:{repo} is:issue in:body "Scheduled job ID: {pair[0]["workflow_id"]}"', safe="")
                    found = one(f"search/issues?q={query}")
                    if found.get("incomplete_results") is not False or not isinstance(found.get("items"), list):
                        raise ValueError("Scheduled job issue lookup incomplete")
                    candidates = []
                    for found_item in found["items"]:
                        candidate_issue = fresh_one(f"repos/{repo}/issues/{found_item['number']}")
                        candidate_issue["_comments"] = [row for row in fresh_listing(f"repos/{repo}/issues/{candidate_issue['number']}/comments")
                                             if login(row) in bots | {allowed}]
                        candidates.append(candidate_issue)
                    with direction_next.deadline(reader.deadline_at):
                        outcome = direction_next.scheduled_issue(repo, pair, candidates=candidates)
                    applied_actions.append({"repo": repo, "kind": "scheduled_job", "result": outcome})
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError, github_read.GitHubReadError,
                github_read.GitHubReadShapeError, direction_next.github_issue.IssueError, direction_next.github_comment.CommentError) as scheduled_error:
            gaps.append({"source": repo, "reason": "scheduled_job_unavailable", "detail": str(scheduled_error)})

    def status_body_time(status_record, *, source_reader=reader):
        """Read body edit time for an incidental merge with body status, or a
        body status that routes the next action to the Supervisor.

        REST updated_at also moves on labels/comments; it is not status evidence.
        This read shares the collector's request budget and reader deadline.
        """
        nonlocal reads, budget
        if status_record.get("status_body_updated_at") and source_reader is reader:
            return
        selected = merged_link(status_record)
        incidental = selected and not selected[0] and has_status(status_record["item"])
        commented_status = any(login(comment) in bots | {allowed} and has_status(comment) for comment in status_record.get("comments", []))
        if not incidental and not supervisor_next_action(status_record["item"].get("body")) and not commented_status:
            return
        source = f"{status_record['repo']}#{status_record['item']['number']}/status"
        try:
            if reads >= budget:
                raise ValueError("request_budget_exhausted")
            reads += 1
            owner_name, repo_name = status_record["repo"].split("/")
            body_result = source_reader.graphql_json(
                "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issue(number:$number){createdAt lastEditedAt}}}",
                {"owner": owner_name, "name": repo_name, "number": status_record["item"]["number"]},
                step="stranded_merged_status")
            if not body_result.ok:
                cause = getattr(getattr(body_result, "failure", None), "cause", None)
                if cause in {"quota_reserve", "deadline_exceeded", "cancelled"}:
                    budget = reads
                raise ValueError(cause or "status_edit_time_unavailable")
            if not isinstance(body_result.body, dict) or body_result.body.get("errors"):
                raise ValueError("status_edit_time_unavailable")
            issue = body_result.body["data"]["repository"]["issue"]
            value = issue["lastEditedAt"] or issue["createdAt"]
            if not stamp(value):
                raise ValueError("status_edit_time_invalid")
            status_record["status_body_updated_at"] = value
            status_record["item"]["body_recorded_at"] = value
        except (github_read.GitHubReadError, github_read.GitHubReadShapeError, TimeoutError,
                ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, TimeoutError):
                budget = reads
            gaps.append({"source": source, "reason": str(exc) if isinstance(exc, ValueError) else "status_edit_time_unavailable"})

    quota = one("rate_limit").get("resources", {}).get("core", {})
    remaining = quota.get("remaining")
    if type(quota.get("limit")) is int:
        quota_reserve = max(quota_reserve, github_request_usage.bulk_read_reserve(quota["limit"]))
    if type(remaining) is not int:
        gaps.append({"source": "rate_limit", "reason": "quota_unavailable"})
        budget = reads
    else:
        budget = min(max_requests, reads + max(0, remaining - quota_reserve))
    if owner:
        try:
            discovered, cut = audit.owner_repositories(owner, fetch=fetch, include_archived=True)
            repos = sorted(set(repos) | set(discovered))
            if cut:
                gaps.append({"source": owner, "reason": "repository_page_limit"})
                closed_pr_inventory_complete = False
        except audit.AuditError as exc:
            gaps.append({"source": owner, "reason": str(exc)})
            closed_pr_inventory_complete = False
    repos = list(dict.fromkeys(repos))
    # Stateless daily rotation prevents a bounded run from always skipping the
    # same alphabetically later repositories. Open records stay ahead of closed.
    offset = now.date().toordinal() % len(repos) if repos else 0
    ordered_repos = repos[offset:] + repos[:offset]
    history_start = now - dt.timedelta(days=history_days)
    delivery_view = None
    question_delivery = None
    registered = []
    unread_registered = set()
    related_comments = []
    normalized_queue: list[dict] = []
    queue_director = director
    if question_queue:
        question_queue = question_queue.casefold()
        queue_repo, queue_number = question_queue.split("#")
        queue = listing(f"repos/{queue_repo}/issues/{queue_number}/comments")
        related_comments.extend(queue)
        try:
            from skills.supervisor.scripts import question_delivery

            normalized_queue = [queue_comment(c) for c in queue]
            queue_director = director or queue_repo.split("/")[0]
            registered = sorted({r["question_url"] for r in question_delivery.records(
                normalized_queue, bots | {queue_director})})
            # A fleet-wide queue can reference a closed or out-of-scope source.
            # Recover its original discussion using the sibling's existing markers.
            recovery_sources = [url for url in registered if not question_delivery.settled(
                url, normalized_queue, queue_director, bots)]
            for index, original in enumerate(recovery_sources):
                if reads >= budget:
                    unread_registered.update(recovery_sources[index:])
                    gaps.append({"source": question_queue, "reason": "unread_registered_sources", "count": len(recovery_sources) - index})
                    break
                source_repo, source_number = question_delivery.source_item(original).split("#")
                related_comments.extend(listing(f"repos/{source_repo.casefold()}/issues/{source_number}/comments"))
        except ImportError:
            question_delivery = None
            gaps.append({"source": question_queue, "reason": "question_delivery_view_unavailable"})
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            question_delivery = None
            gaps.append({"source": question_queue, "reason": "question_delivery_contract_error", "error_type": type(error).__name__})
    else:
        gaps.append({"source": "question_delivery", "reason": "question_queue_not_selected"})
    def file_text(path):
        """A default-branch file, or None when the repository has none there."""
        try:
            body = fetch(["api", path])
        except audit.AuditError as exc:
            if str(exc) == "not_found":
                return None
            raise
        if not isinstance(body, dict) or body.get("type") != "file" or not isinstance(body.get("content"), str):
            raise audit.AuditError("unexpected_response_shape")
        return base64.b64decode(body["content"]).decode()

    def operational_register(repo, default_branch):
        # The repository declares its register in its own metadata; nothing is assumed.
        source = f"repos/{repo}/contents/{METADATA}"
        try:
            text = file_text(source)
            if text is None:
                return None
            path = (json.loads(text).get("docs") or {}).get(REGISTER_KEY)
            if path is None:
                return None
            source = f"{repo}:{REGISTER_KEY}"
            if (not isinstance(path, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", path)
                    or ".." in path.split("/")):
                raise audit.AuditError("operational_register_path_invalid")
            source = f"repos/{repo}/contents/{path}"
            text = file_text(source)
            if text is None:
                raise audit.AuditError("operational_register_missing")
        except (audit.AuditError, ValueError, AttributeError, UnicodeDecodeError) as exc:
            gaps.append({"source": source, "reason": str(exc) if isinstance(exc, audit.AuditError) else "unreadable_metadata"})
            return None
        return {"repo": repo, "path": path, "default_branch": default_branch, "text": text}

    registers = []
    unread_scopes = []
    attempted_records = 0
    collected = set()
    supervisor_bodies = {}
    for state in ("reopen_triggers", "open", "closed"):
        inventories = []
        for repo in ordered_repos:
            if reads >= budget:
                unread_scopes.append({"repo": repo, "state": state})
                if state == "closed":
                    closed_pr_inventory_complete = False
                continue
            metadata = one(f"repos/{repo}")
            if metadata.get("archived") is True and state != "reopen_triggers":
                continue
            if not isinstance(metadata.get("archived"), bool) or not metadata.get("default_branch"):
                gaps.append({"source": repo, "reason": "repository_state_unavailable"})
                if state == "closed":
                    closed_pr_inventory_complete = False
                continue
            if state == "open":
                register = operational_register(repo, metadata["default_branch"])
                if register:
                    registers.append(register)
            suffix = "state=open" if state == "open" else (
                "state=closed&labels=" + quote(github_plan_trigger.INDEX_LABEL, safe="")
                if state == "reopen_triggers" else f"state=closed&since={history_start.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            before_inventory = len(gaps)
            issues = listing(f"repos/{repo}/issues?{suffix}")
            if state == "closed" and len(gaps) != before_inventory:
                closed_pr_inventory_complete = False
            inventories.append((repo, metadata, issues))
            if state == "open":
                for item in issues:
                    if (type(item.get("number")) is int and item.get("state") == "open"
                            and "pull_request" not in item and supervisor_next_action(item.get("body"))):
                        supervisor_bodies[(repo, item["number"])] = {"repo": repo, "item": item}
        for repo, metadata, issues in inventories:
            if state == "open":
                check_scheduled(repo)
            policy = None
            if any("pull_request" in item and item.get("state") == "open" for item in issues):
                if policy_reader and reads < budget:
                    if repo not in policies:
                        policies[repo] = policy_reader(repo)
                        policy_reads += 1
                    policy = policies[repo]
                if not isinstance(policy, dict) or policy.get("status") not in {"enrolled", "not_enrolled"}:
                    gaps.append({"source": repo, "reason": "train_policy_unavailable"})
                    policy = None
            seen = set()
            for index, item in enumerate(issues):
                if reads >= budget:
                    gaps.append({"source": repo, "reason": "unread_records_after_budget", "count": len(issues) - index})
                    if state == "closed":
                        closed_pr_inventory_complete = False
                    break
                if not isinstance(item, dict) or type(item.get("number")) is not int:
                    gaps.append({"source": repo, "reason": "invalid_issue"})
                    if state == "closed":
                        closed_pr_inventory_complete = False
                    continue
                number = item["number"]
                if (repo, number) in collected or (state != "open" and item.get("state") != "closed"):
                    continue
                recent = (stamp(item.get("updated_at")) or now) >= history_start
                # Closed PRs carry no reopen record; retain recent promises only.
                if state != "open" and "pull_request" in item and not recent:
                    continue
                if number in seen:
                    gaps.append({"source": f"{repo}#{number}", "reason": "inventory_changed_during_pagination"})
                    if state == "closed":
                        closed_pr_inventory_complete = False
                    continue
                seen.add(number)
                attempted_records += 1
                before_comments = len(gaps)
                record = {"repo": repo, "item": item, "default_branch": metadata["default_branch"], "archived": metadata["archived"],
                          "train_policy": policy,
                          "comments": [] if item.get("comments") == 0 else listing(f"repos/{repo}/issues/{number}/comments")}
                if len(gaps) != before_comments:
                    if not metadata["archived"] and item.get("state") == "closed" and "pull_request" in item:
                        closed_pr_inventory_complete = False
                    continue
                valid_comments = [c for c in record["comments"] if type(c.get("id")) is int
                                  and isinstance(c.get("html_url"), str) and isinstance(c.get("created_at"), str)]
                if len(valid_comments) != len(record["comments"]):
                    gaps.append({"source": f"{repo}#{number}/comments", "reason": "invalid_comment"})
                    if not metadata["archived"] and item.get("state") == "closed" and "pull_request" in item:
                        closed_pr_inventory_complete = False
                comments_complete = len(valid_comments) == len(record["comments"])
                record["comments"] = valid_comments
                if comments_complete:
                    apply_record(record)
                if item.get("state") == "closed":
                    record["trigger_only"] = metadata["archived"] or not recent
                    if "pull_request" not in item and comments_complete:
                        try:
                            trigger = github_plan_trigger.current(item, valid_comments,
                                github_plan_wait.authors(repo))
                            if trigger:
                                record["reopen_trigger"] = trigger
                                before_event = len(gaps)
                                fired = github_plan_trigger.fired(trigger, now=now, one=one, listing=listing)
                                if len(gaps) != before_event:
                                    raise ValueError("Trigger target could not be read completely")
                                record["trigger_fired"] = fired
                            elif state == "reopen_triggers":
                                raise ValueError("Discovery label has no authoritative trigger record")
                        except (ValueError, TypeError) as exc:
                            gaps.append({"source": f"{repo}#{number}", "reason": "reopen_trigger_unverifiable", "detail": str(exc)})
                if item.get("state") == "open":
                    gap_count = len(gaps)
                    record["timeline"] = listing(f"repos/{repo}/issues/{number}/timeline")
                    record["timeline_complete"] = len(gaps) == gap_count
                    if "pull_request" in item:
                        record["pull"] = one(f"repos/{repo}/pulls/{number}")
                        if not re.fullmatch(r"[0-9a-f]{40}", str((record["pull"].get("head") or {}).get("sha") or "")):
                            gaps.append({"source": f"{repo}#{number}", "reason": "pull_head_unavailable"})
                            record["pull"] = None
                    else:
                        if "plan:blocked" in labels(item):
                            record["blockers"] = listing(f"repos/{repo}/issues/{number}/dependencies/blocked_by")
                        linked = set()
                        for event in record["timeline"]:
                            source = (event.get("source") or {}).get("issue") or {}
                            if event.get("event") != "cross-referenced" or not source.get("pull_request"):
                                continue
                            match = re.fullmatch(r"https://api.github.com/repos/([\w.-]+/[\w.-]+)", source.get("repository_url") or "")
                            if not match or type(source.get("number")) is not int:
                                gaps.append({"source": f"{repo}#{number}", "reason": "linked_pull_identity_unknown"})
                                continue
                            linked.add((match[1].casefold(), source["number"]))
                        record["linked_pulls"] = []
                        for source_repo, n in sorted(linked):
                            pull = one(f"repos/{source_repo}/pulls/{n}")
                            source_metadata = one(f"repos/{source_repo}") if source_repo != repo else metadata
                            pull["_repository"] = source_repo
                            pull["_default_branch"] = source_metadata.get("default_branch")
                            record["linked_pulls"].append(pull)
                        status_body_time(record)
                records.append(record)
                collected.add((repo, number))
    if unread_scopes:
        gaps.append({"source": "repository_inventory", "reason": "unread_repository_scopes", "scopes": unread_scopes})
    if question_delivery is not None:
        try:
            source_comments = [c for r in records if not r.get("trigger_only") for c in r["comments"]] + related_comments
            sources = {c.get("html_url"): queue_comment(c) for c in source_comments}
            scope = set(registered) | {c["url"] for c in sources.values()
                                      if c["author"] in bots | {queue_director}
                                      and question_body(c).startswith(PREFIXES)}
            delivery_view = question_delivery.inventory(
                list(sources.values()), normalized_queue, queue_director, bots, now=now, question_urls=scope,
            )
            delivery_view = {**delivery_view,
                "missing_sources": [url for url in delivery_view.get("missing_sources", []) if url not in unread_registered],
                "questions": [{**row, "status": "unread_source"} if row["url"] in unread_registered else row
                              for row in delivery_view.get("questions", [])]}
            if (not delivery_view["missing_sources"] and not delivery_view.get("untrusted_sources")
                    and not delivery_view.get("unnumbered_sources")
                    and not delivery_view.get("untrusted_records") and unread_registered):
                # The separate unread-source gap remains authoritative; these
                # records are unexamined, not evidence that a source was deleted.
                delivery_view["complete"] = True
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            gaps.append({"source": question_queue, "reason": "question_delivery_contract_error", "error_type": type(error).__name__})
    result = report(records, now=now, age_hours=age_hours,
                    trusted=bots,
                    owners={director} if director else {r.split('/')[0] for r in repos} | ({owner} if owner else set()),
                    gaps=gaps, director_names=director_names, delivery_view=delivery_view,
                    related_comments=related_comments, registers=registers,
                    supervisor_bodies=[record for key, record in supervisor_bodies.items() if key not in collected],
                    worktrees=finished_worktrees(
                        worktree_root, fetch, gaps,
                        lambda slug: slug.casefold() in repos or (owner or "").casefold() == slug.split("/")[0].casefold(),
                    ) if worktree_root else None)
    result["read_only"] = not apply_next
    result["actions"] = applied_actions
    result["complete"] = result["complete"] and not result["coverage_gaps"]
    selected_owners = {repo.split('/')[0] for repo in repos}
    measure = tooling_measure(records, owner=owner or director or (next(iter(selected_owners)) if len(selected_owners) == 1 else None),
                              since=history_start, now=now, fetch=fetch, incomplete=not closed_pr_inventory_complete,
                              trusted=bots)
    result.update({"tooling_measure": measure, "repositories": repos, "operational_registers": [f"{r['repo']}:{r['path']}" for r in registers], "checked_records": len(records), "attempted_records": attempted_records, "requests": reads,
                   "history_days": history_days, "closed_trigger_history": "all", "quota_reserve": quota_reserve, "policy_reads": policy_reads})
    return result


def finished_worktrees(root: pathlib.Path, fetch, gaps: list[dict], include) -> list[dict]:
    """Finished task worktrees under this host's root; PR reads share the sweep's budget."""
    def pulls_for(slug, branch):
        owner = slug.split("/")[0]
        try:
            rows = fetch(["api", f"repos/{slug}/pulls?state=all&per_page=100&head={quote(owner)}:{quote(branch, safe='')}"])
        except audit.AuditError as exc:
            raise LookupError(str(exc)) from exc
        if not isinstance(rows, list):
            raise LookupError("unexpected_response_shape")
        if len(rows) >= 100:
            # A full page may hide the PR that contains the head; never conclude from it.
            raise LookupError("page_limit")
        return rows

    try:
        rows, worktree_gaps = landed_worktrees.finished(root, pulls_for, include)
    except OSError:
        gaps.append({"source": "worktree_root", "reason": "unreadable"})
        return []
    gaps.extend(worktree_gaps)
    return rows


def train_policy(repo: str, *, deadline_at: float) -> dict | None:
    """Read only the existing scoped active-policy projection, never saved topology."""
    remaining = deadline_at - time.time()
    if remaining <= 0:
        return None
    helper = audit.REPO_ROOT / "skills/launchplane/scripts/launchplane-write-action.py"
    try:
        result = subprocess.run(["uv", "run", str(helper), "merge-train-policy-read", "--repo", repo],
                                capture_output=True, text=True, timeout=min(30, remaining))
        payload = json.loads(result.stdout)
        policy = payload.get("result")
        if (result.returncode or payload.get("status") != "available" or not isinstance(policy, dict)
                or policy.get("status") not in {"enrolled", "not_enrolled"}
                or not isinstance(policy.get("targets"), list)
                or any(not isinstance(t, dict) or not isinstance(t.get("baseBranch"), str)
                       or not isinstance(t.get("readyLabel"), str) or not t["readyLabel"] for t in policy["targets"])):
            return None
        return policy
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def markdown(result: dict) -> str:
    lines = ["Daily stranded-work sweep", f"Observed: {result['sampled_at']}",
             f"Coverage: {'complete within selected scope' if result['complete'] else 'INCOMPLETE'}; {len(result['findings'])} candidates."]
    measure: dict = result.get("tooling_measure") or {}
    if measure:
        if measure["status"] == "complete":
            lines.append(f"Tooling PRs: {measure['qualifying']}/{len(measure['rows'])} ({measure['share']:.0%}); net lines <= 0 and Removed:.")
        else:
            lines.append(f"Tooling PRs: unavailable; {measure['reason']}.")
        lines.append(f"Tooling scope: non-archived among {', '.join(result['repositories'])}; history {result['history_days']} days; weekly capacity rank.")
        for row in measure["rows"]:
            lines.append(f"  Tooling PR: {row['url']} net lines {row['net_lines']:+d}; Removed: {', '.join(row['removed']) or 'absent'}")
    missing: list[dict] = result.get("needs_status") or []
    if missing:
        counts = Counter(row["repo"] for row in missing)
        lines.append("Next records needing one-time review (wait --all): " + ", ".join(f"{repo} {count}" for repo, count in sorted(counts.items())))
    for action in result.get("actions", []):
        lines.append(f"Action: {action['kind']} on {action['repo']}#{action.get('number', 'scheduled-job')}; applied={action.get('applied', True)}")
    for row in result["findings"]:
        age = f"{row['age_hours']:g}h" if row["age_hours"] is not None else "age unknown"
        lines.append(f"- {row['kind']} ({age}): {row['url']} — {row['next_actor']}. {row['detail']}")
        lines.append("  Name: " + " ".join(row.get("title", row['url']).split()))
    if routed := result.get("supervisor_next_action_by_repo"):
        lines.append("Next action names the Supervisor: " + ", ".join(f"{repo} {count}" for repo, count in routed.items()))
    for gap in result["coverage_gaps"]:
        count = f" ({gap['count']} records)" if "count" in gap else ""
        lines.append(f"- Coverage gap: {gap['source']} — {gap['reason']}{count}")
        if gap.get("scopes"):
            grouped = {}
            for scope in gap["scopes"]:
                grouped.setdefault(scope["repo"], []).append(scope["state"])
            lines.append("  Unread: " + "; ".join(f"{repo} ({','.join(states)})" for repo, states in grouped.items()))
        for field in ("missing_sources", "untrusted_sources", "untrusted_records"):
            if gap.get(field):
                urls = [r.get("url", "unknown") if isinstance(r, dict) else r for r in gap[field]]
                lines.append(f"  {field}: " + ", ".join(urls))
    lines.extend(result["scope_notes"])
    return "\n".join(lines) + "\n"
