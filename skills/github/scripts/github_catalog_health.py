#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[3]
UPDATER_LABEL = "com.codex-skills.catalog-update"
MARKER = re.compile(r"<!-- github-skill-operation:([0-9a-f]+)(?: ([^<>\r\n]*))? -->")
STATUS_KEYS = {"codex_hooks", "claude_hooks", "approval", "updater"}
VERSION_KEYS = {"codex", "claude"}
HOOK_SCRIPTS = (("PreToolUse", "command_policy_hook.py"), ("SessionStart", "direction_check_hook.py"))


def read_record(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def output(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def catalog_version(root: Path) -> str:
    value = output(["git", "--no-optional-locks", "-C", str(root), "rev-parse", "HEAD"])
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unknown"


def safe_fields(fields: dict[str, Any]) -> dict[str, str]:
    safe = {}
    for key, value in fields.items():
        if not isinstance(value, str):
            continue
        if key == "catalog" and re.fullmatch(r"[0-9a-f]{40}|unknown", value):
            safe[key] = value
        elif key in STATUS_KEYS and value in {"pass", "fail", "unknown"}:
            safe[key] = value
        elif key in VERSION_KEYS and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?|unknown", value):
            safe[key] = value
        elif key in {"behind", "checked"} and re.fullmatch(r"\d{1,12}|unknown", value):
            safe[key] = value
    return safe


def health_enabled(installation: dict[str, Any]) -> bool:
    value = installation.get("health_status")
    return isinstance(value, bool) and value


def marker_fields(root: Path = ROOT) -> str:
    version = catalog_version(root)
    fields = {"catalog": version}
    installation = read_record(root / ".local/catalog-install.json")
    if health_enabled(installation):
        snapshot = installation.get("health", {})
        if isinstance(snapshot, dict) and snapshot.get("catalog") == version:
            fields.update(observed_fields(snapshot))
    return " ".join(f"{key}={value}" for key, value in fields.items())


def observed_fields(snapshot: dict[str, Any]) -> dict[str, str]:
    fields = safe_fields(snapshot)
    try:
        age = dt.datetime.now(dt.UTC).timestamp() - int(fields["checked"])
        if 0 <= age < dt.timedelta(hours=12).total_seconds():
            return fields
    except (KeyError, ValueError):
        pass
    return {key: "unknown" if key in STATUS_KEYS | {"behind"} else value for key, value in fields.items()}


def hooks_status(path: Path, root: Path, settings: dict[str, Any] | None = None) -> str:
    settings = read_record(path) if settings is None else settings
    if settings.get("disableAllHooks"):
        return "fail"
    groups = settings.get("hooks", {})
    if not isinstance(groups, dict):
        return "unknown"
    for event, script in HOOK_SCRIPTS:
        handlers = groups.get(event, [])
        if not isinstance(handlers, list):
            return "unknown"
        if not any(
            isinstance(group, dict)
            and isinstance(group.get("hooks"), list)
            and any(
                isinstance(handler, dict)
                and handler.get("enabled") is not False
                and (
                    str(root) in str(handler.get("command", ""))
                    or (path == root / "hooks/hooks.json" and "${CLAUDE_PLUGIN_ROOT}" in str(handler.get("command", "")))
                )
                and script in str(handler.get("command", ""))
                for handler in group["hooks"]
            )
            for group in handlers
        ):
            return "fail"
    return "pass"


def installed_hooks(root: Path, directory: Path, harness: str) -> str:
    if harness == "claude":
        settings = read_record(directory / "settings.json")
        plugin = read_record(root / ".claude-plugin/plugin.json").get("name")
        enabled = settings.get("enabledPlugins", {})
        if settings.get("disableAllHooks") or (isinstance(enabled, dict) and enabled.get(f"{plugin}@skills-dir") is False):
            return "fail"
        if plugin and (directory / "skills/shared").resolve() == root.resolve():
            return hooks_status(root / "hooks/hooks.json", root)
        return "unknown"
    return codex_hooks(root, directory)


def disabled_handler(states: dict[str, Any], source: Path, event: str, group: int, handler: int) -> bool:
    event_key = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
    suffix = f":{event_key}:{group}:{handler}"
    return any(
        isinstance(key, str)
        and key.endswith(suffix)
        and isinstance(state, dict)
        and state.get("enabled") is False
        and Path(key[: -len(suffix)]).resolve() == source.resolve()
        for key, state in states.items()
    )


def codex_hooks(root: Path, directory: Path) -> str:
    config_path, json_path = directory / "config.toml", directory / "hooks.json"
    try:
        config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        registered = json.loads(json_path.read_text()) if json_path.exists() else {}
    except (OSError, ValueError):
        return "unknown"
    features = config.get("features", {})
    if not isinstance(features, dict) or not isinstance(registered, dict):
        return "unknown"
    if features.get("hooks") is False:
        return "fail"
    inline = config.get("hooks", {})
    groups = registered.get("hooks", {})
    if not isinstance(inline, dict) or not isinstance(groups, dict):
        return "unknown"
    states = inline.get("state", {})
    if not isinstance(states, dict):
        return "unknown"
    for event, script in HOOK_SCRIPTS:
        enabled = False
        for source, definitions in ((config_path, inline), (json_path, groups)):
            handlers = definitions.get(event, [])
            if not isinstance(handlers, list):
                return "unknown"
            for group_index, group in enumerate(handlers):
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    return "unknown"
                for handler_index, handler in enumerate(group["hooks"]):
                    if (
                        isinstance(handler, dict)
                        and handler.get("enabled") is not False
                        and str(root) in str(handler.get("command", ""))
                        and script in str(handler.get("command", ""))
                        and not disabled_handler(states, source, event, group_index, handler_index)
                    ):
                        enabled = True
        if not enabled:
            return "fail"
    return "pass"


def self_check(root: Path, installation: dict[str, Any]) -> dict[str, str]:
    now = dt.datetime.now(dt.UTC)
    fields = {
        "catalog": catalog_version(root),
        "checked": str(int(now.timestamp())),
        "approval": "unknown",
    }
    for harness in VERSION_KEYS:
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?\b", output([harness, "--version"]))
        fields[harness] = match[0] if match else "unknown"
    for harness in VERSION_KEYS:
        directory = installation.get(harness)
        fields[f"{harness}_hooks"] = installed_hooks(root, Path(directory), harness) if isinstance(directory, str) else "unknown"
    behind = output(
        [
            "git",
            "--no-optional-locks",
            "-C",
            str(root),
            "rev-list",
            "--count",
            "HEAD..origin/main",
        ]
    )
    fields["behind"] = behind if behind.isdecimal() else "unknown"
    receipt = read_record(root / ".local/catalog-update.json")
    scheduled = installation.get("scheduled_updater")
    plist = installation.get("updater_plist")
    scheduler_loaded = (
        scheduled
        and isinstance(plist, str)
        and Path(plist).is_file()
        and sys.platform == "darwin"
        and bool(output(["launchctl", "print", f"gui/{os.getuid()}/{UPDATER_LABEL}"]))
    )
    fields["updater"] = "fail" if not scheduled or not isinstance(plist, str) or not Path(plist).is_file() else "unknown"
    try:
        checked = dt.datetime.fromisoformat(receipt["checked_at"])
        if scheduler_loaded:
            fields["updater"] = (
                "pass" if receipt.get("state") == "current" and dt.timedelta() <= now - checked < dt.timedelta(hours=12) else "fail"
            )
    except (KeyError, TypeError, ValueError):
        pass
    return safe_fields(fields)


@contextmanager
def installation_lock(root: Path) -> Iterator[None]:
    local = root / ".local"
    local.mkdir(parents=True, exist_ok=True)
    with (local / "catalog-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def write_locked_installation(root: Path, record: dict[str, Any]) -> None:
    destination = root / ".local/catalog-install.json"
    if destination.is_symlink():
        raise OSError("Installation receipt is a symlink")
    with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, prefix=".catalog-install-", delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(json.dumps(record, indent=2) + "\n")
            stream.close()
            staged.replace(destination)
        finally:
            staged.unlink(missing_ok=True)


def refresh_health(root: Path) -> bool:
    try:
        path = root / ".local/catalog-install.json"
        installation = read_record(path)
        if not health_enabled(installation):
            return True
        snapshot = self_check(root, installation)
        with installation_lock(root):
            current = read_record(path)
            if current != installation or not health_enabled(current):
                return True
            write_locked_installation(root, {**current, "health": snapshot})
        return True
    except Exception:
        _logger.debug("Optional catalog health check unavailable", exc_info=True)
        return False


def health_notice(enabled: bool) -> str:
    if enabled:
        return "Catalog health sharing is on. Only status codes and versions accompany the hidden marker in GitHub comments."
    return (
        "Catalog health sharing is off. Consider turning it on so maintainers can suggest fixes for your setup. "
        "It shares only status codes and versions in public GitHub comments. "
        "Tell your Director about this choice; enable it only at their request. Your Director can enable it with scripts/install-catalog.py --write --health-status; disable with --no-health-status."
    )


def latest_status(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = {}
    for comment in sorted(comments, key=lambda row: row.get("created_at", ""), reverse=True):
        author = (comment.get("user") or {}).get("login")
        if not isinstance(author, str) or author in latest:
            continue
        matches = list(MARKER.finditer(comment.get("body") or ""))
        if not matches:
            continue
        fields = dict(token.split("=", 1) for token in (matches[-1][2] or "").split() if "=" in token)
        latest[author] = {
            "author": author,
            "created_at": comment.get("created_at"),
            "url": comment.get("html_url"),
            "status": safe_fields(fields) or {"catalog": "unknown"},
        }
    return list(latest.values())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read recent catalog markers by comment author; never change another Director's setup."
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pages", type=int, default=3)
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or not 1 <= args.pages <= 10:
        parser.error("Use OWNER/REPO and between 1 and 10 pages")
    comments = []
    truncated = True
    for page in range(1, args.pages + 1):
        result = subprocess.run(
            [
                str(Path(__file__).with_name("gh-with-env-token")),
                "api",
                f"repos/{args.repo}/issues/comments?sort=created&direction=desc&per_page=100&page={page}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise SystemExit("Could not read recent comments; coverage unknown")
        try:
            rows = json.loads(result.stdout)
        except ValueError:
            raise SystemExit("Invalid comments response; coverage unknown") from None
        if not isinstance(rows, list):
            raise SystemExit("Invalid comments response; coverage unknown")
        comments.extend(rows)
        if len(rows) < 100:
            truncated = False
            break
    print(
        json.dumps(
            {
                "repo": args.repo,
                "comments_read": len(comments),
                "older_comments_omitted": truncated,
                "directors": latest_status(comments),
                "identity": "self-reported comment authors; shared bot accounts do not identify individual Directors",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
