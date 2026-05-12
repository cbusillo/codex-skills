#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compact GitHub issue planning helper for Codex skills."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from typing import Any


SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
BOT_GH = SKILL_DIR.parent / "github/scripts/gh-with-env-token"
API_VERSION_ARGS = ["-H", "X-GitHub-Api-Version: 2022-11-28"]

DEFAULT_CONFIG: dict[str, Any] = {
    "labels": {
        "plan": "plan",
        "active": "plan:active",
        "blocked": "plan:blocked",
        "stale": "plan:stale",
        "done": "plan:done",
    },
    "label_defs": {
        "plan": {"color": "5319e7", "description": "Durable planning issue"},
        "plan:active": {"color": "0e8a16", "description": "Current active plan"},
        "plan:blocked": {"color": "d93f0b", "description": "Plan is blocked"},
        "plan:stale": {"color": "bfbfbf", "description": "Plan needs review"},
        "plan:done": {"color": "006b75", "description": "Plan completed or superseded"},
    },
    "default_sections": [
        "Finish Line",
        "Current Status",
        "Relationships",
        "Acceptance Criteria",
        "Open Questions",
    ],
    "projects": {"enabled": True, "owner": None, "default_project": None},
    "workflow": {"default_manager": None, "repo_managers": {}},
    "project_fields": {
        "focus": "Focus",
        "manager": "Manager",
        "finish_line": "Finish Line",
    },
}

class PlanError(Exception):
    pass


def die(message: str, *, detail: str | None = None, code: int = 1) -> None:
    payload = {"ok": False, "error": message}
    if detail:
        payload["detail"] = detail.strip()
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    raise SystemExit(code)


