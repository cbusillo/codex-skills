#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import services
from check_changes import save_state
from session_liveness import generation_ended

SCRIPTS = Path(__file__).resolve().parent
CATALOG = SCRIPTS.parents[2]
START_TIMEOUT = 900


class LaunchCommandError(ValueError):
    def __init__(self, message: str, payload: dict) -> None:
        super().__init__(message)
        self.not_started = payload.get("mutation_started") is False and not payload.get("launched")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Start a managed Codex or Claude Supervisor and its existing message reader.")
    subcommands = command.add_subparsers(dest="command", required=True)
    start_command = subcommands.add_parser("start")
    start_command.add_argument("--owner", required=True)
    start_command.add_argument("--harness", choices=("codex", "claude"), required=True)
    start_command.add_argument("--miss-issue", required=True, help="existing internal repair issue, OWNER/REPO#NUMBER")
    start_command.add_argument("--directory", type=Path, default=Path.cwd())
    start_command.add_argument("--state-dir", type=Path)
    start_command.add_argument("--ledger", type=Path)
    start_command.add_argument("--foreign-state", type=Path)
    start_command.add_argument("--services-config", type=Path)
    start_command.add_argument("--watch-args-file", type=Path, help="existing watcher flags to preserve during adoption")
    start_command.add_argument("--recovery", type=Path, help="existing Supervisor recovery note")
    start_command.add_argument("--queue", help="existing Director question queue, OWNER/REPO#NUMBER")
    start_command.add_argument("--events", action="store_true", help="use the existing optional Launchplane attention subscription")
    start_command.add_argument("--tmux-socket", default="supervisor")
    start_command.add_argument("--account")
    start_command.add_argument("--account-config", type=Path)
    start_command.add_argument("--takeover", action="store_true", help="launch a successor after the current Supervisor's handoff")
    start_command.add_argument("--recover", action="store_true", help="finish the retained native launch; never relaunch")
    return command


def run_json(argv: list[str], *, receipt: Path | None = None) -> dict:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=START_TIMEOUT)
    except subprocess.TimeoutExpired as error:
        if receipt:
            output = error.stdout or b""
            receipt.write_bytes(output.encode() if isinstance(output, str) else output)
        raise
    if receipt:
        receipt.write_text(result.stdout)
        receipt.chmod(0o600)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = {}
    if result.returncode:
        raise LaunchCommandError(
            f"startup command failed; inspect its retained receipt: {result.stdout} {result.stderr}",
            payload if isinstance(payload, dict) else {},
        )
    if not isinstance(payload, dict) or not payload:
        raise ValueError("startup command returned no single receipt; reconcile before retry")
    return payload


def option_value(argv: list[str], option: str) -> str | None:
    values = [
        value.split("=", 1)[1] if value.startswith(option + "=") else argv[index + 1] if index + 1 < len(argv) else None
        for index, value in enumerate(argv)
        if value == option or value.startswith(option + "=")
    ]
    if len(values) > 1 or None in values:
        raise ValueError(f"ambiguous watcher argument: {option}")
    return values[0] if values else None


def configure(args: argparse.Namespace, root: Path, config_path: Path) -> tuple[Path, Path]:
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with services.supervisor_lock(config_path):
        return configure_locked(args, root, config_path)


def configure_locked(args: argparse.Namespace, root: Path, config_path: Path) -> tuple[Path, Path]:
    import github_identity
    from session_record import load_ledger

    uv = shutil.which("uv")
    if not uv:
        raise ValueError("uv is required")
    config = services.load_config(config_path) if config_path.exists() else {"uv": uv, "services": {}}
    previous = config["services"].get("foreign")
    argv = list(previous["argv"]) if previous else [uv, "run", "--no-cache", str(SCRIPTS / "foreign_watch.py")]
    if args.watch_args_file:
        flags = shlex.split(args.watch_args_file.read_text())
        if previous:
            index = next(index for index, value in enumerate(argv) if Path(value).name == "foreign_watch.py") + 1
            if argv[index : index + len(flags)] != flags:
                raise ValueError("existing foreign service differs from the supplied adoption flags")
        else:
            argv.extend(flags)
    ledger = (args.ledger or Path(option_value(argv, "--ledger") or root / "workers.json")).resolve()
    state = (args.foreign_state or Path(option_value(argv, "--state") or root / "foreign.json")).resolve()
    for option, value in (
        ("--owner", args.owner),
        ("--state", str(state)),
        ("--ledger", str(ledger)),
        ("--notify-config", str(config_path)),
        ("--miss-issue", args.miss_issue),
    ):
        existing = option_value(argv, option)
        same = (
            Path(existing).resolve() == Path(value).resolve()
            if existing is not None and option in {"--state", "--ledger", "--notify-config"}
            else existing is not None and existing.casefold() == value.casefold()
        )
        if existing is not None and not same:
            raise ValueError(f"existing {option} differs; choose matching startup arguments rather than changing custody")
        if existing is None:
            argv.extend([option, value])
    if not any(value == "--own-login" or value.startswith("--own-login=") for value in argv):
        for login in filter(None, [args.owner, github_identity.automation_login(), *github_identity.configured_bot_logins()]):
            argv.extend(["--own-login", login])
    if "--persistent" not in argv:
        argv.append("--persistent")
    if args.events and "--events" not in argv:
        argv.append("--events")
    if previous and previous["argv"] != argv:
        with services.service_lock(config_path, "foreign"):
            pass
    config["services"]["foreign"] = {**(previous or {"cwd": str(CATALOG), "restart_seconds": 60}), "argv": argv}
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not ledger.exists():
        with ledger.open("x") as stream:
            stream.write("[]\n")
        ledger.chmod(0o600)
    load_ledger(ledger)
    save_state(config_path, config)
    return ledger, state


