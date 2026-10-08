#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Follow Context Panel's ranked choices and record count-only launch receipts.

Capacity and resets come from Context Panel's agent account snapshot (schema 1).
Accounts, their launch environment and the snapshot command come from private
local config; nothing here reads credentials or changes any login.
"""

import argparse
import json
import os
import pwd
import re
import subprocess
import tempfile
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROVIDERS = ("openai", "anthropic", "google")
# The variable that puts a launched harness on an account; Google has none yet.
ACCOUNT_VARIABLES = {"openai": "CODEX_HOME", "anthropic": "CLAUDE_CONFIG_DIR"}
SNAPSHOT_TIMEOUT_SECONDS = 60
OWN_RECEIPT = re.compile(r"^\d{8}T\d{6}Z-supervisor-[0-9a-f]{32}\.json$")


def config_paths(env, home):
    paths = [
        Path(env[key]).expanduser() / "skill-data/supervisor.toml"
        for key in ("CODE_HOME", "CODEX_HOME")
        if env.get(key)
    ]
    paths.append(home / ".code/skill-data/supervisor.toml")
    return list(dict.fromkeys(paths))


def load_config(path=None, env=None, home=None):
    """Read [accounts] from the first private config that has it; invalid config fails closed."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    for candidate in [path] if path else config_paths(env, home):
        if not candidate.exists():
            if path:
                raise ValueError("account config file does not exist")
            continue
        try:
            data = tomllib.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError("cannot read private account config") from error
        if "accounts" in data:
            return validate_config(data["accounts"])
    raise ValueError("no [accounts] in private supervisor config (skill-data/supervisor.toml)")


