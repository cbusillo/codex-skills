# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
import copy
import io
import json
import urllib.error
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import patch

import pytest
from test_launchplane_helpers import _product_profile_response
from test_launchplane_helpers import write_action as h

DIGEST = "a" * 64
NEW_DIGEST = "b" * 64
RECORD_ID = "c" * 64
PRODUCTION_COMMIT = "1" * 40
CANDIDATE_COMMIT = "2" * 40
OPERATION_KEY = "release-request-1"
REASON = "PRIVATE_REASON_SENTINEL ship the guarded importer"
DECISION = "https://github.com/example/direction/issues/98#issuecomment-20"
OTHER_DECISION = "https://github.com/example/direction/issues/98#issuecomment-21"
CLIENT_APPROVAL = "Client approval of this release is required."
SERVICE_URL = "https://plane.example.invalid"


def patch_attr(name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(h, name, *args, **kwargs)


def plan_args() -> list[str]:
    return ["release-request", "--product", "site", "--plan"]


def request_args(digest: str = DIGEST, decision: str = DECISION, key: str = OPERATION_KEY) -> list[str]:
    return ["release-request", "--product", "site", "--checklist-digest", digest, "--reason", REASON, "--decision-reference", decision, "--idempotency-key", key]


def profile(acceptance: str = "director_standing") -> dict[str, Any]:
    body: dict[str, Any] = copy.deepcopy(_product_profile_response())
    body["profile"]["product"] = "site"
    body["profile"]["release_on_acceptance"] = acceptance
    return body


def review(digest: str = DIGEST, saved: dict[str, object] | None = None, **fields: Any) -> dict[str, Any]:
    status: dict[str, object] = {
        "required": True,
        "approved": False,
        "checklist_complete": True,
        "checklist": {
            "production": {"artifact_id": "private-artifact-production", "source_commit": PRODUCTION_COMMIT},
            "candidate": {
                "artifact_id": "private-artifact-candidate",
                "source_commit": CANDIDATE_COMMIT,
                "runtime_settings": {"keys": ["PRIVATE_SETTING_NAME"]},
            },
            "items": [{"title": "PRIVATE_ITEM_TITLE"}],
        },
        "checklist_digest": digest,
        "blockers": [CLIENT_APPROVAL],
        "latest_decision": None,
        "unavailable_reason": None,
    }
    status.update(fields)
    body: dict[str, Any] = {"trace_id": "trace-read", "product": "site", "review": status, "reviewing_temporary_hotfix": False}
    if saved is not None:
        body["release_request"] = saved
    return body


def saved_request(digest: str = DIGEST, decision: str = DECISION) -> dict[str, object]:
    return {
        "record_id": RECORD_ID,
        "product": "site",
        "checklist_digest": digest,
        "decision_reference": decision,
        "source": "director_request",
        "actor": "private-actor-subject",
        "reason": REASON,
        "decided_at": "2026-11-09T03:00:00+00:00",
    }


def http_error(code: int, error_code: str) -> urllib.error.HTTPError:
    body = json.dumps({"trace_id": "trace-error", "error": {"code": error_code, "message": "PRIVATE_SERVICE_PROSE"}}).encode()
    return urllib.error.HTTPError(SERVICE_URL, code, "error", None, io.BytesIO(body))  # type: ignore[arg-type]


def invoke(
    args: list[str],
    *,
    reviews: list[Any],
    posts: list[Any] | None = None,
    acceptance: str = "director_standing",
) -> tuple[int, dict[str, Any], list[Any]]:
    pending_reviews = iter(reviews)

    def read(**kwargs: Any) -> object:
        if "/product-profiles/" in kwargs["path"]:
            return profile(acceptance)
        reply = next(pending_reviews)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    output = io.StringIO()
    settings = {"service_url": SERVICE_URL, "token": "unused"}
    with (
        patch_attr("prepare_operator_settings", return_value=settings),
        patch_attr("request_launchplane_read", side_effect=read),
        patch_attr("request_launchplane", side_effect=posts or []) as post,
        redirect_stdout(output),
    ):
        code = h.main(args)
    return code, json.loads(output.getvalue()), post.call_args_list


def assert_private_values_absent(payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload)
    for private in (REASON, DECISION, "PRIVATE_ITEM_TITLE", "PRIVATE_SETTING_NAME", "private-artifact", "private-actor-subject", "PRIVATE_SERVICE_PROSE", CLIENT_APPROVAL):
        assert private not in rendered, private


def test_plan_reports_the_exact_checklist_and_sends_nothing() -> None:
    code, payload, posts = invoke(plan_args(), reviews=[review()])
    assert code == 0 and posts == []
    result = payload["result"]
    assert result["checklist_digest"] == DIGEST
    assert result["production"] == {"source_commit": PRODUCTION_COMMIT, "carries_runtime_settings": False}
    assert result["candidate"] == {"source_commit": CANDIDATE_COMMIT, "carries_runtime_settings": True}
    assert result["review_url"] == f"{SERVICE_URL}/ui/owner-review?product=site"
    assert_private_values_absent(payload)


@pytest.mark.parametrize("acceptance", ["held", "promote", "promote_with_rollback_drill"])
@pytest.mark.parametrize("args", [plan_args(), request_args()])
def test_only_director_standing_products_can_request(acceptance: str, args: list[str]) -> None:
    code, payload, posts = invoke(args, reviews=[review()], acceptance=acceptance)
    assert code == 1 and posts == []
    assert payload["status"] == "refused"
    assert payload["summary"]["refusal_reason"] == "not_director_standing"


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"blockers": [CLIENT_APPROVAL, "A second real blocker."]}, "checklist_blocked"),
        ({"blockers": ["A different single blocker."]}, "checklist_blocked"),
        ({"blockers": []}, "checklist_blocked"),
        ({"checklist_complete": False}, "checklist_incomplete"),
        ({"unavailable_reason": "evidence_missing"}, "checklist_unavailable"),
        ({"checklist": None, "checklist_digest": ""}, "checklist_unavailable"),
        ({"approved": True}, "release_already_decided"),
        ({"latest_decision": {"decision": "accepted"}}, "release_already_decided"),
        ({"required": False}, "release_review_not_required"),
        ({"no_new_release": True}, "no_new_release"),
    ],
)
def test_blocked_or_unready_checklists_refuse_before_any_request(fields: dict[str, Any], reason: str) -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(**fields)])
    assert code == 1 and posts == []
    assert payload["status"] == "refused"
    assert payload["summary"]["refusal_reason"] == reason
    assert_private_values_absent(payload)


