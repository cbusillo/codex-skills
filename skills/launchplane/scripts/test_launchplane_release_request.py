# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///
import copy
import importlib
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from launchplane_release_request import COMMAND, RECOVERY, REQUEST_SOURCE, REVIEW_PAGE
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
OTHER_REASON = "A different recorded reason"
SERVICE_URL = "https://plane.example.invalid"
REVIEW_URL = f"{SERVICE_URL}{REVIEW_PAGE}?product=site"


def patch_attr(name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(h, name, *args, **kwargs)


def plan_args() -> list[str]:
    return ["release-request", "--product", "site", "--plan"]


def request_args(digest: str = DIGEST, decision: str = DECISION, key: str = OPERATION_KEY) -> list[str]:
    return ["release-request", "--product", "site", "--checklist-digest", digest, "--reason", REASON, "--decision-reference", decision, "--idempotency-key", key]


def profile(acceptance: str = "promote_with_rollback_drill") -> dict[str, Any]:
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
        "blockers": ["PRIVATE_CLIENT_APPROVAL_BLOCKER"],
        "latest_decision": None,
        "unavailable_reason": None,
    }
    status.update(fields)
    body: dict[str, Any] = {"trace_id": "trace-read", "product": "site", "review": status, "reviewing_temporary_hotfix": False}
    if saved is not None:
        body["release_request"] = saved
    return body


def saved_request(digest: str = DIGEST, decision: str = DECISION, reason: str = REASON) -> dict[str, object]:
    return {
        "record_id": RECORD_ID,
        "product": "site",
        "checklist_digest": digest,
        "decision_reference": decision,
        "source": REQUEST_SOURCE,
        "actor": "private-actor-subject",
        "reason": reason,
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
    acceptance: str = "promote_with_rollback_drill",
    read_calls: list[Any] | None = None,
) -> tuple[int, dict[str, Any], list[Any]]:
    pending_reviews = iter(reviews)

    def read(**kwargs: Any) -> object:
        if read_calls is not None:
            read_calls.append(kwargs)
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
    for private in (REASON, DECISION, "PRIVATE_ITEM_TITLE", "PRIVATE_SETTING_NAME", "private-artifact", "private-actor-subject", "PRIVATE_SERVICE_PROSE", "PRIVATE_CLIENT_APPROVAL_BLOCKER"):
        assert private not in rendered, private


def test_plan_reports_the_exact_checklist_and_sends_nothing() -> None:
    code, payload, posts = invoke(plan_args(), reviews=[review()])
    assert code == 0 and posts == []
    result = payload["result"]
    assert result["checklist_digest"] == DIGEST
    assert result["production"] == {"source_commit": PRODUCTION_COMMIT, "carries_runtime_settings": False}
    assert result["candidate"] == {"source_commit": CANDIDATE_COMMIT, "carries_runtime_settings": True}
    assert result["review_url"] == REVIEW_URL
    assert_private_values_absent(payload)


@pytest.fixture
def private_result(tmp_path: Path) -> Path:
    directory = tmp_path / "task-evidence"
    directory.mkdir()
    return directory / "review.json"


@pytest.mark.parametrize("complete", [True, False])
def test_private_plan_preserves_full_review_before_public_projection(private_result: Path, complete: bool) -> None:
    body = review(checklist_complete=complete)
    body["review"]["checklist"]["candidate"].update(shared_addons_digest="d" * 64, website_settings_digest="e" * 64)
    code, payload, posts = invoke([*plan_args(), "--result-file", str(private_result)], reviews=[body])
    assert code == (0 if complete else 1) and posts == []
    assert json.loads(private_result.read_text()) == body
    assert private_result.stat().st_mode & 0o777 == 0o600
    assert_private_values_absent(payload)
    baseline_code, baseline, _ = invoke(plan_args(), reviews=[body])
    assert baseline_code == code
    for key in ("status", "result", "summary", "warnings", "request"):
        assert payload[key] == baseline[key]
    if not complete:
        assert payload["summary"]["refusal_reason"] == "checklist_incomplete"


