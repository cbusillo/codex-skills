#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import datetime as dt
import json
import os
import re
import runpy
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

MARKER_NAME = "direction-last-check.json"
MEMORY_MARKER_NAME = "memory-last-check.json"
MEMORY_STALE_DAYS = 14
MEMORY_MAX_WORDS = 30_000
TURN_STALE = dt.timedelta(hours=24)
AUDIT_STALE = dt.timedelta(days=7)
LOOP_PATH = Path(__file__).resolve().parents[1] / "skills" / "references" / "executing-loop.md"
SKILLS_PROTOCOL_PATH = LOOP_PATH.with_name("using-skills.md")
GH_READER = Path(__file__).resolve().parents[1] / "skills" / "github" / "scripts" / "gh-with-env-token"
OVERALL_REPO = "direction"
OVERALL_READ_TIMEOUT = 6
RUNTIME_CATCHUP_TIMEOUT = 5
# Test seam: fixtures under a loaded parallel gate need more than the session budget.
RUNTIME_CATCHUP_TIMEOUT_ENV = "DIRECTION_RUNTIME_CATCHUP_TIMEOUT"
# The shared records: issues in OWNER/direction that direction_mark.py keeps current.
TURN_RECORD_TAG = "<!-- direction-turn-record -->"
TURN_RECORD_LINE = re.compile(r"^Last daily turn: (\S+) covering (\S+)$", re.MULTILINE)
WEEKLY_RECORD_TAG = "<!-- direction-weekly-audit-record -->"
WEEKLY_RECORD_LINE = re.compile(r"^Last weekly audit: (\S+)$", re.MULTILINE)
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Pages of open issues read to find the records; more than that leaves them unconfirmed.
RECORD_PAGES = 10
# The first words of each reminder line, by check and state. Tests match these, not the wording.
REMINDER_LINES = {
    ("turn", "due"): "Direction turn overdue",
    ("turn", "unconfirmed"): "Direction turn unconfirmed",
    ("audit", "due"): "Weekly direction audit overdue",
    ("audit", "unconfirmed"): "Weekly direction audit unconfirmed",
}
RUNTIME_HELPER = Path("skills/github/scripts/reconcile-runtime-checkout.py")


def agent_tools_warning(catalog: Path, directory: Path, env: Mapping[str, str]) -> str:
    """Static session-start check only: never connect a browser or change settings."""
    if not (catalog / ".local/chrome.toml").is_file():
        return ""
    helpers = Path(__file__).resolve().parents[1] / "skills/supervisor/scripts/agent_tools.py"
    try:
        checker = runpy.run_path(str(helpers))
        if env.get("CLAUDECODE") == "1":
            home = Path(env["CLAUDE_CONFIG_DIR"]).expanduser() if env.get("CLAUDE_CONFIG_DIR") else None
            problems = checker["claude_problems"](home, directory, env, check_computer_use=False)
        elif env.get("CODEX_HOME") and env.get(checker["CODEX_SOURCE_ENV"]):
            problems = checker["codex_problems"](Path(env["CODEX_HOME"]).expanduser(), env)
        else:
            return ""
        return "Agent tools setup: " + "; ".join(" ".join(p.split()) for p in problems) if problems else ""
    except Exception:
        return "Agent tools check unavailable; verify catalog-chrome and Computer Use through /mcp."


def runtime_catchup_budget() -> float:
    try:
        return float(os.environ[RUNTIME_CATCHUP_TIMEOUT_ENV])
    except (KeyError, ValueError):
        return RUNTIME_CATCHUP_TIMEOUT


