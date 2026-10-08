#!/usr/bin/env -S uv run --quiet --no-python-downloads --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Opt-in Claude account moves; Context Panel remains the only account chooser."""

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import account_choice

MOVE_DIR = "CLAUDE_ACCOUNT_MOVE_DIR"


def session_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def config_home(env):
    return Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve()


def state_root(env, *, create=False):
    value = env.get(MOVE_DIR)
    if not value:
        return None
    root = Path(value).expanduser()
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("move directory must be an absolute private directory")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.exists():
        return root
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("move directory must be owned by this user with mode 700")
    return root


def read_request(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("invalid move request file")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != 1 or session_id(data.get("session_id")) != path.stem:
        raise ValueError("invalid move request")
    return data


def write_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def history_matches(data, home):
    transcript = Path(data["transcript_path"])
    projects = home / "projects"
    return (projects.is_dir() and transcript.is_file()
            and transcript.name == data["session_id"] + ".jsonl"
            and transcript.resolve().is_relative_to(projects.resolve()))


def hook(payload, env):
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    if event not in ("StopFailure", "SessionStart"):
        return
    if event == "StopFailure" and payload.get("error") != "rate_limit":
        return
    identifier = session_id(payload.get("session_id"))
    if not identifier or not env.get(MOVE_DIR):
        return
    root = state_root(env, create=True)
    path = root / (identifier + ".json")
    home = config_home(env)
    with (root / (identifier + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if event == "SessionStart":
            if payload.get("source") != "resume" or not path.exists():
                return
            data = read_request(path)
            if (data.get("target_home") == str(home)
                    and payload.get("cwd") == data.get("cwd")
                    and Path(payload.get("transcript_path", "")).resolve() == Path(data["transcript_path"]).resolve()):
                write_json(root / (identifier + ".resumed.json"),
                           {"session_id": identifier, "account": data["account"], "state": "resumed"})
                path.unlink()
            return
        data = {"schema": 1, "session_id": identifier, "cwd": payload.get("cwd"),
                "transcript_path": payload.get("transcript_path"), "source_home": str(home)}
        if (not isinstance(data["cwd"], str) or not isinstance(data["transcript_path"], str)
                or not Path(data["cwd"]).is_absolute() or not history_matches(data, home)):
            return
        # A repeated failure records a new move, including one after an earlier move.
        write_json(path, data)
        (root / (identifier + ".resumed.json")).unlink(missing_ok=True)


def resumed_session(argv):
    identifiers = []
    for index, argument in enumerate(argv[1:], 1):
        if argument == "--":
            break
        if argument in ("--resume", "-r"):
            identifiers.append(session_id(argv[index + 1]) if index + 1 < len(argv) else None)
        elif argument.startswith("--resume="):
            identifiers.append(session_id(argument.split("=", 1)[1]))
    return identifiers[0] if len(identifiers) == 1 else None


def bounded_snapshot(*args, **kwargs):
    kwargs["timeout"] = 1.5
    return subprocess.run(*args, **kwargs)


def move_environment(argv, env):
    """Ordinary self-spawns neither read private account config nor query Context Panel."""
    identifier = resumed_session(argv)
    if not identifier or not env.get(MOVE_DIR):
        return env
    root = state_root(env)
    path = root / (identifier + ".json")
    if not path.exists():
        return env
    with (root / (identifier + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not path.exists():
            return env
        data = read_request(path)
        try:
            if any(env.get(key) for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")):
                raise ValueError("authentication override is present; account move cannot select a home login")
            if data["cwd"] != str(Path.cwd()) or not history_matches(data, config_home(env)):
                raise ValueError("current working directory or source history differs")
            if not data.get("target_home"):
                config = account_choice.load_config(env=env)
                snapshot, problem = account_choice.read_snapshot(config["snapshot_command"], runner=bounded_snapshot)
                choice = account_choice.choose("anthropic", config, snapshot, problem)
                if choice["source"] != "context-panel":
                    raise ValueError("move requires Context Panel useNext from fresh readings")
                target = Path(choice["env"].get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve()
                if target == config_home(env):
                    raise ValueError("Context Panel still chooses the current account")
                if (not target.is_dir() or not history_matches(data, target)
                        or (target / "projects").resolve() != (config_home(env) / "projects").resolve()):
                    raise ValueError("next account must already share this session history")
                data.update(target_home=str(target), account=choice["name"])
            target = Path(data["target_home"])
            if not target.is_dir() or not history_matches(data, target):
                raise ValueError("selected account history is unavailable")
            data.pop("problem", None)
            data["state"] = "restarting"
            write_json(path, data)
            # Keep the request until SessionStart confirms resume on the selected home.
            return {**env, "CLAUDE_CONFIG_DIR": str(target)}
        except (ValueError, OSError, KeyError, TypeError) as error:
            data["problem"] = str(error) if isinstance(error, ValueError) else type(error).__name__
            data["state"] = "pending"
            write_json(path, data)
            return env


def wrapper(argv):
    if not argv:
        return 2
    env = dict(os.environ)
    try:
        env = move_environment(argv, env)
    except (ValueError, OSError, KeyError, TypeError):
        pass  # Preserve the request and Claude's ordinary fallback; no terminal output.
    try:
        os.execvpe(argv[0], argv, env)
    except OSError:
        return 127  # The pending request survives a failed exec.


def install(settings, directory, write=False):
    if not settings.is_absolute() or settings.is_symlink():
        raise ValueError("pass the canonical absolute user settings path, not a symlink")
    if not directory.is_absolute():
        raise ValueError("pass an absolute shared move directory")
    data = json.loads(settings.read_text()) if settings.exists() else {}
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise ValueError("user settings and env must be objects")
    wrapper_path = str(Path(__file__).resolve())
    env = data.setdefault("env", {})
    expected = {"CLAUDE_CODE_PROCESS_WRAPPER": json.dumps([wrapper_path, "wrap"]), MOVE_DIR: str(directory)}
    for key, value in expected.items():
        if key in env and env[key] != value:
            raise ValueError("existing account-move or process wrapper setting preserved; reconcile explicitly")
    env.update(expected)
    groups = data.setdefault("hooks", {})
    if not isinstance(groups, dict):
        raise ValueError("hooks must be an object")
    command = shlex.join([wrapper_path, "hook"])
    for event, matcher in (("StopFailure", "rate_limit"), ("SessionStart", "resume")):
        entries = groups.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("hook entries must be lists")
        entry = {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 3}]}
        if entry not in entries:
            entries.append(entry)
    if write:
        settings.parent.mkdir(parents=True, exist_ok=True)
        write_json(settings, data)
    return data


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "wrap":
        return wrapper(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    sub.add_parser("status")
    setup = sub.add_parser("install")
    setup.add_argument("--settings", type=Path, required=True)
    setup.add_argument("--move-dir", type=Path, required=True)
    setup.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "hook":
            hook(json.load(sys.stdin), os.environ)
        elif args.command == "install":
            data = install(args.settings, args.move_dir, args.write)
            print(json.dumps({"written": args.write, "env": {key: data["env"][key] for key in
                             ("CLAUDE_CODE_PROCESS_WRAPPER", MOVE_DIR)},
                             "hook_events": ["StopFailure", "SessionStart"]}, indent=2))
        else:
            root = state_root(os.environ)
            print(json.dumps([json.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root else [], indent=2))
    except (ValueError, OSError, KeyError, TypeError) as error:
        if args.command != "hook":
            parser.exit(1, f"refused: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