@pytest.mark.parametrize("kind", ["existing", "symlink", "parent_symlink", "repository", "outside_evidence", "missing_parent"])
def test_private_plan_rejects_unsafe_destination_before_transport(private_result: Path, kind: str) -> None:
    destination = private_result
    if kind == "existing":
        destination.write_text("preserve")
    elif kind == "symlink":
        target = destination.with_name("original")
        target.write_text("preserve")
        destination.symlink_to(target)
    elif kind == "parent_symlink":
        link = destination.parent.with_name("redirect")
        link.symlink_to(destination.parent, target_is_directory=True)
        destination = link / destination.name
    elif kind == "outside_evidence":
        destination = destination.parent.parent / destination.name
    elif kind == "missing_parent":
        destination = destination.parent / "missing" / destination.name
    reads: list[Any] = []
    caller = importlib.import_module("launchplane-call")
    repository = private_result.parent.parent if kind == "repository" else Path(__file__).resolve().parent
    with patch.object(caller.operator, "active_repo_root", return_value=repository):
        code, payload, posts = invoke([*plan_args(), "--result-file", str(destination)], reviews=[], read_calls=reads)
    assert reads == []
    assert code != 0 and posts == []
    assert payload["status"] != "outcome_unknown"
    if kind in {"existing", "symlink"}:
        assert destination.read_text() == "preserve"


def test_result_file_cannot_be_used_to_send_a_request(private_result: Path) -> None:
    reads: list[Any] = []
    code, _, posts = invoke([*request_args(), "--result-file", str(private_result)], reviews=[review()], read_calls=reads)
    assert reads == []
    assert code != 0 and posts == [] and not private_result.exists()


def test_private_plan_reserves_destination_before_authenticated_gets(private_result: Path) -> None:
    calls: list[str] = []

    def transport(request: urllib.request.Request, **_kwargs: Any) -> io.BytesIO:
        assert private_result.exists() and private_result.stat().st_mode & 0o777 == 0o600
        assert request.get_method() == "GET" and request.data is None
        assert request.get_header("Authorization") == "Bearer fictional-token"
        path = urllib.parse.urlsplit(request.full_url).path
        calls.append(path)
        body = profile() if "/product-profiles/" in path else review()
        return io.BytesIO(json.dumps(body).encode())

    settings = {"service_url": SERVICE_URL, "token": "fictional-token"}
    with patch_attr("prepare_operator_settings", return_value=settings), patch_attr("safe_urlopen", side_effect=transport):
        with redirect_stdout(io.StringIO()):
            assert h.main([*plan_args(), "--result-file", str(private_result)]) == 0
    assert calls == [h.helper_command_path("product-profile-read").format(product="site"), h.internal_helper_path("release-review-read")]
    assert json.loads(private_result.read_text()) == review()


@pytest.mark.parametrize("error", [http_error(403, "authorization_denied"), TimeoutError("PRIVATE_SERVICE_PROSE")])
def test_private_plan_read_failure_is_not_an_unknown_write(private_result: Path, error: Exception) -> None:
    code, payload, posts = invoke([*plan_args(), "--result-file", str(private_result)], reviews=[error])
    assert code == 1 and posts == []
    assert payload["status"] in {"denied", "unavailable"}
    assert private_result.read_bytes() == b""
    assert_private_values_absent(payload)


def test_private_save_failure_never_claims_a_success_or_unknown_write(private_result: Path) -> None:
    caller = importlib.import_module("launchplane-call")
    with patch.object(caller, "save_evidence", return_value=False):
        code, payload, posts = invoke([*plan_args(), "--result-file", str(private_result)], reviews=[review()])
    assert code == 1 and posts == [] and payload["status"] == "unavailable"
    assert_private_values_absent(payload)


@pytest.mark.parametrize("unknown_field", [False, True])
def test_private_save_uses_existing_field_contract_before_public_projection(private_result: Path, unknown_field: bool) -> None:
    body = review()
    if unknown_field:
        body["unexpected_producer_field"] = "private"
    else:
        body["review"]["checklist"]["candidate"]["source_commit"] = "invalid"
    code, payload, posts = invoke([*plan_args(), "--result-file", str(private_result)], reviews=[body])
    assert code == 1 and posts == []
    assert payload["status"] != "outcome_unknown"
    if unknown_field:
        assert private_result.read_bytes() == b""
    else:
        assert json.loads(private_result.read_text()) == body


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"checklist_complete": False}, "checklist_incomplete"),
        ({"unavailable_reason": "evidence_missing"}, "checklist_unavailable"),
        ({"checklist": None, "checklist_digest": ""}, "checklist_unavailable"),
        ({"approved": True}, "release_already_decided"),
        ({"latest_decision": {"decision": "accepted"}}, "release_already_decided"),
        ({"required": False}, "release_review_not_required"),
        ({"no_new_release": True}, "no_new_release"),
    ],
)
def test_plan_reports_current_review_prerequisites(fields: dict[str, Any], reason: str) -> None:
    code, payload, posts = invoke(plan_args(), reviews=[review(**fields)])
    assert code == 1 and posts == []
    assert payload["status"] == "refused"
    assert payload["summary"]["refusal_reason"] == reason
    assert_private_values_absent(payload)


