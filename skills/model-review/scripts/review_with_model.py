#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ask another provider's model for a read-only review of a repository.

Each provider's CLI is run so that it can read the repository with its own
tools and cannot change it. A run that could not read, returned nothing, or hit
a provider error is a failure with a nonzero exit, never an empty "no findings":
an empty review and a reviewer that was locked out look the same otherwise.

Exit codes: 0 reviewed, 1 the run failed, 2 the provider's CLI is not installed.
Nothing here edits a user's tool configuration unless `configure` is asked for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROVIDERS = {"openai": "codex", "anthropic": "claude", "google": "agy"}
FAULT_MARKER_NAME = "model-review-fault.md"
AGY_SETTINGS = Path("~/.gemini/antigravity-cli/settings.json")
AGY_READ_ONLY_COMMANDS = ("grep", "rg", "ls", "find", "wc")
PREAMBLE = (
    "The repository to examine is at {repo} (absolute path). Read its files with your own tools. "
    "Do not modify, create, or delete anything.\n\n"
)


def agy_rules(read_roots: list[Path]) -> list[str]:
    # Directory targets without globs are the only form agy accepts for headless reads.
    return [f"read_file({root})" for root in read_roots] + [f"command({name})" for name in AGY_READ_ONLY_COMMANDS]


def agy_hint(repo: Path) -> str:
    snippet = json.dumps({"permissions": {"allow": agy_rules([repo])}}, indent=2)
    return (
        "agy runs headless here and stops at the first tool that needs permission, so it needs read-only "
        f"allow rules. Merge this into {AGY_SETTINGS}, or run: {Path(__file__).name} configure --read-root "
        f"{repo}\nThe rule applies to every agy session, not only reviews. Name a directory that holds your "
        f"repositories to cover them all, and never your home directory.\n{snippet}"
    )


