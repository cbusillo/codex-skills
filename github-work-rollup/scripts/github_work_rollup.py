#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Read-only GitHub work rollup collector and renderer."""
# pyright: reportMissingModuleSource=false

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml  # type: ignore[import-untyped]


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
GH = os.environ.get("GITHUB_WORK_ROLLUP_GH") or str(ROOT / "github/scripts/gh-with-env-token")
DEFAULT_CONFIG = ROOT / ".local/github-work-rollup.yaml"
DEFAULT_PEOPLE_INDEX = ROOT / ".local/people.yaml"
SCRIPT_VERSION = 1
SUMMARY_LEVELS = {"concise", "standard", "detailed"}
REPORT_MODES = {"activity", "backlog", "standup"}
REPORT_LAYOUTS = {"operator", "manager", "executive"}
DEFAULT_REPO_ITEM_COLLECTION_LIMIT = 1000
DEFAULT_RELEASE_COLLECTION_LIMIT = 1000
DEFAULT_WORKFLOW_COLLECTION_LIMIT = 1000
GITHUB_SEARCH_PAGE_SIZE = 100
GITHUB_SEARCH_RESULT_LIMIT = 1000
BUCKET_ORDER = [
    "needs_attention",
    "blocked",
    "waiting",
    "ready_for_review",
    "ready_for_merge_decision",
    "in_progress",
    "stale_or_needs_reconciliation",
    "recently_completed",
]
ACTIONABLE_PRIORITY_BUCKETS = {bucket for bucket in BUCKET_ORDER if bucket != "recently_completed"}


class RollupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime
    label: str


@dataclass(frozen=True)
class BriefPeriod:
    title_prefix: str
    summary_subject: str
    priorities_heading: str
    alignment_window: str
    followup_phrase: str
    source_window: str


