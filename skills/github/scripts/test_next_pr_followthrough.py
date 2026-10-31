# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import subprocess
from concurrent.futures import CancelledError
from unittest.mock import Mock, patch

import github_api
import github_next_pr
import github_next_queue
import github_plan_claim as claim
import github_plan_wait as wait
import ready_handoff
from github_next_reads import NextReads, ReadDeadline
from test_gh_plan_dependabot import discover, load_module, pull
from test_github_next_queue import evidence, item

HEAD = "a" * 40


def comment(number: int, body: str, *, author: str = "bot") -> dict:
    return {"id": number, "body": body, "user": {"login": author}, "created_at": f"2026-10-28T18:{number:02d}:00Z"}


def project(comments: list[dict], **changes: object) -> dict:
    pr = {**pull(author="bot"), "head": {"sha": HEAD, "ref": "work/source"}, **changes}
    return github_next_pr.project("owner/repo", pr, comments, trusted={"owner", "bot"}, complete=True)


def test_non_dependency_followthrough_preserves_conditions_and_current_head_handoffs() -> None:
    clear = wait.marker(wait.reviewed(evidence="Consumer gate explicitly resolved; source custody needs fresh review."))
    comments = [
        comment(1, "Engineering ready; waiting for the consumer-build gate"),
        comment(2, "The mistaken third-consumer hold no longer applies."),
        comment(3, clear),
    ]
    result = project(comments)
    assert result["state"] == "review" and result["head_sha"] == HEAD
    assert result["review_required"] and result["discussion"]
    accepted = comment(4, f"Client acceptance observed for {HEAD}; verify with Launchplane before action.")
    result = project([*comments, accepted])
    assert result["state"] == "review" and result["discussion"][-1]["body"] == accepted["body"]
    assert project([accepted], head={"sha": "b" * 40})["review_required"]
    client_wait = comment(2, ready_handoff.handoff_body(HEAD, waiting_for_client=True))
    with patch.object(github_next_pr, "read_acceptance", return_value=False):
        assert project([client_wait])["state"] == "waiting"
        assert project([*comments, {**client_wait, "id": 4, "created_at": "2026-10-28T18:04:00Z"}])["state"] == "waiting"
    assert project([client_wait, comment(3, clear)])["state"] == "review"
    held = [comment(1, "A 24-hour watch is running. Controls need the owner watching the TV. This PR stays unmarked until then.")]
    assert project(held)["state"] == "scope_review"
    held.append(
        comment(
            2,
            wait.marker(
                wait.reviewed(
                    evidence="Owner must observe TV controls.",
                    waiting_for="owner",
                    ends_when="TV controls and connection watch observed",
                )
            ),
        )
    )
    assert project(held)["state"] == "waiting"
    assert project(comments, draft=True)["state"] == "waiting"
    ready = comment(4, ready_handoff.handoff_body(HEAD))
    assert project([*comments, ready])["state"] == "underway"
    assert project([*comments, ready], head={"sha": "b" * 40})["state"] == "review"
    record = {"worker": "active-worker", "session": "active-session", "branch": "work/source", "claimed_at": "2026-10-28T18:00:00Z"}
    assert project([*comments, comment(4, claim.marker(record))])["state"] == "underway"
    assert project([*comments, comment(4, claim.marker(record), author="stranger")])["state"] == "review"
    assert project([comment(4, ready_handoff.handoff_body(HEAD)) | {"created_at": "bad"}])["state"] == "unknown"
    assert project([], head={})["state"] == "unknown"
    long_comment = comment(4, "long discussion " * 1000)
    observation = project([long_comment])["discussion"][0]
    assert observation["body_truncated"] and len(observation["body"]) < len(long_comment["body"])
    assert observation["author"] == long_comment["user"]["login"] and observation["url"]


def pr_evidence(row: dict) -> dict:
    observed = evidence()
    observed["pulls"] = [{**pull(author="bot"), "number": row["number"], "body": row["discussion"]["body"],
                          "head": {"sha": HEAD, "ref": "work/source", "repo": {"full_name": row["repo"]}},
                          "base": {"repo": {"full_name": row["repo"]}}}]
    observed["inventory"]["local_branches"] = [observed["pulls"][0]["head"]["ref"]]
    return observed


