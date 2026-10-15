# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Read-only stranded-work view of the existing direction audit inventory."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

import direction_audit as audit
from skills.github.scripts import github_identity, github_plan_wait, github_read, ready_handoff
from skills.supervisor.scripts import oq

UTC = dt.timezone.utc
QUESTION = re.compile(r"\bQ[1-9][0-9]*\b")
PREFIXES = ("Owner question:", "Director question:")
DECISION = re.compile(r"^\s*\**(?:Director|Owner) decision\b", re.I)
DUE = re.compile(r"(?im)^\s*(?:[-*] )?\**Due\**:\s*(\d{4}-\d{2}-\d{2}T[^\s*]+)")
CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+(?:https://github.com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)(\d+)\b", re.I)


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
           delivery_view: dict | None = None) -> dict:
    """Interpret retained API facts. A finding requests review, never authorizes a write."""
    findings: list[dict] = []
    gaps = list(gaps or [])
    trusted = {name.casefold() for name in trusted} | {name.casefold() for name in owners}
    owners = {name.casefold() for name in owners}
    comments = [c for r in records for c in r.get("comments", [])]
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
                 (login(c) in trusted and DECISION.match(question_body(c)))]
    deliveries = {row["url"]: row for row in (delivery_view or {}).get("questions", [])}
    if delivery_view is not None and delivery_view.get("complete") is not True:
        gaps.append({"source": "question_delivery", "reason": "queue_view_incomplete"})

    def add(kind, record, source, since, actor, detail, *, evidence=None):
        since = stamp(since)
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
        qids = set(QUESTION.findall(question_body(question)))
        url = question.get("html_url") or question.get("url")
        asked = stamp(question.get("created_at"))
        return any(stamp(c.get("created_at")) and asked and stamp(c["created_at"]) > asked
                   and not question_body(c).startswith(PREFIXES)
                   and ((url and url in (c.get("body") or "")) or
                        bool(qids & set(QUESTION.findall(c.get("body") or "")))) for c in candidates)

    for record in records:
        item, repo = record["item"], record["repo"]
        url = item.get("html_url") or f"https://github.com/{repo}/issues/{item['number']}"
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
            if DECISION.match(body) and re.search(r"Measured after one day", body, re.I):
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
                if "ready-to-merge" not in labels(item):
                    add("ready_handoff_unrouted", record, latest["html_url"], latest.get("updated_at") or latest.get("created_at"),
                        "Supervisor", "Current-head Ready has no enqueue view; verify existing train custody before routing.")
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
                    source.get("updated_at") or source.get("created_at"), "; ".join(receivers),
                    "Recorded receiver still has an unresolved wait. Age uses the wait source's last update as a conservative lower bound.",
                    evidence=wait["waits"])
        blockers = record.get("blockers")
        if ("plan:blocked" in labels(item) and wait["state"] == "clear" and blockers
                and all(b.get("state") == "closed" and b.get("state_reason") == "completed" for b in blockers)):
            closed = max((b.get("closed_at") or "" for b in blockers), default="")
            add("stale_blocked_view", record, url, closed, "Supervisor / landing reconciler",
                "Blocked view remains after all recorded native blockers completed and structured wait is clear.",
                evidence=[b.get("html_url") for b in blockers])
        for pull in record.get("linked_pulls", []):
            # Explicit closing scope is a review candidate; even this does not settle real waits.
            refs = {(m[1] or m[2] or repo, int(m[3])) for m in CLOSING.finditer(pull.get("body") or "")}
            if pull.get("merged_at") and pull.get("base", {}).get("ref") == record.get("default_branch"):
                completing = (repo, item["number"]) in refs
                add("open_after_completing_merge" if completing else "merged_link_needs_review", record, url,
                    pull["merged_at"], "Supervisor / landing reconciler",
                    "Linked PR merged while issue remains open; completing scope is explicit." if completing else
                    "Linked PR merged while issue remains open; Refs/partial scope is not proof of completion. Review the existing finish line.",
                    evidence=[pull.get("html_url"), {"wait_state": wait["state"], "waits": wait["waits"]}])

    findings.sort(key=lambda row: (row["since"] or "", row["repo"], row["number"], row["kind"]))
    return {"read_only": True, "sampled_at": now.isoformat(), "age_threshold_hours": age_hours,
            "complete": not gaps, "findings": findings, "coverage_gaps": gaps,
            "scope_notes": ["Decisions and completion records outside the selected repositories/history are unverified.",
                            "Unscheduled promises without a parseable due time are not ageable; no promise state is invented.",
                            "GitHub enqueue/blocked labels are existing helper views, not train custody authority; service-only holds are outside coverage."]}