def emit(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def get_codex_home() -> pathlib.Path:
    if os.environ.get("CODEX_HOME"):
        return pathlib.Path(os.environ["CODEX_HOME"]).expanduser()
    code_home = pathlib.Path("~/.code").expanduser()
    if (code_home / "skills").is_dir() or (code_home / "plans").exists():
        return code_home
    return pathlib.Path("~/.codex").expanduser()


def workspace_config_path() -> pathlib.Path:
    return get_codex_home() / "github-planning.json"


def run_raw(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    prefer_active: bool = False,
    recoverable: bool = False,
) -> tuple[str, str, str]:
    """Run gh through automation first, with active gh as a rate-limit fallback."""
    tried: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    bot_enabled = BOT_GH.exists() and os.environ.get("GH_PLAN_SKIP_BOT") != "1"
    active_first = prefer_active and os.environ.get("GH_PLAN_ALLOW_ACTIVE_FIRST") == "1"
    commands: list[tuple[str, list[str]]] = []
    if active_first:
        commands.append(("active-gh-user", ["gh", *args]))
        if bot_enabled:
            commands.append(("automation-gh", [str(BOT_GH), *args]))
    elif bot_enabled:
        commands.append(("automation-gh", [str(BOT_GH), *args]))
    else:
        commands.append(("active-gh-user", ["gh", *args]))

    for index, (actor, command) in enumerate(commands):
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tried.append((actor, proc))
        if proc.returncode == 0:
            return actor, proc.stdout, proc.stderr
        if (
            not active_first
            and actor == "automation-gh"
            and index + 1 == len(commands)
            and is_graphql_rate_limited(proc.stderr)
        ):
            continue
        if actor == "automation-gh" or active_first:
            break

    if tried and tried[-1][0] == "automation-gh" and is_graphql_rate_limited(tried[-1][1].stderr):
        print(
            "warning: automation gh token hit a GraphQL/API rate limit; retrying with active gh auth",
            file=sys.stderr,
        )
        proc = subprocess.run(
            ["gh", *args],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tried.append(("active-gh-user", proc))
        if proc.returncode == 0:
            return "active-gh-user", proc.stdout, proc.stderr

    actor, proc = tried[-1]
    if check:
        detail = "\n".join(
            f"[{name}] exit={p.returncode}\n{p.stderr.strip()}" for name, p in tried if p.stderr.strip()
        )
        if recoverable:
            raise PlanError(f"gh command failed: {detail or proc.stdout}")
        die("gh command failed", detail=detail or proc.stdout)
    return actor, proc.stdout, proc.stderr


def is_graphql_rate_limited(stderr: str) -> bool:
    lowered = stderr.lower()
    return "rate limit" in lowered and (
        "graphql" in lowered or "api rate" in lowered or "secondary rate" in lowered
    )


def gh_json(
    args: list[str],
    *,
    input_text: str | None = None,
    prefer_active: bool = False,
    recoverable: bool = False,
) -> tuple[str, Any]:
    actor, stdout, _ = run_raw(
        args, input_text=input_text, prefer_active=prefer_active, recoverable=recoverable
    )
    if not stdout.strip():
        return actor, None
    try:
        return actor, json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PlanError(f"Expected JSON from gh, got: {stdout[:300]}") from exc


def git_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start or pathlib.Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return pathlib.Path(proc.stdout.strip())
    return None


def repo_from_git(start: pathlib.Path | None = None) -> str | None:
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=start or pathlib.Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def default_repo(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    repo = repo_from_git()
    if repo:
        return repo
    actor, stdout, stderr = run_raw(["repo", "view", "--json", "nameWithOwner"], check=False)
    if not stdout.strip():
        raise PlanError("Could not resolve a GitHub repo; pass --repo OWNER/REPO")
    try:
        data = json.loads(stdout)
        return data["nameWithOwner"]
    except Exception as exc:  # pragma: no cover - defensive CLI UX
        raise PlanError("Could not resolve a GitHub repo; pass --repo OWNER/REPO") from exc


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def repo_config_path(repo: str | None) -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    root = git_root()
    if root:
        candidates.append(root)
    if repo:
        repo_name = repo.split("/", 1)[1]
        candidates.append(pathlib.Path.home() / "Developer" / repo_name)
    for candidate in candidates:
        if repo and repo_from_git(candidate) != repo:
            continue
        path = candidate / ".github/github.json"
        if path.exists():
            return path
    return None


def load_config(repo: str | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    workspace_config = workspace_config_path()
    if workspace_config.exists():
        config = deep_merge(config, json.loads(workspace_config.read_text()))
    repo_config = repo_config_path(repo)
    if repo_config and repo_config.exists():
        data = json.loads(repo_config.read_text())
        if isinstance(data.get("planning"), dict):
            config = deep_merge(config, data["planning"])
    return config


def labels(config: dict[str, Any], *keys: str) -> list[str]:
    return [config["labels"][key] for key in keys]


def manager_for_repo(config: dict[str, Any], repo: str) -> str | None:
    workflow = config.get("workflow") or {}
    repo_managers = workflow.get("repo_managers") or {}
    return repo_managers.get(repo) or workflow.get("default_manager")


def normalize_labels(items: list[dict[str, Any]] | None) -> list[str]:
    return [item.get("name", "") for item in items or [] if item.get("name")]


def issue_ref(ref: str, repo: str) -> tuple[str, int]:
    ref = ref.strip()
    url = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if url:
        return url.group(1), int(url.group(2))
    full = re.fullmatch(r"([^/\s]+/[^#\s]+)#(\d+)", ref)
    if full:
        return full.group(1), int(full.group(2))
    if ref.startswith("#"):
        return repo, int(ref[1:])
    if ref.isdigit():
        return repo, int(ref)
    raise PlanError(f"Unsupported issue reference: {ref}")


def get_issue(ref: str, repo: str) -> tuple[str, dict[str, Any]]:
    issue_repo, number = issue_ref(ref, repo)
    actor, data = gh_json(["api", *API_VERSION_ARGS, f"repos/{issue_repo}/issues/{number}"])
    data["repo"] = issue_repo
    return actor, data


def issue_labels(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for item in labels:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def rest_create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    milestone: str | None = None,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if milestone:
        payload["milestone"] = milestone
    actor, data = gh_json(
        [
            "api",
            *API_VERSION_ARGS,
            "-X",
            "POST",
            f"repos/{repo}/issues",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
    )
    if not isinstance(data, dict):
        raise PlanError("gh api issue create returned no issue")
    data["repo"] = repo
    return actor, data


def rest_edit_issue(
    repo: str,
    number: int,
    *,
    body: str | None = None,
    title: str | None = None,
    labels: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {}
    if body is not None:
        payload["body"] = body
    if title is not None:
        payload["title"] = title
    if labels is not None:
        payload["labels"] = labels
    if not payload:
        raise PlanError("No issue fields to update")
    actor, data = gh_json(
        [
            "api",
            *API_VERSION_ARGS,
            "-X",
            "PATCH",
            f"repos/{repo}/issues/{number}",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
    )
    if not isinstance(data, dict):
        raise PlanError("gh api issue edit returned no issue")
    data["repo"] = repo
    return actor, data


def get_issue_compact(ref: str, repo: str) -> tuple[str, dict[str, Any]]:
    actor, issue = get_issue(ref, repo)
    return actor, compact_issue(issue)


def compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    deps = issue.get("issue_dependencies_summary") or {}
    sub = issue.get("sub_issues_summary") or {}
    repo = issue.get("repo") or (issue.get("repository") or {}).get("full_name")
    return {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "updated_at": issue.get("updated_at") or issue.get("updatedAt"),
        "url": issue.get("html_url") or issue.get("url"),
        "labels": normalize_labels(issue.get("labels")),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
        "dependencies": {
            "blocked_by": deps.get("blocked_by"),
            "blocking": deps.get("blocking"),
        },
        "sub_issues": {
            "total": sub.get("total"),
            "completed": sub.get("completed"),
            "percent_completed": sub.get("percent_completed"),
        },
    }


def section_map(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body or ""))
    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def replace_section(body: str, section: str, new_text: str) -> str:
    body = body or ""
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(section)}\s*\n.*?(?=^##\s+|\Z)")
    replacement = f"## {section}\n\n{new_text.strip()}\n\n"
    if pattern.search(body):
        return pattern.sub(replacement, body).rstrip() + "\n"
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{body}\n{replacement}".lstrip()


def template_body(title: str) -> str:
    return f"""## Objective

{title}

## Finish Line

This work is done when the desired outcome is observable and the next re-entry
point is captured.

## Current Status

State: Active
Next action: Decide the first concrete implementation step.
Blocked by: None.
Last verified: Not yet verified.

## Scope

- In:
- Out:

## Acceptance Criteria

- [ ] Outcome is defined.
- [ ] Validation is captured.

## Relationships

- None yet.

## Validation

- Not defined yet.

## Decisions

- None yet.

## Open Questions

- None yet.
"""


def read_body(args: argparse.Namespace, fallback: str = "") -> str:
    if getattr(args, "body", None) is not None:
        return args.body
    body_file = getattr(args, "body_file", None)
    if body_file:
        if body_file == "-":
            return sys.stdin.read()
        return pathlib.Path(body_file).read_text()
    return fallback


def write_temp_body(body: str) -> pathlib.Path:
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(body)
        return pathlib.Path(tmp.name)


def relationship_line(rel: str, target: dict[str, Any]) -> str:
    target_repo = target["repo"]
    target_number = int(target["number"])
    return f"- {rel}: {target_repo}#{target_number} - {target.get('html_url') or target.get('url')}"


def add_relationship_note(body: str, rel: str, target: dict[str, Any]) -> str:
    line = relationship_line(rel, target)
    sections = section_map(body)
    current = sections.get("Relationships", "")
    if line in current:
        return body
    if not current or current.strip() in {"- None yet.", "None yet.", "None."}:
        new_text = line
    else:
        new_text = current.rstrip() + "\n" + line
    return replace_section(body, "Relationships", new_text)


def remove_relationship_note(body: str, rel: str, target: dict[str, Any]) -> str:
    line = relationship_line(rel, target)
    sections = section_map(body)
    current = sections.get("Relationships", "")
    kept = [item for item in current.splitlines() if item.strip() != line]
    new_text = "\n".join(kept).strip() or "- None yet."
    return replace_section(body, "Relationships", new_text)


def ensure_labels(repo: str, wanted: list[str], config: dict[str, Any]) -> tuple[str, list[str]]:
    actor, existing = gh_json(["label", "list", "-R", repo, "--json", "name", "--limit", "500"])
    existing_names = {item["name"] for item in existing or []}
    created: list[str] = []
    defs = config.get("label_defs", {})
    for name in wanted:
        if name in existing_names:
            continue
        info = defs.get(name, {"color": "ededed", "description": "Planning label"})
        run_raw([
            "label",
            "create",
            name,
            "-R",
            repo,
            "--color",
            info.get("color", "ededed"),
            "--description",
            info.get("description", "Planning label"),
        ])
        created.append(name)
    return actor, created


def cmd_index(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    label = args.label or config["labels"]["plan"]
    fields = "number,title,state,updatedAt,labels,milestone,url"
    actor, data = gh_json([
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        args.state,
        "--limit",
        str(args.limit),
        "--label",
        label,
        "--json",
        fields,
    ])
    items = []
    for item in data or []:
        items.append({
            "repo": repo,
            "number": item["number"],
            "title": item["title"],
            "state": item["state"],
            "updated_at": item["updatedAt"],
            "url": item["url"],
            "labels": normalize_labels(item.get("labels")),
            "milestone": (item.get("milestone") or {}).get("title"),
        })
    emit({"ok": True, "actor": actor, "repo": repo, "count": len(items), "plans": items})


def cmd_search(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    query = args.query
    actor, data = gh_json([
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        args.state,
        "--limit",
        str(args.limit),
        "--search",
        query,
        "--json",
        "number,title,state,updatedAt,labels,milestone,url",
    ])
    items = [{
        "repo": repo,
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "updated_at": item["updatedAt"],
        "url": item["url"],
        "labels": normalize_labels(item.get("labels")),
        "milestone": (item.get("milestone") or {}).get("title"),
    } for item in data or []]
    emit({"ok": True, "actor": actor, "repo": repo, "count": len(items), "issues": items})


def cmd_show(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    actor, issue = get_issue(args.issue, repo)
    body = issue.get("body") or ""
    result = compact_issue(issue)
    if args.full:
        result["body"] = body
    else:
        names = args.sections or config.get("default_sections") or []
        sections = section_map(body)
        result["sections"] = {name: sections.get(name, "") for name in names}
    emit({"ok": True, "actor": actor, "issue": result})


def cmd_create(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    title = (args.title_flag or args.title or "").strip()
    if not title:
        raise PlanError("Issue title is required (pass as positional argument or --title)")
    search_query = f'"{title}" in:title'
    _, matches = gh_json([
        "issue", "list", "-R", repo, "--state", "all", "--limit", "100",
        "--search", search_query, "--json", "number,title,state,url"
    ])
    exact = [item for item in matches or [] if item.get("title") == title]
    if exact and not args.force:
        emit({"ok": True, "deduped": True, "repo": repo, "existing": exact[0]})
        return

    base_labels = labels(config, "plan")
    if args.plan_status != "none":
        base_labels.extend(labels(config, args.plan_status))
    extra_labels = args.label or []
    wanted_labels = base_labels + extra_labels
    _, created_labels = ensure_labels(repo, wanted_labels, config)

    body = read_body(args, template_body(title))
    if args.finish_line:
        body = replace_section(body, "Finish Line", args.finish_line)
    project_config = config.get("projects") or {}
    project = args.project
    if project is None and project_config.get("enabled", True):
        project = project_config.get("default_project")
    actor, issue = rest_create_issue(
        repo,
        title,
        body,
        wanted_labels,
        args.milestone,
    )
    project_fields_set: dict[str, Any] = {}
    if project:
        try:
            owner = project_config.get("owner") or repo.split("/", 1)[0]
            _, number, _ = resolve_project(owner, project, recoverable=True)
            run_raw(["project", "item-add", str(number), "--owner", owner, "--url", issue["html_url"], "--format", "json"], prefer_active=True, recoverable=True)
            project_fields_set = set_project_fields(
                owner=owner,
                project_ref=project,
                issue_url=issue["html_url"],
                config=config,
                focus=args.focus,
                manager=args.manager or manager_for_repo(config, repo),
                finish_line=args.finish_line,
                recoverable=True,
            )
        except PlanError as exc:
            project_fields_set = {"error": str(exc)}
    emit({
        "ok": True,
        "actor": actor,
        "created_labels": created_labels,
        "project_fields": project_fields_set,
        "issue": compact_issue(issue),
    })


def cmd_update_section(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    issue_repo, number = issue_ref(args.issue, repo)
    _, issue = get_issue(args.issue, repo)
    body = issue.get("body") or ""
    new_text = read_body(args)
    updated = replace_section(body, args.section, new_text)
    actor, refreshed = rest_edit_issue(issue_repo, number, body=updated)
    emit({"ok": True, "actor": actor, "updated_section": args.section, "issue": compact_issue(refreshed)})


def cmd_link(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    actor_a, source = get_issue(args.issue, repo)
    _, target = get_issue(args.target, repo)
    source_repo = source["repo"]
    target_repo = target["repo"]
    source_number = int(source["number"])
    target_number = int(target["number"])
    rel = args.relationship

    if rel == "blocked-by":
        actor, _, _ = run_raw(["api", *API_VERSION_ARGS, "-X", "POST", f"repos/{source_repo}/issues/{source_number}/dependencies/blocked_by", "-F", f"issue_id={target['id']}"])
    elif rel == "blocks":
        actor, _, _ = run_raw(["api", *API_VERSION_ARGS, "-X", "POST", f"repos/{target_repo}/issues/{target_number}/dependencies/blocked_by", "-F", f"issue_id={source['id']}"])
    elif rel == "subissue":
        actor, _, _ = run_raw(["api", *API_VERSION_ARGS, "-X", "POST", f"repos/{source_repo}/issues/{source_number}/sub_issues", "-F", f"sub_issue_id={target['id']}"])
    elif rel == "related":
        updated = add_relationship_note(source.get("body") or "", "related", target)
        actor, source = rest_edit_issue(source_repo, source_number, body=updated)
    else:
        raise PlanError(f"Unsupported relationship: {rel}")

    emit({
        "ok": True,
        "actor": actor,
        "relationship": rel,
        "source": compact_issue(source),
        "target": compact_issue(target),
    })


def cmd_unlink(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    _, source = get_issue(args.issue, repo)
    _, target = get_issue(args.target, repo)
    source_repo = source["repo"]
    target_repo = target["repo"]
    source_number = int(source["number"])
    target_number = int(target["number"])
    rel = args.relationship
    if rel == "blocked-by":
        actor, _, _ = run_raw(["api", *API_VERSION_ARGS, "-X", "DELETE", f"repos/{source_repo}/issues/{source_number}/dependencies/blocked_by/{target['id']}"])
    elif rel == "blocks":
        actor, _, _ = run_raw(["api", *API_VERSION_ARGS, "-X", "DELETE", f"repos/{target_repo}/issues/{target_number}/dependencies/blocked_by/{source['id']}"])
    elif rel == "subissue":
        actor, _, _ = run_raw(["api", *API_VERSION_ARGS, "-X", "DELETE", f"repos/{source_repo}/issues/{source_number}/sub_issue", "-F", f"sub_issue_id={target['id']}"])
    elif rel == "related":
        updated = remove_relationship_note(source.get("body") or "", "related", target)
        actor, _ = rest_edit_issue(source_repo, source_number, body=updated)
    else:
        raise PlanError(f"Unlink supports blocked-by, blocks, subissue, and related; got {rel}")
    emit({"ok": True, "actor": actor, "relationship_removed": rel})


def cmd_deps(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    _, issue = get_issue(args.issue, repo)
    issue_repo = issue["repo"]
    number = int(issue["number"])
    result: dict[str, Any] = {"issue": compact_issue(issue)}
    for name, endpoint in [("blocked_by", "blocked_by"), ("blocking", "blocking")]:
        actor, data = gh_json(["api", *API_VERSION_ARGS, f"repos/{issue_repo}/issues/{number}/dependencies/{endpoint}"])
        result[name] = [compact_issue({**item, "repo": (item.get("repository") or {}).get("full_name")}) for item in data or []]
    actor, data = gh_json(["api", *API_VERSION_ARGS, f"repos/{issue_repo}/issues/{number}/sub_issues"])
    result["sub_issues"] = [compact_issue({**item, "repo": (item.get("repository") or {}).get("full_name")}) for item in data or []]
    emit({"ok": True, "actor": actor, **result})


def resolve_project(owner: str, title_or_number: str, *, recoverable: bool = False) -> tuple[str, int, dict[str, Any] | None]:
    if title_or_number.isdigit():
        return "unknown", int(title_or_number), None
    actor, data = gh_json(
        ["project", "list", "--owner", owner, "--format", "json", "--limit", "100"],
        prefer_active=True,
        recoverable=recoverable,
    )
    projects = data.get("projects", data) if isinstance(data, dict) else data
    for item in projects or []:
        if item.get("title") == title_or_number:
            return actor, int(item.get("number")), item
    raise PlanError(f"Project not found for {owner}: {title_or_number}")


def project_meta(owner: str, title_or_number: str, *, recoverable: bool = False) -> tuple[str, int, dict[str, Any]]:
    actor, number, project_data = resolve_project(owner, title_or_number, recoverable=recoverable)
    if project_data:
        return actor, number, project_data
    actor, data = gh_json(["project", "view", str(number), "--owner", owner, "--format", "json"], prefer_active=True, recoverable=recoverable)
    return actor, number, data


def project_fields(owner: str, project_number: int, *, recoverable: bool = False) -> dict[str, dict[str, Any]]:
    _, data = gh_json(["project", "field-list", str(project_number), "--owner", owner, "--format", "json"], prefer_active=True, recoverable=recoverable)
    return {item["name"]: item for item in data.get("fields", [])}


def project_items(owner: str, project_number: int, *, recoverable: bool = False) -> list[dict[str, Any]]:
    _, data = gh_json(["project", "item-list", str(project_number), "--owner", owner, "--format", "json", "--limit", "200"], prefer_active=True, recoverable=recoverable)
    return data.get("items", [])


def find_project_item(
    owner: str,
    project_number: int,
    issue_url: str,
    *,
    recoverable: bool = False,
    attempts: int = 3,
) -> dict[str, Any]:
    for attempt in range(attempts):
        for item in project_items(owner, project_number, recoverable=recoverable):
            content = item.get("content") or {}
            if content.get("url") == issue_url:
                return item
        if attempt + 1 < attempts:
            time.sleep(1)
    raise PlanError(f"Issue is not in project {owner}/{project_number}: {issue_url}")


def set_project_field(
    *,
    owner: str,
    project: dict[str, Any],
    project_number: int,
    item: dict[str, Any],
    field: dict[str, Any],
    value: str,
    recoverable: bool = False,
) -> str:
    args = [
        "project",
        "item-edit",
        "--id",
        item["id"],
        "--project-id",
        project["id"],
        "--field-id",
        field["id"],
        "--format",
        "json",
    ]
    if field.get("type") == "ProjectV2SingleSelectField":
        option = next((opt for opt in field.get("options", []) if opt.get("name") == value), None)
        if not option:
            raise PlanError(f"Unknown option for {field['name']}: {value}")
        args.extend(["--single-select-option-id", option["id"]])
    else:
        args.extend(["--text", value])
    actor, _, _ = run_raw(args, prefer_active=True, recoverable=recoverable)
    return actor


def clear_project_field(
    *,
    project: dict[str, Any],
    item: dict[str, Any],
    field: dict[str, Any],
    recoverable: bool = False,
) -> str:
    actor, _, _ = run_raw([
        "project",
        "item-edit",
        "--id",
        item["id"],
        "--project-id",
        project["id"],
        "--field-id",
        field["id"],
        "--clear",
        "--format",
        "json",
    ], prefer_active=True, recoverable=recoverable)
    return actor


def set_project_fields(
    *,
    owner: str,
    project_ref: str,
    issue_url: str,
    config: dict[str, Any],
    focus: str | None = None,
    manager: str | None = None,
    finish_line: str | None = None,
    recoverable: bool = False,
) -> dict[str, Any]:
    actor, project_number, project = project_meta(owner, project_ref, recoverable=recoverable)
    fields = project_fields(owner, project_number, recoverable=recoverable)
    item = find_project_item(owner, project_number, issue_url, recoverable=recoverable)
    field_names = config.get("project_fields") or {}
    updates = {
        field_names.get("focus", "Focus"): focus,
        field_names.get("manager", "Manager"): manager,
        field_names.get("finish_line", "Finish Line"): finish_line,
    }
    updated: dict[str, str] = {}
    for field_name, value in updates.items():
        if not value:
            continue
        field = fields.get(field_name)
        if not field:
            raise PlanError(f"Project field not found: {field_name}")
        actor = set_project_field(owner=owner, project=project, project_number=project_number, item=item, field=field, value=value, recoverable=recoverable)
        updated[field_name] = value
    return {"actor": actor, "project": project.get("title"), "updated": updated}


def cmd_project_add(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    _, issue = get_issue(args.issue, repo)
    issue_repo = issue["repo"]
    owner = args.owner or (config.get("projects") or {}).get("owner") or issue_repo.split("/", 1)[0]
    project = args.project or (config.get("projects") or {}).get("default_project")
    if not project:
        raise PlanError("Pass --project or set planning.projects.default_project")
    _, number, project_data = resolve_project(owner, project, recoverable=True)
    actor, data = gh_json([
        "project", "item-add", str(number), "--owner", owner, "--url", issue["html_url"], "--format", "json"
    ], prefer_active=True, recoverable=True)
    emit({"ok": True, "actor": actor, "owner": owner, "project": project_data or {"number": number}, "item": data})


def cmd_project_set(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    _, issue = get_issue(args.issue, repo)
    project_config = config.get("projects") or {}
    owner = args.owner or project_config.get("owner") or issue["repo"].split("/", 1)[0]
    project = args.project or project_config.get("default_project")
    if not project:
        raise PlanError("Pass --project or set planning.projects.default_project")
    result = set_project_fields(
        owner=owner,
        project_ref=project,
        issue_url=issue["html_url"],
        config=config,
        focus=args.focus,
        manager=args.manager,
        finish_line=args.finish_line,
        recoverable=True,
    )
    emit({"ok": True, **result})


def cmd_close(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    issue_repo, number = issue_ref(args.issue, repo)
    _, issue = get_issue(args.issue, repo)
    plan_labels = config.get("labels") or {}
    edit_args = ["issue", "edit", str(number), "-R", issue_repo]
    if plan_labels.get("active"):
        edit_args.extend(["--remove-label", plan_labels["active"]])
    if plan_labels.get("done"):
        edit_args.extend(["--add-label", plan_labels["done"]])
    actor, _, _ = run_raw(edit_args)

    close_comment = read_body(args) if args.body is not None or args.body_file else ""
    if close_comment:
        tmp_path = write_temp_body(close_comment)
        try:
            actor, _, _ = run_raw([
                "issue", "comment", str(number), "-R", issue_repo,
                "--body-file", str(tmp_path),
            ])
        finally:
            tmp_path.unlink(missing_ok=True)

    close_args = ["issue", "close", str(number), "-R", issue_repo, "--reason", args.reason]
    actor, _, _ = run_raw(close_args)

    project_result: dict[str, Any] = {}
    project_config = config.get("projects") or {}
    project = args.project or project_config.get("default_project")
    owner = args.owner or project_config.get("owner") or issue_repo.split("/", 1)[0]
    if project:
        try:
            _, project_number, project_data = project_meta(owner, project, recoverable=True)
            fields = project_fields(owner, project_number, recoverable=True)
            item = find_project_item(owner, project_number, issue["html_url"], recoverable=True)
            updated: dict[str, str | None] = {}
            status_field = fields.get("Status")
            if status_field:
                set_project_field(
                    owner=owner,
                    project=project_data,
                    project_number=project_number,
                    item=item,
                    field=status_field,
                    value="Done",
                    recoverable=True,
                )
                updated["Status"] = "Done"
            focus_field = fields.get((config.get("project_fields") or {}).get("focus", "Focus"))
            if focus_field:
                clear_project_field(project=project_data, item=item, field=focus_field, recoverable=True)
                updated["Focus"] = None
            project_result = {"project": project_data.get("title"), "updated": updated}
        except PlanError as exc:
            project_result = {"error": str(exc)}

    emit({
        "ok": True,
        "actor": actor,
        "closed": {"repo": issue_repo, "number": number, "reason": args.reason, "url": issue["html_url"]},
        "project": project_result,
    })


def cmd_project_list(args: argparse.Namespace) -> None:
    owner = args.owner
    cmd = ["project", "list", "--owner", owner, "--format", "json", "--limit", str(args.limit)]
    if args.closed:
        cmd.append("--closed")
    actor, data = gh_json(
        cmd,
        prefer_active=True,
        recoverable=True,
    )
    emit({"ok": True, "actor": actor, "owner": owner, "projects": data})


def cmd_ensure_labels(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    wanted = list(config["labels"].values())
    actor, created = ensure_labels(repo, wanted, config)
    emit({"ok": True, "actor": actor, "repo": repo, "ensured": wanted, "created": created})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compact GitHub issue planning helper")
    parser.add_argument("--repo", help="Default OWNER/REPO for issue refs")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="Compact plan issue index, no bodies")
    p.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--label")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="Compact issue search, no bodies")
    p.add_argument("query")
    p.add_argument("--state", default="all", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="Show compact issue sections by default")
    p.add_argument("issue")
    p.add_argument("--full", action="store_true")
    p.add_argument("--sections", nargs="+")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("create", help="Create a durable plan issue")
    p.add_argument("title", nargs="?", help="Issue title (positional)")
    p.add_argument("--title", dest="title_flag", help="Issue title (flag)")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--label", action="append")
    p.add_argument("--milestone")
    p.add_argument("--project")
    p.add_argument("--force", action="store_true")
    p.add_argument("--plan-status", choices=["active", "blocked", "stale", "done", "none"], default="active")
    p.add_argument("--focus", choices=["Now", "Next", "Waiting", "Later"])
    p.add_argument("--manager")
    p.add_argument("--finish-line")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("update-section", help="Patch one markdown section")
    p.add_argument("issue")
    p.add_argument("section")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.set_defaults(func=cmd_update_section)

    p = sub.add_parser("link", help="Create native issue relationships")
    p.add_argument("issue")
    p.add_argument("relationship", choices=["blocked-by", "blocks", "subissue", "related"])
    p.add_argument("target")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="Remove native issue relationships")
    p.add_argument("issue")
    p.add_argument("relationship", choices=["blocked-by", "blocks", "subissue", "related"])
    p.add_argument("target")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("deps", help="Show dependencies and sub-issues")
    p.add_argument("issue")
    p.set_defaults(func=cmd_deps)

    p = sub.add_parser("close", help="Close a completed plan and update Project state")
    p.add_argument("issue")
    p.add_argument("--reason", default="completed")
    p.add_argument("--comment", dest="body")
    p.add_argument("--comment-file", dest="body_file")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("project-add", help="Add issue to a personal/org Project")
    p.add_argument("issue")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.set_defaults(func=cmd_project_add)

    p = sub.add_parser("project-set", help="Set human workflow Project fields")
    p.add_argument("issue")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.add_argument("--focus", choices=["Now", "Next", "Waiting", "Later"])
    p.add_argument("--manager")
    p.add_argument("--finish-line")
    p.set_defaults(func=cmd_project_set)

    p = sub.add_parser("project-list", help="List Projects for an owner")
    p.add_argument("--owner", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--closed", action="store_true")
    p.set_defaults(func=cmd_project_list)

    p = sub.add_parser("ensure-labels", help="Create fixed planning labels when missing")
    p.set_defaults(func=cmd_ensure_labels)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except PlanError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        die("Interrupted", code=130)


if __name__ == "__main__":
    main()
