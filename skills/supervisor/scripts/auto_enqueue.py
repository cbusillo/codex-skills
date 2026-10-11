#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Supervisor-only, bounded auto-enqueueing; Launchplane still owns landing."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import math
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "github/scripts"))
import github_api
from github_read import (
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    automation_only_gh_prefix_args,
    pull_request_checks,
)

LP = ROOT / "launchplane/scripts"
sys.path.insert(0, str(LP))
spec = importlib.util.spec_from_file_location(
    "enqueue_operator", LP / "launchplane-write-action.py"
)
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)
spec = importlib.util.spec_from_file_location(
    "enqueue_review", LP / "launchplane-owner-review.py"
)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
READY = "Ready for the merge train"
WAITING = "Engineering ready; waiting for"
HEAD = re.compile(r"(?im)^Head(?: SHA)?:\s*`?([0-9a-f]{40})`?(?:[.\s]|$)")


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.timestamp()


@contextmanager
def hold_lock(path, *, blocking=False):
    """Pause updates and all local enqueue passes share one persistent inode."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_name(path.name + ".lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def read_holds(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {
        "paused_repositories",
        "coordinated_repositories",
        "client_releases",
    }:
        raise ValueError("hold record missing or invalid")
    for key in ("paused_repositories", "coordinated_repositories"):
        if not isinstance(data[key], list) or any(
            not isinstance(repo, str) or not REPO.fullmatch(repo) for repo in data[key]
        ):
            raise ValueError("invalid hold repository")
    if not isinstance(data["client_releases"], list):
        raise ValueError("invalid Client release holds")
    for release in data["client_releases"]:
        if (
            not isinstance(release, dict)
            or set(release) != {"repository", "status"}
            or not (
                isinstance(release["repository"], str)
                and REPO.fullmatch(release["repository"])
                and release["status"] in {"ready", "open"}
            )
        ):
            raise ValueError("invalid Client release hold")
    return data


def held(data, repo):
    # A Client release pauses only its own product (Q160, launchplane#3145).
    names = (
        data["paused_repositories"]
        + data["coordinated_repositories"]
        + [item["repository"] for item in data["client_releases"]]
    )
    return repo.casefold() in {name.casefold() for name in names}


def latest_ready(pr, comments, trusted):
    """A renewed wait invalidates Ready; a push requires a new explicit head."""
    handoffs = []
    for comment in comments:
        if (comment.get("user") or {}).get("login", "").casefold() not in trusted:
            continue
        body = comment.get("body") or ""
        first = body.splitlines()[0] if body.splitlines() else ""
        if first == READY or first.startswith(WAITING):
            handoffs.append(comment)
    if not handoffs:
        return False
    latest = max(
        handoffs,
        key=lambda item: (
            timestamp(item.get("updated_at") or item["created_at"]),
            item["id"],
        ),
    )
    body = latest["body"]
    heads = HEAD.findall(body)
    return body.splitlines()[0] == READY and heads == [pr["head"]["sha"]]


def areas(files):
    result = set()
    for item in files:
        for name in (item["filename"], item.get("previous_filename")):
            if name:
                result.add(name.split("/", 1)[0] if "/" in name else "<root>")
    return result


def group(candidates, maximum):
    """Oldest ready PR anchors a group with shared top-level areas."""
    ordered = sorted(candidates, key=lambda item: (item["created_at"], item["number"]))
    if not ordered:
        return []
    anchor = ordered[0]
    return [anchor] + [item for item in ordered[1:] if item["areas"] & anchor["areas"]][
        : maximum - 1
    ]


def idle(response, own=()):
    """Only fresh queue phases prove no stored candidate/landing is active."""
    if response.get("status") != "accepted":
        return False
    result = response.get("result") or {}
    if any((response.get("records") or {}).values()):
        return False
    allowed_actions = {"idle", "wait_for_checks", "plan_candidate"}
    if own:
        allowed_actions.add("update_branch")
    if (
        result.get("mode") != "dry-run"
        or result.get("controller_action") not in allowed_actions
    ):
        return False
    if any(value for key, value in result.items() if "record_id" in key) or result.get(
        "controller_reconciliation_status"
    ):
        return False
    dry = result.get("dry_run_result") or {}
    if not isinstance(dry.get("queue"), list) or not isinstance(
        dry.get("queue_order"), list
    ):
        return False
    if any(
        reason.endswith(" label ignored: could not read who applied it")
        for item in dry["queue"]
        for reason in item["ineligible_reasons"]
    ):
        return False
    # Even queued work not yet built belongs to its existing routing actor.
    return set(dry["queue_order"]) <= set(own)


class Live:
    def __init__(self, deadline):
        self.deadline = deadline
        self.reader = GitHubReader(
            operation="github.pr.view",
            gh_prefix_args=automation_only_gh_prefix_args(),
            cache_enabled=True,
            cache_coalesce_seconds=0,
            cache_revalidate=True,
            deadline_at=deadline,
        )
        self.args = operator.parse_args(["merge-train-policy-read"])
        self.settings = operator.resolve_settings(self.args)
        operator.validate_service_url(self.settings["service_url"])
        if not self.settings["token"]:
            raise ValueError("Launchplane admin configuration unavailable")

    def policies(self, owner):
        payload = operator.request_launchplane_read(
            service_url=self.settings["service_url"],
            settings=self.settings,
            path=operator.helper_command_path("merge-train-policy-read"),
            query={},
            timeout=10,
        )
        policies = payload["record"]["policy"]["policies"]
        # Reuse the maintained projection's full policy validation for every scoped repo.
        names = {
            item["repository"]
            for item in policies
            if item["repository"].split("/")[0].casefold() == owner.casefold()
        }
        targets = []
        for repo in sorted(names):
            projected = operator.summarize_merge_train_policy_read(
                request={"repository": repo}, provider_payload=payload
            )["result"]
            for target in projected["targets"]:
                targets.append(
                    {
                        "repo": repo,
                        "base": target["baseBranch"],
                        "label": target["readyLabel"],
                        "policy": projected["policy"],
                    }
                )
        return targets

    def get(self, path):
        return self.reader.get_json(path, step="enqueue_read")

    def pages(self, path, **params):
        return self.reader.paged_json(path, step_prefix="enqueue_read", params=params)

    def train(self, target):
        remaining = self.deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("watch deadline")
        run = subprocess.run(
            [
                "uv",
                "run",
                str(LP / "launchplane-write-action.py"),
                "merge-train-controller-run-once",
                "--repo",
                target["repo"],
                "--base-branch",
                target["base"],
            ],
            capture_output=True,
            text=True,
            timeout=min(180, remaining),
        )
        if run.returncode:
            raise ValueError("controller read unavailable")
        return json.loads(run.stdout)

    def acceptance(self, repo, pr):
        args = argparse.Namespace(**vars(self.args), pr=pr["number"], decision_id="")
        args.repo = repo
        try:
            decision = review.read_review(args)
        except urllib.error.HTTPError as error:
            if error.code == 403:
                return False
            raise
        return bool(
            decision
            and decision["decision"] == "accepted"
            and decision["head_sha"] == pr["head"]["sha"]
        )

    def qualified(self, target, pr, extra_trusted):
        repo, number = target["repo"], pr["number"]
        if (
            pr.get("state") != "open"
            or pr.get("draft") is not False
            or pr["base"]["ref"] != target["base"]
        ):
            return False
        labels = {item["name"].casefold() for item in pr["labels"]}
        if target["label"].casefold() in labels:
            return False
        comments = self.pages(f"/repos/{repo}/issues/{number}/comments")
        trusted = {pr["user"]["login"].casefold()} | extra_trusted
        if not latest_ready(pr, comments, trusted):
            return False
        reviews = self.pages(f"/repos/{repo}/pulls/{number}/reviews")
        newest = {}
        for item in sorted(reviews, key=lambda value: value["id"]):
            if item.get("state") != "COMMENTED":
                newest[(item.get("user") or {}).get("login")] = item
        if any(
            item.get("state") == "CHANGES_REQUESTED"
            and item.get("commit_id") == pr["head"]["sha"]
            for item in newest.values()
        ):
            return False
        checks = pull_request_checks(
            self.reader, repo, number, head_sha=pr["head"]["sha"]
        )
        summary = checks["summary"]
        if (
            not summary["countsComplete"]
            or summary["unavailableComponents"]
            or (
                summary["failingCount"]
                or summary["pendingCount"]
                or summary["unfinishedWorkflowRunCount"]
            )
        ):
            return False
        protection = self.get(
            f"/repos/{repo}/branches/{quote(target['base'], safe='')}/protection/required_status_checks"
        )
        requirements = protection.get("checks")
        if not isinstance(requirements, list) or not requirements:
            return False
        for required in requirements:
            found = [
                item
                for item in checks["checkRuns"]
                if item["name"] == required["context"]
                and (
                    required.get("app_id") in {None, -1}
                    or item.get("appId") == required["app_id"]
                )
            ]
            statuses = [
                item
                for item in checks["statuses"]
                if item["context"] == required["context"]
            ]
            if not (
                found
                and all(
                    item["status"] == "completed"
                    and item["conclusion"] in {"success", "neutral", "skipped"}
                    for item in found
                )
            ):
                if (
                    required.get("app_id") not in {None, -1}
                    or not statuses
                    or any(item["state"] != "success" for item in statuses)
                ):
                    return False
        needs_acceptance = "owner-review" in labels or any(
            item["name"] == "launchplane/owner-review" for item in checks["checkRuns"]
        )
        # Real Client notes without their review label stay on the coordinated path (Q155).
        if re.search(r"(?im)^## (?:Client|Owner) test notes\s*$", pr.get("body") or ""):
            if not re.search(
                r"(?im)^Nothing for the Client to test:\s*\S", pr.get("body") or ""
            ):
                needs_acceptance = True
        if needs_acceptance and not self.acceptance(repo, pr):
            return False
        return True

    def label(self, target, number):
        result = github_api.call_gh_with_retry(
            "POST",
            f"/repos/{target['repo']}/issues/{number}/labels",
            {"labels": [target["label"]]},
            operation="github.pr.edit",
            gh_prefix_args=automation_only_gh_prefix_args(),
            deadline_at=self.deadline,
        )
        if (
            not result.ok
            or result.retry_summary is None
            or result.retry_summary.outcome_certainty
            not in {
                "confirmed",
                "reconciled_applied",
            }
        ):
            raise ValueError("enqueue write uncertain; reconcile before restarting")
        labels = self.pages(f"/repos/{target['repo']}/issues/{number}/labels")
        if target["label"] not in {item["name"] for item in labels}:
            raise ValueError("enqueue label readback failed")


def run_pass(io, args, emit):
    targets = io.policies(args.owner)
    selected = {repo.casefold() for repo in args.repo}
    for target in targets:
        repo = target["repo"]
        if selected and repo.casefold() not in selected:
            continue
        with hold_lock(args.hold_file):
            if held(read_holds(args.hold_file), repo):
                emit("held", {"repository": repo})
                continue
            response = io.train(target)
            others = [
                other
                for other in targets
                if other["repo"].casefold() == repo.casefold() and other != target
            ]
            responses = [response] + [io.train(other) for other in others]
            if any(not idle(value) for value in responses):
                busy_result = response.get("result") or {}
                emit(
                    "busy",
                    {
                        "repository": repo,
                        "action": busy_result.get("controller_action"),
                        "queue": busy_result.get("dry_run_result", {}).get("queue", []),
                    },
                )
                continue
            queue = response["result"]["dry_run_result"]["queue"]
            author_allowed = {
                item["number"]
                for item in queue
                if item["ineligible_reasons"] == [f"missing {target['label']} label"]
            }
            pulls = io.pages(f"/repos/{repo}/pulls", state="open")
            # Active candidates were checked above. Only controller-proven
            # ineligible PRs can be ignored here; absent or stale inventory stays
            # busy. Include other enrolled bases without dropping stack children.
            dispositions = {
                item["number"]: item
                for value in responses
                for item in value["result"]["dry_run_result"]["queue"]
            }
            labeled = [
                pr["number"]
                for pr in pulls
                if target["label"] in {label["name"] for label in pr["labels"]}
                and (
                    not dispositions.get(pr["number"], {}).get("ineligible_reasons")
                    or f"missing {target['label']} label"
                    in dispositions[pr["number"]]["ineligible_reasons"]
                )
            ]
            if labeled:
                emit(
                    "busy",
                    {
                        "repository": repo,
                        "labeled": labeled,
                        "queue": [item for item in queue if item["number"] in labeled],
                    },
                )
                continue
            candidates = []
            for listed in pulls:
                if listed["number"] not in author_allowed:
                    continue
                pr = io.get(f"/repos/{repo}/pulls/{listed['number']}")
                if io.qualified(target, pr, set(args.handoff_login)):
                    files = io.pages(f"/repos/{repo}/pulls/{pr['number']}/files")
                    if len(files) != pr["changed_files"] or not files:
                        emit(
                            "skipped",
                            {
                                "repository": repo,
                                "pr": pr["number"],
                                "reason": "file_inventory_changed_or_incomplete",
                            },
                        )
                        continue
                    candidates.append(
                        {
                            "number": pr["number"],
                            "head": pr["head"]["sha"],
                            "created_at": pr["created_at"],
                            "areas": areas(files),
                        }
                    )
                else:
                    emit(
                        "skipped",
                        {
                            "repository": repo,
                            "pr": pr["number"],
                            "reason": "current_head_readiness_not_proven",
                        },
                    )
            batch = group(candidates, args.max_batch_size)
            emit(
                "batch",
                {
                    "repository": repo,
                    "base": target["base"],
                    "prs": [item["number"] for item in batch],
                    "apply": args.apply,
                },
            )
            if not args.apply or not batch:
                continue
            own = []
            for item in batch:
                # Recheck policy, pause, active state, exact head and all readiness at each write.
                if target not in io.policies(args.owner) or held(
                    read_holds(args.hold_file), repo
                ):
                    raise ValueError("policy or hold changed")
                fresh = io.train(target)
                pr = io.get(f"/repos/{repo}/pulls/{item['number']}")
                entries = (
                    (fresh.get("result") or {})
                    .get("dry_run_result", {})
                    .get("queue", [])
                )
                allowed = any(
                    row["number"] == item["number"]
                    and row["head_sha"] == item["head"]
                    and row["ineligible_reasons"]
                    == [f"missing {target['label']} label"]
                    for row in entries
                )
                if (
                    not idle(fresh, own)
                    or any(not idle(io.train(other)) for other in others)
                    or not allowed
                    or pr["head"]["sha"] != item["head"]
                    or not io.qualified(target, pr, set(args.handoff_login))
                ):
                    emit("batch_stopped", {"repository": repo, "labeled": own})
                    break
                io.label(target, item["number"])
                own.append(item["number"])
                emit(
                    "enqueued",
                    {"repository": repo, "pr": item["number"], "head": item["head"]},
                )
            if own:
                emit(
                    "driver_handoff",
                    {
                        "repository": repo,
                        "prs": own,
                        "argv": [
                            "uv",
                            "run",
                            str(LP / "launchplane-train-drive.py"),
                            "--repo",
                            repo,
                            "--base-branch",
                            target["base"],
                            "--pr",
                            str(own[0]),
                        ],
                    },
                )


def edit_holds(argv):
    parser = argparse.ArgumentParser(
        description="Update the Supervisor's single pause record under the enqueue lock."
    )
    parser.add_argument("--hold-file", required=True, type=Path)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--coordinated-repo", action="append", default=[])
    parser.add_argument("--pause", action="append", default=[])
    parser.add_argument("--resume", action="append", default=[])
    parser.add_argument("--release-ready", action="append", default=[])
    parser.add_argument("--release-finished", action="append", default=[])
    args = parser.parse_args(argv)
    names = (
        args.coordinated_repo
        + args.pause
        + args.resume
        + args.release_ready
        + args.release_finished
    )
    if any(not REPO.fullmatch(name) for name in names):
        parser.error("invalid repository")
    with hold_lock(args.hold_file, blocking=True):
        if args.init:
            if args.hold_file.exists():
                raise ValueError("hold record already exists")
            data = {
                "paused_repositories": [],
                "coordinated_repositories": [],
                "client_releases": [],
            }
        else:
            data = read_holds(args.hold_file)
        data["coordinated_repositories"] = sorted(
            set(data["coordinated_repositories"] + args.coordinated_repo)
        )
        pauses = {
            name.casefold(): name for name in data["paused_repositories"] + args.pause
        }
        resumes = {name.casefold() for name in args.resume}
        data["paused_repositories"] = sorted(
            name for key, name in pauses.items() if key not in resumes
        )
        finished = {name.casefold() for name in args.release_finished}
        releases = {
            item["repository"]: item
            for item in data["client_releases"]
            if item["repository"].casefold() not in finished
        }
        releases.update(
            {
                name: {"repository": name, "status": "ready"}
                for name in args.release_ready
            }
        )
        data["client_releases"] = list(releases.values())
        with tempfile.NamedTemporaryFile(
            mode="w", dir=args.hold_file.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
        try:
            temporary.replace(args.hold_file)
        finally:
            temporary.unlink(missing_ok=True)
    print(json.dumps({"event": "holds_updated"}))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "holds":
        return edit_holds(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--hold-file", required=True, type=Path)
    parser.add_argument("--handoff-login", action="append", default=[])
    parser.add_argument("--max-batch-size", type=int, default=4)
    parser.add_argument("--interval", type=float, default=300)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.owner or any(
        not REPO.fullmatch(repo)
        or repo.split("/")[0].casefold() != args.owner.casefold()
        for repo in args.repo
    ):
        parser.error("repositories must belong to the configured Director")
    if args.max_batch_size < 1 or any(
        not math.isfinite(value) or value <= 0
        for value in (args.interval, args.deadline_seconds)
    ):
        parser.error("batch size and finite timings must be positive")
    args.handoff_login = [login.casefold() for login in args.handoff_login]
    deadline = time.time() + args.deadline_seconds

    def emit(event, payload):
        print(json.dumps({"event": event, "payload": payload}), flush=True)

    try:
        while time.time() < deadline:
            run_pass(Live(deadline), args, emit)
            if args.once:
                break
            time.sleep(min(args.interval, max(0, deadline - time.time())))
        emit("stop", {"outcome": "complete" if args.once else "deadline"})
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        GitHubReadError,
        GitHubReadShapeError,
        subprocess.SubprocessError,
        operator.LaunchplaneSafetyError,
    ) as error:
        # Provider bodies, credentials and private Client prose never reach output.
        emit(
            "stop",
            {
                "outcome": "error",
                "cause": type(error).__name__,
                "action": "Supervisor reconciles the failed pass before restarting",
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
