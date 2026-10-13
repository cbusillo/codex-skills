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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT / "github/scripts"))
from skills.github.scripts import github_api, github_pr_batch
from skills.github.scripts.github_identity import automation_login
from skills.github.scripts.ready_handoff import latest_ready, requires_client_acceptance
from skills.github.scripts.github_read import (
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
review: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
DEPENDENCY_REVIEW = "dependency update needs agent review"


class WatchError(ValueError):
    """A diagnostic authored by this helper, safe to include in events."""


class LaunchplaneReadError(WatchError):
    """A read-only controller failure; no enqueue outcome is implied."""


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
        raise WatchError("hold record missing or invalid")
    for key in ("paused_repositories", "coordinated_repositories"):
        if not isinstance(data[key], list) or any(
            not isinstance(repo, str) or not REPO.fullmatch(repo) for repo in data[key]
        ):
            raise WatchError("invalid hold repository")
    if not isinstance(data["client_releases"], list):
        raise WatchError("invalid Client release holds")
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
            raise WatchError("invalid Client release hold")
    return data


def held(data, repo):
    # A Client release pauses only its own product (Q160, launchplane#3145).
    names = (
        data["paused_repositories"]
        + data["coordinated_repositories"]
        + [item["repository"] for item in data["client_releases"]]
    )
    return repo.casefold() in {name.casefold() for name in names}


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
    return own_queue(result.get("dry_run_result") or {}, own)


def own_queue(dry, own):
    """A fresh queue may contain only this pass's labels with known authors."""
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


def own_candidate(response, own, candidate_records=()):
    """Only this pass's confirmed labels may account for a new candidate."""
    if not own or response.get("status") != "accepted":
        return False
    result = response.get("result") or {}
    if (
        result.get("mode") != "dry-run"
        or result.get("controller_action")
        not in {"plan_candidate", "build_candidate", "observe_candidate"}
        or result.get("controller_reconciliation_status")
        or result.get("error")
        or result.get("active_record_id")
        or result.get("landing_plan")
        or result.get("stack_collapse_plan")
        or any(
            value
            for key, value in result.items()
            if "record_id" in key and key != "merge_train_batch_candidate_record_id"
        )
    ):
        return False
    for key, value in (response.get("records") or {}).items():
        if not value or key == "merge_train_batch_candidate_record_id":
            continue
        if (
            key != "superseded_merge_train_batch_candidate_record_id"
            or value not in candidate_records
            or result.get("controller_action") != "plan_candidate"
        ):
            return False
    candidate = result.get("candidate") or {}
    members = candidate.get("pull_request_numbers")
    return (
        candidate.get("status") in {"planned", "building", "ready_for_checks", "passed"}
        and isinstance(members, list)
        and bool(members)
        and all(
            isinstance(number, int) and not isinstance(number, bool)
            for number in members
        )
        and len(set(members)) == len(members) == candidate.get("entries_count")
        and set(members) <= set(own)
        and (
            "dry_run_result" not in result
            or own_queue(result["dry_run_result"], own)
        )
    )


class Live:
    def __init__(self, deadline):
        self.deadline = deadline
        self.mergeability_reads_remaining = 20
        self.reader = GitHubReader(
            operation="github.pr.view",
            gh_prefix_args=automation_only_gh_prefix_args(),
            cache_enabled=True,
            cache_coalesce_seconds=0,
            cache_revalidate=True,
            deadline_at=deadline,
        )
        self.args = operator.parse_args(["merge-train-policy-read"])
        try:
            self.settings = operator.resolve_settings(self.args)
        except ValueError as error:
            if str(error) not in {"invalid_config", "invalid_env_config"}:
                raise
            raise LaunchplaneReadError("Launchplane admin configuration unavailable") from error
        if not self.settings["service_url"]:
            raise LaunchplaneReadError("Launchplane admin configuration unavailable")
        operator.validate_service_url(self.settings["service_url"])
        if not self.settings["token"]:
            raise LaunchplaneReadError("Launchplane admin configuration unavailable")

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

    def batch(self, repo, numbers):
        if numbers:
            github_pr_batch.try_batch(self.reader, repo, numbers[:20])
        else:
            self.reader.batch_responses.clear()
            self.reader.batch_diagnostic={"used":False,"cost":0,"reason":"no author-allowed PRs"}
        return self.reader.batch_diagnostic

    def get(self, path):
        value = self.reader.get_json(path, step="enqueue_read")
        if not re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/[0-9]+", path):
            return value
        for _ in range(2):
            if value.get("mergeable") is not None:
                break
            if self.mergeability_reads_remaining <= 0 or time.time() + 1 >= self.deadline:
                break
            time.sleep(1)
            self.mergeability_reads_remaining -= 1
            # Discard this PR's batch evidence: REST may find a new head or hold.
            # Preserve other PRs' snapshots and the batch request savings.
            repo_path, number = path.rsplit("/pulls/", 1)
            for endpoint in (
                f"{repo_path}/issues/{number}/comments", f"{path}/reviews",
                f"{repo_path}/commits/{value['head']['sha']}/check-runs",
                f"{repo_path}/commits/{value['head']['sha']}/statuses",
            ):
                self.reader.batch_responses.pop(endpoint, None)
            # An ETag/304 may preserve the null response. Ask GitHub directly,
            # retaining the shared transport's identity, quota and deadline guards.
            value = self.reader.request("GET", path, step="mergeability_read", fresh=True).body
        return value

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
        try:
            payload = json.loads(run.stdout)
        except json.JSONDecodeError as error:
            raise LaunchplaneReadError("controller response invalid") from error
        if not isinstance(payload, dict):
            raise LaunchplaneReadError("controller response invalid")
        if run.returncode:
            summary = payload.get("summary") or {}
            codes = {item["code"] for item in payload.get("warnings", [])}
            status = summary.get("http_status", 0)
            if (
                status in {401, 403}
                or payload.get("status") in {"unauthorized", "denied"}
                or codes & {"authorization_denied", "unsafe_redirect"}
            ):
                raise WatchError("controller read rejected")
            if (
                payload.get("operation") == "merge-train-controller-run-once"
                and payload.get("request") == {
                    "repository": target["repo"],
                    "base_branch": target["base"],
                    "mutate": False,
                }
                and (
                    500 <= status < 600
                    or (
                        payload.get("status") == "unavailable"
                        and codes & {"provider_unavailable", "client_timeout"}
                    )
                )
            ):
                raise ConnectionError("controller transient read failure")
            raise LaunchplaneReadError("controller read unavailable")
        return payload

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

    def qualified(self, target, pr, extra_trusted, agent_review_required=False):
        repo, number = target["repo"], pr["number"]
        if (
            pr.get("state") != "open"
            or pr.get("draft") is not False
            or pr["base"]["ref"] != target["base"]
            or pr.get("mergeable") is not True
        ):
            return False
        labels = {item["name"].casefold() for item in pr["labels"]}
        if target["label"].casefold() in labels:
            return False
        comments = self.pages(f"/repos/{repo}/issues/{number}/comments")
        author = pr["user"]["login"].casefold()
        # The controller's dependency disposition, rather than a bot name,
        # decides whether a distinct trusted agent must review the update.
        trusted = (
            extra_trusted - {author}
            if agent_review_required else {author} | extra_trusted
        )
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
        if requires_client_acceptance(pr, checks["checkRuns"]) and not self.acceptance(repo, pr):
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
            raise WatchError("enqueue write uncertain; reconcile before restarting")
        labels = self.pages(f"/repos/{target['repo']}/issues/{number}/labels")
        if target["label"] not in {item["name"] for item in labels}:
            raise WatchError("enqueue label readback failed")


@dataclass
class PassState:
    write_started: bool = False


def transient_read_error(error):
    if isinstance(error, LaunchplaneReadError):
        return True
    if isinstance(error, urllib.error.HTTPError):
        return 500 <= error.code < 600
    if isinstance(error, GitHubReadError):
        failure = error.result.failure
        # Legacy failures without HTTP status include both transport and setup
        # errors under the shared cause. Require positive transport evidence.
        return bool(
            failure
            and failure.retryable
            and failure.cause in {"deadline_exceeded", "network_provider_failure"}
            and not re.search(
                r"^error:|failed to launch GitHub CLI|GitHub CLI execution failed",
                failure.message,
                re.IGNORECASE | re.MULTILINE,
            )
            and (
                failure.cause == "deadline_exceeded"
                or 500 <= error.result.status < 600
                or re.search(
                    r"connection (?:reset|refused)|timed? out|timeout|"
                    r"\bEOF\b|error connecting to|network is unreachable|dial tcp|"
                    r"temporary failure in name resolution|no such host|"
                    r"GitHub returned a non-JSON provider response",
                    failure.message,
                    re.IGNORECASE,
                )
            )
        )
    return isinstance(
        error,
        (urllib.error.URLError, TimeoutError, ConnectionError, subprocess.TimeoutExpired),
    )


def exception_message(error):
    """Expose local diagnostics without copying provider bodies or private prose."""
    if isinstance(error, WatchError):
        return str(error)
    if isinstance(error, operator.LaunchplaneSafetyError):
        return error.code
    return f"{type(error).__name__}: details withheld"


def needs_ready_handoff(row, target):
    """These two missing prerequisites are proved by qualification before writing."""
    return bool(row["ineligible_reasons"]) and set(row["ineligible_reasons"]) <= {
        f"missing {target['label']} label",
        DEPENDENCY_REVIEW,
    }


def run_pass(io, args, emit, *, state=None):
    state = PassState() if state is None else state
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
                item["number"]: item
                for item in queue
                if needs_ready_handoff(item, target)
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
            held_labels = {
                pr["number"]: sorted(label["name"] for label in pr["labels"])
                for pr in pulls
                if target["label"] in {label["name"] for label in pr["labels"]}
                and any(
                    f"held by {label['name']} label"
                    in dispositions[pr["number"]]["ineligible_reasons"]
                    for label in pr["labels"]
                )
            }
            candidates = []
            if getattr(args, "batch_read", False):
                diagnostic = io.batch(repo, [item["number"] for item in pulls if item["number"] in author_allowed])
                emit("batch_read", {"repository": repo, **diagnostic})
            for listed in pulls:
                if listed["number"] not in author_allowed:
                    continue
                pr = io.get(f"/repos/{repo}/pulls/{listed['number']}")
                if pr.get("mergeable") is not True:
                    emit("skipped", {
                        "repository": repo,
                        "pr": pr["number"],
                        "reason": "conflicting" if pr.get("mergeable") is False
                        else "mergeability_unknown",
                    })
                    continue
                agent_review_required = (
                    DEPENDENCY_REVIEW
                    in author_allowed[pr["number"]]["ineligible_reasons"]
                )
                if io.qualified(target, pr, set(args.handoff_login), agent_review_required):
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
                            "labels": sorted(label["name"] for label in pr["labels"]),
                            "agent_review_required": agent_review_required,
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
            candidate_records = set()
            for item in batch:
                # Recheck policy, pause, active state, exact head and all readiness at each write.
                if target not in io.policies(args.owner) or held(
                    read_holds(args.hold_file), repo
                ):
                    raise WatchError("policy or hold changed")
                fresh = io.train(target)
                pr = io.get(f"/repos/{repo}/pulls/{item['number']}")
                continuing = own_candidate(fresh, own, candidate_records)
                # Candidate observation can omit the queue. Its unchanged policy,
                # selected head and labels retain the scan's author admission;
                # readiness is still read again below before every label write.
                entries = (
                    (fresh.get("result") or {})
                    .get("dry_run_result", {})
                    .get("queue", [])
                )
                allowed = any(
                    row["number"] == item["number"]
                    and row["head_sha"] == item["head"]
                    and needs_ready_handoff(row, target)
                    for row in entries
                )
                if continuing and "dry_run_result" not in (fresh.get("result") or {}):
                    allowed = (
                        sorted(label["name"] for label in pr["labels"]) == item["labels"]
                        and pr.get("mergeable") is True
                    )
                # A scheduler may not yet have incorporated another actor's label
                # into its candidate. Do not extend that actor's queue either.
                if continuing and any(
                    other["number"] not in own
                    and target["label"] in {label["name"] for label in other["labels"]}
                    and sorted(label["name"] for label in other["labels"])
                    != held_labels.get(other["number"])
                    for other in io.pages(f"/repos/{repo}/pulls", state="open")
                ):
                    continuing = False
                if (
                    not (idle(fresh, own) or continuing)
                    or any(not idle(io.train(other)) for other in others)
                    or not allowed
                    or pr["head"]["sha"] != item["head"]
                    or pr.get("mergeable") is not True
                    or not io.qualified(
                        target, pr, set(args.handoff_login),
                        item["agent_review_required"] or any(
                            row["number"] == item["number"]
                            and DEPENDENCY_REVIEW in row["ineligible_reasons"]
                            for row in entries
                        ),
                    )
                ):
                    emit("batch_stopped", {"repository": repo, "labeled": own})
                    break
                if continuing:
                    record = (fresh.get("records") or {}).get(
                        "merge_train_batch_candidate_record_id"
                    )
                    if record:
                        candidate_records.add(record)
                # Mark before dispatch: a raised transport error cannot prove no write.
                state.write_started = True
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
                raise WatchError("hold record already exists")
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
    parser.add_argument("--handoff-login", action="append", default=[automation_login() or "shiny-code-app[bot]"])
    parser.add_argument("--max-batch-size", type=int, default=4)
    parser.add_argument("--interval", type=float, default=300)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-read", action="store_true", help="Explicit cold/changed PR observer batch; default REST preserves idle 304 savings")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.batch_read and not args.once:
        parser.error("--batch-read requires --once; continuous observations retain conditional REST")
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
    completed_passes = failed_passes = 0

    def emit(event, payload):
        print(json.dumps({"event": event, "payload": payload}), flush=True)

    try:
        while time.time() < deadline:
            state = PassState()
            try:
                run_pass(Live(deadline), args, emit, state=state)
            except (
                OSError, GitHubReadError, LaunchplaneReadError, subprocess.TimeoutExpired,
            ) as error:
                if state.write_started or not transient_read_error(error):
                    raise
                emit(
                    "pass_failed",
                    {
                        "cause": type(error).__name__,
                        "message": exception_message(error),
                        "action": "retry next interval",
                    },
                )
                failed_passes += 1
                if args.once:
                    return 1
            else:
                completed_passes += 1
            if args.once:
                break
            time.sleep(min(args.interval, max(0, deadline - time.time())))
        emit(
            "stop",
            {
                "outcome": "complete" if args.once else "deadline",
                "message": "single pass completed" if args.once else "watch deadline reached",
                "completed_passes": completed_passes,
                "failed_passes": failed_passes,
            },
        )
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
                "message": exception_message(error),
                "action": "Supervisor reconciles the failed pass before restarting",
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