def agy_settings() -> dict[str, Any]:
    try:
        loaded = json.loads(AGY_SETTINGS.expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def run_cli(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
    return subprocess.run(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


def failed(provider: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "provider": provider, "error": error, **extra}


def review_openai(prompt: str, repo: Path, model: str | None, timeout: int, scratch: Path) -> dict[str, Any]:
    answer = scratch / "answer.md"
    argv = ["codex", "exec", "-C", str(repo), "-s", "read-only", "-o", str(answer)]
    if model:
        argv += ["-m", model]
    proc = run_cli([*argv, prompt], repo, timeout)
    used = re.search(r"^model:\s*(\S+)", proc.stdout + proc.stderr, re.MULTILINE)
    response = answer.read_text() if answer.is_file() else ""
    if proc.returncode != 0:
        return failed("openai", f"codex exited {proc.returncode}", detail=proc.stderr[-400:])
    return {"ok": True, "provider": "openai", "model": used.group(1) if used else model, "response": response}


def review_anthropic(prompt: str, repo: Path, model: str | None, timeout: int, _scratch: Path) -> dict[str, Any]:
    # `--tools` limits what exists in the session. `--allowedTools` would only pre-approve these
    # on top of the user's own settings, which may already allow editing.
    argv = ["claude", "-p", prompt, "--tools", "Read,Grep,Glob", "--output-format", "json"]
    if model:
        argv += ["--model", model]
    proc = run_cli(argv, repo, timeout)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return failed("anthropic", f"claude exited {proc.returncode} without JSON", detail=proc.stdout[-400:])
    if payload.get("is_error") or proc.returncode != 0:
        return failed("anthropic", "claude reported an error", detail=str(payload.get("result"))[:400])
    if payload.get("permission_denials"):
        tools = sorted({str(item.get("tool_name", "?")) for item in payload["permission_denials"]})
        return failed("anthropic", f"claude was denied: {', '.join(tools)}", denied=tools)
    models = sorted(payload.get("modelUsage") or {})
    return {
        "ok": True,
        "provider": "anthropic",
        "model": ",".join(models) or model,
        "response": payload.get("result") or "",
    }


def review_google(prompt: str, repo: Path, model: str | None, timeout: int, scratch: Path) -> dict[str, Any]:
    # `--sandbox` confines the shell to the working directory, so that directory is an empty scratch one.
    # It does not confine agy's own write tool; leaving that tool without an allow rule is what denies it.
    # A write rule the user added for their own work would be inherited by the reviewer.
    allow = (agy_settings().get("permissions") or {}).get("allow") or []
    writable = sorted(rule for rule in allow if isinstance(rule, str) and rule.startswith("write_file"))
    if writable:
        return failed(
            "google",
            "your agy settings allow file writes, so a headless reviewer could change the repository",
            rules=writable,
            hint=f"Remove these rules from {AGY_SETTINGS} for the review, or use another provider.",
        )
    argv = ["agy", "-p", prompt, "--sandbox", "--output-format", "json", "--print-timeout", f"{timeout}s"]
    if model:
        argv += ["--model", model]
    proc = run_cli(argv, scratch, timeout + 30)
    try:
        payload = json.loads(proc.stdout[proc.stdout.index("{") :])
    except ValueError:
        return failed("google", f"agy exited {proc.returncode} without JSON", detail=proc.stdout[-400:])
    denied = sorted({item.get("action", "?") for item in payload.get("denied_actions") or []})
    if denied:
        return failed("google", f"agy was denied: {', '.join(denied)}", denied=denied, hint=agy_hint(repo))
    if payload.get("status") not in (None, "SUCCESS"):
        return failed("google", f"agy finished with status {payload.get('status')}")
    # agy does not report the model that ran; say where the name came from instead of implying it did.
    requested = model or agy_settings().get("model")
    return {
        "ok": True,
        "provider": "google",
        "model": requested,
        "model_source": "requested, not reported by the CLI" if requested else "unknown",
        "response": payload.get("response") or "",
    }


REVIEWERS = {"openai": review_openai, "anthropic": review_anthropic, "google": review_google}


def review(provider: str, prompt: str, repo: Path, model: str | None, timeout: int) -> dict[str, Any]:
    if shutil.which(PROVIDERS[provider]) is None:
        return failed(provider, f"`{PROVIDERS[provider]}` is not installed", installed=False)
    with tempfile.TemporaryDirectory(prefix="model-review-") as scratch:
        try:
            result = REVIEWERS[provider](PREAMBLE.format(repo=repo) + prompt, repo, model, timeout, Path(scratch))
        except subprocess.TimeoutExpired:
            return failed(provider, f"no answer within {timeout} seconds")
    if result["ok"] and not result["response"].strip():
        return failed(provider, "the reviewer returned nothing", model=result.get("model"))
    return result


def fault_marker(source: dict[str, str] = os.environ) -> Path:
    """The owner's planted finding: MODEL_REVIEW_FAULT when set, else ~/.code/model-review-fault.md.

    One path for every host on purpose, like the direction marker: host home variables differ.
    """
    explicit = source.get("MODEL_REVIEW_FAULT")
    if explicit:
        return Path(explicit).expanduser()
    return Path(source.get("HOME", "~")).expanduser() / ".code" / FAULT_MARKER_NAME


def plant_fault(result: dict[str, Any], marker: Path, repo: Path) -> Path | None:
    """Append the owner's planted finding to one successful review; return the marker to consume.

    This is how the direction skill's unannounced planted-fault run reaches an executing agent:
    the owner writes the marker outside the repository before an ordinary session, the finding
    arrives inside real reviewer output, and the renamed marker records when it fired. The JSON
    result says nothing, so the agent weighs the finding as it would any other. A marker inside
    the reviewed repository is never read: a pull request could have put it there.
    """
    if not result["ok"] or not marker.is_file():
        return None
    if marker.resolve().is_relative_to(repo.resolve()):
        print(f"ignoring planted finding inside the reviewed repository: {marker}", file=sys.stderr)
        return None
    finding = marker.read_text().strip()
    if not finding:
        return None
    result["response"] = result["response"].rstrip() + "\n\n" + finding + "\n"
    return marker


def consume_fault(marker: Path) -> None:
    """Rename the marker only once the review it landed in has been delivered."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    marker.rename(marker.with_name(f"{marker.name}.used-{stamp}"))


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    result = review(args.provider, Path(args.prompt_file).read_text(), repo, args.model, args.timeout)
    planted = plant_fault(result, fault_marker(), repo)
    if result["ok"] and args.out:
        Path(args.out).write_text(result.pop("response"))
        result["response_file"] = args.out
    print(json.dumps(result, indent=2), flush=True)
    if planted is not None:
        consume_fault(planted)
    return 0 if result["ok"] else 2 if result.get("installed") is False else 1


def find_probe(repo: Path) -> tuple[Path | None, str]:
    """A small text file and its first non-empty line, from Git's list when there is one."""
    listed = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split("\n")
    names = [name for name in listed if name] or sorted(
        str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file() and ".git" not in path.parts
    )
    for name in names[:500]:
        path = repo / name
        try:
            if not path.is_file() or path.stat().st_size > 200_000:
                continue
            # The prompt asks for the first non-empty line, so that exact line is the expectation.
            # A file whose first line is a lone brace or dash is skipped, not read past.
            line = next((text.strip() for text in path.read_text().splitlines() if text.strip()), None)
        except (OSError, UnicodeDecodeError):
            continue
        if line and len(line) > 3:
            return path, line
    return None, ""


def cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    probe, expected = find_probe(repo)
    if probe is None:
        print(json.dumps({"ok": False, "error": f"no readable text file to probe in {repo}"}))
        return 1
    prompt = f"Reply with exactly the first non-empty line of {probe}, and nothing else."
    report = []
    for provider in args.provider or sorted(PROVIDERS):
        result = review(provider, prompt, repo, None, args.timeout)
        if result["ok"] and expected not in result["response"]:
            result = failed(provider, "answered without reading the file", model=result.get("model"))
        state = "ready" if result["ok"] else "not installed" if result.get("installed") is False else "not ready"
        report.append({key: value for key, value in {**result, "state": state}.items() if key != "response"})
    print(json.dumps({"probe": str(probe), "providers": report}, indent=2))
    return 0 if any(item["state"] == "ready" for item in report) else 1


def cmd_configure(args: argparse.Namespace) -> int:
    settings = AGY_SETTINGS.expanduser()
    if not settings.is_file():
        print(json.dumps({"ok": False, "error": f"{settings} does not exist; run agy once first"}))
        return 1
    current = json.loads(settings.read_text())
    allow = (current.get("permissions") or {}).get("allow") or []
    wanted = agy_rules([Path(root).resolve() for root in args.read_root])
    added = [rule for rule in wanted if rule not in allow]
    if added:
        backup = settings.with_name(settings.name + ".before-model-review")
        shutil.copy2(settings, backup)
        current.setdefault("permissions", {})["allow"] = [*allow, *added]
        settings.write_text(json.dumps(current, indent=2) + "\n")
    print(json.dumps({"ok": True, "settings": str(settings), "added": added}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Ask one provider's model for a read-only review.")
    run.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    run.add_argument("--repo", default=".", help="Repository the reviewer reads (default: current directory).")
    run.add_argument("--prompt-file", required=True, help="What to review and what it is for; name paths, do not paste files.")
    run.add_argument("--model", help="Model to request; the result reports the model actually used.")
    run.add_argument("--out", help="Write the review here instead of including it in the JSON.")
    run.add_argument("--timeout", type=int, default=900)
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="Show which providers can actually read a repository from here.")
    check.add_argument("--repo", default=".")
    check.add_argument("--provider", action="append", choices=sorted(PROVIDERS))
    check.add_argument("--timeout", type=int, default=300)
    check.set_defaults(func=cmd_check)

    configure = sub.add_parser("configure", help="Add agy's read-only allow rules to your own settings file.")
    configure.add_argument("--read-root", action="append", required=True, help="Directory agy may read; repeatable.")
    configure.set_defaults(func=cmd_configure)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