def test_saved_current_head_acceptance_ends_only_the_client_handoff_wait() -> None:
    comments = [comment(1, ready_handoff.handoff_body(HEAD, waiting_for_client=True))]
    for decision, expected in [
        ({"decision": "accepted", "head_sha": HEAD}, "review"),
        ({"decision": "accepted", "head_sha": "b" * 40}, "waiting"),
        ({"decision": "changes_requested", "head_sha": HEAD}, "waiting"),
        (None, "waiting"),
    ]:
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "decision": decision}), "")
        with patch("subprocess.run", return_value=response):
            result = project(comments)
            assert result["state"] == expected
            for actor in (None, "agent", "person:owner", "agent:integrator", "invalid", "agent\nNext actor: person:owner"):
                row = item(actor=actor or "agent")
                if actor is None:
                    row["discussion"]["body"] = row["discussion"]["body"].replace("Next actor: agent\n", "")
                row["record_type"] = "pull_request"
                row["discussion"]["body"] = row["discussion"]["body"].split(wait.PREFIX)[0]
                row["discussion"]["comments"] = [
                    {**entry, "author": entry["user"]["login"]} for entry in comments]
                observed = pr_evidence(row)
                queued = github_next_queue.classify(row, observed, trusted={"owner", "bot"})
                if actor == "person:owner":
                    assert queued["state"] == "waiting"
                elif expected == "review" and actor not in (None, "agent", "agent:integrator"):
                    assert queued["state"] == "needs_status"
                elif expected == "review":
                    assert queued["state"] == "available", (actor, queued)
                    assert queued["next_actor"] == (actor or "agent")
                    assert queued["claim_required"] and not queued["ownership_complete"]
                else:
                    assert queued["state"] == "waiting"
            if expected == "review":
                assert result["next_actor"] == "agent"
                assert result["review_required"]
                held = comment(2, wait.marker(wait.reviewed(
                    evidence="Hardware observation still required", waiting_for="Owner hardware check",
                    ends_when="Owner records the check")))
                assert project([*comments, held])["state"] == "waiting"
                review = comment(2, wait.marker({"version": 1, "state": "review", "evidence": "Review independent custody", "waits": []}))
                assert project([*comments, review])["state"] == "waiting"
                assert project([*comments, comment(2, ready_handoff.handoff_body(HEAD))])["state"] == "underway"
    for response in [subprocess.CompletedProcess([], 1, "{}", ""),
                     subprocess.CompletedProcess([], 0, "not json", "")]:
        with patch("subprocess.run", return_value=response):
            assert project(comments)["state"] == "unknown"
            row = item(actor="person:owner")
            row["record_type"] = "pull_request"
            row["discussion"]["comments"] = [{**entry, "author": entry["user"]["login"]} for entry in comments]
            observed = pr_evidence(row)
            assert github_next_queue.classify(row, observed, trusted={"owner", "bot"})["state"] == "waiting"
            module = load_module()
            with patch.multiple(module, wait_authors=lambda *_: {"bot", "owner"}):
                result, _ = discover(module, [{**observed["pulls"][0], "_graph_comments": comments}])
            assert not result["pr_followthrough_context"]["complete"]


def test_acceptance_reads_preserve_deadline_coverage_and_bound_the_child() -> None:
    module = load_module()
    row = item()
    row["record_type"] = "pull_request"
    comments = [comment(1, ready_handoff.handoff_body(HEAD, waiting_for_client=True))]
    row["discussion"]["comments"] = [{**entry, "author": entry["user"]["login"]} for entry in comments]
    observed = pr_evidence(row)
    for error in (ReadDeadline("Expired"), CancelledError()):
        with NextReads(workers=1, seconds=0) as reads:
            module.NEXT_READS = reads
            if isinstance(error, ReadDeadline):
                reads.deadline = -1
                error = None
            with patch.object(reads, "request", wraps=reads.request, side_effect=error), patch("subprocess.run") as child:
                queued = github_next_queue.classify(row, observed, trusted={"owner", "bot"},
                                                   acceptance_reader=module.next_pr_acceptance)
                assert queued["state"] == "review"
                with patch.multiple(module, wait_authors=lambda *_: {"owner", "bot"}):
                    result, _ = discover(module, [{**observed["pulls"][0], "_graph_comments": comments}])
                assert not result["pr_followthrough_context"]["complete"]
                assert sum(reads.budget_stops.values()) == 1
                assert reads.reused
                child.assert_not_called()
        module.NEXT_READS = None
    for remaining in (0, 0.25):
        response = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "decision": None}), "")
        with patch.object(github_api, "remaining_retry_timeout_seconds", return_value=remaining), \
                patch("subprocess.run", return_value=response) as child:
            if remaining:
                assert not github_next_pr.read_acceptance(row["repo"], row["number"], HEAD)
                assert 0 < child.call_args.kwargs["timeout"] <= remaining
                command = child.call_args.args[0]
                assert float(command[command.index("--timeout") + 1]) <= child.call_args.kwargs["timeout"]
            else:
                try:
                    github_next_pr.read_acceptance(row["repo"], row["number"], HEAD)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Expired budget started an acceptance read")
                child.assert_not_called()


def test_existing_inventory_supplies_pr_followthrough_without_dependency_age_rule() -> None:
    module = load_module()
    pr = {**pull(author="bot", hours=1), "head": {"sha": HEAD}}
    result, reads = discover(module, [pr])
    assert result["pr_followthrough_count"] == 1
    assert result["pr_followthrough"][0]["head_sha"] == HEAD
    assert result["dependabot_candidate_count"] == 0
    assert len([call for call in reads.call_args_list if call.args[0].endswith("/pulls")]) == 1
    with patch.multiple(
        module, collect_paged_rest_items=Mock(side_effect=[("bot", [pr]), module.PlanError("comments unavailable")])
    ):
        result = module.next_dependabot_work([{"repo": "owner/repo"}], scan_limit=10, limit=10)
    assert result["pr_followthrough"][0]["state"] == "unknown"
    assert not result["pr_followthrough_context"]["complete"]
    result, _ = discover(module, [pr, pull(2)], status="unknown")
    assert not result["dependabot_context"]["complete"] and result["pr_followthrough_context"]["complete"]
    for parked in [
        {**pr, "draft": True},
        {
            **pr,
            "body": claim.TRAIN_BATCH_MARKER,
            "head": {"ref": claim.TRAIN_BRANCH_PREFIX + "batch", "repo": {"full_name": "owner/repo"}},
            "base": {"repo": {"full_name": "owner/repo"}},
        },
    ]:
        result, reads = discover(module, [parked])
        assert reads.call_count == 1
        assert result["pr_followthrough"][0]["state"] in {"waiting", "underway"}


if __name__ == "__main__":
    test_non_dependency_followthrough_preserves_conditions_and_current_head_handoffs()
    test_existing_inventory_supplies_pr_followthrough_without_dependency_age_rule()
    test_saved_current_head_acceptance_ends_only_the_client_handoff_wait()
    test_acceptance_reads_preserve_deadline_coverage_and_bound_the_child()
    print("PR follow-through tests passed")