def collect(repos: list[str], *, reader, now: dt.datetime, age_hours: float = 2,
            history_days: int = 30, max_requests: int = 800, max_pages: int = 20,
            quota_reserve: int = 250, owner: str | None = None, director_names: tuple[str, ...] = (),
            question_queue: str | None = None) -> dict:
    """Reuse the direction audit's paged inventory with one global bounded read budget."""
    gaps: list[dict] = []
    records: list[dict] = []
    cache: dict[str, Any] = {}
    budget = max_requests
    reads = 0

    def fetch(args):
        nonlocal reads
        path = args[1]
        if path not in cache:
            if reads >= budget:
                raise audit.AuditError("request_budget_exhausted")
            reads += 1
            try:
                cache[path] = reader.get_json(path, step="stranded_inventory")
            except (github_read.GitHubReadError, github_read.GitHubReadShapeError, TimeoutError) as exc:
                raise audit.AuditError(type(exc).__name__) from exc
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
    if type(remaining) is not int:
        gaps.append({"source": "rate_limit", "reason": "quota_unavailable"})
        budget = reads
    else:
        budget = min(max_requests, reads + max(0, remaining - quota_reserve))
    if owner:
        try:
            discovered, cut = audit.owner_repositories(owner, fetch=fetch)
            repos = sorted(set(repos) | set(discovered))
            if cut:
                gaps.append({"source": owner, "reason": "repository_page_limit"})
        except audit.AuditError as exc:
            gaps.append({"source": owner, "reason": str(exc)})
    since = (now - dt.timedelta(days=history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for repo in dict.fromkeys(repos):
        if reads >= budget:
            gaps.append({"source": repo, "reason": "request_or_quota_budget_exhausted"})
            continue
        metadata = one(f"repos/{repo}")
        if metadata.get("archived") is True:
            continue
        if metadata.get("archived") is not False or not metadata.get("default_branch"):
            gaps.append({"source": repo, "reason": "repository_state_unavailable"})
            continue
        issues = listing(f"repos/{repo}/issues?state=open")
        # Closed records matter: Q249 was promised on an already-completed research issue.
        issues += listing(f"repos/{repo}/issues?state=closed&since={since}")
        seen = set()
        for item in issues:
            if not isinstance(item, dict) or type(item.get("number")) is not int:
                gaps.append({"source": repo, "reason": "invalid_issue"})
                continue
            number = item["number"]
            if number in seen:
                gaps.append({"source": f"{repo}#{number}", "reason": "inventory_changed_during_pagination"})
                continue
            seen.add(number)
            record = {"repo": repo, "item": item, "default_branch": metadata["default_branch"],
                      "comments": listing(f"repos/{repo}/issues/{number}/comments")}
            valid_comments = [c for c in record["comments"] if type(c.get("id")) is int
                              and isinstance(c.get("html_url"), str) and isinstance(c.get("created_at"), str)]
            if len(valid_comments) != len(record["comments"]):
                gaps.append({"source": f"{repo}#{number}/comments", "reason": "invalid_comment"})
            record["comments"] = valid_comments
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
                    linked = {(event.get("source") or {}).get("issue", {}).get("number") for event in record["timeline"]
                              if event.get("event") == "cross-referenced" and
                              (event.get("source") or {}).get("issue", {}).get("pull_request") and
                              (event.get("source") or {}).get("issue", {}).get("repository_url") == f"https://api.github.com/repos/{repo}"}
                    record["linked_pulls"] = [one(f"repos/{repo}/pulls/{n}") for n in sorted(linked) if type(n) is int]
            records.append(record)
    delivery_view = None
    if question_queue:
        queue_repo, queue_number = question_queue.split("#")
        queue = listing(f"repos/{queue_repo}/issues/{queue_number}/comments")
        try:
            from skills.supervisor.scripts import question_delivery

            normalize = lambda c: {**c, "author": login(c), "url": c.get("html_url") or c.get("url")}
            delivery_view = question_delivery.inventory(
                [normalize(c) for r in records for c in r["comments"]],
                [normalize(c) for c in queue], queue_repo.split("/")[0],
                set(github_identity.configured_bot_logins()), now=now,
            )
        except (ImportError, ValueError, TypeError, KeyError):
            gaps.append({"source": question_queue, "reason": "question_delivery_view_unavailable"})
    else:
        gaps.append({"source": "question_delivery", "reason": "question_queue_not_selected"})
    result = report(records, now=now, age_hours=age_hours,
                    trusted=set(github_identity.configured_bot_logins()),
                    owners={r.split('/')[0] for r in repos} | ({owner} if owner else set()),
                    gaps=gaps, director_names=director_names, delivery_view=delivery_view)
    result.update({"repositories": repos, "checked_records": len(records), "requests": reads,
                   "history_days": history_days, "quota_reserve": quota_reserve})
    return result


def markdown(result: dict) -> str:
    lines = ["Daily stranded-work sweep", f"Observed: {result['sampled_at']}",
             f"Coverage: {'complete within selected scope' if result['complete'] else 'INCOMPLETE'}; {len(result['findings'])} candidates."]
    for row in result["findings"]:
        age = f"{row['age_hours']:g}h" if row["age_hours"] is not None else "age unknown"
        lines.append(f"- {row['kind']} ({age}): {row['url']} — {row['next_actor']}. {row['detail']}")
    for gap in result["coverage_gaps"]:
        lines.append(f"- Coverage gap: {gap['source']} — {gap['reason']}")
    lines.extend(result["scope_notes"])
    return "\n".join(lines) + "\n"
