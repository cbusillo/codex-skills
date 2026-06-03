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
    items: list[dict[str, Any]] = []
    for repo in repos:
        items.extend(collect_repo_items(repo, settings))
    if settings["subjects"] and (settings["include_external_activity"] or not repos):
        items.extend(collect_subject_items(settings))
    items = deduplicate_items(items)
    buckets = bucket_items(items, settings)

    releases = []
    workflows = []
    for repo in repos:
        releases.extend(collect_repo_releases(repo, settings))
        workflows.extend(collect_repo_workflows(repo, settings))

    collection_warnings = collection_warnings_from(settings)

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
        "limitations": [*limitations(settings, repos), *collection_warnings],
        "releases": releases,
        "workflows": workflows,
    }


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
        ]
        completed = [
            item
            for item in sorted_items
            if item.get("repo") in repos and item.get("bucket") == "recently_completed"
        ]
        if matched or completed:
            sections.append(
                {
                    "name": str(raw.get("name") or "Priority Section"),
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


def linked_item_ref(item: dict[str, Any]) -> str:
    repo = str(item.get("repo") or "")
    number = item.get("number")
    title = str(item.get("title") or "untitled")
    url = item.get("url")
    ref = f"{repo_short_name(repo)}#{number}" if number else repo_short_name(repo)
    return f"[{ref}]({url}) {title}" if url else f"{ref} {title}"


def compact_titles(items: list[dict[str, Any]], limit: int = 3) -> str:
    titles = [str(item.get("title") or "untitled") for item in items[:limit]]
    if not titles:
        return "general updates"
    if len(items) > limit:
        titles.append(f"{len(items) - limit} more")
    return "; ".join(titles)


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


def attention_items_from_repo_data(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    attention_items: list[dict[str, Any]] = []
    for repo, data in repo_data.items():
        for item in data["attention"]:
            attention_items.append({**item, "repo": repo})
    return attention_items


def repo_risk_lines(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> list[str]:
    risk_lines: list[str] = []
    for repo, data in repo_data.items():
        failed = [workflow for workflow in data["workflows"] if str(workflow.get("conclusion") or "").casefold() == "failure"]
        if failed:
            risk_lines.append(f"`{repo_short_name(repo)}` had {len(failed)} failed workflow run(s).")
        if data["attention"]:
            risk_lines.append(f"`{repo_short_name(repo)}` has {len(data['attention'])} item(s) that need explicit attention.")
    return risk_lines


def completion_total(repo_data: dict[str, dict[str, list[dict[str, Any]]]]) -> int:
    return total_repo_count(repo_data, "pr_merged") + total_repo_count(repo_data, "pr_closed") + total_repo_count(repo_data, "issue_closed")


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
    label = focus_area_label(payload)
    if label == "Codex Skill Updates and Every Code Product Issues":
        return "Every Code and Skills Impact"
    return f"{label} Impact"


def decision_framing(profile: dict[str, Any]) -> str:
    if profile.get("detail_preference"):
        return str(profile["detail_preference"])
    if profile.get("framing"):
        return str(profile["framing"])
    return "priority, risk, sequencing, and impact"


def executive_headline(
    repo_data: dict[str, dict[str, list[dict[str, Any]]]],
    active_repos: list[str],
    profile: dict[str, Any],
    focus_sections: list[dict[str, Any]],
) -> str:
    company = profile.get("company")
    if isinstance(company, str) and company.strip():
        audience = company
    else:
        audience = "the configured work"
    if not active_repos:
        return f"No material GitHub movement was collected for {audience} in this window."

    repo_focus = repo_display_list(active_repos, 3)
    focus_names = [str(section.get("name")) for section in focus_sections[:2] if section.get("name")]
    if focus_names:
        focus_text = " and ".join(focus_names)
        return f"A focused day of work advanced {audience}'s tooling and operating loop across {repo_focus}, with the clearest movement in {focus_text}."
    return f"A focused day of work advanced {audience}'s tooling and operating loop across {repo_focus}."


def executive_impact_sentence(
    completed: int,
    open_work: int,
    release_count: int,
    profile: dict[str, Any],
) -> str:
    framing = decision_framing(profile)
    if release_count:
        ship_clause = f"{release_count} release(s) shipped"
    else:
        ship_clause = "no public release was cut"
    return (
        f"In practical terms, {completed} item(s) were completed, {ship_clause}, and {open_work} item(s) remain visible for planning; "
        f"the important read is {framing}."
    )


def report_window_text(payload: dict[str, Any]) -> str:
    tz_name = payload["timezone"]
    window = payload["window"]
    since_dt = parse_timestamp(window["since"]) or datetime.now(timezone.utc)
    until_dt = parse_timestamp(window["until"]) or datetime.now(timezone.utc)
    return f"{format_brief_datetime(since_dt, tz_name)} to {format_brief_datetime(until_dt, tz_name)}"


def render_manager_brief_markdown(payload: dict[str, Any]) -> str:
    repo_data = get_repo_data(payload)
    profile = recipient_profile(payload)
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

    lines.extend(["", "## Today's Priorities", ""])
    if attention_items:
        for item in attention_items[:5]:
            handoff = f" Handoff: {item['handoff']}." if item.get("handoff") else ""
            lines.append(f"- {linked_item_ref(item)}.{handoff}")
    elif open_work:
        lines.append(
            f"- No explicit attention items were collected; use the open work list to choose what stays active today based on {decision_framing(profile)}."
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
    active_repos = active_repo_names(repo_data)
    recipient = str(payload.get("report_recipient") or "the recipient")
    completed = completion_total(repo_data)
    open_work = open_work_total(repo_data)
    release_count = total_repo_count(repo_data, "releases")
    workflow_total, workflow_success, workflow_failed = workflow_health_summary(repo_data)

    lines = [
        f"# Daily GitHub Brief for {payload['report_recipient']}",
        "",
        f"Window: {report_window_text(payload)}",
        "",
        "## Executive Summary",
        "",
    ]

    if active_repos:
        lines.append(executive_headline(repo_data, active_repos, profile, payload.get("priority_sections") or []))
        lines.append(executive_impact_sentence(completed, open_work, release_count, profile))
        health = automation_sentence(workflow_total, workflow_success, workflow_failed)
        if health:
            lines.append(health)
    else:
        lines.append("No active work or material changes were collected for the configured scope in this window.")

    attention_items = attention_items_from_repo_data(repo_data)

    if attention_items:
        lines.extend(["", f"## Needs {recipient}'s Attention", ""])
        for item in attention_items[:5]:
            handoff = f" Handoff: {item['handoff']}." if item.get("handoff") else ""
            lines.append(f"- {linked_item_ref(item)}.{handoff}")

    lines.extend(["", "## What This Means", ""])
    change_lines = [sentence for repo, data in sorted(repo_data.items()) if (sentence := repo_outcome_sentence(repo, data))]
    if change_lines:
        lines.extend(f"- {line}" for line in change_lines[:8])
    else:
        lines.append("- No meaningful repo changes were collected in this window.")

    focus_sections = payload.get("priority_sections") or []
    if focus_sections:
        lines.extend(["", f"## {focus_area_heading(payload)}", ""])
        for section in focus_sections:
            name = str(section.get("name") or "Priority area")
            items = section.get("items") or []
            completed_items = section.get("recently_completed") or []
            item_count = int(section.get("item_count") or len(items))
            completed_count = int(section.get("recently_completed_count") or len(completed_items))
            parts = []
            if completed_count:
                parts.append(f"{completed_count} recent completion(s)")
            if item_count:
                parts.append(f"{item_count} open item(s)")
            summary = ", ".join(parts) if parts else "no active signal in the collected window"
            example = compact_titles((completed_items or items), 2) if completed_items or items else ""
            suffix = f" Themes: {example}." if example else ""
            lines.append(f"- **{name}**: {summary}.{suffix}")

    risk_lines = repo_risk_lines(repo_data)
    if risk_lines:
        lines.extend(["", "## Decisions or Risks", ""])
        lines.extend(f"- {line}" for line in risk_lines[:6])

    lines.extend(["", "## Velocity Snapshot", ""])
    lines.append(f"- Completed: {completed} item(s) collected in-window.")
    lines.append(f"- Open work visible now: {open_work} item(s).")
    lines.append(f"- Releases: {release_count} collected in-window.")
    if workflow_total:
        lines.append(f"- Automation: {workflow_success} successful and {workflow_failed} failed workflow run(s) collected.")

    lines.extend(["", "## Conversation Starters", ""])
    if risk_lines:
        lines.append("- Do any of the flagged risks need a decision today?")
    if focus_sections:
        lines.append(f"- Are the {focus_area_label(payload)} changes aligned with how {recipient} expects to use the product this week?")
    if open_work:
        lines.append("- Which visible open work should stay active versus move to backlog?")
    if not risk_lines and not open_work:
        lines.append("- Is there any follow-up outside GitHub that should be captured before tomorrow's brief?")

    limitations_list = payload.get("limitations") or []
    profile_note = profile_source_note(profile)
    if profile_note:
        limitations_list = [*limitations_list, profile_note]
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