def test_same_production_and_candidate_is_not_a_release() -> None:
    body = review()
    body["review"]["checklist"]["candidate"] = body["review"]["checklist"]["production"]
    code, payload, posts = invoke(plan_args(), reviews=[body])
    assert code == 1 and posts == []
    assert payload["summary"]["refusal_reason"] == "no_new_release"


def test_changed_checklist_refuses_locally_and_requires_a_new_read() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(digest=NEW_DIGEST)])
    assert code == 1 and posts == []
    assert payload["status"] == "stale"


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
    assert call["path"] == h.helper_command_path(COMMAND)
    assert call["idempotency_key"] == OPERATION_KEY
    assert call["body"] == {
        "product": "site",
        "checklist_digest": DIGEST,
        "reason": REASON,
        "source": REQUEST_SOURCE,
        "decision_reference": DECISION,
    }
    assert payload["status"] == "ok"
    assert payload["result"]["invitation_delivery"] == "confirmed"
    assert payload["result"]["request_id"] == RECORD_ID
    assert payload["result"]["review_url"] == REVIEW_URL
    assert payload["summary"]["idempotency_key"] == OPERATION_KEY
    assert_private_values_absent(payload)


def test_accepted_response_for_another_checklist_is_not_success() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[review(saved=saved_request(digest=NEW_DIGEST))])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "accepted_unverified"
    assert "request_id" not in payload.get("result", {})


@pytest.mark.parametrize("acceptance", ["promote", "director_standing"])
@pytest.mark.parametrize("decision", ["accepted", "changes_requested"])
def test_a_decided_request_is_observed_without_sending_again(acceptance: str, decision: str) -> None:
    code, payload, posts = invoke(
        request_args(),
        reviews=[review(saved=saved_request(), latest_decision={"decision": decision})],
        acceptance=acceptance,
    )
    assert code == 0 and posts == []
    assert payload["result"]["review_decided"] is True
    assert payload["summary"]["refusal_reason"] == "release_already_decided"
    assert payload["result"]["request_recorded"] is True
    assert payload["result"]["request_id"] == RECORD_ID


@pytest.mark.parametrize("lost_response", [False, True])
def test_saved_request_cannot_report_current_candidate_for_a_changed_checklist(lost_response: bool) -> None:
    current = review(digest=NEW_DIGEST, saved=saved_request())
    code, payload, posts = invoke(
        request_args(),
        reviews=[review(), current] if lost_response else [current],
        posts=[TimeoutError("lost")] if lost_response else None,
    )
    assert code == 1 and len(posts) == int(lost_response)
    assert payload["status"] == "stale"
    assert "candidate" not in payload.get("result", {})
    assert payload["result"]["request_id"] == RECORD_ID
    assert payload["result"]["request_recorded"] is True


@pytest.mark.parametrize(
    "recorded",
    [saved_request(decision=OTHER_DECISION), saved_request(reason=OTHER_REASON), {**saved_request(), "source": "finish_line"}],
)
def test_a_request_recorded_with_another_decision_or_reason_conflicts(recorded: dict[str, object]) -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(saved=recorded)])
    assert code == 1 and posts == []
    assert payload["status"] == "conflict"
    assert "request_id" not in payload.get("result", {})


def test_service_keeping_an_earlier_reason_is_not_reported_as_this_request() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[review(saved=saved_request(reason=OTHER_REASON))])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "conflict"
    assert OTHER_REASON not in json.dumps(payload)


@pytest.mark.parametrize("acceptance", ["promote", "director_standing"])
@pytest.mark.parametrize(
    "failure",
    [
        lambda: TimeoutError("lost"),
        lambda: http_error(502, "bad_gateway"),
        lambda: ValueError("not json"),
        lambda: http_error(409, "release_review_not_opened"),
    ],
)
def test_uncertain_outcome_settled_by_readback(failure: Any, acceptance: str) -> None:
    code, payload, posts = invoke(
        request_args(), reviews=[review(), review(saved=saved_request())], posts=[failure()], acceptance=acceptance
    )
    assert code == (0 if acceptance == "director_standing" else 1) and len(posts) == 1
    assert payload["status"] == ("ok" if acceptance == "director_standing" else "accepted_unverified")
    if acceptance != "director_standing":
        assert payload["result"]["invitation_delivery"] == "unverified"
    assert payload["result"]["request_id"] == RECORD_ID
    assert payload["result"]["verified_by_readback"] is True