def validate_config(section):
    if not isinstance(section, dict):
        raise ValueError("[accounts] must be a table")
    command = section.get("snapshot_command")
    if command is not None and (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ValueError("snapshot_command must be a nonempty list of strings")
    accounts = section.get("account")
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("configure at least one [[accounts.account]]")
    names = set()
    result = []
    for account in accounts:
        if not isinstance(account, dict):
            raise ValueError("each [[accounts.account]] must be a table")
        name = account.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("each account needs a unique nonempty name")
        names.add(name)
        if account.get("provider") not in PROVIDERS:
            raise ValueError(f"account {name}: provider must be one of {', '.join(PROVIDERS)}")
        keys = [k for k in ("context_panel_label", "context_panel_configuration_id") if k in account]
        if len(keys) != 1 or not isinstance(account[keys[0]], str) or not account[keys[0]]:
            raise ValueError(
                f"account {name}: set exactly one of context_panel_label or context_panel_configuration_id"
            )
        launch_env = account.get("env")
        if (
            not isinstance(launch_env, dict)
            or (not launch_env and account["provider"] != "anthropic")
            or not all(
                isinstance(k, str) and k.isidentifier() and isinstance(v, str) and v
                for k, v in launch_env.items()
            )
        ):
            raise ValueError(f"account {name}: env must map variable names to nonempty strings")
        required = ACCOUNT_VARIABLES.get(account["provider"])
        default_claude = account["provider"] == "anthropic" and not launch_env
        if required and required not in launch_env and not default_claude:
            raise ValueError(f"account {name}: {account['provider']} accounts must set {required} in env")
        result.append(
            {
                "name": name,
                "provider": account["provider"],
                "match": keys[0],
                "match_value": account[keys[0]],
                "env": dict(launch_env),
            }
        )
    return {"snapshot_command": command, "accounts": result}


def read_snapshot(command, runner=None):
    """Return (snapshot, None) or (None, why it is unavailable). Never raises for a bad read."""
    runner = runner or subprocess.run
    if not command:
        return None, "no snapshot_command configured"
    argv = [str(Path(command[0]).expanduser()), *command[1:]]
    try:
        completed = runner(
            argv, capture_output=True, text=True, timeout=SNAPSHOT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"snapshot command could not run ({type(error).__name__})"
    if completed.returncode != 0:
        return None, f"snapshot command exited {completed.returncode}"
    try:
        snapshot = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, "snapshot output is not JSON"
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
        return None, "snapshot schema is not version 1"
    if not isinstance(snapshot.get("accounts"), list):
        return None, "snapshot has no accounts list"
    return snapshot, None


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def matching_rows(account, rows):
    """Resolve the private configuration selector against provider-scoped rows."""
    if account["match"] == "context_panel_label":
        wanted = account["match_value"].casefold()
        matches = [r for r in rows if str(r.get("label", "")).casefold() == wanted]
    else:
        matches = [r for r in rows if r.get("configurationID") == account["match_value"]]
    return matches


def snapshot_notices(snapshot):
    """Preserve Context Panel's configuration errors and plain reset prompts."""
    errors = snapshot.get("configurationErrors", [])
    prompts = snapshot.get("resetPrompts", [])
    if not isinstance(errors, list) or not all(isinstance(e, str) for e in errors):
        raise ValueError("Context Panel configurationErrors must be a list of strings")
    if not isinstance(prompts, list) or not all(
        isinstance(p, dict) and isinstance(p.get("line"), str) for p in prompts
    ):
        raise ValueError("Context Panel resetPrompts must contain plain lines")
    return {"configuration_errors": errors, "reset_prompts": [p["line"] for p in prompts]}


def provider_ranking(snapshot, provider):
    rankings = snapshot.get("ranking", [])
    if not isinstance(rankings, list) or not all(isinstance(r, dict) for r in rankings):
        raise ValueError("Context Panel ranking must be a list")
    matches = [r for r in rankings if r.get("provider") == provider]
    if len(matches) > 1:
        raise ValueError(f"Context Panel has an ambiguous {provider} ranking")
    return matches[0] if matches else {}


def resolve_choice(provider, config, snapshot, account_id, source, reason):
    if not isinstance(account_id, str) or not re.fullmatch(rf"{provider}-[0-9a-f]{{1,64}}", account_id):
        raise ValueError("Context Panel choice needs the snapshot's opaque account ID")
    rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == provider]
    matched = [r for r in rows if r.get("id") == account_id]
    if len(matched) != 1:
        raise ValueError(f"Context Panel {provider} choice matches no single account row")
    configured = [a for a in config["accounts"] if a["provider"] == provider
                  and matching_rows(a, rows) == matched]
    if len(configured) != 1:
        raise ValueError(f"Context Panel {provider} choice matches no single configured account")
    row = matched[0]
    return decision(configured[0], source, reason, account_id=account_id,
                    remaining=row.get("remainingFraction"), **snapshot_notices(snapshot))


def choose_batch(provider, config, snapshot, unavailable_reason, count=1, now=None, name=None):
    try:
        return _choose_batch(provider, config, snapshot, unavailable_reason, count, now, name)
    except ValueError as error:
        if snapshot is not None:
            notices = snapshot_notices(snapshot)
            lines = [*notices["configuration_errors"], *notices["reset_prompts"]]
            if lines:
                raise ValueError(str(error) + "\n" + "\n".join(lines)) from error
        raise


def _choose_batch(provider, config, snapshot, unavailable_reason, count, now, name):
    """Consume the published order without ranking or reserving capacity ourselves."""
    now = now or datetime.now(timezone.utc)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("launch count must be a positive integer")
    accounts = [a for a in config["accounts"] if a["provider"] == provider]
    if not accounts:
        raise ValueError(f"no configured {provider} accounts")
    if snapshot is None:
        raise ValueError(f"Context Panel snapshot unavailable ({unavailable_reason}); no account chosen")
    if name is not None:
        named = [a for a in accounts if a["name"] == name]
        if len(named) != 1:
            raise ValueError(f"no configured {provider} account named {name}")
        rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == provider]
        matches = matching_rows(named[0], rows)
        if len(matches) != 1 or not matches[0].get("id"):
            raise ValueError("named account needs one Context Panel row for its launch receipt")
        return [resolve_choice(provider, config, snapshot, matches[0]["id"], "named",
                               "explicit --account; capacity not checked") for _ in range(count)]
    ranking = provider_ranking(snapshot, provider)
    stale = ranking.get("basedOnStaleReadings", False)
    if stale:
        observed = parse_time(ranking.get("readingsObservedAt"))
        if observed is None or not timedelta(0) <= now - observed < timedelta(minutes=30):
            raise ValueError(f"Context Panel {provider} stale ranking is not under 30 minutes old; no account chosen")
    answers = snapshot.get("answers", {})
    if not isinstance(answers, dict):
        raise ValueError("Context Panel answers must be an object")
    recommendations = answers.get("useNext", [])
    if not isinstance(recommendations, list) or not all(isinstance(r, dict) for r in recommendations):
        raise ValueError("Context Panel useNext must be a list of recommendations")
    choices = [r for r in recommendations if r.get("provider") == provider]
    if len(choices) > 1 or (choices and not choices[0].get("accountID")):
        raise ValueError(f"Context Panel has an ambiguous {provider} use-next choice")
    order = ranking.get("launchOrder", [])
    if not isinstance(order, list) or not all(isinstance(i, str) and i for i in order):
        raise ValueError("Context Panel launchOrder must be a list of account IDs")
    if count == 1 and not stale:
        ids = [choices[0]["accountID"]] if choices else []
        source, reason = "context-panel", "Context Panel use next"
    else:
        ids = order[:count]
        source = "context-panel-stale" if stale else "context-panel-batch"
        reason = "Context Panel recent stale launch order" if stale else "Context Panel ranked launch order"
    if not ids:
        raise ValueError(f"Context Panel has no {provider} choice; next capacity at "
                         f"{ranking.get('nextCapacityAt') or 'unknown'}")
    if len(ids) != count:
        raise ValueError(f"Context Panel {provider} launch order covers only {len(ids)} of {count} launches; use a smaller batch")
    return [resolve_choice(provider, config, snapshot, i, source, reason) for i in ids]


