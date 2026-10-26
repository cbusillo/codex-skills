#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""Follow Context Panel's ranked choices and record count-only launch receipts.

Capacity and resets come from Context Panel's agent account snapshot (schema 1).
Accounts, their launch environment and the snapshot command come from private
local config; nothing here reads credentials or changes any login.
"""

import argparse
import errno
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
from typing import Any

PROVIDERS = ("openai", "anthropic", "google")


class UnconfiguredChoice(ValueError):
    pass
# Supported variables that put a launched harness on an account. AGY uses its existing login.
ACCOUNT_VARIABLES = {"openai": "CODEX_HOME", "anthropic": "CLAUDE_CONFIG_DIR"}
GOOGLE_ACCOUNT_BINDING = "existing-agy-login"
SNAPSHOT_TIMEOUT_SECONDS = 60
RECORD_LAUNCH_TIMEOUT_SECONDS = 30
LAUNCHER = "supervisor"
OWN_RECEIPT = re.compile(r"^\d{8}T\d{6}Z-supervisor-[0-9a-f]{32}\.json$")
APP_RECEIPT = re.compile(rf"^\d{{8}}T\d{{6}}Z-{LAUNCHER}-[0-9A-Za-z]+\.json$")


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
        if account["provider"] == "google":
            if account.get("single_login") is not True or launch_env != {}:
                raise ValueError(f"account {name}: google requires single_login = true and env = {{}}; "
                                 "AGY has no account override")
        elif "single_login" in account:
            raise ValueError(f"account {name}: single_login is only supported for google")
        if (
            not isinstance(launch_env, dict)
            or (not launch_env and account["provider"] == "openai")
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
    if sum(a["provider"] == "google" for a in result) > 1:
        raise ValueError("configure only one google account; AGY has no account override")
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


def resolve_choice(provider: str, config: dict, snapshot: dict, account_id: object, source: str, reason: str) -> dict[str, Any]:
    if not isinstance(account_id, str) or not re.fullmatch(rf"{provider}-[0-9a-f]{{1,64}}", account_id):
        raise ValueError("Context Panel choice needs the snapshot's opaque account ID")
    rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == provider]
    matched = [r for r in rows if r.get("id") == account_id]
    if len(matched) != 1:
        raise ValueError(f"Context Panel {provider} choice matches no single account row")
    configured = [a for a in config["accounts"] if a["provider"] == provider
                  and matching_rows(a, rows) == matched]
    if not configured:
        raise UnconfiguredChoice(f"Context Panel {provider} choice matches no single configured account")
    if len(configured) != 1:
        raise ValueError(f"Context Panel {provider} choice matches no single configured account")
    row = matched[0]
    return decision(configured[0], source, reason, account_id=account_id,
                    remaining=row.get("remainingFraction"), **window_report(row), **snapshot_notices(snapshot))


REPORTED_PERIODS = {"fiveHour": "five_hour", "weekly": "weekly"}


def number_or_none(value):
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


# https://github.com/cbusillo/codex-skills/issues/2478
def window_report(row):
    label = row.get("limitingWindowLabel")
    windows = row.get("windows")
    windows = [w for w in windows if isinstance(w, dict)] if isinstance(windows, list) else []
    capacity = dict.fromkeys(REPORTED_PERIODS.values())
    if any("period" in w for w in windows):
        capacity = {key: [] for key in REPORTED_PERIODS.values()}
        for window in windows:
            period = window.get("period")
            key = REPORTED_PERIODS.get(period) if isinstance(period, str) else None
            if key is not None:
                capacity[key].append(period_window(window))
    return {"limiting_window_label": label if isinstance(label, str) and label else None,
            "window_capacity": capacity}


def period_window(window):
    burn = window.get("burn")
    rate = number_or_none(burn.get("unitsPerHour")) if isinstance(burn, dict) else None
    samples = burn.get("sampleCount") if isinstance(burn, dict) else None
    reset = window.get("naturalResetAt")
    return {"label": window.get("label") if isinstance(window.get("label"), str) else None,
            "remaining_fraction": number_or_none(window.get("remainingFraction")),
            "natural_reset_at": reset if isinstance(reset, str) else None,
            "burn_units_per_hour": rate,
            "burn_sample_count": samples if isinstance(samples, int) and not isinstance(samples, bool) else None}


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
    if provider == "openai":
        if __package__:
            from skills.supervisor.scripts import codex_auth_health
        else:
            import codex_auth_health
    now = now or datetime.now(timezone.utc)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("launch count must be a positive integer")
    accounts = [a for a in config["accounts"] if a["provider"] == provider]
    if not accounts:
        raise ValueError(f"no configured {provider} accounts")
    if snapshot is None:
        raise ValueError(f"Context Panel snapshot unavailable ({unavailable_reason}); no account chosen")
    if provider == "google":
        rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == provider]
        if len(accounts) != 1 or len(rows) != 1:
            raise ValueError("google requires one configured account and one Context Panel row; "
                             "AGY has no account override")
        if name is not None:
            raise ValueError("google account choice must follow Context Panel; --account is unsupported")
    if name is not None:
        named = [a for a in accounts if a["name"] == name]
        if len(named) != 1:
            raise ValueError(f"no configured {provider} account named {name}")
        rows = [r for r in snapshot["accounts"] if isinstance(r, dict) and r.get("provider") == provider]
        matches = matching_rows(named[0], rows)
        if len(matches) != 1 or not matches[0].get("id"):
            raise ValueError("named account needs one Context Panel row for its launch receipt")
        if provider == "openai" and named[0]["env"].get("CODEX_HOME"):
            codex_auth_health.require_usable(Path(named[0]["env"]["CODEX_HOME"]))
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
    if ids and len(ids) != count:
        raise ValueError(f"Context Panel {provider} launch order covers only {len(ids)} of {count} launches; use a smaller batch")
    if provider == "openai" and ids:
        published = order if count > 1 or stale else [*ids, *order]
        available = []
        checked = {}
        skipped_failure = False
        for ident in published:
            try:
                choice = resolve_choice(provider, config, snapshot, ident, source, reason)
            except UnconfiguredChoice:
                if skipped_failure:
                    continue
                raise
            if choice["name"] not in checked:
                home = choice["env"].get("CODEX_HOME")
                checked[choice["name"]] = isinstance(home, str) and bool(codex_auth_health.observe(Path(home)).get("blocked"))
            if not checked[choice["name"]]:
                if skipped_failure:
                    choice["source"] = "context-panel-auth-fallback"
                    choice["reason"] = "Context Panel published order after skipping a failed sign-in"
                available.append(choice)
            else:
                skipped_failure = True
            if len(available) == count:
                return available
        if len(available) < count:
            raise ValueError("Context Panel has too few usable Codex accounts for this batch; "
                             "an account needs sign-in. Use a smaller batch or sign in again")
    if not ids:
        raise ValueError(f"Context Panel has no {provider} choice; next capacity at "
                         f"{ranking.get('nextCapacityAt') or 'unknown'}")
    return [resolve_choice(provider, config, snapshot, i, source, reason) for i in ids]


def choose(provider, config, snapshot, unavailable_reason, now=None, name=None):
    return choose_batch(provider, config, snapshot, unavailable_reason, now=now, name=name)[0]


def decision(account, source, reason, account_id=None, remaining=None, limiting_window_label=None,
             window_capacity=None, configuration_errors=(), reset_prompts=()):
    binding = {"account_binding": GOOGLE_ACCOUNT_BINDING} if account["provider"] == "google" else {}
    return {**binding, "name": account["name"], "provider": account["provider"], "source": source,
            "reason": reason, "account_id": account_id, "remaining_fraction": remaining,
            "limiting_window_label": limiting_window_label,
            "window_capacity": window_capacity or dict.fromkeys(REPORTED_PERIODS.values()),
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


def receipt_route(command):
    """Where this launch's receipt goes, from the reader's snapshot command.

    An explicit --storage-root is written directly; Context Panel's writer takes no root override.
    The App Group default always goes through the reader's own --record-launch, because macOS refuses
    that store to processes with no app behind them, such as launchd tmux workers (codex-skills#2069),
    and the app reaches it from any process without a new grant.
    """
    root = storage_root(command)
    if command and "--storage-root" in command:
        return {"storage_root": root}
    if not command:
        raise ValueError("no snapshot_command configured; no launch receipt writer")
    return {"storage_root": root, "receipt_app": str(Path(command[0]).expanduser())}


def select_batch(provider, config_path=None, name=None, count=1):
    config = load_config(config_path)
    snapshot, problem = read_snapshot(config["snapshot_command"])
    choices = choose_batch(provider, config, snapshot, problem, count=count, name=name)
    route = receipt_route(config["snapshot_command"])
    return [{**choice, **route} for choice in choices]


def select(provider, config_path=None, name=None):
    return select_batch(provider, config_path, name)[0]


def prepare_launch(choice):
    """Detect storage failures before making tabs, without creating a receipt."""
    app = choice.get("receipt_app")
    if app is not None:
        if not (Path(app).is_file() and os.access(app, os.X_OK)):
            raise ValueError("cannot prepare launch receipts: the snapshot command's Context Panel executable is "
                             "not installed, and launch receipts for its App Group store are written through its "
                             "--record-launch mode (Context Panel 1.0.75 or later); install it before retrying")
        return
    try:
        directory = Path(choice["storage_root"]) / "Launch Receipts"
        directory.mkdir(mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".supervisor-probe-", suffix=".tmp") as probe:
            probe.write(b" ")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as error:
        if error.errno == errno.EPERM:
            # Only an explicit --storage-root is written directly. EPERM there is macOS
            # app-data protection or a file flag such as uchg, so name both.
            raise ValueError("cannot prepare launch receipt storage (PermissionError: EPERM) at the snapshot "
                             "command's --storage-root; macOS may refuse this process that directory, and "
                             "Context Panel's --record-launch writes only its App Group store. Remove "
                             "--storage-root to record through the app, or check write access and file flags "
                             "on that directory before retrying") from error
        raise ValueError(f"cannot prepare launch receipt storage ({type(error).__name__}); "
                         "check write access to the reader's storage root before retrying") from error


def record_launch(choice, now=None, runner=None):
    """Write immediately before submitting a launch; retain on an uncertain submit."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    provider, account_id = choice["provider"], choice.get("account_id")
    if not isinstance(account_id, str) or not re.fullmatch(rf"{provider}-[0-9a-f]{{1,64}}", account_id):
        raise ValueError("launch receipt needs the snapshot's opaque account ID")
    receipt = {"schemaVersion": 1, "provider": provider,
               "accountID": account_id, "launchedAt": now.isoformat(timespec="seconds").replace("+00:00", "Z")}
    directory = Path(choice["storage_root"]) / "Launch Receipts"
    if choice.get("receipt_app") is not None:
        target = directory / record_through_app(choice["receipt_app"], receipt, runner)
        if "gui" in choice:
            write_gui_receipt(directory.parent, target.name, receipt, choice["gui"], now)
        return target
    # A bad root must not create a replacement Context Panel store.
    directory.mkdir(mode=0o700, exist_ok=True)
    prune_receipts(directory, now)
    target = directory / f"{now:%Y%m%dT%H%M%SZ}-{LAUNCHER}-{uuid.uuid4().hex}.json"
    if "gui" in choice:
        write_gui_receipt(directory.parent, target.name, receipt, choice["gui"], now)
    write_launch_receipt(target, receipt)
    return target


def write_gui_receipt(root, name, receipt, gui, now):
    """The private GUI receipt is written directly; GUI launches run from the Supervisor's terminal."""
    gui_directory = root / "Supervisor GUI Receipts"
    gui_directory.mkdir(mode=0o700, exist_ok=True)
    prune_receipts(gui_directory, now, max_bytes=16 * 1024)
    write_launch_receipt(gui_directory / name, {**receipt, "gui": gui})


def record_through_app(app, receipt, runner=None):
    """Hand one receipt to Context Panel's --record-launch on stdin; return the file name it wrote."""
    runner = runner or subprocess.run
    try:
        completed = runner([app, "--record-launch", "--launcher", LAUNCHER],
                           input=json.dumps(receipt, separators=(",", ":")), capture_output=True, text=True,
                           timeout=RECORD_LAUNCH_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"Context Panel --record-launch did not finish within {RECORD_LAUNCH_TIMEOUT_SECONDS} "
                         "seconds, so the receipt is unconfirmed and nothing was launched; Context Panel before "
                         "1.0.75 has no --record-launch mode") from error
    except OSError as error:
        raise ValueError(f"Context Panel --record-launch could not run ({type(error).__name__}); "
                         "no launch receipt was written and nothing was launched") from error
    reason = (completed.stderr or "").strip().splitlines()
    detail = f": {reason[0][:200]}" if reason else ""
    if completed.returncode == 64:
        raise ValueError(f"Context Panel refused the launch receipt as invalid (exit 64{detail}); "
                         "nothing was launched")
    if completed.returncode == 1:
        raise ValueError(f"Context Panel could not store the launch receipt (exit 1{detail}); "
                         "nothing was launched")
    if completed.returncode != 0:
        raise ValueError(f"Context Panel --record-launch exited {completed.returncode}{detail}; "
                         "the receipt is unconfirmed and nothing was launched")
    name = completed.stdout.strip()
    if not APP_RECEIPT.fullmatch(name):
        raise ValueError("Context Panel --record-launch did not name the receipt it wrote; "
                         "the receipt is unconfirmed and nothing was launched")
    return name


def write_launch_receipt(target, receipt):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".supervisor-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(receipt, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prune_receipts(directory, now, *, max_bytes=1024):
    """Delete only this launcher's day-old files; preserve other launchers and symlinks."""
    for path in directory.iterdir():
        if not OWN_RECEIPT.fullmatch(path.name) or path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            launched = parse_time(data.get("launchedAt")) if isinstance(data, dict) else None
            if launched and now - launched >= timedelta(days=1):
                path.unlink()
        except (OSError, ValueError):
            continue


def public(choice):
    """Launch output contains environment names, never private paths."""
    result = {**{k: v for k, v in choice.items() if k not in {"env", "storage_root", "receipt_app", "gui", "tools", "require_browser", "tool_directory"}},
              "env_keys": sorted(choice["env"])}
    if "tools" in choice:
        tools = choice["tools"]
        result["tools"] = {key: tools[key] for key in ("warnings", "browsers", "route") if key in tools}
        result["tools"]["warnings"] = [
            warning.replace(os.path.expanduser(choice["env"].get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")), "<account-home>")
            .replace(os.path.expanduser(choice["env"].get("CODEX_HOME") or str(Path.home() / ".codex")), "<account-home>")
            for warning in tools.get("warnings", [])]
    if "gui" in choice:
        gui = choice["gui"]
        result["gui"] = {key: gui[key] for key in ("app", "token", "phase", "screen")}
        result["gui"]["marketplace_link"] = gui["marketplace"]["link"]
    return result


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
