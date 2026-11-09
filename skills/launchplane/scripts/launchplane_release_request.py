# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import hashlib
import http.client
import re
import urllib.error
import urllib.parse
from typing import Any, NamedTuple

COMMAND = "release-request"
COMMANDS = frozenset({COMMAND})
PROFILE_READ = "product-profile-read"
REVIEW_READ = "release-review-read"
REVIEW_PAGE = "/ui/owner-review"
REQUEST_SOURCE = "director_request"
STANDING_ACCEPTANCE = "director_standing"
CLIENT_APPROVAL_BLOCKER = "Client approval of this release is required."
MAX_REASON_LENGTH = 1000
MAX_KEY_LENGTH = 200
SHA256_RE = re.compile(r"[a-f0-9]{64}")
COMMIT_RE = re.compile(r"[a-f0-9]{40}")
DECISION_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*#issuecomment-[1-9][0-9]*")

CHECKLIST_CHANGED_DIAGNOSTICS: dict[str, object] = {"error_code": "release_checklist_changed"}

REFUSAL_ADVICE = {
    "not_director_standing": "This product's release needs its Client's acceptance; this command only serves Director-standing products.",
    "release_review_not_required": "Launchplane requires no release review for this product now.",
    "checklist_unavailable": "The checklist is unavailable. Resolve its evidence before requesting a release.",
    "release_already_decided": "A decision already exists for this release. Observe its existing outcome.",
    "temporary_hotfix_pending": "A temporary hotfix is awaiting its Client review first.",
    "no_new_release": "Production already runs the testing version.",
    "checklist_incomplete": "The checklist is incomplete. Finish its evidence before requesting a release.",
    "checklist_blocked": "The checklist has blockers besides Client approval. Resolve them; this command never clears a blocker.",
    "service_not_opened": "Launchplane recorded no request for this checklist. Read it again with --plan and resolve what the release review reports.",
}

RECOVERY = {
    "ok": "Request recorded. Standing acceptance starts the gated release; observe it and do not promote by hand.",
    "plan": "Nothing was sent. Review this checklist, then submit its checklist_digest with --checklist-digest.",
    "stale": "The checklist changed. Run --plan for a new current read and review it before submitting its digest. A retry or a new key cannot bypass this.",
    "conflict": "A request for this checklist is already recorded with a different decision or reason. Inspect it instead of replacing it.",
    "accepted_unverified": "Launchplane accepted the call but did not return this exact request. Keep the same digest, reason, decision reference and key, and rerun to read it back.",
    "outcome_unknown": "The request may have been recorded. Keep the same digest, reason, decision reference and key, and rerun to read it back. Do not start over with a new key.",
    "unavailable": "The current release review could not be read. Nothing was sent; retry once the service is available.",
}


class Intent(NamedTuple):
    product: str
    digest: str
    reason: str
    decision_reference: str
    key: str
    request: dict[str, object]