def catch_up_runtime(catalog: Path) -> str:
    """Catch up only the bound catalog, using the reconciler's existing safety gates."""
    helper = catalog / RUNTIME_HELPER
    if not helper.is_file():
        return ""
    try:
        # run_path reads the script without writing __pycache__ into the runtime.
        reconciler = SimpleNamespace(**runpy.run_path(str(helper)))
        deadline = time.monotonic() + runtime_catchup_budget()
        token = reconciler.RECONCILIATION_DEADLINE.set(deadline)
        try:
            roots = []
            for path, _, _ in reconciler.runtime_skills_paths():
                if not path.exists():
                    continue
                try:
                    root = reconciler.git_root(path)
                except (reconciler.GitCommandError, OSError):
                    if time.monotonic() >= deadline:
                        # Report the expired budget rather than an unbound catalog.
                        return f"Catalog catch-up retryable: runtime_catchup_timeout ({catalog})."
                    continue
                if (root / RUNTIME_HELPER).is_file() and root not in roots:
                    roots.append(root)
            if catalog.resolve() not in roots:
                # Only a registered plugin hook may fall back from its cache copy.
                # An unbound development invocation must leave the install alone.
                plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
                if not plugin_root or Path(plugin_root).resolve() != catalog.resolve() or not roots:
                    return ""
                catalog = next((root for root in roots if reconciler.current_branch(root) == reconciler.resolve_default_branch(root)), roots[0])
            blockers = reconciler.runtime_blockers(catalog, reconciler.resolve_default_branch(catalog))
            if blockers:
                return f"Catalog catch-up blocked: {', '.join(blockers)} ({catalog})."
            repo = reconciler.repository_from_remote_url(
                reconciler.git_text(catalog, "config", "--get", "remote.origin.url")
            )
            # Execute the bound checkout's copy, preserving its provenance check.
            runtime = SimpleNamespace(**runpy.run_path(str(catalog / RUNTIME_HELPER)))
            receipt = runtime.reconcile_runtime_checkout(
                catalog, repo, None, timeout_seconds=max(0, deadline - time.monotonic()),
            )
        finally:
            reconciler.RECONCILIATION_DEADLINE.reset(token)
        if receipt["status"] == "already_current":
            return ""
        detail = receipt.get("detail") or receipt["reason_code"]
        return f"Catalog catch-up {receipt['status']}: {detail} ({catalog})."
    except Exception:  # A broken or older helper must not prevent the session from starting.
        return (f"Catalog catch-up unavailable: could not reconcile {catalog}. "
                "See README.md#runtime-binding-lookup for the guarded manual catch-up command.")


def marker_path(env: Mapping[str, str] | None = None) -> Path:
    """One marker for every host: DIRECTION_MARKER when set, else ~/.code/direction-last-check.json.

    Host home variables are deliberately not consulted. Codex sets CODEX_HOME for
    its hooks and Claude Code does not, so a lookup by those would give each host
    its own file and a check done in one would never clear the other's reminder.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    fallback = Path(source.get("HOME", "~")) / ".code" / MARKER_NAME
    return Path(source.get("DIRECTION_MARKER") or fallback).expanduser()


def parse_stamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


def memory_reminder(now: dt.datetime, env: Mapping[str, str] | None = None) -> str:
    """Read optional review state and the registry; never write memory or markers."""
    source = os.environ if env is None else env
    path = Path(source.get("MEMORY_MARKER") or marker_path(source).with_name(MEMORY_MARKER_NAME)).expanduser()
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    memory_root = state.get("memory_root")
    root = Path(memory_root).expanduser() if isinstance(memory_root, str) and memory_root else Path(source.get("HOME", "~")) / ".codex" / "memories"
    if not root.is_absolute():
        return ""
    try:
        with (root / "MEMORY.md").open(encoding="utf-8") as registry:
            words = sum(len(line.split()) for line in registry)
    except (OSError, UnicodeError, ValueError):
        return ""
    days = state.get("max_age_days", MEMORY_STALE_DAYS)
    limit = state.get("max_words", MEMORY_MAX_WORDS)
    if type(days) is not int or not 0 < days <= dt.timedelta.max.days:
        days = MEMORY_STALE_DAYS
    if type(limit) is not int or limit <= 0:
        limit = MEMORY_MAX_WORDS
    last = parse_stamp(state.get("last_distilled_at"))
    reasons = []
    if last is not None and now - last > dt.timedelta(days=days):
        reasons.append(f"last distillation was more than {days} days ago")
    if words > limit:
        reasons.append(f"registry has {words:,} words, over {limit:,}")
    if not reasons:
        return ""
    return f"Codex memory review due: {'; '.join(reasons)}. Ask for distillation; record completion in {path}."


def read_marker(path: Path) -> dict[str, object]:
    """The marker as {"turn": datetime | None, "audits": {repo: datetime}}."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {"turn": None, "audits": {}}
    audits_raw = raw.get("audits")
    audits: dict[str, dt.datetime] = {}
    if isinstance(audits_raw, dict):
        for repo, value in audits_raw.items():
            stamp = parse_stamp(value)
            if isinstance(repo, str) and stamp:
                audits[repo] = stamp
    runs_raw = raw.get("audit_runs")
    runs: dict[str, tuple[dt.datetime, str | None]] = {}
    if isinstance(runs_raw, dict):
        for repo, value in runs_raw.items():
            ran = parse_stamp(value.get("ran")) if isinstance(value, dict) else None
            if isinstance(repo, str) and ran:
                held = value.get("held")
                runs[repo] = (ran, held if isinstance(held, str) and held else None)
    result: dict[str, object] = {"turn": parse_stamp(raw.get("turn")), "audits": audits, "audit_runs": runs}
    if isinstance(raw.get("turn_repo"), str):
        result["turn_repo"] = raw["turn_repo"]
    weekly = raw.get("weekly_audit")
    if isinstance(weekly, dict):
        ran, record = parse_stamp(weekly.get("ran")), weekly.get("record")
        if ran and isinstance(record, str) and "/" in record:
            result["weekly"], result["weekly_record"] = ran, record
    return result


