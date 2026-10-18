#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Session-start executing loop and overdue direction reminder.

The `direction` skill records the end of every daily turn in a small local
marker and in a shared turn record in OWNER/direction, and the audit script
records each weekly audit in the marker per repository, including an audit
that ran but held its window open, which is reported as held, not overdue. When this machine's
own turn is older than a day, the hook reads the shared record, so a turn
taken on another machine counts; when that read fails it says so instead of
calling the turn overdue.
With --turn, run for each Claude Code prompt, it prints a one-line skills reminder.
At session start this hook prints the skills protocol on Claude Code and the executing loop's command block and reference for repositories with a
root DIRECTION.md. In a repository without one, whose origin owner keeps an overall direction in OWNER/direction
that has been audited on this machine or is checked out beside it, it prints that file's stop boundaries, where the file lives, and the loop.
It reads the marker and prints one line when the last
turn is older than a day, or when the repository the session opened in has a
`DIRECTION.md` and its last audit is older than a week. Outside a direction
repository it prints nothing when checks are current. It never reads stdin,
never blocks, and exits 0 whatever
it finds, so the same script serves Claude Code's SessionStart hook and a
Codex session-start hook.
"""

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
# The shared turn record: one issue in OWNER/direction that direction_mark.py keeps current.
TURN_RECORD_TAG = "<!-- direction-turn-record -->"
TURN_RECORD_LINE = re.compile(r"^Last daily turn: (\S+) covering (\S+)$", re.MULTILINE)
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
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


def section(text: str, heading: str) -> str | None:
    """One `## heading` section of a Markdown file, heading included."""
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if lines:
                break
            if line[3:].strip().lower() == heading.lower():
                lines.append(line)
        elif lines:
            lines.append(line)
    return "\n".join(lines).strip() or None


def loop_summary(text: str) -> str:
    """Print the command block; keep the full procedure at its owning reference."""
    block = re.search(r"```text\n.*?\n```", text, re.DOTALL)
    link = f"Read the [executing loop]({LOOP_PATH}) before its steps."
    return f"{block.group()}\n\n{link}" if block else link


def run_reader(*args: str) -> subprocess.CompletedProcess[str]:
    """The GitHub reader, bounded across its whole process tree.

    The reader is a shell wrapper that starts credential helpers. A plain timeout
    kills only the wrapper and then waits on pipes a surviving helper still holds.
    """
    process = subprocess.Popen(
        [str(GH_READER), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=OVERALL_READ_TIMEOUT)
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


def read_merged_overall(owner: str) -> tuple[str | None, str]:
    """The merged overall DIRECTION.md from GitHub, or None and why it could not be read."""
    try:
        result = run_reader("api", f"repos/{owner}/{OVERALL_REPO}/contents/DIRECTION.md", "--method", "GET", "-H", "Accept: application/vnd.github.raw")
    except subprocess.TimeoutExpired:
        return None, "GitHub did not answer in time"
    except OSError as exc:
        return None, f"the GitHub reader could not run ({exc.strerror or exc})"
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout, "the merged default branch"
    detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").splitlines()[-1]
    return None, f"GitHub read failed: {detail[:160]}"


def local_overall_checkout(owner: str, root: Path) -> Path | None:
    """A `direction` checkout of OWNER/direction beside this repository's main checkout."""
    common = git_line(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    local = Path(common).parent.parent / OVERALL_REPO
    return local if (origin_repo(local) or "").lower() == f"{owner}/{OVERALL_REPO}".lower() else None


def read_local_overall(local: Path | None) -> tuple[str | None, str]:
    """DIRECTION.md as last fetched from the remote default branch, never a local branch or draft."""
    if local is None:
        return None, "no local checkout beside this repository"
    try:
        result = subprocess.run(["git", "show", "refs/remotes/origin/HEAD:DIRECTION.md"], cwd=local, text=True, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, f"could not read {local}"
    if result.returncode != 0 or not result.stdout.strip():
        return None, f"{local} has no fetched default branch with a DIRECTION.md"
    return result.stdout, f"the default branch last fetched into {local}, which may be behind GitHub"


def overall_direction(repo: str | None, root: Path | None, marker: dict[str, object], loop: str) -> str:
    """Overall direction for a checkout without its own DIRECTION.md, empty when its owner keeps none here.

    Only an owner who set up OWNER/direction on this machine counts: it has been audited here, or
    it is checked out beside this repository. That keeps another person's repository from putting
    its text into the session, and it keeps owners who never set up an overall direction from
    paying for a network read at every start.
    """
    if not repo or root is None:
        return ""
    owner = repo.split("/", 1)[0]
    audits = marker.get("audits")
    audited = isinstance(audits, dict) and any(str(key).lower() == f"{owner}/{OVERALL_REPO}".lower() for key in audits)
    local = local_overall_checkout(owner, root)
    if not audited and local is None:
        return ""
    url = f"https://github.com/{owner}/{OVERALL_REPO}/blob/HEAD/DIRECTION.md"
    where = url
    text, source = read_merged_overall(owner)
    if text is None:
        text, local_reason = read_local_overall(local)
        if text is None:
            return (
                f"Overall direction: this repository has no DIRECTION.md of its own, so {owner}'s overall direction in "
                f"{url} applies, but it could not be read at session start ({source}; {local_reason})."
            )
        source = local_reason
        where = f"`git -C {local} show refs/remotes/origin/HEAD:DIRECTION.md` (on GitHub: {url})"
    boundaries = section(text, "Stop Boundaries") or "(the file has no Stop Boundaries section; read it in full)"
    return (
        f"Overall direction: this repository has no DIRECTION.md of its own, so {owner}'s overall direction applies. "
        f"Read {where} before acting. Its stop boundaries, from {source}:\n\n{boundaries}"
        + (f"\n\n{loop}" if loop else "")
    )


def parse_turn_record(body: object) -> tuple[dt.datetime, str] | None:
    """The turn time and covered repository from a shared turn record body, if it is one."""
    if not isinstance(body, str) or TURN_RECORD_TAG not in body:
        return None
    match = TURN_RECORD_LINE.search(body)
    stamp = parse_stamp(match.group(1)) if match else None
    return (stamp, match.group(2)) if match and stamp else None


def read_shared_turn(owner: str) -> tuple[tuple[dt.datetime, str] | None, str | None]:
    """The newest turn in OWNER/direction's shared record, and why it could not be read.

    No record yet is (None, None): nobody has taken a turn that wrote one.
    """
    try:
        # Oldest first: the record is created at the first shared turn, so it stays on this page.
        result = run_reader("api", f"repos/{owner}/{OVERALL_REPO}/issues?state=open&sort=created&direction=asc&per_page=100", "--method", "GET")
    except subprocess.TimeoutExpired:
        return None, "GitHub did not answer in time"
    except OSError as exc:
        return None, f"the GitHub reader could not run ({exc.strerror or exc})"
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").splitlines()[-1]
        return None, f"GitHub read failed: {detail[:160]}"
    try:
        issues = json.loads(result.stdout)
    except ValueError:
        return None, "GitHub returned something other than an issue list"
    if not isinstance(issues, list):
        return None, "GitHub returned something other than an issue list"
    turns = []
    for issue in issues:
        if not isinstance(issue, dict) or "pull_request" in issue:
            continue
        user = issue.get("user")
        # Only the automation identity or a repository member writes the record.
        if issue.get("author_association") not in TRUSTED_ASSOCIATIONS and not (isinstance(user, dict) and user.get("type") == "Bot"):
            continue
        turn = parse_turn_record(issue.get("body"))
        if turn:
            turns.append(turn)
    return (max(turns, key=lambda turn: turn[0]) if turns else None), None


def with_shared_turn(marker: dict[str, object], now: dt.datetime, repo: str | None) -> dict[str, object]:
    """The marker with a newer turn from the shared record, read only when this machine's turn is stale.

    The record lives in the overall direction repository of the owner whose turn this
    machine last recorded, or, on a machine that never recorded one, of the repository
    the session opened in.
    """
    turn = marker.get("turn")
    if isinstance(turn, dt.datetime) and now - turn <= TURN_STALE:
        return marker
    coverage = marker.get("turn_repo")
    source = coverage if isinstance(coverage, str) and "/" in coverage else repo
    if not source:
        return marker
    owner = source.split("/", 1)[0]
    shared, error = read_shared_turn(owner)
    result = dict(marker)
    result["shared_record"] = f"{owner}/{OVERALL_REPO}"
    if error:
        result["shared_error"] = error
    elif shared and (not isinstance(turn, dt.datetime) or shared[0] > turn):
        result["turn"], result["turn_repo"] = shared
    return result


def last_audit(marker: dict[str, object], repo: str) -> dt.datetime | None:
    """When an audit of the repository last ran, whether or not its window settled."""
    audits, runs = marker.get("audits"), marker.get("audit_runs")
    stamps = [audits.get(repo) if isinstance(audits, dict) else None,
              (runs.get(repo) or (None,))[0] if isinstance(runs, dict) else None]
    return max((stamp for stamp in stamps if isinstance(stamp, dt.datetime)), default=None)


def weekly_audit_repo(marker: dict[str, object], now: dt.datetime, repo: str | None) -> str | None:
    """Suppress a due weekly reminder only for a confirmed archived repository."""
    if not repo:
        return None
    stamp = last_audit(marker, repo)
    if isinstance(stamp, dt.datetime) and now - stamp <= AUDIT_STALE:
        return repo
    try:
        result = run_reader("api", f"repos/{repo}", "--method", "GET")
        metadata = json.loads(result.stdout) if result.returncode == 0 else None
        if (isinstance(metadata, dict) and metadata.get("archived") is True
                and str(metadata.get("full_name", "")).casefold() == repo.casefold()):
            return None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return repo


def reminder(marker: dict[str, object], now: dt.datetime, repo: str | None, path: Path) -> str:
    """One line when something is overdue, empty when the checks are current."""
    overdue: list[str] = []
    turn = marker.get("turn")
    audits = marker.get("audits")
    audits = audits if isinstance(audits, dict) else {}
    unconfirmed = ""
    shared_error = marker.get("shared_error")
    turn_stale = not isinstance(turn, dt.datetime) or now - turn > TURN_STALE
    if turn_stale and isinstance(shared_error, str):
        local = ("has recorded no direction turn" if not isinstance(turn, dt.datetime)
                 else f"last recorded a direction turn {(now - turn).days} days ago")
        unconfirmed = (
            f"Direction turn unconfirmed: this machine {local}, and the shared turn record in "
            f"{marker.get('shared_record')} could not be read ({shared_error}), so a turn taken on another machine "
            "would not show here. Tell the owner once at the start of the session that the shared record could not be "
            "read, and to run the `direction` skill only if no machine has taken today's turn. "
        )
    elif not isinstance(turn, dt.datetime):
        overdue.append("no direction turn has been recorded on this machine")
    elif turn_stale:
        overdue.append(f"the last direction turn was {(now - turn).days} days ago")
    held_note = ""
    if repo:
        audit = last_audit(marker, repo)
        runs = marker.get("audit_runs")
        run = runs.get(repo) if isinstance(runs, dict) else None
        if not isinstance(audit, dt.datetime):
            overdue.append(f"{repo} has a DIRECTION.md but no recorded weekly audit")
        elif now - audit > AUDIT_STALE:
            overdue.append(f"the last weekly audit of {repo} was {(now - audit).days} days ago")
        elif run and run[1] and run[0] == audit:
            # The audit ran; only its window is still open, so this is not a missed audit.
            held_note = (
                f"The weekly audit of {repo} ran {(now - audit).days} days ago and held its window open ({run[1]}); "
                "the next audit rereads that window. Do not call this audit overdue. "
            )
    if not overdue and not unconfirmed and not held_note:
        return ""
    if not overdue:
        return unconfirmed + held_note + f"Do not run the marking helpers yourself; the marker is {path}."
    coverage_note = ""
    coverage = marker.get("turn_repo")
    if isinstance(turn, dt.datetime) and isinstance(coverage, str) and coverage:
        coverage_note = f" The last daily turn covered {coverage}."
    return unconfirmed + held_note + (
        "Direction check overdue: " + "; ".join(overdue) + "." + coverage_note + " "
        "Tell the owner once at the start of the session to open Claude Code and run the `direction` skill "
        "(a daily turn, or the weekly audit of this repository when that is what is overdue), then continue with the task. "
        f"Do not run the marking helpers yourself; the marker is {path}."
    )


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
        else:
            checkout = checkout_root(Path.cwd())
            overall = overall_direction(origin_repo(checkout), checkout, marker, loop)
            if overall:
                print(overall, flush=True)
        now = dt.datetime.now(dt.timezone.utc)
        try:
            memory_text = memory_reminder(now)
        except Exception:  # Optional memory input must not hide direction or catch-up.
            memory_text = ""
        if memory_text:
            print(memory_text, flush=True)
        marker = with_shared_turn(marker, now, origin_repo(root or checkout_root(Path.cwd())))
        reminder_text = reminder(marker, now, weekly_audit_repo(marker, now, origin_repo(root)), path)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-only", action="store_true")
    parser.add_argument("--turn", action="store_true", help="print a one-line skills reminder for each prompt")
    parser.add_argument("--runtime-catchup", action="store_true", help="Enable catch-up with the updated native hook timeout")
    parser.add_argument("--catalog-root", type=Path, help="Catalog checkout for status diagnostics")
    args = parser.parse_args()
    sys.exit(main(skills_only=args.skills_only, catalog_root=args.catalog_root, runtime_catchup=args.runtime_catchup,
                  turn=args.turn))
