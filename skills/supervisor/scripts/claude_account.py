#!/usr/bin/env -S uv run --quiet --no-python-downloads --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Opt-in Claude account moves; Context Panel remains the only account chooser."""

import argparse
import fcntl
import json
import importlib.util
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

MOVE_DIR = "CLAUDE_ACCOUNT_MOVE_DIR"
ENABLED = "CLAUDE_ACCOUNT_MOVE_ENABLED"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reader():
    return load_module("account_choice", Path(__file__).with_name("account_choice.py"))


def runtime_script():
    script = Path(__file__).resolve()
    catalog = script.parents[3]
    runtime = load_module("runtime_binding", catalog / "skills/github/scripts/reconcile-runtime-checkout.py")
    if not any(path.resolve() in (catalog, catalog / "skills") for path, _, _ in runtime.runtime_skills_paths()):
        raise ValueError("run enrollment from the active catalog runtime binding")
    def git(*args):
        result = subprocess.run(["git", "-C", str(catalog), *args], capture_output=True, text=True, check=False, timeout=5)
        if result.returncode:
            raise ValueError("runtime must have a known default branch and clean checkout")
        return result.stdout.strip()
    branch = git("symbolic-ref", "--short", "HEAD")
    default = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
    if branch != default or git("status", "--porcelain"):
        raise ValueError("enroll only from the clean default-branch runtime checkout")
    return script


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


