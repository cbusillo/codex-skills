# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Read-only stranded-work view of the existing direction audit inventory."""
from __future__ import annotations

import base64
import datetime as dt
import re
import json
import subprocess
import sys
import time
from urllib.parse import quote
from typing import Any

import direction_audit as audit
from skills.github.scripts import github_identity, github_plan_wait, github_plan_trigger, github_read, ready_handoff, github_request_usage

# The existing Supervisor helpers use sibling imports when called as scripts.
sys.path.insert(0, str(audit.REPO_ROOT / "skills/supervisor/scripts"))
import oq

UTC = dt.timezone.utc
QUESTION = re.compile(r"\bQ[1-9][0-9]*\b")
PREFIXES = oq.QUESTION_PREFIXES


def is_decision(body: str) -> bool:
    return body.startswith(oq.DECISION_PREFIXES)
DUE = re.compile(r"(?im)^\s*(?:[-*] )?\**Due\**:\s*(\d{4}-\d{2}-\d{2}T[^\s*]+)")
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:https://github.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)(\d+)\b", re.I)


DATE = (r"(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))?"
        r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4})")
# A wait's own date names when it ends; a date after "since" or in a cited
# record ("2026-10-12 scope comment") does not.
ENDS_AT = re.compile(r"\b(?:until|till|after|by|due|before|no earlier than|on or after)\s+(?:the\s+)?(?:[`*]+)?(" + DATE + ")", re.I)
LEADING_DATE = re.compile(r"\s*(?:[`*]+)?(" + DATE + ")", re.I)
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


def ends_at(text: str, *, leading: bool) -> dt.datetime | None:
    """The earliest date at which a recorded wait or check says it ends."""
    found = [m[1] for m in ENDS_AT.finditer(text)]
    if leading and (start := LEADING_DATE.match(text)):
        found.append(start[1])
    dates = [d for d in map(calendar, found) if d]
    return min(dates) if dates else None


def dated_waits(item: dict, wait: dict) -> list[tuple[str, dt.datetime]]:
    """Waits whose own date ends them; the structured record supersedes prose."""
    if wait["state"] == "clear":
        return []
    if wait["state"] == "waiting":
        rows = [(f"{w['waiting_for']} (ends when: {w['ends_when']})",
                 ends_at(w["waiting_for"], leading=False) or ends_at(w["ends_when"], leading=True))
                for w in wait["waits"]]
        return [(text, due) for text, due in rows if due]
    body = item.get("body") or ""
    status = next((v for k, v in audit.github_direction_next.section_map(body).items()
                   if k.casefold() == "current status"), body)
    rows = audit.github_direction_next.waiting_records(
        {"repo": "", "number": item.get("number"), "url": item.get("html_url")}, status)
    rows = [(r["waiting_for"], ends_at(r["waiting_for"], leading=True)) for r in rows if not r["no_current_wait"]]
    return [(text, due) for text, due in rows if due]


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


def labels(row: dict) -> set[str]:
    return {name.casefold() for name in audit.github_direction_next.normalize_labels(row.get("labels"))}


def question_body(row: dict) -> str:
    return str(row.get("body") or "").strip().lstrip("*")


