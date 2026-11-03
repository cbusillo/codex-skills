#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

CATALOG = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CATALOG))
from skills.supervisor.scripts.check_changes import save_state
from skills.supervisor.scripts.services import service_lock


def baseline_path() -> Path:
    return Path(os.environ.get("JB_WEEKLY_BASELINE") or Path.home() / ".code/jetbrains-inspection/weekly-baselines.json")


def read_baselines(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {"repositories": {}}


def category_counts(result: dict[str, Any]) -> dict[str, int]:
    if (
        result.get("backend") != "native"
        or result.get("agent_result", {}).get("verdict") not in {"GREEN", "RED"}
        or result.get("coverage", {}).get("complete") is not True
        or result.get("cleanup", {}).get("status") != "closed"
    ):
        raise ValueError(
            f"inspection coverage or custody is incomplete: {result.get('agent_result', {}).get('agent_report', 'no terminal report')}"
        )
    artifact = result.get("findings_artifact")
    if not artifact:
        if all(result.get(key) == 0 for key in ("finding_count", "preexisting_finding_count", "advisory_finding_count")):
            return {}
        raise ValueError("full findings artifact is unavailable")
    findings = json.loads(Path(artifact).read_text())
    return dict(Counter(finding["rule_id"] for finding in findings))


def record_run(state: dict[str, Any], repo: str, run: dict[str, Any]) -> None:
    record = state["repositories"].setdefault(repo, {})
    record["attempt"] = {key: run[key] for key in ("status", "at", "evidence", "reason") if key in run}
    if run["status"] != "complete":
        return
    latest = record.get("latest")
    if latest and latest["week"] != run["week"]:
        record["previous"] = latest
    record["latest"] = run
    ceiling = record.setdefault("ceiling", dict(run["counts"]))
    record["ceiling"] = {category: min(count, run["counts"].get(category, 0)) for category, count in ceiling.items()}
    for category in run["counts"]:
        record["ceiling"].setdefault(category, 0)


def report(repo: str, *, path: Path | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    try:
        state = read_baselines(path or baseline_path())
        record = state["repositories"].get(repo, {})
        latest, previous = record.get("latest"), record.get("previous")
        if not latest:
            return {"status": "unknown", "reason": "no qualified weekly inspection", "attempt": record.get("attempt")}
        now = now or dt.datetime.now(dt.UTC)
        fresh = 0 <= (now - dt.datetime.fromisoformat(latest["at"])).total_seconds() < 8 * 86400
        comparable = (
            previous
            and latest["signature"] == previous["signature"]
            and dt.date.fromisoformat(latest["week"]) - dt.date.fromisoformat(previous["week"]) == dt.timedelta(days=7)
        )
        categories = []
        for category, count in sorted(latest["counts"].items(), key=lambda pair: (-pair[1], pair[0]))[:3]:
            before = previous["counts"].get(category, 0) if comparable else None
            direction = "unavailable" if before is None else "down" if count < before else "up" if count > before else "stayed"
            categories.append(
                {
                    "category": category,
                    "count": count,
                    "ceiling": record["ceiling"].get(category, 0),
                    "previous": before,
                    "direction": direction,
                }
            )
        return {
            "status": "complete"
            if fresh
            and record.get("attempt", {}).get("status") == "complete"
            and state.get("discovery", {}).get("status") != "unknown"
            else "unknown",
            "at": latest["at"],
            "source": latest["source"],
            "evidence": latest["evidence"],
            "top_categories": categories,
            "increased_categories": sorted(
                category
                for category, count in latest["counts"].items()
                if comparable and count > previous["counts"].get(category, 0)
            ),
            "attempt": record.get("attempt"),
            "discovery": state.get("discovery"),
            "baseline_file": str(path or baseline_path()),
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "unknown", "reason": str(error)}


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True, timeout=120
    ).stdout.strip()


def inspection_worktree(repo: str, root: Path, state: dict[str, Any]) -> Path:
    source = root / repo.split("/")[1]
    remote = git(source, "remote", "get-url", "origin").removesuffix(".git")
    if not remote.endswith((f"github.com/{repo}", f"github.com:{repo}")):
        raise ValueError("local repository origin does not match audit repository")
    git(source, "fetch", "origin")
    base = git(source, "symbolic-ref", "refs/remotes/origin/HEAD")
    entry = state["repositories"].setdefault(repo, {})
    if entry.get("worktree") and Path(entry["worktree"]).exists():
        worktree = Path(entry["worktree"])
        if (
            git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
            != git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
            or git(worktree, "branch", "--show-current") != entry["branch"]
            or git(worktree, "status", "--porcelain", "--untracked-files=all")
        ):
            raise ValueError("weekly worktree identity or clean state changed; preserve it")
        git(worktree, "merge", "--ff-only", base)
        return worktree
    slug = f"weekly-inspection-{dt.datetime.now(dt.UTC):%Y%m%d%H%M%S}"
    created = subprocess.run(
        [str(CATALOG / "skills/github/scripts/dev-worktree"), str(source), slug, base], check=True, capture_output=True, text=True
    )
    worktree = Path(created.stdout.strip().splitlines()[-1])
    if not worktree.is_absolute() or not worktree.is_dir():
        raise ValueError("worktree creator did not return an existing absolute directory")
    entry.update(worktree=str(worktree), branch=f"work/{slug}")
    return worktree


def inspection_helper() -> Any:
    spec = importlib.util.spec_from_file_location("weekly_jb_inspect", Path(__file__).with_name("jb-inspect.py"))
    if spec is None or spec.loader is None:
        raise ValueError("inspection helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def selections(worktree: Path) -> tuple[dict[str, list[str]], dict[str, Any]]:
    helper = inspection_helper()
    metadata = json.loads((worktree / ".github/github.json").read_text())["qualityGate"]["inspection"]
    lanes = helper.parse_inspection_lanes(metadata)
    args = helper.build_parser().parse_args(["agent-inspect", "--scope", "whole_project"])
    policy = {key: metadata.get(key) for key in ("ide", "lanes", "coveredBy")}
    if not lanes:
        paths = helper.inspection_scope_paths(args, worktree, "whole_project")
        native = helper.native_backend_module()
        product = native.common.configured_product_key(helper, metadata, set(paths))
        suffixes = {
            "pycharm": native.proof.NATIVE_PROVEN_SUFFIXES,
            "webstorm": native.proof.WEBSTORM_PROVEN_SUFFIXES,
            "intellij": native.proof.INTELLIJ_PROVEN_SUFFIXES,
        }.get(product)
        if not suffixes:
            raise ValueError("no configured native IDE")
        files = [file for file in paths if Path(file).suffix in suffixes]
        return {"project": files}, {"unmatched": sorted(set(paths) - set(files)), "policy": policy}
    selection = helper.resolve_inspection_lane_selection(
        args, {"scope": "whole_project", "worktree_root": str(worktree), "_inspection_lanes": lanes}
    )
    groups = {lane.lane_id: [entry["file"] for entry in selection["lane_files"][lane.lane_id]] for lane in lanes}
    generated = [file for files in groups.values() for file in files if Path(file).name == "pnpm-lock.yaml"]
    groups = {lane: [file for file in files if file not in generated] for lane, files in groups.items()}
    return groups, {
        "excluded": selection["excluded_files"],
        "unmatched": selection["unmatched_files"],
        "generated_locks": generated,
        "policy": policy,
    }


def inspect_repository(worktree: Path, evidence: Path, now: dt.datetime) -> dict[str, Any]:
    source = git(worktree, "rev-parse", "HEAD")
    groups, omitted = selections(worktree)
    counts: Counter[str] = Counter()
    signatures, results = {}, {}
    for lane, files in groups.items():
        if not files:
            continue
        command = [
            "uv",
            "run",
            str(Path(__file__).with_name("jb-inspect.py")),
            "agent-inspect",
            "--repo",
            str(worktree),
            "--timeout-ms",
            "7200000",
            "--scope",
            "files",
        ]
        for file in files:
            command.extend(["--file", file])
        environment = dict(os.environ, JETBRAINS_INSPECTION_CACHE_DIR=str(evidence / "helper"))
        result = subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
        payload = json.loads(result.stdout)
        save_state(evidence / f"{lane}.json", payload)
        results[lane] = str(evidence / f"{lane}.json")
        counts.update(category_counts(payload))
        native = payload["diagnostic"]["native"]
        signatures[lane] = {key: native.get(key) for key in ("profile_sha256", "helper_revision", "backend_revision", "ide_build")}
    if not signatures:
        raise ValueError("no configured code selections")
    if git(worktree, "rev-parse", "HEAD") != source or git(worktree, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("source changed between lane inspections")
    return {
        "status": "complete",
        "at": now.isoformat(),
        "week": (now.date() - dt.timedelta(days=now.weekday())).isoformat(),
        "source": source,
        "signature": {"lanes": signatures, "selection_policy": omitted["policy"]},
        "selection": omitted,
        "counts": dict(counts),
        "evidence": str(evidence),
        "lanes": results,
    }


def audit_repositories(owner: str) -> list[str]:
    from skills.direction.scripts import direction_audit as audit
    from skills.github.scripts.github_next_reads import NextReads

    with NextReads(workers=4, seconds=0, label="weekly inspection") as reads:
        fetch = lambda args: audit.gh_json(args, gh=str(audit.WRAPPER))
        repos, discovery = audit.weekly_repositories(owner, fetch=fetch, reads=reads)
    if not discovery["complete"]:
        raise ValueError(f"weekly audit repository discovery incomplete: {discovery}")
    return repos


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly project inspections using the existing isolated worker.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=baseline_path())
    parser.add_argument("--repo", action="append", help="bounded initial proof; omit for the weekly audit list")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+", args.owner) or any(
        not re.fullmatch(rf"{re.escape(args.owner)}/[\w.-]+", repo) for repo in args.repo or []
    ):
        parser.error("repositories must belong to --owner and use OWNER/REPO form")
    args.baseline.parent.mkdir(parents=True, exist_ok=True)
    with service_lock(args.baseline, "inspection"):
        state = read_baselines(args.baseline)
        started = dt.datetime.now(dt.UTC)
        try:
            args.evidence_root.resolve(strict=True)
            repos = args.repo or audit_repositories(args.owner)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            state["discovery"] = {"status": "unknown", "at": started.isoformat(), "reason": str(error)}
            save_state(args.baseline, state)
            print(json.dumps(state["discovery"]), flush=True)
            return 0
        state["discovery"] = {"status": "complete", "at": started.isoformat(), "repositories": repos}
        save_state(args.baseline, state)
        for repo in repos:
            evidence = args.evidence_root / started.strftime("%Y%m%dT%H%M%S%f") / repo.replace("/", "--")
            try:
                evidence.mkdir(parents=True, exist_ok=False)
                worktree = inspection_worktree(repo, args.repository_root, state)
                save_state(args.baseline, state)
                run = inspect_repository(worktree, evidence, started)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
                run = {"status": "unknown", "at": started.isoformat(), "evidence": str(evidence), "reason": str(error)}
            record_run(state, repo, run)
            save_state(args.baseline, state)
            if evidence.is_dir():
                save_state(evidence / "summary.json", run)
            print(json.dumps({"repo": repo, **report(repo, path=args.baseline)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