class ReviewState(NamedTuple):
    digest: str
    production: dict[str, object] | None
    candidate: dict[str, object] | None
    blocker_count: int
    refusal: str
    saved: dict[str, Any] | None


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(COMMAND, help="Record an exact-checklist release request for a Director-standing product; never accepts or promotes.")
    parser.add_argument("--product", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--plan", action="store_true", help="Read the current checklist and send nothing.")
    target.add_argument("--checklist-digest", help="checklist_digest from a reviewed --plan read.")
    parser.add_argument("--reason")
    parser.add_argument("--decision-reference", help="Recorded Director decision comment URL.")
    parser.add_argument("--idempotency-key")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _intent(h: Any, args: argparse.Namespace) -> Intent:
    product = h.public_identifier(args.product)
    if args.plan:
        return Intent(product, "", "", "", "", {"product": product, "mode": "plan"})
    reason = (args.reason or "").strip()
    reference = (args.decision_reference or "").strip()
    key = (args.idempotency_key or "").strip()
    if not SHA256_RE.fullmatch(args.checklist_digest):
        raise ValueError("invalid_release_request_digest")
    if not reason or len(reason) > MAX_REASON_LENGTH or not DECISION_RE.fullmatch(reference):
        raise ValueError("invalid_release_request_decision")
    if not key or len(key) > MAX_KEY_LENGTH or h.public_identifier(key) != key:
        raise ValueError("invalid_release_request_key")
    request = {
        "product": product,
        "mode": "request",
        "checklist_digest": args.checklist_digest,
        "reason_sha256": _digest(reason),
        "decision_reference_sha256": _digest(reference),
    }
    h.assert_public_safe_shape(request)
    return Intent(product, args.checklist_digest, reason, reference, key, request)


def _require_mapping(h: Any, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise h.LaunchplaneSafetyError("invalid_response")
    return value


def _require_known_fields(h: Any, operation: str, response: dict[str, Any], required: frozenset[str] = frozenset()) -> None:
    # noinspection PyProtectedMember
    h._require_read_fields(operation, response, required=required)


def _read_acceptance(h: Any, settings: dict[str, str], product: str, timeout: float) -> str:
    response = h.request_launchplane_read(
        service_url=settings["service_url"],
        path=h.helper_command_path(PROFILE_READ).format(product=urllib.parse.quote(product, safe="")),
        settings=settings,
        query={},
        timeout=timeout,
    )
    _require_known_fields(h, PROFILE_READ, response)
    # noinspection PyProtectedMember
    profile = h._project_product_profile(response.get("profile"))
    if response.get("status") != "ok" or profile["product"] != product:
        raise h.LaunchplaneSafetyError("invalid_response")
    return str(profile["release_on_acceptance"])


def _project_version(h: Any, value: object) -> dict[str, object]:
    version = _require_mapping(h, value)
    commit = version.get("source_commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise h.LaunchplaneSafetyError("invalid_response")
    return {"source_commit": commit, "carries_runtime_settings": version.get("runtime_settings") is not None}


def _project_saved_request(h: Any, value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    saved = _require_mapping(h, value)
    for field, pattern in (("record_id", SHA256_RE), ("checklist_digest", SHA256_RE)):
        if not isinstance(saved.get(field), str) or not pattern.fullmatch(saved[field]):
            raise h.LaunchplaneSafetyError("invalid_response")
    if not isinstance(saved.get("decision_reference"), str) or not isinstance(saved.get("reason"), str):
        raise h.LaunchplaneSafetyError("invalid_response")
    return saved


def _refusal(status: dict[str, Any], checklist: dict[str, Any] | None, blockers: list[str], *, hotfix: bool) -> str:
    review_required = status.get("required") is True
    if not review_required:
        return "release_review_not_required"
    if status.get("unavailable_reason") is not None or checklist is None:
        return "checklist_unavailable"
    checks = (
        ("release_already_decided", status.get("approved") is True or status.get("latest_decision") is not None),
        ("temporary_hotfix_pending", hotfix),
        ("no_new_release", status.get("no_new_release") is True or checklist.get("production") == checklist.get("candidate")),
        ("checklist_incomplete", status.get("checklist_complete") is not True),
        ("checklist_blocked", blockers != [CLIENT_APPROVAL_BLOCKER]),
    )
    return next((code for code, failed in checks if failed), "")


def _project_versions(h: Any, checklist: dict[str, Any] | None) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    if checklist is None:
        return None, None
    return _project_version(h, checklist.get("production")), _project_version(h, checklist.get("candidate"))


def _project_review(h: Any, response: dict[str, Any], product: str) -> ReviewState:
    _require_known_fields(h, REVIEW_READ, response, frozenset({"product", "review"}))
    if response.get("product") != product:
        raise h.LaunchplaneSafetyError("invalid_response")
    status = _require_mapping(h, response.get("review"))
    checklist = None if status.get("checklist") is None else _require_mapping(h, status.get("checklist"))
    digest = status.get("checklist_digest") or ""
    blockers = status.get("blockers") or []
    if not isinstance(digest, str) or (digest and not SHA256_RE.fullmatch(digest)) or not isinstance(blockers, list):
        raise h.LaunchplaneSafetyError("invalid_response")
    production, candidate = _project_versions(h, checklist)
    return ReviewState(
        digest=digest,
        production=production,
        candidate=candidate,
        blocker_count=len(blockers),
        refusal=_refusal(status, checklist, blockers, hotfix=response.get("reviewing_temporary_hotfix") is True),
        saved=_project_saved_request(h, response.get("release_request")),
    )


def _read_review(h: Any, settings: dict[str, str], product: str, timeout: float) -> ReviewState:
    response = h.request_launchplane_read(
        service_url=settings["service_url"],
        path=h.internal_helper_path(REVIEW_READ),
        settings=settings,
        query={"product": product},
        timeout=timeout,
    )
    return _project_review(h, response, product)


def _matching_request(state: ReviewState, intent: Intent) -> dict[str, Any] | None:
    saved = state.saved
    if saved is None or (saved["checklist_digest"], saved["decision_reference"], saved["reason"]) != (intent.digest, intent.decision_reference, intent.reason):
        return None
    return saved


def _recorded_differently(state: ReviewState, intent: Intent) -> bool:
    saved = state.saved
    return saved is not None and saved["checklist_digest"] == intent.digest and _matching_request(state, intent) is None


def _result(h: Any, settings: dict[str, str], intent: Intent, state: ReviewState, acceptance: str) -> dict[str, object]:
    return {
        "release_on_acceptance": acceptance,
        "checklist_digest": state.digest,
        "production": state.production,
        "candidate": state.candidate,
        "blocker_count": state.blocker_count,
        "review_url": h.build_launchplane_url(settings["service_url"], REVIEW_PAGE, query=urllib.parse.urlencode({"product": intent.product})),
    }


def _finish(
    h: Any,
    intent: Intent,
    status: str,
    *,
    result: dict[str, object] | None = None,
    refusal: str = "",
    diagnostics: dict[str, object] | None = None,
    recommendation: str = "",
) -> int:
    output = h.base_payload(status=status, operation=COMMAND, request=intent.request)
    summary = dict(diagnostics or {})
    summary.pop("error_reason", None)
    if intent.key:
        summary["idempotency_key"] = intent.key
    if refusal:
        summary["refusal_reason"] = refusal
    service_advice = summary.get("recommendation")
    advice = recommendation or REFUSAL_ADVICE.get(refusal) or RECOVERY.get(status) or (service_advice if isinstance(service_advice, str) else "Inspect the service refusal before retrying.")
    summary["recommendation"] = advice
    output["summary"] = summary
    if result is not None:
        output["result"] = result
    if status != "ok":
        error_code = summary.get("error_code")
        output["warnings"] = [h.warning(error_code if isinstance(error_code, str) else refusal or status, advice)]
    for value in output.values():
        h.assert_public_safe_shape(value)
    h.emit(output)
    return 0 if status == "ok" else 1


def _finish_recorded(h: Any, settings: dict[str, str], intent: Intent, state: ReviewState, acceptance: str, saved: dict[str, Any], *, readback: bool = False) -> int:
    result = _result(h, settings, intent, state, acceptance)
    result.update(request_id=saved["record_id"], request_recorded=True, checklist_digest=intent.digest)
    if readback:
        result["verified_by_readback"] = True
    return _finish(h, intent, "ok", result=result)


def _settle_uncertain_post(
    h: Any,
    settings: dict[str, str],
    intent: Intent,
    acceptance: str,
    timeout: float,
    *,
    status: str,
    refusal: str = "",
    diagnostics: dict[str, object] | None = None,
) -> int:
    try:
        state = _read_review(h, settings, intent.product, timeout)
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
        return _finish(h, intent, "outcome_unknown", diagnostics=diagnostics)
    if (saved := _matching_request(state, intent)) is not None:
        return _finish_recorded(h, settings, intent, state, acceptance, saved, readback=True)
    return _finish(h, intent, status, refusal=refusal, diagnostics=diagnostics)


def _submit(h: Any, settings: dict[str, str], intent: Intent, acceptance: str, timeout: float) -> int:
    body: dict[str, object] = {
        "product": intent.product,
        "checklist_digest": intent.digest,
        "reason": intent.reason,
        "source": REQUEST_SOURCE,
        "decision_reference": intent.decision_reference,
    }
    try:
        response = h.request_launchplane(service_url=settings["service_url"], path=h.helper_command_path(COMMAND), settings=settings, body=body, timeout=timeout, idempotency_key=intent.key)
        state = _project_review(h, response, intent.product)
    except urllib.error.HTTPError as error:
        try:
            return _finish_refused_post(h, settings, intent, acceptance, timeout, error)
        finally:
            error.close()
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        diagnostics: dict[str, object] = {"error_code": "release_request_unverified", **h.service_response_diagnostics(error)}
        return _settle_uncertain_post(h, settings, intent, acceptance, timeout, status="outcome_unknown", diagnostics=diagnostics)
    if (saved := _matching_request(state, intent)) is not None:
        return _finish_recorded(h, settings, intent, state, acceptance, saved)
    status = "conflict" if _recorded_differently(state, intent) else "accepted_unverified"
    return _finish(h, intent, status, diagnostics=h.service_response_diagnostics(response))


def _finish_refused_post(h: Any, settings: dict[str, str], intent: Intent, acceptance: str, timeout: float, error: urllib.error.HTTPError) -> int:
    # noinspection PyProtectedMember
    provider = h._error_payload_from_http(error)
    projected = h.summarize_http_error(operation=COMMAND, request=intent.request, exc=error, provider_payload=provider)
    diagnostics = projected["summary"]
    code = diagnostics.get("error_code")
    if code == "release_checklist_changed":
        return _finish(h, intent, "stale", diagnostics=diagnostics, recommendation=RECOVERY["stale"])
    if code == "release_review_not_opened":
        return _settle_uncertain_post(h, settings, intent, acceptance, timeout, status="refused", refusal="service_not_opened", diagnostics=diagnostics)
    if error.code >= 500:
        return _settle_uncertain_post(h, settings, intent, acceptance, timeout, status="outcome_unknown", diagnostics=diagnostics)
    return _finish(h, intent, projected["status"], diagnostics=diagnostics)


def _finish_read_failure(h: Any, intent: Intent, timeout: float, error: Exception) -> int:
    if isinstance(error, urllib.error.HTTPError):
        try:
            h.emit_http_error_payload(operation=COMMAND, request=intent.request, exc=error)
        finally:
            error.close()
    elif isinstance(error, h.LaunchplaneSafetyError):
        h.emit_safety_error_payload(operation=COMMAND, request=intent.request, exc=error)
    elif isinstance(error, OSError | http.client.HTTPException):
        h.emit_read_transport_error(operation=COMMAND, request=intent.request, timeout=timeout, exc=error)
    else:
        h.emit_invalid_response(operation=COMMAND, request=intent.request, diagnostics=h.service_response_diagnostics(error))
    return 1


def execute(h: Any, args: argparse.Namespace) -> int:
    intent = _intent(h, args)
    settings = h.prepare_operator_settings(args=args, operation=COMMAND, request=intent.request)
    if settings is None:
        return 2
    try:
        acceptance = _read_acceptance(h, settings, intent.product, args.timeout)
        state = _read_review(h, settings, intent.product, args.timeout)
    except (ValueError, OSError, http.client.HTTPException) as error:
        return _finish_read_failure(h, intent, args.timeout, error)
    if acceptance != STANDING_ACCEPTANCE:
        return _finish(h, intent, "refused", refusal="not_director_standing")
    if args.plan:
        return _finish_plan(h, settings, intent, state, acceptance)
    if (saved := _matching_request(state, intent)) is not None:
        return _finish_recorded(h, settings, intent, state, acceptance, saved)
    if _recorded_differently(state, intent):
        return _finish(h, intent, "conflict")
    if state.digest and state.digest != intent.digest:
        return _finish(h, intent, "stale", diagnostics=CHECKLIST_CHANGED_DIAGNOSTICS)
    if state.refusal:
        return _finish(h, intent, "refused", refusal=state.refusal)
    return _submit(h, settings, intent, acceptance, args.timeout)


def _finish_plan(h: Any, settings: dict[str, str], intent: Intent, state: ReviewState, acceptance: str) -> int:
    result = _result(h, settings, intent, state, acceptance)
    saved = state.saved
    if saved is not None and saved["checklist_digest"] == state.digest:
        result.update(request_id=saved["record_id"], request_recorded=True)
    if state.refusal:
        return _finish(h, intent, "refused", result=result, refusal=state.refusal)
    return _finish(h, intent, "ok", result=result, recommendation=RECOVERY["plan"])