def choose(provider, config, snapshot, unavailable_reason, now=None, name=None):
    return choose_batch(provider, config, snapshot, unavailable_reason, now=now, name=name)[0]


def decision(account, source, reason, account_id=None, remaining=None,
             configuration_errors=(), reset_prompts=()):
    return {"name": account["name"], "provider": account["provider"], "source": source,
            "reason": reason, "account_id": account_id, "remaining_fraction": remaining,
            "env": account["env"], "configuration_errors": list(configuration_errors),
            "reset_prompts": list(reset_prompts)}


def storage_root(command):
    """Use the reader's explicit root or its documented default, never an account home."""
    if command and "--storage-root" in command:
        index = command.index("--storage-root")
        if index + 1 >= len(command):
            raise ValueError("snapshot --storage-root has no directory")
        return Path(command[index + 1]).expanduser()
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / "Library/Group Containers/MM5YXC7T6E.group.com.shinycomputers.contextpanel/Context Panel"


def select_batch(provider, config_path=None, name=None, count=1):
    config = load_config(config_path)
    snapshot, problem = read_snapshot(config["snapshot_command"])
    choices = choose_batch(provider, config, snapshot, problem, count=count, name=name)
    root = storage_root(config["snapshot_command"])
    return [{**choice, "storage_root": root} for choice in choices]


def select(provider, config_path=None, name=None):
    return select_batch(provider, config_path, name)[0]


def record_launch(choice, now=None):
    """Write immediately before submitting a launch; retain on an uncertain submit."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    provider, account_id = choice["provider"], choice.get("account_id")
    if not isinstance(account_id, str) or not re.fullmatch(rf"{provider}-[0-9a-f]{{1,64}}", account_id):
        raise ValueError("launch receipt needs the snapshot's opaque account ID")
    directory = Path(choice["storage_root"]) / "Launch Receipts"
    # A bad root must not create a replacement Context Panel store.
    directory.mkdir(mode=0o700, exist_ok=True)
    prune_receipts(directory, now)
    receipt = {"schemaVersion": 1, "provider": provider,
               "accountID": account_id, "launchedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z")}
    target = directory / f"{now:%Y%m%dT%H%M%SZ}-supervisor-{uuid.uuid4().hex}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".supervisor-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(receipt, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def prune_receipts(directory, now):
    """Delete only this launcher's day-old files; preserve other launchers and symlinks."""
    for path in directory.iterdir():
        if not OWN_RECEIPT.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            launched = parse_time(data.get("launchedAt")) if isinstance(data, dict) else None
            if launched and now - launched >= timedelta(days=1):
                path.unlink()
        except (OSError, ValueError):
            continue


def public(choice):
    """Launch output contains environment names, never private paths."""
    return {**{k: v for k, v in choice.items() if k not in {"env", "storage_root"}},
            "env_keys": sorted(choice["env"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--config", type=Path, help="private config; default skill-data/supervisor.toml")
    parser.add_argument("--account", help="use this configured account by name")
    parser.add_argument("--count", type=int, default=1, help="preview a batch from the published launch order")
    args = parser.parse_args()
    try:
        choices = select_batch(args.provider, args.config, args.account, args.count)
        print(json.dumps(public(choices[0]) if args.count == 1 else [public(c) for c in choices], indent=2))
    except ValueError as error:
        parser.exit(1, f"refused: {error}\n")


if __name__ == "__main__":
    main()