def write_json(path, data, *, expected=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        if expected is not None and (not path.exists() or path.read_bytes() != expected):
            raise ValueError("settings changed during enrollment; preview again")
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
    if not identifier or not env.get(MOVE_DIR) or env.get(ENABLED) != "1":
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
                    and Path(payload.get("cwd", "")).resolve() == Path(data["cwd"]).resolve()
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
    if "--fork-session" in argv:
        return None
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
    if not identifier or not env.get(MOVE_DIR) or env.get(ENABLED) != "1":
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
            if any(env.get(key) for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
                                                  "ANTHROPIC_BASE_URL", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                                                  "CLAUDE_CODE_HOST_AUTH_ENV_VAR", "CLAUDE_CODE_HOST_CREDS_FILE",
                                                  "CLAUDE_CODE_USE_BEDROCK",
                                                  "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
                raise ValueError("authentication override is present; account move cannot select a home login")
            settings = config_home(env) / "settings.json"
            if not settings.is_file():
                raise ValueError("move requires shared user settings")
            user_settings = json.loads(settings.read_text())
            if not isinstance(user_settings, dict) or user_settings.get("apiKeyHelper"):
                raise ValueError("move requires shared user settings without apiKeyHelper")
            if Path(data["cwd"]).resolve() != Path.cwd() or not history_matches(data, config_home(env)):
                raise ValueError("current working directory or source history differs")
            if not data.get("target_home"):
                account_choice = reader()
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
                if set(choice["env"]) - {"CLAUDE_CONFIG_DIR"}:
                    raise ValueError("account move supports only the account home environment")
                data.update(target_home=str(target), account=choice["name"],
                            target_default="CLAUDE_CONFIG_DIR" not in choice["env"])
            target = Path(data["target_home"])
            if not (target / "settings.json").is_file() or (target / "settings.json").resolve() != settings.resolve():
                raise ValueError("selected account must share the enrolled settings file")
            if not target.is_dir() or not history_matches(data, target):
                raise ValueError("selected account history is unavailable")
            data.pop("problem", None)
            data["state"] = "restarting"
            write_json(path, data)
            # Keep the request until SessionStart confirms resume on the selected home.
            moved = dict(env)
            if data.get("target_default"):
                moved.pop("CLAUDE_CONFIG_DIR", None)
            else:
                moved["CLAUDE_CONFIG_DIR"] = str(target)
            return moved
        except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
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
    except Exception:
        pass  # Preserve the request and Claude's ordinary fallback; no terminal output.
    try:
        os.execvpe(argv[0], argv, env)
    except OSError:
        return 127  # The pending request survives a failed exec.


def own_wrapper(value, script, action):
    if not isinstance(value, str):
        return False
    try:
        parts = json.loads(value) if action == "wrap" else shlex.split(value)
    except (ValueError, TypeError):
        return False
    return (isinstance(parts, list) and len(parts) == 4 and parts[1:] == ["-I", script, action])


def install(settings, directory, write=False, refresh=False):
    if not settings.is_absolute() or settings.is_symlink():
        raise ValueError("pass the canonical absolute user settings path, not a symlink")
    if not directory.is_absolute():
        raise ValueError("pass an absolute shared move directory")
    previous = settings.read_bytes() if settings.exists() else None
    data = json.loads(previous) if previous is not None else {}
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise ValueError("user settings and env must be objects")
    wrapper_path = str(runtime_script())
    python = str(Path(sys._base_executable).resolve())
    if not os.access(python, os.X_OK):
        raise ValueError("base Python interpreter is not executable")
    if directory.exists():
        state_root({MOVE_DIR: str(directory)})
    env = data.setdefault("env", {})
    expected = {"CLAUDE_CODE_PROCESS_WRAPPER": json.dumps([python, "-I", wrapper_path, "wrap"]), MOVE_DIR: str(directory)}
    for key, value in expected.items():
        if (key in env and env[key] != value
                and not (refresh and key == "CLAUDE_CODE_PROCESS_WRAPPER"
                         and own_wrapper(env[key], wrapper_path, "wrap"))):
            raise ValueError("existing account-move or process wrapper setting preserved; reconcile explicitly")
    env.update(expected)
    groups = data.setdefault("hooks", {})
    if not isinstance(groups, dict):
        raise ValueError("hooks must be an object")
    command = shlex.join([python, "-I", wrapper_path, "hook"])
    for event, matcher in (("StopFailure", "rate_limit"), ("SessionStart", "resume")):
        entries = groups.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("hook entries must be lists")
        entry = {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": 3}]}
        if refresh:
            managed_groups = []
            for existing in entries:
                if isinstance(existing, dict) and isinstance(existing.get("hooks"), list):
                    before = len(existing["hooks"])
                    existing["hooks"] = [handler for handler in existing["hooks"] if not (
                        isinstance(handler, dict) and own_wrapper(handler.get("command"), wrapper_path, "hook"))]
                    if before and not existing["hooks"]:
                        managed_groups.append(existing)
            entries[:] = [existing for existing in entries if existing not in managed_groups]
        if entry not in entries:
            entries.append(entry)
    if write:
        state_root({MOVE_DIR: str(directory)}, create=True)
        settings.parent.mkdir(parents=True, exist_ok=True)
        write_json(settings, data, expected=previous)
    return data


def uninstall(settings, write=False):
    if not settings.is_absolute() or settings.is_symlink():
        raise ValueError("pass the canonical absolute user settings path")
    previous = settings.read_bytes()
    data = json.loads(previous)
    if not isinstance(data, dict) or not isinstance(data.get("env"), dict) or not isinstance(data.get("hooks"), dict):
        raise ValueError("invalid enrollment settings; preserved")
    script = str(runtime_script())
    env = data.get("env", {})
    if not own_wrapper(env.get("CLAUDE_CODE_PROCESS_WRAPPER"), script, "wrap"):
        raise ValueError("existing wrapper is not this helper's enrollment; preserved")
    env.pop("CLAUDE_CODE_PROCESS_WRAPPER")
    env.pop(MOVE_DIR, None)
    for event in ("StopFailure", "SessionStart"):
        entries = data.get("hooks", {}).get(event, [])
        retained = []
        for entry in entries:
            handlers = entry.get("hooks", [])
            remaining = [h for h in handlers if not own_wrapper(h.get("command"), script, "hook")]
            if remaining or not handlers:
                retained.append({**entry, "hooks": remaining})
        data["hooks"][event] = retained
    if write:
        write_json(settings, data, expected=previous)
    return data


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "wrap":
        return wrapper(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    sub.add_parser("status")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--session-id", required=True)
    setup = sub.add_parser("install")
    setup.add_argument("--settings", type=Path, required=True)
    setup.add_argument("--move-dir", type=Path, required=True)
    setup.add_argument("--write", action="store_true")
    setup.add_argument("--refresh", action="store_true", help="reconcile only this helper's own wrapper and hook interpreter")
    remove = sub.add_parser("uninstall")
    remove.add_argument("--settings", type=Path, required=True)
    remove.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "hook":
            hook(json.load(sys.stdin), os.environ)
        elif args.command == "install":
            data = install(args.settings, args.move_dir, args.write, args.refresh)
            print(json.dumps({"written": args.write, "env": {key: data["env"][key] for key in
                             ("CLAUDE_CODE_PROCESS_WRAPPER", MOVE_DIR)},
                             "hook_events": ["StopFailure", "SessionStart"]}, indent=2))
        elif args.command == "uninstall":
            uninstall(args.settings, args.write)
            print(json.dumps({"written": args.write, "removed": ["CLAUDE_CODE_PROCESS_WRAPPER", MOVE_DIR]}))
        elif args.command == "cancel":
            identifier = session_id(args.session_id)
            if not identifier or not os.environ.get(MOVE_DIR):
                raise ValueError("cancel requires a session UUID and configured move directory")
            root = state_root(os.environ)
            path = root / (identifier + ".json")
            if path.exists():
                with (root / (identifier + ".lock")).open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    read_request(path)
                    path.unlink()
            print(json.dumps({"session_id": identifier, "state": "cancelled"}))
        else:
            if not os.environ.get(MOVE_DIR):
                raise ValueError("move directory is not configured")
            root = state_root(os.environ)
            print(json.dumps([json.loads(p.read_text()) for p in sorted(root.glob("*.json"))] if root else [], indent=2))
    except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
        if args.command != "hook":
            parser.exit(1, f"refused: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