def report(records: list[dict], *, now: dt.datetime, age_hours: float, trusted: set[str],
           owners: set[str], gaps: list[dict] | None = None, director_names: tuple[str, ...] = (),
           delivery_view: dict | None = None, related_comments: list[dict] | None = None,
           registers: list[dict] | None = None) -> dict:
    """Interpret retained API facts. A finding requests review, never authorizes a write."""
    findings: list[dict] = []
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
    deliveries = {row["url"]: row for row in (delivery_view or {}).get("questions", [])}
    if delivery_view is not None and delivery_view.get("complete") is not True:
        gaps.append({"source": "question_delivery", "reason": "queue_view_incomplete",
                     **{k: delivery_view.get(k, []) for k in ("missing_sources", "untrusted_sources", "untrusted_records")}})

    def add(kind, record, source, since, actor, detail, *, evidence=None):
        since = since if isinstance(since, dt.datetime) else stamp(since)
        hours = max(0.0, (now - since).total_seconds() / 3600) if since else None
        if hours is not None and hours < age_hours:
            return
        findings.append({"kind": kind, "repo": record["repo"], "number": record["item"]["number"],
                         "url": source, "since": since.isoformat() if since else None,
                         "age_hours": round(hours, 2) if hours is not None else None,
                         "age_basis": "recorded_event" if since else "unknown",
                         "next_actor": actor, "detail": detail, "evidence": evidence or [],
                         "review_required": True})

    def answered(question, candidates):
        heading = question_body(question).partition("\n")[0]
        own_id = (QUESTION.match(heading.partition(":")[2].strip().lstrip("*"))
                  if heading.startswith(PREFIXES) else QUESTION.search(heading))
        qids = {own_id[0]} if own_id else set()
        url = question.get("html_url") or question.get("url")
        asked = stamp(question.get("created_at"))
        return any(stamp(c.get("created_at")) and asked and stamp(c["created_at"]) > asked
                   and not question_body(c).startswith(PREFIXES)
                   and ((url and url in (c.get("body") or "")) or
                        bool(qids & set(QUESTION.findall(question_body(c).partition("\n")[0])[:1]))) for c in candidates)

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
                    and not answered(comment, decisions)):
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
                    if not answered(comment, completions):
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
                if target is None:
                    add("ready_handoff_routing_unknown", record, latest["html_url"], latest.get("updated_at") or latest.get("created_at"),
                        "Supervisor", "Current-head Ready remains open; active policy confirms no target for this base branch. Supervisor resolves routing." if policy.get("status") in {"enrolled", "not_enrolled"} else
                        "Current-head Ready remains open; active train policy is unavailable. No routing conclusion is inferred.")
                elif target["readyLabel"].casefold() not in labels(item):
                    add("ready_handoff_unrouted", record, latest["html_url"], latest.get("updated_at") or latest.get("created_at"),
                        "Supervisor", "Current-head Ready has no enqueue view; verify existing train custody before routing.")
                else:
                    add("aged_ready_handoff", record, latest["html_url"], latest.get("updated_at") or latest.get("created_at"),
                        "Supervisor / train recovery owner", "Current-head Ready remains open with its enqueue view set; that view does not prove an active receiver or train progress.")
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
        if wait["state"] == "waiting":
            receivers = [w["waiting_for"] for w in wait["waits"] if any(
                re.search(r"\b" + re.escape(name) + r"\b", w["waiting_for"], re.I)
                for name in ("Supervisor", "Director", "Owner", *owners, *director_names))]
            if receivers:
                sources = [item, *current]
                source = next((s for s in reversed(sources) if login(s) in trusted
                               and github_plan_wait.records(s.get("body") or "")), item)
                add("aged_structured_wait", record, source.get("html_url") or url,
                    None if source is item else source.get("updated_at") or source.get("created_at"), "; ".join(receivers),
                    "Recorded receiver still has an unresolved wait. Body-only marker age is unknown; issue activity cannot reset the wait clock.",
                    evidence=wait["waits"])
        for text, due in dated_waits(item, wait):
            if due <= now:
                add("passed_dated_wait", record, url, due, "Supervisor",
                    f"Dated wait passed; re-evaluate its trigger, then resume, re-park or close. Wait: {text}",
                    evidence=[text])
        blockers = record.get("blockers")
        if ("plan:blocked" in labels(item) and wait["state"] == "clear" and blockers
                and all(b.get("state") == "closed" and b.get("state_reason") == "completed" for b in blockers)):
            closed = max((b.get("closed_at") or "" for b in blockers), default="")
            add("stale_blocked_view", record, url, closed, "Supervisor / landing reconciler",
                "Blocked view remains after all recorded native blockers completed and structured wait is clear.",
                evidence=[b.get("html_url") for b in blockers])
        merged_links = []
        for pull in record.get("linked_pulls", []):
            # Explicit closing scope is a review candidate; even this does not settle real waits.
            refs = {((m[1] or m[2] or pull.get("_repository", repo)).casefold(), int(m[3])) for m in CLOSING.finditer(pull.get("body") or "")}
            default_branch = pull.get("_default_branch", record.get("default_branch"))
            if pull.get("merged_at") and default_branch and (pull.get("base") or {}).get("ref") == default_branch:
                completing = (repo.casefold(), item["number"]) in refs
                merged_links.append((completing, pull))
        if merged_links:
            # One candidate per issue instead of one permanent alert per Refs PR.
            completing, pull = max(merged_links, key=lambda pair: (pair[0], pair[1]["merged_at"]))
            add("open_after_completing_merge" if completing else "merged_link_needs_review", record, url,
                    pull["merged_at"], "Supervisor / landing reconciler",
                    "Linked PR merged while issue remains open; completing scope is explicit." if completing else
                    "Linked PR merged while issue remains open; Refs/partial scope is not proof of completion. Review the existing finish line.",
                    evidence=[pull.get("html_url"), {"wait_state": wait["state"], "waits": wait["waits"]}])

    owner_terms = ("owner", "Director", *owners, *director_names)
    for register in registers or []:
        rows, register_gaps = register_findings(register, now=now, owner_terms=owner_terms)
        gaps.extend(register_gaps)
        for row in rows:
            add(row["kind"], {"repo": row["repo"], "item": {"number": None}}, row["url"], row["since"],
                row["next_actor"], row["detail"], evidence=[{"last_checked": row["last_checked"], "next_check": row["next_check"]}])
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
    return {"read_only": True, "sampled_at": now.isoformat(), "age_threshold_hours": age_hours,
            "complete": not gaps, "findings": findings, "coverage_gaps": gaps,
            "scope_notes": ["Decisions and completion records outside the selected repositories/history are unverified.",
                            "Unscheduled promises without a parseable due time are not ageable; no promise state is invented.",
                            "GitHub enqueue/blocked labels are existing helper views, not train custody authority; service-only holds are outside coverage."]}


