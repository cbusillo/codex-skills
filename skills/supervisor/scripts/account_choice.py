#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Choose Context Panel's use-next account for the provider, with a reported fallback.

Capacity and resets come from Context Panel's agent account snapshot (schema 1).
Accounts, their launch environment and the snapshot command come from private
local config; nothing here reads credentials or changes any login.
"""

import argparse
import json
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

PROVIDERS = ("openai", "anthropic", "google")
CURRENT_STATES = {"available", "closeToLimit"}
# Context Panel states with no current reading; "limited" is a current reading with no room.
NO_READING_STATES = {"unknown", "stale", "refreshing", "unavailable", "notConnected", "off"}
# The variable that puts a launched harness on an account; Google has none yet.
ACCOUNT_VARIABLES = {"openai": "CODEX_HOME", "anthropic": "CLAUDE_CONFIG_DIR"}
WEEKLY = re.compile(r"week", re.IGNORECASE)
DEFAULT_RESERVE = 0.05
SNAPSHOT_TIMEOUT_SECONDS = 60


class Assessment(NamedTuple):
    account: dict
    eligible: bool
    reset: datetime | None
    remaining: float | None
    reason: str
    no_reading: bool = False


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
    default_reserve = reserve_value(section.get("reserve", DEFAULT_RESERVE))
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
            or not launch_env
            or not all(
                isinstance(k, str) and k.isidentifier() and isinstance(v, str) and v
                for k, v in launch_env.items()
            )
        ):
            raise ValueError(f"account {name}: env must map variable names to nonempty strings")
        required = ACCOUNT_VARIABLES.get(account["provider"])
        if required and required not in launch_env:
            raise ValueError(f"account {name}: {account['provider']} accounts must set {required} in env")
        result.append(
            {
                "name": name,
                "provider": account["provider"],
                "match": keys[0],
                "match_value": account[keys[0]],
                "env": dict(launch_env),
                "reserve": reserve_value(account.get("reserve", default_reserve)),
            }
        )
    return {"snapshot_command": command, "accounts": result}


def reserve_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1:
        raise ValueError("reserve must be a fraction from 0 up to 1")
    return float(value)


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


def assess(account, rows, now):
    """Judge one configured account against its Context Panel row."""
    matches = matching_rows(account, rows)
    if len(matches) != 1:
        return Assessment(account, False, None, None, f"{len(matches)} Context Panel rows match; expected one")
    row = matches[0]
    remaining = row.get("remainingFraction")
    if row.get("state") in NO_READING_STATES:
        return Assessment(account, False, None, None, f"no current reading (state {row['state']})", True)
    if row.get("state") not in CURRENT_STATES or not isinstance(remaining, (int, float)):
        return Assessment(account, False, None, None, f"no room (state {row.get('state')})")
    if remaining <= account["reserve"]:
        return Assessment(
            account,
            False,
            None,
            float(remaining),
            f"{remaining:.0%} left, at or below its {account['reserve']:.0%} reserve",
        )
    # Five-hour windows reset for every account all the time; the weekly allowance
    # is the capacity that lapses, so its soonest reset ranks. Without a weekly
    # label, the latest reset stands in for the longest window.
    parsed = [
        (str(window.get("label", "")), parse_time(window.get("naturalResetAt")))
        for window in row.get("windows") or []
        if isinstance(window, dict)
    ]
    future = [(label, at) for label, at in parsed if at and at > now]
    weekly = [at for label, at in future if WEEKLY.search(label)]
    reset = min(weekly) if weekly else max((at for _, at in future), default=None)
    when = f"resets {reset.isoformat()}" if reset else "no reset time reported"
    return Assessment(account, True, reset, float(remaining), f"{remaining:.0%} left, {when}")


def choose(provider, config, snapshot, unavailable_reason, now=None, name=None):
    """Pick one configured account for provider; raise ValueError when none may be used."""
    now = now or datetime.now(timezone.utc)
    accounts = [a for a in config["accounts"] if a["provider"] == provider]
    if not accounts:
        raise ValueError(f"no configured {provider} accounts")
    if name is not None:
        named = [a for a in accounts if a["name"] == name]
        if not named:
            raise ValueError(f"no configured {provider} account named {name}")
        return decision(named[0], "named", "chosen by name; capacity not checked")
    if snapshot is None:
        return decision(
            accounts[0],
            "fallback",
            f"Context Panel snapshot unavailable ({unavailable_reason}); "
            "used the configured order, capacity not checked",
        )
    rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == provider]
    assessed = [assess(account, rows, now) for account in accounts]
    answers = snapshot.get("answers") or {}
    if not isinstance(answers, dict):
        raise ValueError("Context Panel answers must be an object")
    recommendations = answers.get("useNext") or []
    if not isinstance(recommendations, list) or not all(isinstance(r, dict) for r in recommendations):
        raise ValueError("Context Panel useNext must be a list of recommendations")
    next_choices = [r for r in recommendations if r.get("provider") == provider]
    if next_choices:
        if len(next_choices) != 1 or not next_choices[0].get("accountID"):
            raise ValueError(f"Context Panel has an ambiguous {provider} use-next choice")
        next_id = next_choices[0]["accountID"]
        next_rows = [row for row in rows if row.get("id") == next_id]
        if len(next_rows) != 1:
            raise ValueError(f"Context Panel {provider} use-next choice matches no single account row")
        configured = [entry for entry in assessed
                      if matching_rows(entry.account, rows) == next_rows]
        if len(configured) != 1:
            raise ValueError(f"Context Panel {provider} use-next choice matches no single configured account")
        best = configured[0]
        if not best.eligible:
            raise ValueError(f"Context Panel {provider} use-next account cannot be launched ({best.reason})")
        return decision(
            best.account,
            "context-panel",
            f"Context Panel use next: {best.reason}",
            resets_at=best.reset,
            remaining=best.remaining,
            skipped=[entry for entry in assessed if entry is not best],
        )
    fallback_reason = f"Context Panel has no {provider} use-next choice; "
    if all(entry.no_reading for entry in assessed):
        return decision(
            accounts[0],
            "fallback",
            fallback_reason + f"no current {provider} reading for any configured account; "
            "used the configured order, capacity not checked",
            skipped=assessed,
        )
    eligible = [entry for entry in assessed if entry.eligible]
    if not eligible:
        details = "; ".join(f"{e.account['name']}: {e.reason}" for e in assessed)
        raise ValueError(f"no {provider} account can be chosen ({details})")
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    # min keeps the first configured account for ties and unknown resets.
    best = min(eligible, key=lambda entry: entry.reset or far_future)
    return decision(
        best.account,
        "fallback",
        fallback_reason + f"used soonest reset with room: {best.reason}",
        resets_at=best.reset,
        remaining=best.remaining,
        skipped=[entry for entry in assessed if entry is not best],
    )


def decision(account, source, reason, resets_at=None, remaining=None, skipped=()):
    return {
        "name": account["name"],
        "provider": account["provider"],
        "source": source,
        "reason": reason,
        "resets_at": resets_at.isoformat() if resets_at else None,
        "remaining_fraction": remaining,
        "env": account["env"],
        "others": [{"name": e.account["name"], "reason": e.reason} for e in skipped],
    }


def select(provider, config_path=None, name=None):
    config = load_config(config_path)
    snapshot, problem = (None, None) if name else read_snapshot(config["snapshot_command"])
    return choose(provider, config, snapshot, problem, name=name)


def public(choice):
    """Launch output: env variable names only; values are private local paths."""
    return {**{k: v for k, v in choice.items() if k != "env"}, "env_keys": sorted(choice["env"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--config", type=Path, help="private config; default skill-data/supervisor.toml")
    parser.add_argument("--account", help="use this configured account by name")
    args = parser.parse_args()
    try:
        print(json.dumps(public(select(args.provider, args.config, args.account)), indent=2))
    except ValueError as error:
        parser.exit(1, f"refused: {error}\n")


if __name__ == "__main__":
    main()