@pytest.mark.parametrize("readback", [review(), TimeoutError("still lost")])
def test_unproven_outcome_stays_unknown_and_keeps_the_key(readback: object) -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(), readback], posts=[TimeoutError("lost")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "outcome_unknown"
    assert payload["summary"]["idempotency_key"] == OPERATION_KEY
    assert_private_values_absent(payload)


@pytest.mark.parametrize(
    "recorded",
    [saved_request(decision=OTHER_DECISION), saved_request(reason=OTHER_REASON), {**saved_request(), "source": "finish_line"}],
)
@pytest.mark.parametrize("make_failure", [lambda: TimeoutError("lost"), lambda: http_error(409, "release_review_not_opened")])
def test_readback_showing_a_different_request_is_a_conflict(recorded: dict[str, object], make_failure: Any) -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(), review(saved=recorded)], posts=[make_failure()])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "conflict"
    assert "request_id" not in payload.get("result", {})


def test_definite_service_refusal_reads_back_then_refuses() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review(), review()], posts=[http_error(409, "release_review_not_opened")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "refused"
    assert_private_values_absent(payload)


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("fields,hotfix,prerequisite", [
    ({"approved": True, "latest_decision": {"decision": "accepted"}}, True, "temporary_hotfix_pending"),
    ({"checklist": None, "checklist_digest": "", "unavailable_reason": "missing_evidence"}, False, "checklist_unavailable"),
])
def test_saved_request_with_current_prerequisite_keeps_its_identity(fields: dict[str, Any], hotfix: bool, prerequisite: str, uncertain: bool) -> None:
    current = review(saved=saved_request(), **fields)
    current["reviewing_temporary_hotfix"] = hotfix
    code, payload, posts = invoke(
        request_args(), reviews=[review(), current] if uncertain else [current], posts=[http_error(409, "release_review_not_opened")] if uncertain else None,
    )
    assert code == 1 and len(posts) == int(uncertain)
    assert payload["status"] == "refused"
    assert payload["summary"]["refusal_reason"] == prerequisite
    assert payload["result"]["checklist_digest"] == DIGEST
    assert payload["result"]["request_id"] == RECORD_ID
    if fields.get("checklist", True) is None:
        assert payload["result"]["candidate"] is None


@pytest.mark.parametrize("http_status,error_code", [(409, "release_review_not_opened"), (502, "bad_gateway")])
def test_matching_readback_retains_producer_diagnostics(http_status: int, error_code: str) -> None:
    code, payload, posts = invoke(
        request_args(),
        reviews=[review(), review(saved=saved_request())],
        posts=[http_error(http_status, error_code)],
    )
    assert code == 1 and len(posts) == 1
    assert payload["summary"]["error_code"] == error_code
    assert payload["summary"]["trace_id"] == "trace-error"
    assert payload["result"]["invitation_delivery"] == "unverified"


def test_explicit_rerun_can_confirm_a_client_request_after_a_lost_response() -> None:
    code, uncertain, original = invoke(request_args(), reviews=[review(), review(saved=saved_request())], posts=[TimeoutError("lost")])
    assert code == 1 and uncertain["status"] == "accepted_unverified"
    code, confirmed, repeated = invoke(request_args(), reviews=[review(saved=saved_request())], posts=[review(saved=saved_request())])
    assert code == 0 and confirmed["result"]["invitation_delivery"] == "confirmed"
    assert len(original) == len(repeated) == 1
    assert original[0].kwargs == repeated[0].kwargs


def test_lost_standing_response_observes_an_already_decided_request() -> None:
    code, payload, posts = invoke(
        request_args(), reviews=[review(), review(saved=saved_request(), latest_decision={"decision": "accepted"})],
        posts=[http_error(502, "bad_gateway")], acceptance="director_standing",
    )
    assert code == 0 and len(posts) == 1
    assert payload["result"]["review_decided"] is True
    assert payload["summary"]["refusal_reason"] == "release_already_decided"
    assert payload["summary"]["trace_id"] == "trace-error"


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("drift", [False, True])
def test_conflicting_request_does_not_hide_current_state(uncertain: bool, drift: bool) -> None:
    current = review(digest=NEW_DIGEST if drift else DIGEST, saved=saved_request(reason=OTHER_REASON), checklist_complete=False)
    code, payload, posts = invoke(
        request_args(), reviews=[review(), current] if uncertain else [current], posts=[http_error(409, "release_review_not_opened")] if uncertain else None,
    )
    assert code == 1 and len(posts) == int(uncertain)
    assert payload["status"] == ("stale" if drift else "conflict")
    if not drift:
        assert payload["summary"]["refusal_reason"] == "checklist_incomplete"
        assert payload["summary"]["recommendation"] == RECOVERY["conflict"]
    if uncertain:
        assert payload["summary"]["trace_id"] == "trace-error"


def test_service_refusal_preserves_the_specific_readback_prerequisite() -> None:
    code, payload, posts = invoke(
        request_args(),
        reviews=[review(checklist_complete=False), review(checklist_complete=False)],
        posts=[http_error(409, "release_review_not_opened")],
    )
    assert code == 1 and len(posts) == 1
    assert payload["summary"]["error_code"] == "release_review_not_opened"
    assert payload["status"] == "refused"
    assert payload["summary"]["refusal_reason"] == "checklist_incomplete"


def test_denied_request_is_reported_without_retry() -> None:
    code, payload, posts = invoke(request_args(), reviews=[review()], posts=[http_error(403, "release_review_open_denied")])
    assert code == 1 and len(posts) == 1
    assert payload["status"] == "denied"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(unexpected_field=True),
        lambda body: body["review"].update(checklist_digest="not-a-digest"),
        lambda body: body["review"].update(checklist_digest=""),
        lambda body: body.update(release_request={**saved_request(), "product": "another-product"}),
        lambda body: body.update(release_request={**saved_request(), "source": []}),
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


def test_missing_policy_is_not_inferred_as_held() -> None:
    body = profile()
    del body["profile"]["release_on_acceptance"]
    with (
        patch_attr("prepare_operator_settings", return_value={"service_url": SERVICE_URL}),
        patch_attr("request_launchplane_read", return_value=body),
        patch_attr("request_launchplane") as post,
    ):
        output = io.StringIO()
        with redirect_stdout(output):
            assert h.main(plan_args()) == 1
    assert json.loads(output.getvalue())["status"] == "invalid"
    assert not post.called


def test_temporary_hotfix_explains_the_actual_review_prerequisite() -> None:
    body = review(approved=True, latest_decision={"decision": "accepted"})
    body["reviewing_temporary_hotfix"] = True
    code, payload, posts = invoke(plan_args(), reviews=[body])
    assert code == 1 and not posts
    assert payload["summary"]["refusal_reason"] == "temporary_hotfix_pending"


@pytest.mark.parametrize("acceptance", ["held", "promote", "promote_with_rollback_drill", "director_standing"])
def test_typed_transport_uses_the_same_request_route_for_client_confirmation(acceptance: str) -> None:
    saved: dict[str, object] | None = None
    calls: list[tuple[str, str]] = []

    def transport(request: urllib.request.Request, **_kwargs: Any) -> io.BytesIO:
        nonlocal saved
        route = urllib.parse.urlsplit(request.full_url)
        assert route.netloc == urllib.parse.urlsplit(SERVICE_URL).netloc
        method = request.get_method()
        calls.append((method, route.path))
        if route.path == h.helper_command_path("product-profile-read").format(product="site"):
            assert method == "GET"
            body = profile(acceptance)
        elif route.path == h.internal_helper_path("release-review-read"):
            assert method == "GET" and urllib.parse.parse_qs(route.query) == {"product": ["site"]}
            body = review(saved=saved)
        else:
            assert (method, route.path) == ("POST", h.helper_command_path(COMMAND))
            data = request.data
            assert isinstance(data, bytes)
            envelope = json.loads(data)
            assert envelope["checklist_digest"] == DIGEST
            if saved is None:
                saved = saved_request()
            body = review(saved=saved)
        return io.BytesIO(json.dumps(body).encode())

    settings = {"service_url": SERVICE_URL, "token": "fictional-token"}
    with patch_attr("prepare_operator_settings", return_value=settings), patch_attr("safe_urlopen", side_effect=transport):
        for index, args in enumerate((plan_args(), request_args(), request_args())):
            output = io.StringIO()
            with redirect_stdout(output):
                assert h.main(args) == 0
            payload = json.loads(output.getvalue())
            if index and acceptance != "director_standing":
                assert payload["result"]["invitation_delivery"] == "confirmed"
            assert_private_values_absent(payload)
            assert "fictional-token" not in output.getvalue()
    assert [path for method, path in calls if method != "GET"] == [h.helper_command_path(COMMAND)] * (1 if acceptance == "director_standing" else 2)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