def launch_supervisor(args: argparse.Namespace, root: Path, config_path: Path, ledger: Path, state: Path) -> dict:
    attempt = root / "starts" / uuid.uuid4().hex
    attempt.mkdir(parents=True, mode=0o700)
    registration = attempt / "launch.json"
    journal = json.loads((root / "start.json").read_text())
    journal["attempt"] = str(attempt)
    save_state(root / "start.json", journal)
    save_state(registration, [])
    command = attempt / "command.txt"
    command.write_text(
        f"cd {shlex.quote(str(args.directory.resolve()))} && exec "
        + ("codex --remote unix://" if args.harness == "codex" else "claude")
        + "\n"
    )
    recovery = (
        f"Read the existing recovery note at {args.recovery.resolve()}."
        if args.recovery
        else "Read current repository direction and the work queue."
    )
    routine = (
        [
            shutil.which("uv") or "uv",
            "run",
            str(SCRIPTS / "routine_check.py"),
            "--owner",
            args.owner,
            "--queue",
            args.queue,
            "--ledger",
            str(ledger),
            "--foreign-state",
            str(state),
            "--notify-config",
            str(config_path),
            "--miss-issue",
            args.miss_issue,
            "--foreign-log",
            str(root / "jobs/logs/foreign.jsonl"),
        ]
        if args.queue
        else []
    )
    brief = attempt / "brief.txt"
    brief.write_text(
        "You are the Director's managed Supervisor. Read the supervisor skill in full before supervising.\n"
        f"Catalog: {CATALOG}. Owner: {args.owner}. Worker ledger: {ledger}. Foreign state: {state}. "
        f"Notification configuration: {config_path}. {recovery}\n"
        + (
            f"Use the harness's scheduled-check feature about every 23 minutes with this foreground digest: {shlex.join(routine)}.\n"
            if routine
            else "Read any configured Director question queue; do not invent one.\n"
        )
        + "The start command binds this actual session and runs the foreign service outside your conversation. "
        "Keep yourself out of the worker ledger. Reuse that service; do not start another watcher. "
        "On Claude, leave an idle empty prompt without ordinary background tasks between scheduled checks. "
        "Follow the Director's stop boundaries and do not change access, credentials or personal settings.\n"
        f"Rules for this brief: {SCRIPTS.parent / 'references/brief-rules.md'}\n"
    )
    base = [shutil.which("uv") or "uv", "run", str(SCRIPTS / "iterm_tab.py"), "--backend", "tmux", "--tmux-socket", args.tmux_socket]
    try:
        container = run_json([*base, "window"], receipt=attempt / "window.json")
    except (ValueError, OSError, subprocess.SubprocessError):
        journal["status"] = "not_started"
        save_state(root / "start.json", journal)
        raise
    argv = [
        *base,
        "new",
        "--window-id",
        container["window_id"],
        "--command-file",
        str(command),
        "--brief-file",
        str(brief),
        "--ledger",
        str(registration),
        "--account-provider",
        {"codex": "openai", "claude": "anthropic"}[args.harness],
        "--without-browser",
    ]
    if args.account:
        argv.extend(["--account", args.account])
    if args.account_config:
        argv.extend(["--account-config", str(args.account_config)])
    try:
        run_json(argv, receipt=attempt / "receipt.json")
    except LaunchCommandError as error:
        if error.not_started:
            journal["status"] = "not_started"
            save_state(root / "start.json", journal)
        raise
    return recover_supervisor(root, config_path, ledger)