@dataclass(frozen=True)
class ExecutiveWorkstream:
    portfolio_area: str
    workstream: str
    relationship: str
    initiatives: tuple[str, ...]
    active_count: int
    completed_count: int
    active_titles: tuple[str, ...]
    completed_titles: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a read-only GitHub work rollup.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Optional local YAML config.")
    parser.add_argument("--repo", action="append", default=[], help="OWNER/REPO. May be repeated.")
    parser.add_argument("--repo-owner", action="append", default=[], help="Owner/org whose repos should be scanned.")
    parser.add_argument("--subject", action="append", default=[], help="GitHub login to highlight. May be repeated.")
    parser.add_argument("--window", help="Lookback window such as 24h, 7d, or 1w.")
    parser.add_argument("--since", help="UTC ISO timestamp for window start.")
    parser.add_argument("--until", help="UTC ISO timestamp for window end.")
    parser.add_argument("--timezone", help="IANA timezone label for report display metadata.")
    parser.add_argument("--report-recipient", help="Human-facing report recipient label.")
    parser.add_argument("--people-index", type=Path, help="Optional private people YAML for recipient tailoring.")
    parser.add_argument("--mode", choices=sorted(REPORT_MODES), help="activity, backlog, or standup.")
    parser.add_argument("--summary-level", choices=sorted(SUMMARY_LEVELS), help="concise, standard, or detailed.")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--layout", choices=sorted(REPORT_LAYOUTS), help="operator, manager, or executive layout style.")
    parser.add_argument("--output", type=Path, help="Write rendered output to this path.")
    parser.add_argument("--limit-repos", type=int, default=25)
    parser.add_argument("--limit-items", type=int, default=50)
    parser.add_argument("--collection-limit-items", type=int, help="Maximum PRs/issues to collect per repo/state before rendering limits are applied.")
    parser.add_argument("--release-collection-limit", type=int, help="Maximum releases to collect per repo before window filtering.")
    parser.add_argument("--workflow-collection-limit", type=int, help="Maximum workflow runs to collect per repo before window filtering.")
    parser.add_argument("--include-bots", action="store_true")
    parser.add_argument("--include-external-activity", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    settings = resolve_settings(args, config)
    try:
        payload = collect_rollup(settings)
    except RollupError as exc:
        payload = failure_payload(settings, str(exc))
        rendered = render_payload(payload, args.format)
        write_or_print(rendered, args.output or settings.get("output_path"))
        return 1
    rendered = render_payload(payload, args.format)
    write_or_print(rendered, args.output or settings.get("output_path"))
    return 0


def read_text_file(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def put_text_file(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    parsed = yaml.safe_load(read_text_file(path))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SystemExit(f"error: {path} must contain a YAML mapping")
    return parsed


def resolve_settings(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    window = resolve_window(args, config)
    explicit_scope = bool(args.repo or args.repo_owner or args.subject)
    repos = unique(args.repo if explicit_scope else as_str_list(config.get("repositories")))
    owners = unique(args.repo_owner if explicit_scope else as_str_list(config.get("repo_owners")))
    subjects = unique(args.subject if explicit_scope else as_str_list(config.get("subjects")))
    summary_level = args.summary_level or str(config.get("summary_level") or "standard")
    if summary_level not in SUMMARY_LEVELS:
        raise SystemExit(f"error: summary_level must be one of {', '.join(sorted(SUMMARY_LEVELS))}")
    mode = args.mode or str(config.get("mode") or "activity")
    if mode not in REPORT_MODES:
        raise SystemExit(f"error: mode must be one of {', '.join(sorted(REPORT_MODES))}")
    layout = getattr(args, "layout", None) or str(config.get("layout") or "operator")
    if layout not in REPORT_LAYOUTS:
        raise SystemExit(f"error: layout must be one of {', '.join(sorted(REPORT_LAYOUTS))}")
    return {
        "config_path": str(args.config) if args.config else None,
        "people_index": str(args.people_index or config.get("people_index") or DEFAULT_PEOPLE_INDEX),
        "timezone": args.timezone or str(config.get("timezone") or "UTC"),
        "report_recipient": args.report_recipient or str(config.get("report_recipient") or "GitHub work"),
        "window": window,
        "repositories": repos,
        "repo_owners": owners,
        "subjects": subjects,
        "mode": mode,
        "summary_level": summary_level,
        "layout": layout,
        "output_path": str(args.output or config.get("output_path") or ""),
        "include_external_activity": bool(args.include_external_activity or config.get("include_external_activity")),
        "include_bots": bool(args.include_bots or config.get("include_bots")),
        "noise_filters": config.get("noise_filters") if isinstance(config.get("noise_filters"), dict) else {},
        "priority_sections": config.get("priority_sections") if isinstance(config.get("priority_sections"), list) else [],
        "limit_repos": args.limit_repos,
        "limit_items": args.limit_items,
        "collection_limit_items": positive_int(
            args.collection_limit_items if args.collection_limit_items is not None else config.get("collection_limit_items"),
            DEFAULT_REPO_ITEM_COLLECTION_LIMIT,
            "collection_limit_items",
        ),
        "release_collection_limit": positive_int(
            args.release_collection_limit if args.release_collection_limit is not None else config.get("release_collection_limit"),
            DEFAULT_RELEASE_COLLECTION_LIMIT,
            "release_collection_limit",
        ),
        "workflow_collection_limit": positive_int(
            args.workflow_collection_limit if args.workflow_collection_limit is not None else config.get("workflow_collection_limit"),
            DEFAULT_WORKFLOW_COLLECTION_LIMIT,
            "workflow_collection_limit",
        ),
    }


def positive_int(value: object, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise SystemExit(f"error: {name} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise SystemExit(f"error: {name} must be a positive integer")
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise SystemExit(f"error: {name} must be a positive integer") from exc
    else:
        raise SystemExit(f"error: {name} must be a positive integer")
    if parsed < 1:
        raise SystemExit(f"error: {name} must be a positive integer")
    return parsed


def setting_positive_int(settings: dict[str, Any], name: str, default: int) -> int:
    value = settings.get(name)
    return value if isinstance(value, int) and value > 0 else default


def resolve_window(args: argparse.Namespace, config: dict[str, Any]) -> Window:
    until = parse_timestamp(args.until) or datetime.now(timezone.utc)
    since = parse_timestamp(args.since)
    label = args.window or str(config.get("default_window") or "24h")
    if since is None:
        since = until - timedelta(hours=parse_duration_hours(label))
    if since >= until:
        raise SystemExit("error: --since must be before --until")
    return Window(since=since, until=until, label=label)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_duration_hours(value: str) -> float:
    text = value.strip().casefold().replace("_", " ")
    text = re.sub(r"^(past|last)\s+", "", text)
    aliases = {"day": 24.0, "24h": 24.0, "week": 168.0, "7d": 168.0}
    if text in aliases:
        return aliases[text]
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)", text)
    if match:
        return float(match.group(1))
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(d|day|days)", text)
    if match:
        return float(match.group(1)) * 24.0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(w|week|weeks)", text)
    if match:
        return float(match.group(1)) * 168.0
    raise SystemExit(f"error: unsupported window {value!r}")


def as_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        stripped = value.strip()
        key = stripped.casefold()
        if stripped and key not in seen:
            seen.add(key)
            result.append(stripped)
    return result


def resolve_recipient_profile(settings: dict[str, Any]) -> dict[str, Any]:
    query = str(settings.get("report_recipient") or "").strip()
    index_path = Path(str(settings.get("people_index") or ""))
    if not query or not index_path.exists():
        return {}
    try:
        data = yaml.safe_load(read_text_file(index_path)) or {}
    except yaml.YAMLError:
        return {}
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list):
        return {}
    matches = []
    for person in people:
        if not isinstance(person, dict):
            continue
        if person_matches_recipient(person, query):
            matches.append(person)
    if len(matches) == 1:
        return compact_recipient_profile(matches[0])
    if len(matches) > 1:
        add_collection_warning(settings, f"Report recipient {query!r} matched multiple people; recipient tailoring was skipped.")
    return {}


def person_matches_recipient(person: dict[str, Any], query: str) -> bool:
    candidates: list[str] = []
    for key in ("id", "display_name", "preferred_reference"):
        value = person.get(key)
        if isinstance(value, str):
            candidates.append(value)
    candidates.extend(as_str_list(person.get("aliases")))
    contacts = person.get("contacts")
    if isinstance(contacts, dict):
        for contact in contacts.values():
            if isinstance(contact, str):
                candidates.append(contact)
            elif isinstance(contact, dict):
                candidates.extend(as_str_list(contact.get("username")))
                candidates.extend(as_str_list(contact.get("handle")))
    normalized_query = normalize_person_token(query)
    compact_query = compact_person_token(query)
    return any(
        normalize_person_token(candidate) == normalized_query
        or compact_person_token(candidate) == compact_query
        for candidate in candidates
    )


def normalize_person_token(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("@"):
        text = text[1:]
    return re.sub(r"[\s._-]+", " ", text.casefold()).strip()


def compact_person_token(value: object) -> str:
    return re.sub(r"[\s._-]+", "", normalize_person_token(value))


def compact_recipient_profile(person: dict[str, Any]) -> dict[str, Any]:
    relationship_raw = person.get("relationship")
    organization_raw = person.get("organization")
    preferences_raw = person.get("preferences")
    relationship: dict[str, Any] = relationship_raw if isinstance(relationship_raw, dict) else {}
    organization: dict[str, Any] = organization_raw if isinstance(organization_raw, dict) else {}
    preferences: dict[str, Any] = preferences_raw if isinstance(preferences_raw, dict) else {}
    communication_raw = preferences.get("communication_style")
    communication: dict[str, Any] = communication_raw if isinstance(communication_raw, dict) else {}
    return remove_empty(
        {
            "id": person.get("id"),
            "display_name": person.get("display_name"),
            "preferred_reference": person.get("preferred_reference"),
            "relationship": relationship.get("kind"),
            "roles": relationship.get("roles"),
            "company": organization.get("company"),
            "technical_depth": communication.get("technical_depth"),
            "framing": communication.get("framing"),
            "detail_preference": communication.get("detail_preference"),
        }
    )


def public_recipient_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return remove_empty(
        {
            "id": profile.get("id"),
            "display_name": profile.get("display_name"),
            "preferred_reference": profile.get("preferred_reference"),
            "relationship": profile.get("relationship"),
            "roles": profile.get("roles"),
            "company": profile.get("company"),
            "technical_depth": profile.get("technical_depth"),
            "framing": profile.get("framing"),
            "detail_preference": profile.get("detail_preference"),
        }
    )


def remove_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item_identity(item)
        if key in seen:
            seen[key] = merge_duplicate_item(seen[key], item)
        else:
            seen[key] = dict(item)
    return list(seen.values())


def collection_lanes(settings: dict[str, Any]) -> dict[str, bool]:
    mode = str(settings.get("mode") or "activity")
    return {
        "recent_activity": True,
        "open_backlog": mode in {"backlog", "standup"},
        "recent_completions": mode in {"activity", "backlog", "standup"},
    }


def collect_open_backlog(settings: dict[str, Any]) -> bool:
    return collection_lanes(settings)["open_backlog"]


def item_collection_lane(state: str, entry: dict[str, Any], settings: dict[str, Any]) -> str:
    mode = str(settings.get("mode") or "activity")
    if state == "open" and mode == "standup" and item_in_window(entry, state, settings["window"]):
        return "recent_activity"
    if state == "open" and collect_open_backlog(settings):
        return "open_backlog"
    if state in {"closed", "merged"}:
        return "recent_completion"
    return "recent_activity"


def should_window_filter_state(state: str, settings: dict[str, Any]) -> bool:
    return not (state == "open" and collect_open_backlog(settings))


def item_identity(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    repo = item.get("repo")
    number = item.get("number")
    if kind and repo and number is not None:
        return f"{kind}:{repo}:{number}"
    if item.get("url"):
        return str(item["url"])
    return f"{kind}:{repo}:{item.get('title')}:{item.get('updated_at')}"


def merge_duplicate_item(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    primary = preferred_item(existing, incoming)
    merged = dict(primary)
    for field, singular in (("subjects", "subject"), ("subject_matches", "subject_match")):
        values = as_str_list(existing.get(field)) + as_str_list(incoming.get(field))
        values.extend(value for value in (existing.get(singular), incoming.get(singular)) if isinstance(value, str))
        if values:
            merged[field] = unique(values)
    merged["priority"] = max(int(existing.get("priority") or 0), int(incoming.get("priority") or 0))
    merged["collection_lane"] = preferred_collection_lane(existing, incoming)
    if merged.get("bucket") == "recently_completed":
        merged["handoff"] = None
    elif not merged.get("handoff"):
        merged["handoff"] = existing.get("handoff") or incoming.get("handoff")
    return merged


def preferred_collection_lane(*items: dict[str, Any]) -> str | None:
    lanes = [str(item.get("collection_lane")) for item in items if item.get("collection_lane")]
    for lane in ("recent_completion", "recent_activity", "open_backlog"):
        if lane in lanes:
            return lane
    return lanes[0] if lanes else None


def preferred_item(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_score = item_detail_score(first)
    second_score = item_detail_score(second)
    return second if second_score > first_score else first


def item_detail_score(item: dict[str, Any]) -> int:
    score = 0
    if item.get("state") == "merged":
        score += 20
    if item.get("state") == "closed":
        score += 10
    for field in ("review_decision", "draft", "assignees", "completed_at"):
        if field in item:
            score += 3
    if item.get("kind") != "collection_error":
        score += 1
    return score


def collect_rollup(settings: dict[str, Any]) -> dict[str, Any]:
    preflight = github_preflight(settings)
    repos = resolve_repositories(settings)
    if not repos and not settings["subjects"]:
        raise RollupError("No repositories or subjects configured. Pass --repo, --repo-owner, or --subject.")
    settings = {**settings, "resolved_repositories": repos}
    settings.setdefault("collection_warnings", [])
    recipient_profile = resolve_recipient_profile(settings)
    items, buckets, releases, workflows = collect_activity(settings, repos)

    collection_warnings = collection_warnings_from(settings)
    limitations_list = [*limitations(settings, repos), *collection_warnings]
    comparison = collect_activity_comparison(settings, repos, buckets, releases, workflows, collection_warnings)

    return {
        "ok": True,
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "generated_at": format_ts(datetime.now(timezone.utc)),
        "window": window_json(settings["window"]),
        "timezone": settings["timezone"],
        "report_recipient": settings["report_recipient"],
        "recipient_profile": recipient_profile,
        "repositories": repos,
        "subjects": settings["subjects"],
        "summary_level": settings["summary_level"],
        "mode": settings["mode"],
        "layout": settings["layout"],
        "display_limit": display_item_limit(settings),
        "collection_lanes": collection_lanes(settings),
        "preflight": preflight,
        "buckets": buckets,
        "summary": summary_counts(buckets, collection_warnings),
        "display_window": display_window_json(settings["window"], settings["timezone"]),
        "priority_sections": priority_sections(items, settings),
        "limitations": limitations_list,
        "coverage_gaps": configured_repo_gaps(repos, buckets, limitations_list),
        "releases": releases,
        "workflows": workflows,
        "activity_comparison": comparison,
    }


def collect_activity(
    settings: dict[str, Any],
    repos: list[str],
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    for repo in repos:
        items.extend(collect_repo_items(repo, settings))
    if settings["subjects"] and (settings["include_external_activity"] or not repos):
        items.extend(collect_subject_items(settings))
    items = deduplicate_items(items)
    buckets = bucket_items(items, settings)

    releases: list[dict[str, Any]] = []
    workflows: list[dict[str, Any]] = []
    for repo in repos:
        releases.extend(collect_repo_releases(repo, settings))
        workflows.extend(collect_repo_workflows(repo, settings))
    return items, buckets, releases, workflows


def configured_repo_gaps(repos: list[str], buckets: dict[str, list[dict[str, Any]]], limitations_list: list[str]) -> list[str]:
    gaps: list[str] = []
    for repo in repos:
        repo_errors = [
            item
            for rows in buckets.values()
            for item in rows
            if item.get("repo") == repo and is_collection_error(item)
        ]
        repo_limitations = [note for note in limitations_list if collection_warning_repo(note) == repo]
        if repo_errors or repo_limitations:
            gaps.append(repo)
    return unique(gaps)


def collection_warning_repo(note: str) -> str | None:
    if not note.startswith("Could not collect "):
        return None
    prefix, separator, _message = note.partition(":")
    if not separator:
        return None
    _collection_kind, separator, repo = prefix.partition(" for ")
    return repo.strip() if separator else None


def collect_activity_comparison(
    settings: dict[str, Any],
    repos: list[str],
    current_buckets: dict[str, list[dict[str, Any]]],
    current_releases: list[dict[str, Any]],
    current_workflows: list[dict[str, Any]],
    current_warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    if settings.get("layout") != "executive":
        return None
    if has_collection_failures(current_buckets, current_warnings or []):
        return {"summary": "Comparison is incomplete because one or more configured sources could not be collected."}
    current = comparison_counts(current_buckets, current_releases, current_workflows)
    previous_window = preceding_window(settings["window"])
    previous_settings = {**settings, "window": previous_window, "collection_warnings": []}
    try:
        _, previous_buckets, previous_releases, previous_workflows = collect_activity(previous_settings, repos)
    except Exception as exc:  # pragma: no cover - defensive for best-effort comparison only
        return {"summary": f"Previous-window comparison could not be collected: {exc}"}
    previous_warnings = collection_warnings_from(previous_settings)
    if has_collection_failures(previous_buckets, previous_warnings):
        return {"summary": "Comparison is incomplete because one or more configured sources could not be collected in the previous window."}
    previous = comparison_counts(previous_buckets, previous_releases, previous_workflows)
    return {
        "summary": comparison_summary(current, previous, settings["window"]),
        "current": current,
        "previous": previous,
        "previous_window": window_json(previous_window),
    }


def has_collection_failures(buckets: dict[str, list[dict[str, Any]]], warnings: list[str]) -> bool:
    if any(is_collection_error(item) for rows in buckets.values() for item in rows):
        return True
    return any("Could not collect" in warning for warning in warnings)


def preceding_window(window: Window) -> Window:
    duration = window.until - window.since
    return Window(since=window.since - duration, until=window.since, label=f"previous {window.label}")


def comparison_counts(
    buckets: dict[str, list[dict[str, Any]]],
    releases: list[dict[str, Any]],
    workflows: list[dict[str, Any]],
) -> dict[str, int]:
    completed_items = real_work_items(buckets.get("recently_completed") or [])
    open_items = [
        item
        for bucket, rows in buckets.items()
        if bucket != "recently_completed"
        for item in real_work_items(rows)
    ]
    failed_runs = [workflow for workflow in workflows if str(workflow.get("conclusion") or "").casefold() == "failure"]
    return {
        "completed": len(completed_items),
        "open": len(open_items),
        "releases": len(releases),
        "failed_runs": len(failed_runs),
        "visible": len(completed_items) + len(open_items) + len(releases),
    }


def comparison_summary(current: dict[str, int], previous: dict[str, int], window: Window) -> str:
    current_visible = current.get("visible", 0)
    previous_visible = previous.get("visible", 0)
    delta = current_visible - previous_visible
    if previous_visible == 0 and current_visible:
        trend = "higher than the previous window, which had no visible work"
    elif delta > 0:
        trend = f"higher than the previous window (+{delta})"
    elif delta < 0:
        trend = f"lower than the previous window ({delta})"
    else:
        trend = "about even with the previous window"
    period = brief_period({"window": window_json(window)}).source_window
    return (
        f"Activity was {trend}: {item_count_phrase(current_visible, 'visible item')} this {period} "
        f"versus {previous_visible} in the previous {period}."
    )


def github_preflight(settings: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for command in ([GH, "auth", "status"], [GH, "api", "user", "--jq", "{login:.login,id:.id,name:.name}"]):
        result = run(command)
        checks.append(command_summary(command, result))
        if result.returncode != 0:
            raise RollupError(f"GitHub preflight failed: {' '.join(command)}\n{trim(result.stderr or result.stdout)}")
    owners = settings.get("repo_owners") or []
    if owners:
        command = [GH, "repo", "list", owners[0], "--limit", "1", "--json", "nameWithOwner"]
        result = run(command)
        checks.append(command_summary(command, result))
        if result.returncode != 0:
            raise RollupError(f"GitHub repo preflight failed for {owners[0]}: {trim(result.stderr or result.stdout)}")
    return {"ok": True, "checks": checks}


def resolve_repositories(settings: dict[str, Any]) -> list[str]:
    repos = list(settings["repositories"])
    for owner in settings["repo_owners"]:
        command = [GH, "repo", "list", owner, "--limit", str(settings["limit_repos"]), "--json", "nameWithOwner,isArchived"]
        result = run(command)
        if result.returncode != 0:
            raise RollupError(f"Unable to list repositories for {owner}: {trim(result.stderr or result.stdout)}")
        for entry in json.loads(result.stdout or "[]"):
            if isinstance(entry, dict) and not entry.get("isArchived") and isinstance(entry.get("nameWithOwner"), str):
                repos.append(entry["nameWithOwner"])
    return unique(repos)


def collect_repo_items(repo: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *collect_open_items(repo, "pr", settings),
        *collect_prs(repo, "closed", settings),
        *collect_prs(repo, "merged", settings),
        *collect_open_items(repo, "issue", settings),
        *collect_issues(repo, "closed", settings),
    ]


def collect_repo_releases(repo: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    collection_limit = setting_positive_int(settings, "release_collection_limit", DEFAULT_RELEASE_COLLECTION_LIMIT)
    command = [
        GH,
        "release",
        "list",
        "--repo",
        repo,
        "--limit",
        str(collection_limit),
        "--exclude-drafts",
        "--exclude-pre-releases",
        "--json",
        "tagName,name,createdAt,publishedAt,isDraft,isPrerelease",
    ]
    result = run(command)
    if result.returncode != 0:
        add_collection_warning(settings, f"Could not collect releases for {repo}: {trim(result.stderr or result.stdout)}")
        return []

    rows = []
    window = settings["window"]
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(entries, list) and len(entries) >= collection_limit:
        add_collection_warning(settings, f"Release collection for {repo} reached {collection_limit}; release counts may be incomplete.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("isDraft") or entry.get("isPrerelease"):
            continue
        pub_str = entry.get("publishedAt") or entry.get("createdAt")
        pub_dt = parse_timestamp(pub_str)
        if pub_dt and window.since <= pub_dt <= window.until:
            rows.append({
                "repo": repo,
                "tag_name": entry.get("tagName"),
                "name": entry.get("name"),
                "published_at": pub_str,
                "url": release_url(repo, entry.get("tagName")),
            })
    return rows


def release_url(repo: str, tag_name: object) -> str | None:
    return f"https://github.com/{repo}/releases/tag/{tag_name}" if isinstance(tag_name, str) and tag_name else None


def collect_repo_workflows(repo: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    collection_limit = setting_positive_int(settings, "workflow_collection_limit", DEFAULT_WORKFLOW_COLLECTION_LIMIT)
    command = [
        GH,
        "run",
        "list",
        "--repo",
        repo,
        "--limit",
        str(collection_limit),
        "--json",
        "name,status,conclusion,createdAt,updatedAt,url",
    ]
    result = run(command)
    if result.returncode != 0:
        add_collection_warning(settings, f"Could not collect workflow runs for {repo}: {trim(result.stderr or result.stdout)}")
        return []

    rows = []
    window = settings["window"]
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(entries, list) and len(entries) >= collection_limit:
        add_collection_warning(settings, f"Workflow collection for {repo} reached {collection_limit}; automation counts may be incomplete.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").casefold()
        if status != "completed":
            continue
        completed_str = entry.get("updatedAt") or entry.get("createdAt")
        completed_dt = parse_timestamp(completed_str)
        if completed_dt and window.since <= completed_dt <= window.until:
            rows.append({
                "repo": repo,
                "name": entry.get("name"),
                "status": status,
                "conclusion": entry.get("conclusion"),
                "created_at": entry.get("createdAt"),
                "completed_at": completed_str,
                "url": entry.get("url"),
            })
    return rows


def collect_open_items(repo: str, kind: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    collector = collect_prs if kind == "pr" else collect_issues
    if settings["mode"] != "standup":
        return collector(repo, "open", settings)
    return [
        *collector(repo, "open", settings, force_window_filter=False),
        *collector(repo, "open", settings, force_window_filter=True),
    ]


def collect_subject_items(settings: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    since_day = settings["window"].since.date().isoformat()
    collection_limit = min(
        setting_positive_int(settings, "collection_limit_items", DEFAULT_REPO_ITEM_COLLECTION_LIMIT),
        GITHUB_SEARCH_RESULT_LIMIT,
    )
    for subject in settings["subjects"]:
        for qualifier in ("author", "commenter", "mentions"):
            query = f"{qualifier}:{subject} updated:>={since_day}"
            fetched_for_query = 0
            page = 1
            while fetched_for_query < collection_limit:
                per_page = min(GITHUB_SEARCH_PAGE_SIZE, collection_limit - fetched_for_query)
                result = run(
                    [
                        GH,
                        "api",
                        "--method",
                        "GET",
                        "search/issues",
                        "-f",
                        f"q={query}",
                        "-f",
                        f"per_page={per_page}",
                        "-f",
                        f"page={page}",
                    ]
                )
                if result.returncode != 0:
                    rows.append(collection_error(f"subject:{subject}", "search", qualifier, result))
                    break
                payload = json.loads(result.stdout or "{}")
                entries = payload.get("items") or [] if isinstance(payload, dict) else []
                if not isinstance(entries, list):
                    add_collection_warning(
                        settings,
                        f"Subject search for {subject} {qualifier} returned an unexpected response; subject counts may be incomplete.",
                    )
                    break
                total_count = payload.get("total_count") if isinstance(payload, dict) else None
                incomplete = bool(payload.get("incomplete_results")) if isinstance(payload, dict) else False
                fetched_for_query += len(entries)
                for entry in entries:
                    if not isinstance(entry, dict) or not subject_item_in_window(entry, settings["window"]):
                        continue
                    key = str(entry.get("html_url") or entry.get("url") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    if should_skip_bot(login_from(entry.get("user")), entry.get("user"), settings):
                        continue
                    rows.append(normalize_search_item(entry, subject, qualifier, settings))
                if incomplete or (isinstance(total_count, int) and total_count > fetched_for_query and (fetched_for_query >= collection_limit or len(entries) < per_page)):
                    add_collection_warning(
                        settings,
                        subject_search_warning(subject, qualifier, fetched_for_query, collection_limit),
                    )
                if len(entries) < per_page or fetched_for_query >= collection_limit:
                    break
                page += 1
    return rows


def subject_search_warning(subject: str, qualifier: str, fetched: int, limit: int) -> str:
    if fetched >= limit:
        return f"Subject search for {subject} {qualifier} reached {limit}; subject counts may be incomplete."
    return f"Subject search for {subject} {qualifier} returned incomplete results after {fetched} item(s); subject counts may be incomplete."


def collect_prs(
    repo: str,
    state: str,
    settings: dict[str, Any],
    force_window_filter: bool | None = None,
) -> list[dict[str, Any]]:
    fields = "number,title,url,author,labels,reviewDecision,isDraft,createdAt,updatedAt,closedAt,mergedAt,baseRefName"
    window_filter = force_window_filter if force_window_filter is not None else should_window_filter_state(state, settings)
    collection_limit = setting_positive_int(settings, "collection_limit_items", DEFAULT_REPO_ITEM_COLLECTION_LIMIT)
    command = [
        GH,
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(collection_limit),
    ]
    if window_filter:
        command.extend(["--search", search_window(settings["window"])])
    command.extend(["--json", fields])
    result = run(command)
    if result.returncode != 0:
        return [collection_error(repo, "pr", state, result)]
    rows = []
    entries = json.loads(result.stdout or "[]")
    if isinstance(entries, list) and len(entries) >= collection_limit:
        add_collection_warning(settings, f"{repo} {state} PR collection reached {collection_limit}; PR counts may be incomplete.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if window_filter and not item_in_window(entry, state, settings["window"]):
            continue
        if should_skip_bot(login_from(entry.get("author")), entry.get("author"), settings):
            continue
        rows.append(normalize_pr(repo, state, entry, settings))
    return rows


def collect_issues(
    repo: str,
    state: str,
    settings: dict[str, Any],
    force_window_filter: bool | None = None,
) -> list[dict[str, Any]]:
    fields = "number,title,url,author,labels,assignees,createdAt,updatedAt,closedAt,stateReason"
    window_filter = force_window_filter if force_window_filter is not None else should_window_filter_state(state, settings)
    collection_limit = setting_positive_int(settings, "collection_limit_items", DEFAULT_REPO_ITEM_COLLECTION_LIMIT)
    command = [
        GH,
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(collection_limit),
    ]
    if window_filter:
        command.extend(["--search", search_window(settings["window"])])
    command.extend(["--json", fields])
    result = run(command)
    if result.returncode != 0:
        err_msg = (result.stderr or result.stdout or "").casefold()
        if "disabled issues" in err_msg or "issues are disabled" in err_msg:
            add_collection_warning(settings, f"Issues are disabled for {repo}.")
            return []
        return [collection_error(repo, "issue", state, result)]
    rows = []
    entries = json.loads(result.stdout or "[]")
    if isinstance(entries, list) and len(entries) >= collection_limit:
        add_collection_warning(settings, f"{repo} {state} issue collection reached {collection_limit}; issue counts may be incomplete.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if window_filter and not item_in_window(entry, state, settings["window"]):
            continue
        if should_skip_bot(login_from(entry.get("author")), entry.get("author"), settings):
            continue
        rows.append(normalize_issue(repo, state, entry, settings))
    return rows


def item_in_window(entry: dict[str, Any], state: str, window: Window) -> bool:
    key = "mergedAt" if state == "merged" else "closedAt" if state == "closed" else "updatedAt"
    value = entry.get(key) or entry.get("updatedAt") or entry.get("createdAt")
    when = parse_timestamp(value) if isinstance(value, str) else None
    return when is not None and window.since <= when <= window.until


def subject_item_in_window(entry: dict[str, Any], window: Window) -> bool:
    value = entry.get("updated_at") or entry.get("created_at")
    when = parse_timestamp(value) if isinstance(value, str) else None
    return when is not None and window.since <= when <= window.until


def normalize_search_item(entry: dict[str, Any], subject: str, qualifier: str, settings: dict[str, Any]) -> dict[str, Any]:
    labels = labels_from(entry)
    state = str(entry.get("state") or "open")
    is_pr = isinstance(entry.get("pull_request"), dict)
    kind = "pr" if is_pr else "issue"
    repo = repo_from_search_url(entry.get("repository_url")) or "external"
    configured_repos = set(as_str_list(settings.get("resolved_repositories") or settings.get("repositories")))
    if kind == "pr":
        bucket = classify_pr("closed" if state == "closed" else "open", search_pr_entry(entry), labels)
    else:
        bucket = classify_issue("closed" if state == "closed" else "open", labels)
    return {
        "kind": kind,
        "repo": repo,
        "number": entry.get("number"),
        "title": entry.get("title") or f"{kind} involving {subject}",
        "url": entry.get("html_url"),
        "author": login_from(entry.get("user")),
        "labels": labels,
        "state": state,
        "collection_lane": "recent_activity",
        "updated_at": entry.get("updated_at"),
        "completed_at": entry.get("closed_at") if state == "closed" else None,
        "bucket": bucket,
        "subject": subject,
        "subject_match": qualifier,
        "handoff": ("github for explicit follow-up on external subject activity" if configured_repos and repo not in configured_repos else None) if state != "closed" else None,
        "priority": priority_score(labels, settings),
    }


def repo_from_search_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    marker = "/repos/"
    if marker not in value:
        return None
    return value.rsplit(marker, 1)[1]


def search_pr_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "isDraft": False,
        "reviewDecision": "",
        "updatedAt": entry.get("updated_at"),
        "closedAt": entry.get("closed_at"),
    }


def search_window(window: Window) -> str:
    return f"updated:>={window.since.date().isoformat()}"


def should_skip_bot(login: str | None, actor: object, settings: dict[str, Any]) -> bool:
    if settings.get("include_bots"):
        return False
    if login and (login.casefold().endswith("[bot]") or login.casefold() in {"dependabot", "github-actions"}):
        return True
    return isinstance(actor, dict) and str(actor.get("type") or "").casefold() == "bot"


def normalize_pr(repo: str, state: str, entry: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    labels = labels_from(entry)
    return {
        "kind": "pr",
        "repo": repo,
        "number": entry.get("number"),
        "title": entry.get("title") or "untitled PR",
        "url": entry.get("url"),
        "author": login_from(entry.get("author")),
        "labels": labels,
        "state": state,
        "collection_lane": item_collection_lane(state, entry, settings),
        "review_decision": entry.get("reviewDecision") or "",
        "draft": bool(entry.get("isDraft")),
        "updated_at": entry.get("updatedAt"),
        "completed_at": entry.get("mergedAt") if state == "merged" else None,
        "bucket": classify_pr(state, entry, labels),
        "handoff": handoff_for_pr(state, entry, labels),
        "priority": priority_score(labels, settings),
    }


def normalize_issue(repo: str, state: str, entry: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    labels = labels_from(entry)
    return {
        "kind": "issue",
        "repo": repo,
        "number": entry.get("number"),
        "title": entry.get("title") or "untitled issue",
        "url": entry.get("url"),
        "author": login_from(entry.get("author")),
        "labels": labels,
        "state": state,
        "collection_lane": item_collection_lane(state, entry, settings),
        "assignees": [login_from(item) for item in entry.get("assignees") or [] if login_from(item)],
        "updated_at": entry.get("updatedAt"),
        "completed_at": entry.get("closedAt") if state == "closed" else None,
        "bucket": classify_issue(state, labels),
        "handoff": handoff_for_issue(state, labels),
        "priority": priority_score(labels, settings),
    }


def labels_from(entry: dict[str, Any]) -> list[str]:
    labels = []
    for label in entry.get("labels") or []:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            labels.append(label["name"])
        elif isinstance(label, str):
            labels.append(label)
    return labels


def login_from(value: object) -> str | None:
    return value.get("login") if isinstance(value, dict) and isinstance(value.get("login"), str) else None


def classify_pr(state: str, entry: dict[str, Any], labels: list[str]) -> str:
    label_keys = {label.casefold() for label in labels}
    if state in {"merged", "closed"}:
        return "recently_completed"
    if "blocked" in label_keys or "plan:blocked" in label_keys:
        return "blocked"
    if "waiting" in label_keys or "plan:waiting" in label_keys:
        return "waiting"
    if entry.get("isDraft"):
        return "in_progress"
    if "ready-to-merge" in label_keys or entry.get("reviewDecision") == "APPROVED":
        return "ready_for_merge_decision"
    if "needs-attention" in label_keys or "needs attention" in label_keys:
        return "needs_attention"
    return "ready_for_review"


def classify_issue(state: str, labels: list[str]) -> str:
    label_keys = {label.casefold() for label in labels}
    if state == "closed":
        return "recently_completed"
    if "blocked" in label_keys or "plan:blocked" in label_keys:
        return "blocked"
    if "waiting" in label_keys or "plan:waiting" in label_keys:
        return "waiting"
    if "stale" in label_keys or "plan:stale" in label_keys:
        return "stale_or_needs_reconciliation"
    if "needs-attention" in label_keys or "needs attention" in label_keys:
        return "needs_attention"
    return "in_progress"


def handoff_for_pr(state: str, entry: dict[str, Any], labels: list[str]) -> str | None:
    if state in {"merged", "closed"}:
        return None
    if entry.get("reviewDecision") == "APPROVED" or "ready-to-merge" in {label.casefold() for label in labels}:
        return "repo-readiness or github for a fresh merge decision"
    return "babysit-pr if this PR needs active monitoring"


def handoff_for_issue(state: str, labels: list[str]) -> str | None:
    if state == "closed":
        return None
    label_keys = {label.casefold() for label in labels}
    if any(label.startswith("plan:") or label == "plan" for label in label_keys):
        return "github-plan for planning reconciliation"
    return None


def priority_score(labels: list[str], settings: dict[str, Any]) -> int:
    raw_filters = settings.get("noise_filters")
    filters: dict[str, Any] = raw_filters if isinstance(raw_filters, dict) else {}
    noise_labels = {label.casefold() for label in as_str_list(filters.get("labels"))}
    score = 0
    for label in labels:
        key = label.casefold()
        if key in noise_labels:
            score -= 5
        if key in {"security", "urgent", "production", "regression", "needs-attention"}:
            score += 10
    return score


def collection_error(repo: str, kind: str, state: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "kind": "collection_error",
        "repo": repo,
        "state": state,
        "source_kind": kind,
        "title": f"Unable to collect {state} {kind}s",
        "error": trim(result.stderr or result.stdout),
        "bucket": "needs_attention",
        "priority": 100,
    }


def add_collection_warning(settings: dict[str, Any], warning: str) -> None:
    warnings = settings.setdefault("collection_warnings", [])
    if isinstance(warnings, list) and warning not in warnings:
        warnings.append(warning)


def collection_warnings_from(settings: dict[str, Any]) -> list[str]:
    warnings = settings.get("collection_warnings")
    return [warning for warning in warnings if isinstance(warning, str)] if isinstance(warnings, list) else []


def bucket_items(items: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in BUCKET_ORDER}
    sorted_items = sorted(items, key=item_sort_key)
    for item in sorted_items:
        buckets.setdefault(str(item.get("bucket") or "in_progress"), []).append(item)
    return {bucket: rows for bucket, rows in buckets.items() if rows}


def display_item_limit(settings: dict[str, Any]) -> int:
    level = str(settings.get("summary_level") or "standard")
    if level == "concise":
        return 5
    if level == "standard":
        return 12
    return setting_positive_int(settings, "limit_items", 50)


def summary_counts(buckets: dict[str, list[dict[str, Any]]], collection_warnings: list[str]) -> dict[str, int]:
    rows = [row for bucket in buckets.values() for row in bucket]
    return {
        **{bucket: len(buckets.get(bucket) or []) for bucket in BUCKET_ORDER},
        "open_backlog": sum(1 for row in rows if row.get("collection_lane") == "open_backlog"),
        "recent_activity": sum(1 for row in rows if row.get("collection_lane") == "recent_activity"),
        "recent_completions": sum(1 for row in rows if row.get("collection_lane") == "recent_completion"),
        "collection_warnings": len(collection_warnings),
    }


def item_sort_key(row: dict[str, Any]) -> tuple[int, float]:
    priority = -int(row.get("priority") or 0)
    updated = parse_timestamp(str(row.get("updated_at") or ""))
    timestamp = updated.timestamp() if updated else 0.0
    return priority, -timestamp


def priority_sections(items: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    sections = []
    sorted_items = sorted(items, key=item_sort_key)
    for raw in settings.get("priority_sections") or []:
        if not isinstance(raw, dict):
            continue
        repos = set(as_str_list(raw.get("repositories")))
        if not repos:
            continue
        max_completed = int(raw.get("max_recently_completed") or 3)
        matched = [
            item
            for item in sorted_items
            if item.get("repo") in repos
            and item.get("bucket") in ACTIONABLE_PRIORITY_BUCKETS
            and not is_collection_error(item)
        ]
        completed = [
            item
            for item in sorted_items
            if item.get("repo") in repos
            and item.get("bucket") == "recently_completed"
            and not is_collection_error(item)
        ]
        if matched or completed:
            sections.append(
                {
                    "name": str(raw.get("name") or "Priority Section"),
                    "portfolio_area": str(raw.get("portfolio_area") or raw.get("name") or "Priority Section"),
                    "workstream": str(raw.get("workstream") or ""),
                    "relationship": str(raw.get("relationship") or ""),
                    "initiatives": as_str_list(raw.get("initiatives")),
                    "items": matched[:10],
                    "item_count": len(matched),
                    "recently_completed": completed[:max_completed],
                    "recently_completed_count": len(completed),
                }
            )
    return sections


def limitations(settings: dict[str, Any], repos: list[str]) -> list[str]:
    out = []
    if settings["mode"] == "activity":
        out.append("Activity mode applies the window to open and completed work; older open backlog is not included.")
    elif settings["mode"] == "backlog":
        out.append("Backlog mode includes open work regardless of update time; completed work remains window-bound.")
    elif settings["mode"] == "standup":
        out.append("Standup mode includes open backlog regardless of update time plus recent activity and completions inside the window.")
    if not settings["subjects"]:
        out.append("No subjects configured; rollup is repository-scoped only.")
    elif repos and not settings["include_external_activity"]:
        out.append("Subject search outside configured repositories was skipped because include_external_activity is false.")
    if settings["repo_owners"] and len(repos) >= settings["limit_repos"]:
        out.append("Repository owner scan reached --limit-repos; some repos may be omitted.")
    out.append("Read-only v1 does not inspect Project fields, deployment state, or full review timelines.")
    return out


def failure_payload(settings: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": 1,
        "script_version": SCRIPT_VERSION,
        "generated_at": format_ts(datetime.now(timezone.utc)),
        "window": window_json(settings["window"]),
        "display_window": display_window_json(settings["window"], settings["timezone"]),
        "timezone": settings["timezone"],
        "report_recipient": settings["report_recipient"],
        "error": error,
        "next_step": "Run `gh auth status` and verify the configured repo/owner is accessible.",
    }


def render_payload(payload: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(sanitize_payload_for_json(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not payload.get("ok"):
        return render_failure(payload)
    if payload.get("layout") == "manager":
        return render_manager_brief_markdown(payload)
    if payload.get("layout") == "executive":
        return render_executive_brief_markdown(payload)
    if payload.get("layout") in {None, "operator"}:
        return render_operator_markdown(payload)
    raise ValueError(f"unknown layout: {payload.get('layout')}")


def sanitize_payload_for_json(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    profile = sanitized.get("recipient_profile")
    if isinstance(profile, dict):
        sanitized["recipient_profile"] = public_recipient_profile(profile)
    return sanitized


def format_brief_datetime(dt: datetime, tz_name: str) -> str:
    display_tz: tzinfo
    try:
        display_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        display_tz = timezone.utc
    localized = dt.astimezone(display_tz)
    formatted = localized.strftime("%b %d, %Y, %I:%M %p %Z")
    formatted = re.sub(r"\b([A-Z][a-z]{2}) 0(\d)\b", r"\1 \2", formatted)
    formatted = formatted.replace(", 0", ", ")
    return formatted


def empty_repo_data() -> dict[str, list[dict[str, Any]]]:
    return {
        "pr_merged": [],
        "pr_open": [],
        "pr_closed": [],
        "issue_open": [],
        "issue_closed": [],
        "releases": [],
        "workflows": [],
        "attention": [],
    }


def get_repo_data(payload: dict[str, Any]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    repos: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for repo in payload.get("repositories") or []:
        repos[repo] = empty_repo_data()

    buckets = payload.get("buckets") or {}
    for bucket, items in buckets.items():
        for item in items:
            repo = item.get("repo")
            if not repo:
                continue
            if repo not in repos:
                repos[repo] = empty_repo_data()

            kind = item.get("kind")
            state = item.get("state")
            if bucket == "needs_attention":
                repos[repo]["attention"].append(item)

            if kind == "pr":
                if state == "merged":
                    repos[repo]["pr_merged"].append(item)
                elif state == "closed":
                    repos[repo]["pr_closed"].append(item)
                else:
                    repos[repo]["pr_open"].append(item)
            elif kind == "issue":
                if state == "closed":
                    repos[repo]["issue_closed"].append(item)
                else:
                    repos[repo]["issue_open"].append(item)

    for release in payload.get("releases") or []:
        repo = release.get("repo")
        if repo in repos:
            repos[repo]["releases"].append(release)

    for workflow in payload.get("workflows") or []:
        repo = workflow.get("repo")
        if repo in repos:
            repos[repo]["workflows"].append(workflow)

    return repos


def repo_short_name(repo: str) -> str:
    return repo.split("/", 1)[1] if "/" in repo else repo


def repo_display_list(repos: list[str], limit: int = 4) -> str:
    names = [f"`{repo_short_name(repo)}`" for repo in repos]
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", and {len(names) - limit} more"


def total_repo_count(repo_data: dict[str, dict[str, list[dict[str, Any]]]], key: str) -> int:
    return sum(len(data[key]) for data in repo_data.values())


def active_repo_names(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[str]:
    return [
        repo
        for repo, data in repo_data.items()
        if any(
            data[key]
            for key in ("pr_merged", "pr_closed", "pr_open", "issue_open", "issue_closed", "releases", "workflows")
        )
    ]


def product_signal_repo_names(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[str]:
    return [
        repo
        for repo, data in repo_data.items()
        if any(data[key] for key in ("pr_merged", "pr_open", "issue_open", "issue_closed", "releases"))
    ]


def linked_item_ref(item: dict[str, Any]) -> str:
    repo = str(item.get("repo") or "")
    number = item.get("number")
    title = str(item.get("title") or "untitled")
    url = item.get("url")
    ref = f"{repo_short_name(repo)}#{number}" if number else repo_short_name(repo)
    return f"[{ref}]({url}) {title}" if url else f"{ref} {title}"


def is_collection_error(item: dict[str, Any]) -> bool:
    return item.get("kind") == "collection_error"


def real_work_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if not is_collection_error(item)]


def executive_item_ref(item: dict[str, Any]) -> str:
    repo = str(item.get("repo") or "")
    number = item.get("number")
    title = str(item.get("title") or "untitled")
    prefix = f"{repo_short_name(repo)}#{number}" if number else repo_short_name(repo)
    return f"{prefix} {title}".strip()


def executive_item_refs(items: list[dict[str, Any]], limit: int = 3) -> str:
    work_items = real_work_items(items)
    refs = [executive_item_ref(item) for item in work_items[:limit]]
    if not refs:
        return "none collected"
    if len(work_items) > limit:
        refs.append(f"{len(work_items) - limit} more")
    return "; ".join(refs)


def executive_item_topic(items: list[dict[str, Any]], limit: int = 2) -> str:
    work_items = real_work_items(items)
    if not work_items:
        return "this work"
    refs = [executive_item_ref(item) for item in work_items[:limit]]
    if len(work_items) > limit:
        refs.append("the rest of the active set")
    return "; ".join(refs)


def item_count_phrase(count: int, singular: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {singular}{suffix}"


def repo_visible_work_count(data: dict[str, list[dict[str, Any]]]) -> int:
    return sum(
        len(real_work_items(data[key]))
        for key in ("pr_merged", "pr_closed", "pr_open", "issue_open", "issue_closed")
    ) + len(data["releases"])


def top_visible_repo_names(repo_data: dict[str, dict[str, list[dict[str, Any]]]], limit: int = 4) -> list[str]:
    ranked = sorted(
        ((repo_visible_work_count(data), repo) for repo, data in repo_data.items()),
        key=lambda item: (-item[0], item[1]),
    )
    return [repo for count, repo in ranked if count > 0][:limit]


def item_group_counts(items: list[dict[str, Any]], limit: int = 4) -> str:
    counts: dict[str, int] = {}
    for item in real_work_items(items):
        repo = str(item.get("repo") or "")
        if repo:
            counts[repo_short_name(repo)] = counts.get(repo_short_name(repo), 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    pieces = [f"`{repo}` {count}" for repo, count in ranked[:limit]]
    if len(ranked) > limit:
        pieces.append(f"{len(ranked) - limit} more repos")
    return ", ".join(pieces) if pieces else "none"


def compact_titles(items: list[dict[str, Any]], limit: int = 3) -> str:
    titles = [str(item.get("title") or "untitled") for item in items[:limit]]
    if not titles:
        return "general updates"
    if len(items) > limit:
        titles.append(f"{len(items) - limit} more")
    return "; ".join(titles)


def executive_theme_titles(items: list[dict[str, Any]], limit: int = 2) -> str:
    work_items = real_work_items(items)
    themes: list[str] = []
    for item in work_items[:limit]:
        title = str(item.get("title") or "untitled")
        phrases = title_key_phrases(title)
        themes.append(phrases[0] if phrases else title)
    if not themes:
        return "general product and operations work"
    if len(work_items) > limit:
        themes.append(item_count_phrase(len(work_items) - limit, "more related item"))
    return prose_join(themes)


def prose_join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def brief_period(payload: dict[str, Any]) -> BriefPeriod:
    window = payload.get("window") or {}
    label = str(window.get("label") or "").strip().casefold().replace("_", " ")
    label = re.sub(r"^(past|last)\s+", "", label)
    since_dt = parse_timestamp(window.get("since")) if isinstance(window, dict) else None
    until_dt = parse_timestamp(window.get("until")) if isinstance(window, dict) else None
    hours = ((until_dt - since_dt).total_seconds() / 3600.0) if since_dt and until_dt else None

    daily_labels = {"24h", "1d", "1 day", "day"}
    weekly_labels = {"7d", "1w", "1 week", "week"}
    if label in weekly_labels or (hours is not None and 144 <= hours <= 216):
        return BriefPeriod(
            title_prefix="Weekly",
            summary_subject="focused week of work",
            priorities_heading="This Week's Priorities",
            alignment_window="this week",
            followup_phrase="next week's brief",
            source_window="week",
        )
    if label in daily_labels or (hours is not None and 18 <= hours <= 36):
        return BriefPeriod(
            title_prefix="Daily",
            summary_subject="focused day of work",
            priorities_heading="Today's Priorities",
            alignment_window="today",
            followup_phrase="tomorrow's brief",
            source_window="day",
        )
    return BriefPeriod(
        title_prefix="Current",
        summary_subject="focused stretch of work",
        priorities_heading="Current Priorities",
        alignment_window="during this reporting window",
        followup_phrase="the next brief",
        source_window="reporting window",
    )


def repo_outcome_sentence(repo: str, data: dict[str, list[dict[str, Any]]]) -> str | None:
    pieces = []
    if data["releases"]:
        tags = ", ".join(str(release.get("tag_name") or release.get("name") or "release") for release in data["releases"][:3])
        pieces.append(f"published {tags}")
    if data["pr_merged"]:
        pieces.append(f"merged {len(data['pr_merged'])} change(s) around {compact_titles(data['pr_merged'], 2)}")
    if data["pr_closed"]:
        pieces.append(f"closed {len(data['pr_closed'])} unmerged change(s)")
    if data["issue_closed"]:
        pieces.append(f"closed {len(data['issue_closed'])} issue(s)")
    if data["pr_open"] or data["issue_open"]:
        pieces.append(f"has {len(data['pr_open']) + len(data['issue_open'])} open work item(s) still visible")
    if not pieces:
        return None
    return f"`{repo_short_name(repo)}` " + "; ".join(pieces) + "."


def workflow_health_summary(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> tuple[int, int, int]:
    total = failed = successful = 0
    for data in repo_data.values():
        for workflow in data["workflows"]:
            total += 1
            conclusion = str(workflow.get("conclusion") or "").casefold()
            if conclusion == "failure":
                failed += 1
            elif conclusion == "success":
                successful += 1
    return total, successful, failed


def failed_workflow_items(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for repo, data in repo_data.items():
        for workflow in data["workflows"]:
            if str(workflow.get("conclusion") or "").casefold() == "failure":
                failed.append({**workflow, "repo": repo})
    return failed


def workflow_item_ref(workflow: dict[str, Any]) -> str:
    repo = repo_short_name(str(workflow.get("repo") or ""))
    name = str(workflow.get("name") or workflow.get("workflowName") or "workflow")
    url = workflow.get("url")
    label = f"`{repo}` {name}" if repo else name
    return f"[{label}]({url})" if isinstance(url, str) and url else label


def failed_workflow_lines(workflows: list[dict[str, Any]], limit: int = 5) -> list[str]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for workflow in workflows:
        repo = str(workflow.get("repo") or "")
        name = str(workflow.get("name") or workflow.get("workflowName") or "workflow")
        key = (repo, name)
        entry = grouped.setdefault(key, {"workflow": workflow, "count": 0})
        entry["count"] += 1

    lines: list[str] = []
    ranked = sorted(grouped.values(), key=lambda entry: (-int(entry["count"]), workflow_item_ref(entry["workflow"])))
    for entry in ranked[:limit]:
        count = int(entry["count"])
        ref = workflow_item_ref(entry["workflow"])
        if count == 1:
            lines.append(f"- {ref} failed once in this window.")
        else:
            lines.append(f"- {ref} failed {count} times in this window.")
    remaining = len(ranked) - limit
    if remaining > 0:
        lines.append(f"- Plus {item_count_phrase(remaining, 'additional failed workflow group')}.")
    return lines


def failed_workflow_topics(workflows: list[dict[str, Any]], limit: int = 2) -> str:
    grouped: dict[tuple[str, str], int] = {}
    for workflow in workflows:
        repo = repo_short_name(str(workflow.get("repo") or ""))
        name = str(workflow.get("name") or workflow.get("workflowName") or "workflow")
        grouped[(repo, name)] = grouped.get((repo, name), 0) + 1
    ranked = sorted(grouped.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
    topics = [f"`{repo}` {name}" for (repo, name), _count in ranked[:limit]]
    if len(ranked) > limit:
        topics.append("other failed runs")
    return prose_join(topics) if topics else "failed runs"


def attention_items_from_repo_data(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    attention_items: list[dict[str, Any]] = []
    for repo, data in repo_data.items():
        for item in data["attention"]:
            if not is_collection_error(item):
                attention_items.append({**item, "repo": repo})
    return attention_items


def repo_risk_lines(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[str]:
    risk_lines: list[str] = []
    for repo, data in repo_data.items():
        failed = [workflow for workflow in data["workflows"] if str(workflow.get("conclusion") or "").casefold() == "failure"]
        if failed:
            risk_lines.append(f"`{repo_short_name(repo)}` had {len(failed)} failed workflow run(s).")
        attention = real_work_items(data["attention"])
        if attention:
            risk_lines.append(f"`{repo_short_name(repo)}` has {len(attention)} item(s) that need explicit attention.")
    return risk_lines


def completion_total(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> int:
    return total_repo_count(repo_data, "pr_merged") + total_repo_count(repo_data, "pr_closed") + total_repo_count(repo_data, "issue_closed")


def abandoned_change_total(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> int:
    return total_repo_count(repo_data, "pr_closed")


def open_work_total(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> int:
    return total_repo_count(repo_data, "pr_open") + total_repo_count(repo_data, "issue_open")


def automation_sentence(workflow_total: int, workflow_success: int, workflow_failed: int) -> str | None:
    if not workflow_total:
        return None
    if workflow_failed:
        return f"Automation needs attention: {workflow_success} successful and {workflow_failed} failed workflow run(s) were collected."
    if workflow_success:
        return f"Automation was green in the collected sample: {workflow_success} successful run(s)."
    return f"Automation had {workflow_total} completed workflow run(s), but none reported success."


def executive_release_titles(items: list[dict[str, Any]], limit: int = 2) -> str:
    titles = [str(item.get("name") or item.get("tag_name") or "release") for item in items[:limit]]
    if not titles:
        return "recent release work"
    if len(items) > limit:
        titles.append(item_count_phrase(len(items) - limit, "more release"))
    return prose_join(titles)


def executive_evidence_lines(
    repo_data: dict[str, dict[str, list[dict[str, Any]]]],
    period: BriefPeriod,
) -> list[str]:
    completed = [
        item
        for data in repo_data.values()
        for item in [*data["pr_merged"], *data["issue_closed"]]
    ]
    cleared = [
        item
        for data in repo_data.values()
        for item in data["pr_closed"]
    ]
    open_items = [
        item
        for data in repo_data.values()
        for item in [*data["pr_open"], *data["issue_open"]]
    ]
    releases = [release for data in repo_data.values() for release in data["releases"]]

    lines = [
        "Concrete GitHub items behind the summary:",
        f"- Completed by repo: {item_group_counts(completed)}.",
        f"- Open follow-up by repo: {item_group_counts(open_items)}.",
        f"- Completed during the {period.source_window}: {executive_item_refs(completed)}.",
        f"- Still visible for follow-up: {executive_item_refs(open_items)}.",
    ]
    if cleared:
        lines.append(f"- Cleared or superseded paths: {executive_item_refs(cleared)}.")
    if releases:
        release_refs = "; ".join(executive_release_titles([release], 1) for release in releases[:3])
        if len(releases) > 3:
            release_refs += f"; {len(releases) - 3} more"
        lines.append(f"- Releases during the {period.source_window}: {release_refs}.")
    return lines


def executive_activity_comparison_line(payload: dict[str, Any]) -> str:
    comparison = payload.get("activity_comparison") or payload.get("historical_comparison")
    if isinstance(comparison, str) and comparison.strip():
        return comparison.strip()
    if isinstance(comparison, dict):
        summary = comparison.get("summary") or comparison.get("text")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return ""


def executive_work_counts(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> dict[str, int]:
    completed = [
        item
        for data in repo_data.values()
        for item in [*data["pr_merged"], *data["issue_closed"]]
    ]
    cleared = [item for data in repo_data.values() for item in data["pr_closed"]]
    open_items = [item for data in repo_data.values() for item in [*data["pr_open"], *data["issue_open"]]]
    releases = [release for data in repo_data.values() for release in data["releases"]]
    return {
        "completed": len(real_work_items(completed)),
        "cleared": len(real_work_items(cleared)),
        "open": len(real_work_items(open_items)),
        "releases": len(releases),
    }


def movement_phrase(counts: dict[str, int]) -> str:
    pieces: list[str] = []
    if counts["completed"]:
        pieces.append(item_count_phrase(counts["completed"], "completed item"))
    if counts["cleared"]:
        pieces.append(item_count_phrase(counts["cleared"], "cleared path"))
    if counts["releases"]:
        pieces.append(item_count_phrase(counts["releases"], "release"))
    if counts["open"]:
        pieces.append(item_count_phrase(counts["open"], "open follow-up"))
    return prose_join(pieces) if pieces else "no GitHub-visible product movement"


def item_titles(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("title") or "untitled") for item in real_work_items(items)]


LEADING_TITLE_VERBS = {
    "add",
    "align",
    "approve",
    "audit",
    "build",
    "clarify",
    "complete",
    "control",
    "define",
    "design",
    "fix",
    "gate",
    "implement",
    "improve",
    "investigate",
    "plan",
    "port",
    "prefer",
    "purge",
    "record",
    "remove",
    "replace",
    "require",
    "recheck",
    "refresh",
    "rescope",
    "route",
    "run",
    "scope",
    "surface",
    "use",
    "validate",
}
TRAILING_SCOPE_TERMS = {"MVP", "PR", "URL"}
CONNECTOR_WORDS = {"and", "for", "in", "inside", "of", "on", "to", "with"}
GENERIC_LOWER_TITLE_WORDS = {
    "contract",
    "contracts",
    "dogfood",
    "gate",
    "gating",
    "plan",
    "protocol",
    "readiness",
    "scope",
    "slice",
    "sweep",
    "trust",
    "validation",
    "vertical",
}


def is_title_name_token(word: str) -> bool:
    return word.isupper() or (word[:1].isupper() and word[1:].islower())


def normalized_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" -:;,.()[]{}")


def title_key_phrases(title: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", title.replace("-", " "))
    phrases: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if not is_title_name_token(word) or word.casefold() in LEADING_TITLE_VERBS:
            index += 1
            continue
        candidate_words = [word]
        index += 1
        while index < len(words) and len(candidate_words) < 4:
            next_word = words[index]
            normalized_next = next_word.casefold()
            if normalized_next in CONNECTOR_WORDS:
                index += 1
                break
            if is_title_name_token(next_word):
                candidate_words.append(next_word)
                index += 1
                continue
            if len(candidate_words) == 1 and normalized_next not in GENERIC_LOWER_TITLE_WORDS:
                candidate_words.append(next_word)
                index += 1
            break
        while candidate_words and candidate_words[-1] in TRAILING_SCOPE_TERMS:
            candidate_words.pop()
        phrase = normalized_phrase(" ".join(candidate_words))
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return phrases


def phrase_mentions(titles: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    phrase_order: dict[str, int] = {}
    for title in titles:
        for phrase in title_key_phrases(title):
            phrase_order.setdefault(phrase, len(phrase_order))
            counts[phrase] = counts.get(phrase, 0) + 1
    return [
        phrase
        for phrase, _count in sorted(
            counts.items(),
            key=lambda item: (-item[1], phrase_order[item[0]], item[0].casefold()),
        )
    ]


def is_portfolio_alias(phrase: str, portfolio_area: str) -> bool:
    normalized = phrase.casefold()
    portfolio = portfolio_area.casefold()
    return normalized == portfolio or normalized in portfolio


def is_configured_initiative(phrase: str, initiatives: list[str]) -> bool:
    normalized = phrase.casefold()
    return any(normalized == initiative.casefold() for initiative in initiatives)


def executive_section_workstream(section: dict[str, Any]) -> ExecutiveWorkstream:
    portfolio_area = str(section.get("portfolio_area") or section.get("name") or "Priority area")
    items = real_work_items(section.get("items") or [])
    completed_items = real_work_items(section.get("recently_completed") or [])
    titles = [*item_titles(items), *item_titles(completed_items)]
    inferred_phrases = phrase_mentions(titles)
    explicit_initiatives = as_str_list(section.get("initiatives"))
    explicit_workstream = str(section.get("workstream") or "").strip()
    workstream = explicit_workstream
    if not workstream:
        for phrase in inferred_phrases:
            if not is_portfolio_alias(phrase, portfolio_area) and not is_configured_initiative(
                phrase, explicit_initiatives
            ):
                workstream = phrase
                break
    if not workstream:
        workstream = portfolio_area

    initiatives = explicit_initiatives or [
        phrase
        for phrase in inferred_phrases
        if phrase.casefold() != workstream.casefold() and not is_portfolio_alias(phrase, portfolio_area)
    ][:3]

    relationship = str(section.get("relationship") or "").strip()
    if not relationship:
        if workstream.casefold() != portfolio_area.casefold():
            relationship = f"{workstream} work inside {portfolio_area}"
        else:
            relationship = workstream

    return ExecutiveWorkstream(
        portfolio_area=portfolio_area,
        workstream=workstream,
        relationship=relationship,
        initiatives=tuple(initiatives),
        active_count=int(section.get("item_count") or len(items)),
        completed_count=int(section.get("recently_completed_count") or len(completed_items)),
        active_titles=tuple(item_titles(items)),
        completed_titles=tuple(item_titles(completed_items)),
    )


def executive_section_label(section: dict[str, Any]) -> str:
    workstream = executive_section_workstream(section)
    if workstream.workstream.casefold() == workstream.portfolio_area.casefold():
        return workstream.workstream
    return workstream.relationship


def executive_workstream_summary(workstream: ExecutiveWorkstream) -> str:
    movement: list[str] = []
    if workstream.completed_count:
        movement.append(item_count_phrase(workstream.completed_count, "recent completion"))
    if workstream.active_count:
        movement.append(item_count_phrase(workstream.active_count, "active follow-up"))
    movement_text = prose_join(movement) if movement else "no active signal"
    initiative_text = f" Key initiatives: {prose_join(list(workstream.initiatives[:3]))}." if workstream.initiatives else ""
    why_now_source = workstream.completed_titles[0] if workstream.completed_titles else None
    active_source = workstream.active_titles[0] if workstream.active_titles else None
    why_now = (
        f"Why now: {why_now_source} has landed, so the next visible decision is {active_source}."
        if why_now_source and active_source
        else f"Why now: {active_source} is visible and needs sequencing."
        if active_source
        else "Why now: recent completion created a decision point for follow-up."
        if why_now_source
        else "Why now: the collected signal is too thin for a stronger claim."
    )
    analysis: list[str] = []
    if workstream.completed_count and workstream.active_count:
        analysis.append("Impact: turns finished work into a checkable result before the next cycle commits more effort.")
    elif workstream.active_count:
        analysis.append("Impact: sequencing the open thread now keeps it from stalling neighboring priorities.")
    elif workstream.completed_count:
        analysis.append("Impact: confirms the shipped change matches intent before attention moves on.")

    if workstream.active_count >= 2:
        analysis.append("Risk if delayed: the open items can fan out into parallel work before the decision loop closes.")
    elif workstream.active_count == 1:
        analysis.append("Risk if delayed: the open thread can drift without an explicit owner or next decision.")
    elif workstream.completed_count:
        analysis.append("Risk if delayed: finished work may go unvalidated and quietly diverge from intent.")

    total_signal = workstream.active_count + workstream.completed_count
    if total_signal:
        initiative_count = len(workstream.initiatives)
        initiative_clause = f" across {item_count_phrase(initiative_count, 'initiative')}" if initiative_count else ""
        analysis.append(
            f"Confidence: medium; based on {item_count_phrase(total_signal, 'GitHub item')}{initiative_clause}, which shows direction but not full product or deployment state."
        )
    else:
        analysis.append("Confidence: low; the configured area was present, but GitHub did not show enough movement for a stronger claim.")
    return f"{workstream.relationship}: {movement_text}.{initiative_text} {why_now} {' '.join(analysis)}"


def executive_story_lines(
    repo_data: dict[str, dict[str, list[dict[str, Any]]]],
    focus_sections: list[dict[str, Any]],
    comparison: str,
) -> list[str]:
    completed = [
        item
        for data in repo_data.values()
        for item in [*data["pr_merged"], *data["issue_closed"]]
    ]
    open_items = [item for data in repo_data.values() for item in [*data["pr_open"], *data["issue_open"]]]
    counts = executive_work_counts(repo_data)
    top_repos = top_visible_repo_names(repo_data, 4)
    focus_names = [executive_section_label(section) for section in focus_sections[:2] if isinstance(section, dict)]
    focus_clause = f", especially {prose_join(focus_names)}" if focus_names else ""
    if focus_names:
        lead = f"The visible decision is how to sequence {prose_join(focus_names)}: {movement_phrase(counts)}."
    elif top_repos:
        lead = f"Work was concentrated in {repo_display_list(top_repos)}{focus_clause}: {movement_phrase(counts)}."
    else:
        lead = "No clear product story was collected in this window."

    outcome = ""
    if completed and open_items:
        outcome = f"Finished work landed; the next choices are concentrated around {executive_theme_titles(open_items, 2)}."
    elif completed:
        outcome = "The visible work mostly landed; the next question is whether the result matches the intended direction."
    elif open_items:
        outcome = f"The useful signal is what still needs a decision: {executive_theme_titles(open_items, 2)}."
    else:
        outcome = "There is not enough GitHub-visible movement here to justify a long executive read."

    lines = [lead, outcome]
    if comparison:
        lines.append(comparison)
    return lines


def coverage_gap_line(coverage_gaps: list[str]) -> str | None:
    if not coverage_gaps:
        return None
    gap_names = ", ".join(f"`{repo}`" for repo in coverage_gaps)
    return f"Collection incomplete for {gap_names}; this brief may omit work from those configured sources."


def executive_conversation_starters(
    repo_data: dict[str, dict[str, list[dict[str, Any]]]],
    focus_sections: list[dict[str, Any]],
    attention_items: list[dict[str, Any]],
    failed_workflows: list[dict[str, Any]],
    period: BriefPeriod,
) -> list[str]:
    starters: list[str] = []

    if focus_sections:
        labels = [executive_section_label(section) for section in focus_sections[:2] if isinstance(section, dict)]
        if labels:
            starters.append(
                f"Which outcome should {prose_join(labels)} prove {period.alignment_window}, and what should wait?"
            )

    if attention_items:
        starters.append(
            f"What decision would unblock {executive_item_refs(attention_items, 2)} {period.alignment_window}?"
        )

    if failed_workflows:
        starters.append(
            f"Should failed runs in {failed_workflow_topics(failed_workflows)} change release confidence, or are they known noise?"
        )

    open_items = [
        item
        for data in repo_data.values()
        for item in [*data["pr_open"], *data["issue_open"]]
    ]
    if open_items:
        starters.append(
            f"Should {executive_theme_titles(open_items, 2)} stay active {period.alignment_window}, wait, or be reframed?"
        )

    releases = [release for data in repo_data.values() for release in data["releases"]]
    if releases:
        starters.append(
            f"Is {executive_release_titles(releases)} ready for daily use, or should the next brief call out validation risk?"
        )

    finished_items = [
        item
        for data in repo_data.values()
        for item in [*data["pr_merged"], *data["issue_closed"]]
    ]
    if finished_items:
        starters.append(
            f"Looking at {executive_theme_titles(finished_items, 2)}, did the completed work deliver what you expected, or is there a gap to close?"
        )

    focus_items: list[dict[str, Any]] = []
    for section in focus_sections:
        focus_items.extend(section.get("recently_completed") or [])
        focus_items.extend(section.get("items") or [])
    if focus_items:
        starters.append(
            f"Should the next brief keep following {executive_theme_titles(focus_items)}, or shift attention elsewhere?"
        )

    if not starters:
        starters.append(f"Is anything from this window worth escalating to the team before {period.followup_phrase}?")

    return starters[:3]


def recipient_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("recipient_profile")
    return profile if isinstance(profile, dict) else {}


def recipient_context_phrase(profile: dict[str, Any]) -> str | None:
    company = profile.get("company")
    relationship = profile.get("relationship")
    raw_roles = profile.get("roles")
    roles: list[Any] = raw_roles if isinstance(raw_roles, list) else []
    if isinstance(company, str) and company.strip():
        return f"for {company}"
    if "owner" in roles:
        return "for the owner view"
    if isinstance(relationship, str) and relationship.strip():
        return f"for the {relationship} view"
    return None


def profile_source_note(profile: dict[str, Any]) -> str | None:
    if not profile:
        return None
    pieces = []
    if profile.get("framing"):
        pieces.append(f"framing: {profile['framing']}")
    if profile.get("technical_depth"):
        pieces.append(f"technical depth: {profile['technical_depth']}")
    if not pieces:
        return "Report was tailored from the local people profile for the recipient."
    return "Report was tailored from the local people profile (" + "; ".join(str(piece) for piece in pieces) + ")."


def focus_area_label(payload: dict[str, Any]) -> str:
    sections = payload.get("priority_sections") or []
    names = [str(section.get("name")) for section in sections[:2] if isinstance(section, dict) and section.get("name")]
    if not names:
        return "Priority Areas"
    if len(names) == 1:
        return names[0]
    return " and ".join(names)


def focus_area_heading(payload: dict[str, Any]) -> str:
    sections = payload.get("priority_sections") or []
    if len(sections) == 1 and isinstance(sections[0], dict):
        label = str(sections[0].get("name") or "").strip()
        if label:
            return f"{label} Impact"
    portfolio_labels = [
        str(section.get("portfolio_area") or section.get("name") or "")
        for section in sections
        if isinstance(section, dict)
    ]
    category_labels: list[str] = []
    for label in portfolio_labels:
        normalized = label.casefold()
        if "every code" in normalized:
            for category in ("Every Code", "Skills") if "skill" in normalized else ("Every Code Product",):
                if category not in category_labels:
                    category_labels.append(category)
            continue
        if "skill" in normalized:
            category = "Skills"
        else:
            category = label.strip()
        if category and category not in category_labels:
            category_labels.append(category)
    if category_labels:
        category_order = {"Every Code Product": 0, "Every Code": 1, "Skills": 2}
        ordered_categories = sorted(category_labels, key=lambda category: (category_order.get(category, 99), category))
        return f"{prose_join(ordered_categories[:3])} Impact"
    label = focus_area_label(payload)
    return f"{label} Impact"


def decision_framing(profile: dict[str, Any]) -> str:
    if profile.get("detail_preference"):
        return str(profile["detail_preference"])
    if profile.get("framing"):
        return str(profile["framing"])
    return "priority, risk, sequencing, and impact"


def report_window_text(payload: dict[str, Any]) -> str:
    tz_name = payload["timezone"]
    window = payload["window"]
    since_dt = parse_timestamp(window["since"]) or datetime.now(timezone.utc)
    until_dt = parse_timestamp(window["until"]) or datetime.now(timezone.utc)
    return f"{format_brief_datetime(since_dt, tz_name)} to {format_brief_datetime(until_dt, tz_name)}"


def render_manager_brief_markdown(payload: dict[str, Any]) -> str:
    repo_data = get_repo_data(payload)
    profile = recipient_profile(payload)
    period = brief_period(payload)
    context = recipient_context_phrase(profile)
    active_repos = active_repo_names(repo_data)
    completed = completion_total(repo_data)
    open_work = open_work_total(repo_data)
    release_count = total_repo_count(repo_data, "releases")
    workflow_total, workflow_success, workflow_failed = workflow_health_summary(repo_data)
    attention_items = attention_items_from_repo_data(repo_data)
    risk_lines = repo_risk_lines(repo_data)

    lines = [
        f"# GitHub Planning Brief for {payload['report_recipient']}",
        "",
        f"Window: {report_window_text(payload)}",
        "",
        "## Planning Summary",
        "",
    ]

    if active_repos:
        context_suffix = f" {context}" if context else ""
        lines.append(
            f"The active planning scope{context_suffix} is {repo_display_list(active_repos)}. "
            f"The collected window shows {completed} completed item(s), {open_work} open item(s), "
            f"and {release_count} release(s)."
        )
    else:
        lines.append("No active work or material changes were collected for the configured scope in this window.")
    health = automation_sentence(workflow_total, workflow_success, workflow_failed)
    if health:
        lines.append(health)

    lines.extend(["", f"## {period.priorities_heading}", ""])
    if attention_items:
        for item in attention_items[:5]:
            handoff = f" Handoff: {item['handoff']}." if item.get("handoff") else ""
            lines.append(f"- {linked_item_ref(item)}.{handoff}")
    elif open_work:
        lines.append(
            f"- No explicit attention items were collected; use the open work list to choose what stays active {period.alignment_window} based on {decision_framing(profile)}."
        )
    else:
        lines.append("- No GitHub-visible priority needs a decision from this report.")

    lines.extend(["", "## Active Work", ""])
    change_lines = [sentence for repo, data in sorted(repo_data.items()) if (sentence := repo_outcome_sentence(repo, data))]
    if change_lines:
        lines.extend(f"- {line}" for line in change_lines[:10])
    else:
        lines.append("- No active repo work was visible in the collected scope.")

    focus_sections = payload.get("priority_sections") or []
    if focus_sections:
        lines.extend(["", "## Focus Areas", ""])
        for section in focus_sections:
            name = str(section.get("name") or "Priority area")
            items = section.get("items") or []
            completed_items = section.get("recently_completed") or []
            item_count = int(section.get("item_count") or len(items))
            completed_count = int(section.get("recently_completed_count") or len(completed_items))
            lines.append(
                f"- **{name}**: {item_count} open priority item(s), "
                f"{completed_count} recent completion(s)."
            )

    if risk_lines:
        lines.extend(["", "## Decisions and Risks", ""])
        lines.extend(f"- {line}" for line in risk_lines[:8])

    lines.extend(["", "## Velocity", ""])
    lines.append(f"- Completed in window: {completed} item(s).")
    lines.append(f"- Open work now visible: {open_work} item(s).")
    lines.append(f"- Releases in window: {release_count}.")
    if workflow_total:
        lines.append(f"- Automation sample: {workflow_success} successful, {workflow_failed} failed, {workflow_total} total completed run(s).")

    limitations_list = payload.get("limitations") or []
    profile_note = profile_source_note(profile)
    if profile_note:
        limitations_list = [*limitations_list, profile_note]
    if limitations_list:
        lines.extend(["", "## Source Notes"])
        lines.extend(f"- {item}" for item in limitations_list)

    return "\n".join(lines).rstrip() + "\n"


def render_executive_brief_markdown(payload: dict[str, Any]) -> str:
    repo_data = get_repo_data(payload)
    profile = recipient_profile(payload)
    period = brief_period(payload)
    active_repos = active_repo_names(repo_data)
    product_repos = product_signal_repo_names(repo_data)
    recipient = str(payload.get("report_recipient") or "the recipient")
    cleared_paths = abandoned_change_total(repo_data)
    failed_workflows = failed_workflow_items(repo_data)
    focus_sections = payload.get("priority_sections") or []
    comparison_line = executive_activity_comparison_line(payload)
    coverage_gaps = as_str_list(payload.get("coverage_gaps"))

    lines = [
        f"# {period.title_prefix} Work Brief for {payload['report_recipient']}",
        "",
        f"Window: {report_window_text(payload)}",
        "",
        "## Executive Summary",
        "",
    ]

    comparison_in_summary = False
    if product_repos:
        lines.extend(executive_story_lines(repo_data, focus_sections, comparison_line))
        comparison_in_summary = bool(comparison_line)
        if failed_workflows:
            lines.append(f"Automation drag was concentrated in {failed_workflow_topics(failed_workflows)}.")
    elif active_repos:
        if cleared_paths:
            lines.append("No product or planning movement was collected, but abandoned or superseded change paths were cleared in this window.")
        else:
            lines.append("No product or planning movement was collected, but automation activity was visible in this window.")
    else:
        lines.append("No active work or material changes were collected for the configured scope in this window.")

    gap_line = coverage_gap_line(coverage_gaps)
    if gap_line:
        lines.append(gap_line)

    attention_items = attention_items_from_repo_data(repo_data)

    if attention_items:
        lines.extend(["", f"## Needs {recipient}'s Attention", ""])
        for item in attention_items[:5]:
            handoff = f" Handoff: {item['handoff']}." if item.get("handoff") else ""
            lines.append(f"- {linked_item_ref(item)}.{handoff}")

    if focus_sections:
        lines.extend(["", f"## {focus_area_heading(payload)}", ""])
        for section in focus_sections:
            workstream = executive_section_workstream(section)
            lines.append(f"- **{workstream.workstream}**: {executive_workstream_summary(workstream)}")

    lines.extend(["", "## Failed Runs", ""])
    if failed_workflows:
        lines.extend(failed_workflow_lines(failed_workflows))
    else:
        lines.append("- None collected in this window.")

    evidence_lines = executive_evidence_lines(repo_data, period)
    if evidence_lines:
        lines.extend(["", "## Work Items Behind This Brief", ""])
        lines.extend(evidence_lines)
        if comparison_line and not comparison_in_summary:
            lines.append(f"- Previous-window context: {comparison_line}")

    lines.extend(["", "## Questions to Decide", ""])
    lines.extend(
        f"- {starter}"
        for starter in executive_conversation_starters(repo_data, focus_sections, attention_items, failed_workflows, period)
    )

    limitations_list = payload.get("limitations") or []
    if limitations_list:
        lines.extend(["", "## Source Notes"])
        lines.extend(f"- {item}" for item in limitations_list)

    return "\n".join(lines).rstrip() + "\n"


def render_operator_markdown(payload: dict[str, Any]) -> str:
    display_window = payload.get("display_window") or payload["window"]
    lines = [
        f"# GitHub Work Rollup for {payload['report_recipient']}",
        "",
        f"Window: {display_window['since']} to {display_window['until']} ({payload['timezone']})",
        f"Mode: {payload.get('mode') or 'activity'}",
        render_sources(payload.get("repositories") or [], payload.get("subjects") or []),
        "",
        "## Operator Summary",
    ]
    buckets = payload.get("buckets") or {}
    lines.extend(render_operator_summary(payload.get("summary") or summary_counts(buckets, [])))
    display_limit = int(payload.get("display_limit") or 12)
    for bucket in BUCKET_ORDER:
        rows = buckets.get(bucket) or []
        if not rows:
            continue
        lines.extend(["", f"## {bucket.replace('_', ' ').title()}"])
        for item in rows[:display_limit]:
            lines.append(render_item(item))
        remaining = len(rows) - display_limit
        if remaining > 0:
            lines.append(f"- Plus {remaining} more item(s) in this section.")
    for section in payload.get("priority_sections") or []:
        lines.extend(["", f"## {section['name']}"])
        lines.extend(render_priority_section(section))
    limitations_list = payload.get("limitations") or []
    if limitations_list:
        lines.extend(["", "## Source Notes"])
        lines.extend(f"- {item}" for item in limitations_list)
    return "\n".join(lines).rstrip() + "\n"


def render_sources(repos: list[str], subjects: list[str]) -> str:
    subject_text = f"; subjects: {', '.join(subjects)}" if subjects else ""
    if len(repos) <= 5:
        return f"Sources: {', '.join(repos) or 'none'}{subject_text}"
    by_owner: dict[str, list[str]] = {}
    for repo in repos:
        owner, name = repo.split("/", 1) if "/" in repo else (repo, "")
        by_owner.setdefault(owner, []).append(name if name else repo)
    grouped = [f"- **{owner}**: {', '.join(sorted(names))}" for owner, names in sorted(by_owner.items())]
    return (
        f"Sources: {len(repos)} repositories{subject_text}\n"
        "<details>\n"
        "<summary>Show repositories</summary>\n\n"
        + "\n".join(grouped)
        + "\n</details>"
    )


def render_operator_summary(summary: dict[str, int]) -> list[str]:
    attention = summary.get("needs_attention", 0)
    warnings = summary.get("collection_warnings", 0)
    if attention:
        headline = f"{attention} item(s) need attention."
    else:
        headline = "No actual attention items detected."
    counts = [
        f"waiting: {summary.get('waiting', 0)}",
        f"ready for review: {summary.get('ready_for_review', 0)}",
        f"ready for merge: {summary.get('ready_for_merge_decision', 0)}",
        f"in progress: {summary.get('in_progress', 0)}",
        f"completed: {summary.get('recently_completed', 0)}",
    ]
    if summary.get("open_backlog"):
        counts.append(f"open backlog: {summary.get('open_backlog', 0)}")
    if warnings:
        counts.append(f"collection warnings: {warnings}")
    return [headline, "Counts: " + " | ".join(counts)]


def render_priority_section(section: dict[str, Any]) -> list[str]:
    lines = render_priority_section_items(section.get("items") or [])
    item_count = int(section.get("item_count") or len(section.get("items") or []))
    remaining_items = item_count - len(section.get("items") or [])
    if remaining_items > 0:
        lines.append(f"- {remaining_items} more actionable open item(s).")
    completed = section.get("recently_completed") or []
    completed_count = int(section.get("recently_completed_count") or 0)
    if completed:
        lines.append("### Recently Completed")
        lines.extend(render_item(item, include_handoff=False) for item in completed)
        remaining = completed_count - len(completed)
        if remaining > 0:
            lines.append(f"- {remaining} more recently completed item(s).")
    return lines


def render_priority_section_items(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["No actionable open items in this focus area."]
    lines: list[str] = []
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        bucket = str(item.get("bucket") or "in_progress")
        by_bucket.setdefault(bucket, []).append(item)
    for bucket in BUCKET_ORDER:
        rows = by_bucket.get(bucket)
        if not rows:
            continue
        lines.append(f"### {bucket.replace('_', ' ').title()}")
        lines.extend(render_item(item, include_handoff=False) for item in rows)
    return lines


def render_failure(payload: dict[str, Any]) -> str:
    display_window = payload.get("display_window") or payload["window"]
    return "\n".join(
        [
            "# GitHub Work Rollup Failed",
            "",
            f"Attempted: {payload['generated_at']} ({payload['timezone']})",
            f"Window: {display_window['since']} to {display_window['until']}",
            "",
            "## Failure",
            str(payload["error"]),
            "",
            "## Next Step",
            str(payload["next_step"]),
        ]
    ) + "\n"


def render_item(item: dict[str, Any], include_handoff: bool = True) -> str:
    ref = f"{item.get('repo')}#{item.get('number')}" if item.get("number") else str(item.get("repo") or "")
    url = item.get("url")
    link = f"[{ref}]({url})" if url else ref
    handoff = f" Handoff: {item['handoff']}." if include_handoff and item.get("handoff") and item.get("bucket") != "recently_completed" else ""
    lane = " Source: open backlog." if item.get("collection_lane") == "open_backlog" else ""
    error = f" Error: {item['error']}" if item.get("error") else ""
    return f"- {link} {item.get('title')}{lane}{handoff}{error}"


def write_or_print(rendered: str, output: str | Path | None) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        put_text_file(path, rendered)
        return
    print(rendered, end="")


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def command_summary(command: list[str], result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {"command": command[:3], "returncode": result.returncode, "stderr": trim(result.stderr)}


def trim(value: str, limit: int = 700) -> str:
    text = " ".join((value or "").split())
    return text[:limit]


def window_json(window: Window) -> dict[str, str]:
    return {"since": format_ts(window.since), "until": format_ts(window.until), "label": window.label}


def display_window_json(window: Window, timezone_name: str) -> dict[str, str]:
    display_tz: tzinfo
    try:
        display_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        display_tz = timezone.utc
    return {
        "since": window.since.astimezone(display_tz).replace(microsecond=0).isoformat(),
        "until": window.until.astimezone(display_tz).replace(microsecond=0).isoformat(),
        "label": window.label,
    }


def format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    sys.exit(main())
