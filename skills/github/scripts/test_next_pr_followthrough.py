# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

from unittest.mock import Mock, patch

import github_next_pr
import github_plan_claim as claim
import github_plan_wait as wait
import ready_handoff
from test_gh_plan_dependabot import discover, load_module, pull

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
    print("PR follow-through tests passed")