def git_line(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def checkout_root(cwd: Path) -> Path | None:
    top = git_line(cwd, "rev-parse", "--show-toplevel")
    return Path(top) if top else None


def direction_root(cwd: Path) -> Path | None:
    """Checkout root when cwd belongs to a repository with DIRECTION.md."""
    root = checkout_root(cwd)
    return root if root is not None and (root / "DIRECTION.md").is_file() else None


def origin_repo(root: Path | None) -> str | None:
    """OWNER/REPO for a checkout with a GitHub origin, if available."""
    if root is None:
        return None
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", remote.stdout.strip()) if remote.returncode == 0 else None
    return f"{match.group(1)}/{match.group(2)}" if match else None


def loop_summary(text: str) -> str:
    """Print the command block; keep the full procedure at its owning reference."""
    block = re.search(r"```text\n.*?\n```", text, re.DOTALL)
    link = f"Read the [executing loop]({LOOP_PATH}) before its steps."
    return f"{block.group()}\n\n{link}" if block else link


def run_reader(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """The GitHub reader, bounded across its whole process tree.

    The reader is a shell wrapper that starts credential helpers. A plain timeout
    kills only the wrapper and then waits on pipes a surviving helper still holds.
    """
    process = subprocess.Popen(
        [str(GH_READER), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=OVERALL_READ_TIMEOUT if timeout is None else timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.stdout.close()  # type: ignore[union-attr]
        process.stderr.close()  # type: ignore[union-attr]
        process.wait()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def local_overall_checkout(owner: str, root: Path) -> Path | None:
    """A `direction` checkout of OWNER/direction beside this repository's main checkout."""
    common = git_line(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    local = Path(common).parent.parent / OVERALL_REPO
    return local if (origin_repo(local) or "").lower() == f"{owner}/{OVERALL_REPO}".lower() else None


def direction_set_up(owner: str, root: Path, marker: dict[str, object]) -> tuple[bool, Path | None]:
    """Whether OWNER/direction is set up on this machine, and its sibling checkout if there is one.

    It counts when it has been audited here or is checked out beside this repository, so an
    owner who never set one up pays for no network read.
    """
    audits = marker.get("audits")
    audited = isinstance(audits, dict) and any(str(key).lower() == f"{owner}/{OVERALL_REPO}".lower() for key in audits)
    local = local_overall_checkout(owner, root)
    return audited or local is not None, local


def parse_turn_record(body: object) -> tuple[dt.datetime, str] | None:
    """The turn time and covered repository from a shared turn record body, if it is one."""
    if not isinstance(body, str) or TURN_RECORD_TAG not in body:
        return None
    match = TURN_RECORD_LINE.search(body)
    stamp = parse_stamp(match.group(1)) if match else None
    return (stamp, match.group(2)) if match and stamp else None


def parse_weekly_record(body: object) -> dt.datetime | None:
    """The weekly-audit time from a shared weekly-audit record body, if it is one."""
    if not isinstance(body, str) or WEEKLY_RECORD_TAG not in body:
        return None
    match = WEEKLY_RECORD_LINE.search(body)
    return parse_stamp(match.group(1)) if match else None


def read_open_issues(owner: str) -> tuple[list[object], str | None]:
    """Every open issue in OWNER/direction, oldest first, within one read budget, or why not."""
    deadline = time.monotonic() + OVERALL_READ_TIMEOUT
    issues: list[object] = []
    for page in range(1, RECORD_PAGES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], "GitHub did not answer in time"
        try:
            result = run_reader("api", f"repos/{owner}/{OVERALL_REPO}/issues?state=open&sort=created&direction=asc&per_page=100&page={page}",
                                "--method", "GET", timeout=remaining)
        except subprocess.TimeoutExpired:
            return [], "GitHub did not answer in time"
        except OSError as exc:
            return [], f"the GitHub reader could not run ({exc.strerror or exc})"
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").splitlines()[-1]
            return [], f"GitHub read failed: {detail[:160]}"
        try:
            listed = json.loads(result.stdout)
        except ValueError:
            listed = None
        if not isinstance(listed, list):
            return [], "GitHub returned something other than an issue list"
        issues.extend(listed)
        if len(listed) < 100:
            return issues, None
    return [], f"more than {RECORD_PAGES * 100} open issues, so not every record could be read"


def read_shared_records(owner: str) -> tuple[dict[str, object], str | None]:
    """The newest turn and weekly audit across OWNER/direction's shared records, and why they could not be read.

    One listing of every open issue serves both records. No record yet is a missing key, not an error.
    """
    issues, error = read_open_issues(owner)
    if error:
        return {}, error
    turns, weeklies = [], []
    for issue in issues:
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        user = issue.get("user")
        # Only the automation identity or a repository member writes the records.
        if issue.get("author_association") not in TRUSTED_ASSOCIATIONS and not (isinstance(user, dict) and user.get("type") == "Bot"):
            continue
        turn_record = parse_turn_record(issue.get("body"))
        if turn_record:
            turns.append(turn_record)
        weekly = parse_weekly_record(issue.get("body"))
        if weekly:
            weeklies.append(weekly)
    found: dict[str, object] = {}
    if turns:
        # Two machines may each have created a record; the newest stamp wins.
        found["turn"] = max(turns, key=lambda record: record[0])
    if weeklies:
        found["weekly"] = max(weeklies)
    return found, None


def marker_account(marker: dict[str, object]) -> str | None:
    """The owner of the direction repository named by this machine's newest recorded turn or weekly audit."""
    recorded = []
    for stamp_key, repo_key in (("turn", "turn_repo"), ("weekly", "weekly_record")):
        stamp, value = marker.get(stamp_key), marker.get(repo_key)
        if isinstance(value, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", value):
            moment = stamp if isinstance(stamp, dt.datetime) else dt.datetime.min.replace(tzinfo=dt.timezone.utc)
            recorded.append((moment, value.split("/", 1)[0]))
    return max(recorded, key=lambda item: item[0])[1] if recorded else None


def resolve_account(marker: dict[str, object], checkout: Path | None) -> str | None:
    """The Director's account, from the marker; the folder counts only when the marker records no account.

    The folder's origin owner counts only when that owner has OWNER/direction set up on this
    machine, so a fork or another person's repository never picks the account.
    """
    owner = marker_account(marker)
    if owner or checkout is None:
        return owner
    repo = origin_repo(checkout)
    if not repo:
        return None
    owner = repo.split("/", 1)[0]
    if repo.split("/", 1)[1].lower() == OVERALL_REPO:
        return owner
    return owner if direction_set_up(owner, checkout, marker)[0] else None


def local_weekly(marker: dict[str, object], owner: str | None) -> dt.datetime | None:
    """This machine's last complete weekly audit, when it was of the same account."""
    weekly, record = marker.get("weekly"), marker.get("weekly_record")
    if not owner or not isinstance(weekly, dt.datetime) or not isinstance(record, str):
        return None
    return weekly if record.split("/", 1)[0].casefold() == owner.casefold() else None


def local_turn(marker: dict[str, object], owner: str | None) -> dt.datetime | None:
    """This machine's last daily turn, when it was of the same account.

    With no account selected, the turn counts as recorded. Otherwise a turn that
    names another account's direction repository, or names none, does not count.
    """
    turn, repo = marker.get("turn"), marker.get("turn_repo")
    if not isinstance(turn, dt.datetime):
        return None
    if not owner:
        return turn
    return turn if isinstance(repo, str) and repo.split("/", 1)[0].casefold() == owner.casefold() else None


def with_shared_records(marker: dict[str, object], now: dt.datetime, owner: str | None) -> dict[str, object]:
    """The marker with newer stamps from the shared records, read only when a local stamp is stale."""
    if not owner:
        return marker
    turn, weekly = local_turn(marker, owner), local_weekly(marker, owner)
    turn_stale = turn is None or now - turn > TURN_STALE
    weekly_stale = weekly is None or now - weekly > AUDIT_STALE
    if not turn_stale and not weekly_stale:
        return marker
    shared, error = read_shared_records(owner)
    result = dict(marker)
    result["shared_record"] = f"{owner}/{OVERALL_REPO}"
    if error:
        result["shared_error"] = error
        return result
    shared_turn = shared.get("turn")
    if isinstance(shared_turn, tuple) and (turn is None or shared_turn[0] > turn):
        result["turn"], result["turn_repo"] = shared_turn
    shared_weekly = shared.get("weekly")
    if isinstance(shared_weekly, dt.datetime) and (weekly is None or shared_weekly > weekly):
        result["weekly"], result["weekly_record"] = shared_weekly, f"{owner}/{OVERALL_REPO}"
    return result


def reminder_states(marker: dict[str, object], now: dt.datetime, owner: str | None) -> dict[str, str | None]:
    """Each check as current, due or unconfirmed; the audit is None for an account without direction."""
    unreadable = isinstance(marker.get("shared_error"), str)
    turn = local_turn(marker, owner)
    turn_state = ("current" if turn is not None and now - turn <= TURN_STALE
                  else "unconfirmed" if unreadable else "due")
    audit_state = None
    if owner:
        weekly = local_weekly(marker, owner)
        audit_state = ("current" if weekly is not None and now - weekly <= AUDIT_STALE
                       else "unconfirmed" if unreadable else "due")
    return {"turn": turn_state, "audit": audit_state}


def reminder(marker: dict[str, object], now: dt.datetime, path: Path, owner: str | None = None) -> str:
    """At most two lines, one for the daily turn and one for the weekly audit; empty when both are current."""
    states = reminder_states(marker, now, owner)
    lines: list[str] = []
    shared_record, shared_error = marker.get("shared_record"), marker.get("shared_error")
    record = shared_record if isinstance(shared_record, str) else f"{owner}/{OVERALL_REPO}"
    error = shared_error if isinstance(shared_error, str) else "no reason given"
    after = f"Do not run the marking helpers yourself; the marker is {path}."
    turn = local_turn(marker, owner)
    turn_local = ("has recorded no direction turn" if turn is None
                  else f"last recorded a direction turn {(now - turn).days} days ago")
    if states["turn"] == "unconfirmed":
        lines.append(
            f"{REMINDER_LINES['turn', 'unconfirmed']}: this machine {turn_local}, and the shared turn record in {record} could not "
            f"be read ({error}), so a turn taken on another machine would not show here. Tell the owner once at the "
            "start of the session that the shared record could not be read, and to run the `direction` skill only if "
            f"no machine has taken today's turn. {after}"
        )
    elif states["turn"] == "due":
        detail = ("no direction turn has been recorded" if turn is None
                  else f"the last direction turn was {(now - turn).days} days ago")
        lines.append(
            f"{REMINDER_LINES['turn', 'due']}: {detail}. Tell the owner once at the start of the session to open Claude Code "
            f"and run the `direction` skill for the daily turn, then continue with the task. {after}"
        )
    weekly = local_weekly(marker, owner)
    if states["audit"] == "unconfirmed":
        local = ("has recorded no full weekly audit" if weekly is None
                 else f"last recorded a full weekly audit {(now - weekly).days} days ago")
        lines.append(
            f"{REMINDER_LINES['audit', 'unconfirmed']}: this machine {local}, and the shared weekly-audit record in {record} "
            f"could not be read ({error}), so an audit run on another machine would not show here. Tell the owner once "
            "at the start of the session, and to run the weekly audit only if no machine has run it this week. "
            f"{after}"
        )
    elif states["audit"] == "due":
        detail = ("no full weekly audit has been recorded" if weekly is None
                  else f"the last full weekly audit was {(now - weekly).days} days ago")
        lines.append(
            f"{REMINDER_LINES['audit', 'due']}: {detail}. Tell the owner once at the start of the session to run the "
            f"`direction` skill's weekly audit for {owner}, then continue with the task. {after}"
        )
    return "\n".join(lines)


def session_reminder(marker: dict[str, object], now: dt.datetime, checkout: Path | None, path: Path) -> str:
    """The reminder for this session; the folder matters only when the marker records no account."""
    owner = resolve_account(marker, checkout)
    return reminder(with_shared_records(marker, now, owner), now, path, owner)


def main(*, skills_only: bool = False, catalog_root: Path | None = None, runtime_catchup: bool = False,
         turn: bool = False) -> int:
    if turn:
        # Claude Code shows the session-start text once; Codex re-sends its catalog on its own.
        if os.environ.get("CLAUDECODE") == "1":
            print(f"Name each step's owning skill and load it before acting; see {SKILLS_PROTOCOL_PATH}.", flush=True)
        return 0
    try:
        if os.environ.get("CLAUDECODE") == "1":
            try:
                print(SKILLS_PROTOCOL_PATH.read_text().strip(), flush=True)
            except OSError:
                pass  # A missing protocol must not hide the loop or reminder.
        if skills_only:
            return 0
        # Resolve the runtime catalog from this registered hook, never the task cwd.
        catalog = catalog_root or Path(__file__).resolve().parents[1]
        tools_line = agent_tools_warning(catalog, Path.cwd(), os.environ)
        if tools_line:
            print(tools_line, flush=True)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            from scripts.catalog_runtime import status_line
            catalog_line = status_line(catalog, observe=True) if (catalog / ".local" / "catalog-install.json").is_file() else ""
            if catalog_line:
                print(catalog_line, flush=True)
        except (ImportError, OSError):
            pass
        path = marker_path()
        marker = read_marker(path)
        root = direction_root(Path.cwd())
        try:
            loop = loop_summary(LOOP_PATH.read_text())
        except OSError:
            loop = ""  # A missing loop reference must not hide an overdue reminder.
        if root is not None:
            if loop:
                print(loop, flush=True)
        now = dt.datetime.now(dt.timezone.utc)
        try:
            memory_text = memory_reminder(now)
        except Exception:  # Optional memory input must not hide direction or catch-up.
            memory_text = ""
        if memory_text:
            print(memory_text, flush=True)
        reminder_text = session_reminder(marker, now, checkout_root(Path.cwd()), path)
        if reminder_text:
            print(reminder_text, flush=True)
        if runtime_catchup:
            catchup_line = catch_up_runtime(catalog)
            if catchup_line:
                print(catchup_line, flush=True)
    except Exception:  # noqa: BLE001 - a reminder must never break a session start
        pass
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Print overdue direction reminders and existing startup diagnostics.")
    parser.add_argument("--skills-only", action="store_true")
    parser.add_argument("--turn", action="store_true", help="print a one-line skills reminder for each prompt")
    parser.add_argument("--runtime-catchup", action="store_true", help="Enable catch-up with the updated native hook timeout")
    parser.add_argument("--catalog-root", type=Path, help="Catalog checkout for status diagnostics")
    args = parser.parse_args()
    sys.exit(main(skills_only=args.skills_only, catalog_root=args.catalog_root, runtime_catchup=args.runtime_catchup,
                  turn=args.turn))