def collect(repos: list[str], *, reader, now: dt.datetime, age_hours: float = 2,
            history_days: int = 30, max_requests: int = 800, max_pages: int = 20,
            quota_reserve: int = 250, owner: str | None = None, director_names: tuple[str, ...] = (),
            question_queue: str | None = None, policy_reader=None, director: str | None = None) -> dict:
    """Reuse the direction audit's paged inventory with one global bounded read budget."""
    gaps: list[dict] = []
    records: list[dict] = []
    cache: dict[str, Any] = {}
    budget = max_requests
    reads = 0
    policy_reads = 0
    policies = {}
    director = director.casefold() if director else None
    repos = [repo.casefold() for repo in repos]

    def fetch(args):
        nonlocal reads, budget
        path = args[1]
        if path not in cache:
            if reads >= budget:
                raise audit.AuditError("request_budget_exhausted")
            reads += 1
            try:
                cache[path] = reader.get_json(path, step="stranded_inventory")
            except (github_read.GitHubReadError, github_read.GitHubReadShapeError, TimeoutError) as exc:
                cause = getattr(getattr(getattr(exc, "result", None), "failure", None), "cause", None)
                if cause in {"quota_reserve", "deadline_exceeded", "cancelled"} or isinstance(exc, TimeoutError):
                    budget = reads
                raise audit.AuditError(cause or type(exc).__name__) from exc
        return cache[path]

    def listing(path):
        try:
            rows, cut = audit.fetch_paginated(path, fetch=fetch, max_pages=max_pages)
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
        except audit.AuditError as exc:
            gaps.append({"source": owner, "reason": str(exc)})
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
    if question_queue:
        question_queue = question_queue.casefold()
        queue_repo, queue_number = question_queue.split("#")
        queue = listing(f"repos/{queue_repo}/issues/{queue_number}/comments")
        try:
            from skills.supervisor.scripts import question_delivery

            normalize = lambda c: {**c, "author": login(c), "url": c.get("html_url") or c.get("url")}
            normalized_queue = [normalize(c) for c in queue]
            bots = set(github_identity.configured_bot_logins())
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
    for state in ("open", "reopen_triggers", "closed"):
        for repo in ordered_repos:
            if reads >= budget:
                unread_scopes.append({"repo": repo, "state": state})
                continue
            metadata = one(f"repos/{repo}")
            if metadata.get("archived") is True and state == "open":
                continue
            if not isinstance(metadata.get("archived"), bool) or not metadata.get("default_branch"):
                gaps.append({"source": repo, "reason": "repository_state_unavailable"})
                continue
            if state == "open":
                register = operational_register(repo, metadata["default_branch"])
                if register:
                    registers.append(register)
            suffix = "state=open" if state == "open" else (
                "state=closed&labels=" + quote(github_plan_trigger.INDEX_LABEL, safe="")
                if state == "reopen_triggers" else f"state=closed&since={history_start.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            issues = listing(f"repos/{repo}/issues?{suffix}")
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
                    break
                if not isinstance(item, dict) or type(item.get("number")) is not int:
                    gaps.append({"source": repo, "reason": "invalid_issue"})
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
                    continue
                seen.add(number)
                attempted_records += 1
                before_comments = len(gaps)
                record = {"repo": repo, "item": item, "default_branch": metadata["default_branch"],
                          "train_policy": policy,
                          "comments": [] if item.get("comments") == 0 else listing(f"repos/{repo}/issues/{number}/comments")}
                if len(gaps) != before_comments:
                    continue
                valid_comments = [c for c in record["comments"] if type(c.get("id")) is int
                                  and isinstance(c.get("html_url"), str) and isinstance(c.get("created_at"), str)]
                if len(valid_comments) != len(record["comments"]):
                    gaps.append({"source": f"{repo}#{number}/comments", "reason": "invalid_comment"})
                comments_complete = len(valid_comments) == len(record["comments"])
                record["comments"] = valid_comments
                if state != "open":
                    record["trigger_only"] = not recent
                    if "pull_request" not in item and comments_complete:
                        try:
                            trigger = github_plan_trigger.current(item, valid_comments,
                                github_plan_wait.authors(repo) | ({director} if director else set()))
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
                records.append(record)
                collected.add((repo, number))
    if unread_scopes:
        gaps.append({"source": "repository_inventory", "reason": "unread_repository_scopes", "scopes": unread_scopes})
    if question_delivery is not None:
        try:
            source_comments = [c for r in records if not r.get("trigger_only") for c in r["comments"]] + related_comments
            sources = {c.get("html_url"): normalize(c) for c in source_comments}
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
                    and not delivery_view.get("untrusted_records") and unread_registered):
                # The separate unread-source gap remains authoritative; these
                # records are unexamined, not evidence that a source was deleted.
                delivery_view["complete"] = True
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            gaps.append({"source": question_queue, "reason": "question_delivery_contract_error", "error_type": type(error).__name__})
    result = report(records, now=now, age_hours=age_hours,
                    trusted=set(github_identity.configured_bot_logins()),
                    owners={director} if director else {r.split('/')[0] for r in repos} | ({owner} if owner else set()),
                    gaps=gaps, director_names=director_names, delivery_view=delivery_view,
                    related_comments=related_comments, registers=registers)
    result.update({"repositories": repos, "operational_registers": [f"{r['repo']}:{r['path']}" for r in registers], "checked_records": len(records), "attempted_records": attempted_records, "requests": reads,
                   "history_days": history_days, "closed_trigger_history": "all", "quota_reserve": quota_reserve, "policy_reads": policy_reads})
    return result


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
    for row in result["findings"]:
        age = f"{row['age_hours']:g}h" if row["age_hours"] is not None else "age unknown"
        lines.append(f"- {row['kind']} ({age}): {row['url']} — {row['next_actor']}. {row['detail']}")
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