def recover_supervisor(root: Path, config_path: Path, ledger: Path) -> dict:
    journal = json.loads((root / "start.json").read_text())
    attempt = Path(journal["attempt"])
    receipt_path = attempt / "registered.json" if (attempt / "registered.json").exists() else attempt / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    registration = attempt / "launch.json"
    if "native_session_id" not in receipt:
        if not isinstance(receipt.get("failed_launch", {}).get("launch"), dict):
            raise ValueError("retained launcher receipt has no registered source; inspect its exact terminal before recovery")
        receipt = run_json(
            [
                shutil.which("uv") or "uv",
                "run",
                str(SCRIPTS / "iterm_tab.py"),
                "register",
                "--receipt-file",
                str(attempt / "receipt.json"),
                "--ledger",
                str(registration),
            ],
            receipt=attempt / "registered.json",
        )
    if not registration.exists():
        recipient = verify_existing_supervisor(config_path)
        if recipient["session_id"] != receipt["native_session_id"]:
            raise ValueError("retained startup binding differs; reconcile its original native session")
        return recipient
    entries = json.loads(registration.read_text())
    if (
        len(entries) != 1
        or entries[0]["session_id"] != receipt["native_session_id"]
        or entries[0]["session_id"] in {row["session_id"] for row in json.loads(ledger.read_text())}
    ):
        raise ValueError("retained Supervisor registration is incomplete or appears in the worker ledger")
    recipient = services.bind_launched_supervisor(config_path, entries[0])
    registration.unlink()
    return recipient


def verify_existing_supervisor(config_path: Path) -> dict:
    return services.verify_managed_supervisor(services.supervisor_recipient(config_path))


def prepare_foreign_service(root: Path, config_path: Path) -> dict:
    host = services.prepare_service_host(config_path, CATALOG, root / "jobs", "foreign")
    if services.reconciliation_marker(config_path, "foreign").exists() and not services.service_host_active(host):
        raise ValueError("foreign service requires reconciliation; inspect its logs and use services.py clear before retrying")
    return host


def ensure_foreign_service(root: Path, config_path: Path) -> dict:
    return services.ensure_service_host(prepare_foreign_service(root, config_path), config_path, "foreign")


def start(args: argparse.Namespace) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.owner) or not re.fullmatch(
        rf"{re.escape(args.owner)}/[A-Za-z0-9_.-]+#[1-9][0-9]*", args.miss_issue, re.I
    ):
        raise ValueError("--miss-issue must identify the Director's existing repair issue")
    root = (
        (args.state_dir or Path(os.environ.get("CODE_HOME") or Path.home() / ".code") / "skill-data/supervisor" / args.owner)
        .expanduser()
        .resolve()
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = (args.services_config or root / "services.json").resolve()
    with services.service_lock(root / "start.json", "start"):
        path = root / "start.json"
        previous = json.loads(path.read_text()) if path.exists() else {}
        if args.recover and (not previous or args.takeover):
            raise ValueError("--recover requires a retained startup and cannot launch a takeover")
        if config_path.exists() and services.load_config(config_path).get("supervisor") and not previous and not args.takeover:
            raise ValueError("an existing Supervisor is bound; use --takeover only after its handoff")
        if previous and previous.get("status") not in {"running", "not_started"} and not args.recover:
            raise ValueError(f"startup requires reconciliation; inspect {path}; --recover never replays a launch")
        if previous.get("status") == "running" and not args.takeover and not args.recover:
            try:
                recipient = verify_existing_supervisor(config_path)
            except (OSError, ValueError, subprocess.SubprocessError):
                if not generation_ended(services.supervisor_recipient(config_path)):
                    raise
            else:
                return {"status": "already_running", "supervisor": recipient, "service": ensure_foreign_service(root, config_path)}
        if services.reconciliation_marker(config_path, "foreign").exists() and not config_path.exists():
            raise ValueError("foreign service requires reconciliation before launching a Supervisor")
        ledger, state = configure(args, root, config_path)
        prepare_foreign_service(root, config_path)
        journal = previous if args.recover else {"status": "launching"}
        save_state(path, journal)
        recipient = (
            recover_supervisor(root, config_path, ledger)
            if args.recover
            else launch_supervisor(args, root, config_path, ledger, state)
        )
        journal = json.loads(path.read_text())
        journal["supervisor"] = recipient["session_id"]
        save_state(path, journal)
        service = ensure_foreign_service(root, config_path)
        journal["status"] = "running"
        save_state(path, journal)
        return {
            "status": "running",
            "supervisor": recipient,
            "service": service,
            "ledger": str(ledger),
            "state": str(state),
            "services_config": str(config_path),
            "events": "--events" in services.load_config(config_path)["services"]["foreign"]["argv"],
        }


def main() -> int:
    args = parser().parse_args()
    try:
        print(json.dumps(start(args)))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        status = "not_started" if isinstance(error, LaunchCommandError) and error.not_started else "reconciliation_required"
        print(json.dumps({"status": status, "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
