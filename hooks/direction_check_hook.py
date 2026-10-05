#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Session-start executing loop and overdue direction reminder.

The `direction` skill records the end of every daily turn in a small local
marker, and the audit script records each weekly audit there per repository.
At session start this hook prints the skills protocol on Claude Code and the executing loop for repositories with a
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
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

MARKER_NAME = "direction-last-check.json"
TURN_STALE = dt.timedelta(hours=24)
AUDIT_STALE = dt.timedelta(days=7)
LOOP_PATH = Path(__file__).resolve().parents[1] / "skills" / "references" / "executing-loop.md"
SKILLS_PROTOCOL_PATH = LOOP_PATH.with_name("using-skills.md")
GH_READER = Path(__file__).resolve().parents[1] / "skills" / "github" / "scripts" / "gh-with-env-token"
OVERALL_REPO = "direction"
OVERALL_READ_TIMEOUT = 6


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
    result: dict[str, object] = {"turn": parse_stamp(raw.get("turn")), "audits": audits}
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


def read_merged_overall(owner: str) -> tuple[str | None, str]:
    """The merged overall DIRECTION.md from GitHub, or None and why it could not be read."""
    try:
        result = subprocess.run(
            [str(GH_READER), "api", f"repos/{owner}/{OVERALL_REPO}/contents/DIRECTION.md", "--method", "GET", "-H", "Accept: application/vnd.github.raw"],
            text=True, capture_output=True, timeout=OVERALL_READ_TIMEOUT, stdin=subprocess.DEVNULL,
        )
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


def reminder(marker: dict[str, object], now: dt.datetime, repo: str | None, path: Path) -> str:
    """One line when something is overdue, empty when the checks are current."""
    overdue: list[str] = []
    turn = marker.get("turn")
    audits = marker.get("audits")
    audits = audits if isinstance(audits, dict) else {}
    if not isinstance(turn, dt.datetime):
        overdue.append("no direction turn has been recorded on this machine")
    elif now - turn > TURN_STALE:
        overdue.append(f"the last direction turn was {(now - turn).days} days ago")
    if repo:
        audit = audits.get(repo)
        if not isinstance(audit, dt.datetime):
            overdue.append(f"{repo} has a DIRECTION.md but no recorded weekly audit")
        elif now - audit > AUDIT_STALE:
            overdue.append(f"the last weekly audit of {repo} was {(now - audit).days} days ago")
    if not overdue:
        return ""
    coverage = marker.get("turn_repo")
    if isinstance(turn, dt.datetime) and isinstance(coverage, str) and coverage:
        overdue.append(f"the last daily turn covered {coverage}")
    return (
        "Direction check overdue: " + "; ".join(overdue) + ". "
        "Tell the owner once at the start of the session to open Claude Code and run the `direction` skill "
        "(a daily turn, or the weekly audit of this repository when that is what is overdue), then continue with the task. "
        f"Do not run the marking helpers yourself; the marker is {path}."
    )


def main(*, skills_only: bool = False, catalog_root: Path | None = None) -> int:
    try:
        if os.environ.get("CLAUDECODE") == "1":
            try:
                print(SKILLS_PROTOCOL_PATH.read_text().strip())
            except OSError:
                pass  # A missing protocol must not hide the loop or reminder.
        if skills_only:
            return 0
        # Resolve the runtime catalog from this registered hook, never the task cwd.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            from scripts.catalog_runtime import status_line
            catalog = catalog_root or Path(__file__).resolve().parents[1]
            catalog_line = status_line(catalog) if (catalog / ".local" / "catalog-install.json").is_file() else ""
            if catalog_line:
                print(catalog_line)
        except (ImportError, OSError):
            pass
        path = marker_path()
        marker = read_marker(path)
        root = direction_root(Path.cwd())
        try:
            loop = LOOP_PATH.read_text().strip()
        except OSError:
            loop = ""  # A missing loop reference must not hide an overdue reminder.
        if root is not None:
            if loop:
                print(loop)
        else:
            checkout = checkout_root(Path.cwd())
            overall = overall_direction(origin_repo(checkout), checkout, marker, loop)
            if overall:
                print(overall)
        reminder_text = reminder(marker, dt.datetime.now(dt.timezone.utc), origin_repo(root), path)
        if reminder_text:
            print(reminder_text)
    except Exception:  # noqa: BLE001 - a reminder must never break a session start
        pass
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-only", action="store_true")
    parser.add_argument("--catalog-root", type=Path, help="Catalog checkout for status diagnostics")
    args = parser.parse_args()
    sys.exit(main(skills_only=args.skills_only, catalog_root=args.catalog_root))