def test_same_production_and_candidate_is_not_a_release() -> None:
    body = review()
    body["review"]["checklist"]["candidate"] = body["review"]["checklist"]["production"]
    code, payload, posts = invoke(request_args(), reviews=[body])
    assert code == 1 and posts == []
    assert payload["summary"]["refusal_reason"] == "no_new_release"


def test_changed_checklist_refuses_locally_and_requires_a_new_read() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(digest=NEW_DIGEST)])
    assert code == 1 and posts == []
    assert payload["status"] == "stale"
    assert "--plan" in payload["summary"]["recommendation"]


def test_service_checklist_refusal_is_stale_and_never_retried() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[http_error(409, "release_checklist_changed")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "stale"
    assert payload["summary"]["error_code"] == "release_checklist_changed"
    assert_private_values_absent(payload)


def test_request_binds_the_reviewed_digest_decision_and_key() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[review(saved=saved_request())])
    assert code == 0 and len(posts) == 1
    call = posts[0].kwargs
    assert call["path"] == "/v1/release-review/requests"
    assert call["idempotency_key"] == OPERATION_KEY
    assert call["body"] == {"product": "site", "checklist_digest": DIGEST, "reason": REASON, "source": "director_request", "decision_reference": DECISION}
    assert payload["status"] == "ok"
    assert payload["result"]["request_id"] == RECORD_ID
    assert payload["result"]["review_url"] == f"{SERVICE_URL}/ui/owner-review?product=site"
    assert payload["summary"]["idempotency_key"] == OPERATION_KEY
    assert_private_values_absent(payload)


def test_accepted_response_for_another_checklist_is_not_success() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[review(saved=saved_request(digest=NEW_DIGEST))])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "accepted_unverified"
    assert "request_id" not in payload.get("result", {})


def test_a_recorded_request_is_reported_without_sending_again() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(saved=saved_request(), approved=True, latest_decision={"decision": "accepted"})])
    assert code == 0 and posts == []
    assert payload["result"]["request_id"] == RECORD_ID


def test_a_request_recorded_under_another_decision_conflicts() -> None:
    code, payload, posts = invoke(request_args(decision=OTHER_DECISION), reviews=[review(saved=saved_request())])
    assert code == 1 and posts == []
    assert payload["status"] == "conflict"


@pytest.mark.parametrize("failure", [TimeoutError("lost"), http_error(502, "bad_gateway"), ValueError("not json")])
def test_uncertain_outcome_settled_by_readback(failure: BaseException) -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(), review(saved=saved_request())], posts=[failure])
    assert code == 0 and len(posts) == 1
    assert payload["status"] == "ok"
    assert payload["result"]["request_id"] == RECORD_ID
    assert payload["result"]["verified_by_readback"] is True


@pytest.mark.parametrize("readback", [review(), TimeoutError("still lost")])
def test_unproven_outcome_stays_unknown_and_keeps_the_key(readback: object) -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(), readback], posts=[TimeoutError("lost")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "outcome_unknown"
    assert payload["summary"]["idempotency_key"] == OPERATION_KEY
    assert "same" in payload["summary"]["recommendation"]
    assert_private_values_absent(payload)


def test_definite_service_refusal_reads_back_then_refuses() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(), review()], posts=[http_error(409, "release_review_not_opened")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "refused"
    assert_private_values_absent(payload)


def test_denied_request_is_reported_without_retry() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[http_error(403, "release_review_open_denied")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "denied"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(unexpected_field=True),
        lambda body: body["review"].update(checklist_digest="not-a-digest"),
        lambda body: body["review"]["checklist"]["candidate"].update(source_commit="not-a-commit"),
        lambda body: body.update(product="another-product"),
    ],
)
def test_malformed_review_fails_closed_before_any_request(mutate: Any) -> None:
    body = review()
    mutate(body)
    code, payload, posts = invoke(request_args(), reviews=[body])
    assert code == 1 and posts == []
    assert payload["status"] == "invalid"


@pytest.mark.parametrize(
    "args",
    [
        ["release-request", "--product", "site", "--checklist-digest", "short", "--reason", REASON, "--decision-reference", DECISION, "--idempotency-key", OPERATION_KEY],
        ["release-request", "--product", "site", "--checklist-digest", DIGEST, "--reason", " ", "--decision-reference", DECISION, "--idempotency-key", OPERATION_KEY],
        ["release-request", "--product", "site", "--checklist-digest", DIGEST, "--reason", REASON, "--decision-reference", "https://example.invalid/decision", "--idempotency-key", OPERATION_KEY],
        ["release-request", "--product", "site", "--checklist-digest", DIGEST, "--reason", REASON, "--decision-reference", DECISION],
    ],
)
def test_incomplete_requests_stop_before_any_network(args: list[str]) -> None:
    with patch_attr("prepare_operator_settings") as settings:
        output = io.StringIO()
        with redirect_stdout(output):
            code = h.main(args)
    assert code == 2 and not settings.called
    assert json.loads(output.getvalue())["status"] == "invalid"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
