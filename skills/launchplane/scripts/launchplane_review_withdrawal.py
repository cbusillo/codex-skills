# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bound one pending Client review withdrawal to a recorded Director decision."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
from pathlib import Path
import re
import urllib.error
from typing import Any

COMMANDS = frozenset("product-review-withdrawal-" + mode for mode in ("dry-run", "apply", "readback"))


def add_parser(subparsers: Any) -> None:
    for command in sorted(COMMANDS):
        parser = subparsers.add_parser(command, help="Withdraw a pending review request, never a Client decision.")
        parser.add_argument("--product", required=True)
        parser.add_argument("--repo", required=True)
        parser.add_argument("--pr", required=True, type=int)
        parser.add_argument("--head", required=True)
        parser.add_argument("--reason", required=True)
        parser.add_argument("--director-decision", required=True, help="Recorded Director-decision comment URL.")
        if not command.endswith("-dry-run"):
            parser.add_argument("--expected-plan-digest", required=True)
            parser.add_argument("--idempotency-key", required=True)
        if command.endswith("-apply"):
            parser.add_argument("--reviewed-dry-run", required=True, action="store_true")
            parser.add_argument("--dry-run-evidence-file", required=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    mode = args.command.removeprefix("product-review-withdrawal-")
    product = h.public_identifier(args.product)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or args.pr < 1 or not re.fullmatch(r"[a-f0-9]{40}", args.head):
        raise ValueError("invalid_withdrawal_scope")
    reason = args.reason.strip()
    if not reason or len(reason) > 1000 or not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9][0-9]*#issuecomment-[1-9][0-9]*", args.director_decision):
        raise ValueError("invalid_withdrawal_decision")
    body: dict[str, object] = {"product": product, "repository": h.public_identifier(args.repo), "pull_request": args.pr, "expected_head_sha": args.head, "reason": reason, "director_decision_url": args.director_decision, "mode": mode}
    request = {key: value for key, value in body.items() if key not in {"reason", "director_decision_url", "mode"}}
    request.update(reason_sha256=_digest(reason), director_decision_sha256=_digest(args.director_decision))
    if mode != "dry-run":
        if not re.fullmatch(r"[a-f0-9]{64}", args.expected_plan_digest) or not args.idempotency_key.strip() or len(args.idempotency_key) > 200:
            raise ValueError("invalid_withdrawal_review_binding")
        try:
            if h.public_identifier(args.idempotency_key) != args.idempotency_key:
                raise ValueError("invalid_withdrawal_operation_key")
        except h.LaunchplaneSafetyError as error:
            raise ValueError("invalid_withdrawal_operation_key") from error
        body["reviewed_plan_sha256"] = args.expected_plan_digest
    h.assert_public_safe_shape(request)
    return request, body


def project(h: Any, response: object, body: dict[str, object]) -> dict[str, object]:
    source = h._require_dict(response)
    result = h._require_dict(source.get("result"))
    if source.get("status") != "accepted" or type(result.get("applied")) is not bool or not isinstance(result.get("plan_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", result["plan_sha256"]) or result.get("head_sha") != body["expected_head_sha"]:
        raise h.LaunchplaneSafetyError("invalid_response")
    mode = body["mode"]
    if mode == "dry-run" and result["applied"]:
        raise h.LaunchplaneSafetyError("invalid_response")
    if mode != "dry-run" and result["plan_sha256"] != body["reviewed_plan_sha256"]:
        raise h.LaunchplaneSafetyError("invalid_response")
    projected = {key: result[key] for key in ("applied", "plan_sha256", "head_sha")}
    if result["applied"]:
        url = result.get("history_url")
        prefix = f"https://github.com/{body['repository']}/pull/{body['pull_request']}#issuecomment-"
        if not isinstance(url, str) or not url.casefold().startswith(prefix.casefold()) or not re.fullmatch(r"[1-9][0-9]*", url[len(prefix):]):
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["history_url"] = h.public_identifier(h.public_url(url))
    if "replan_required" in result:
        if type(result["replan_required"]) is not bool or result["applied"]:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["replan_required"] = result["replan_required"]
    if "history_unverifiable" in result:
        if type(result["history_unverifiable"]) is not bool or result.get("replan_required") is not True:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["history_unverifiable"] = result["history_unverifiable"]
    if "prior_effects_unknown" in result:
        if type(result["prior_effects_unknown"]) is not bool or result.get("replan_required") is not True:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["prior_effects_unknown"] = result["prior_effects_unknown"]
    if "operation_state" in result:
        if result["operation_state"] not in {"running", "reconcile_required", "completed"}:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["operation_state"] = result["operation_state"]
    if mode == "readback":
        if type(result.get("readback_matches")) is not bool:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["readback_matches"] = result["readback_matches"]
    h.assert_public_safe_shape(projected)
    return projected



REFUSAL_ADVICE = {
    "ineligible": "This request is not eligible for withdrawal.",
    "review_authority_changed": "The product or its Client changed; review the current request.",
    "repository_mismatch": "The product does not own the requested repository.",
    "pull_request_closed": "A new withdrawal requires an open pull request.",
    "head_changed": "The pull request head changed; review the current head.",
    "client_decision_exists": "The Client has already decided on this head.",
    "no_pending_labeled_request": "There is no labeled Client review request to withdraw.",
    "later_review_request": "A later review request may exist; recovery cannot remove its label.",
    "removal_unconfirmed": "Review-label removal was not confirmed on the reviewed head.",
    "history_unconfirmed": "Withdrawal history delivery was not confirmed.",
}


RECOVERY = {
    "ok": "Pending request only. Client decisions and release acceptance remain unchanged.",
    "accepted_unverified": "Keep the applied receipt and exact original intent/key. The original caller must inspect service readback before proceeding.",
    "outcome_unknown": "Outcome unknown. The original caller must keep the exact request, reviewed digest and key, read back, then resubmit that same apply to recover or settle it. Do not start over with a new key.",
    "in_progress": "The original operation is still running. Wait and read back with the same caller, request and key.",
    "target_busy": "This request was not dispatched. Another operation owns the PR; its original caller must recover its saved operation. This caller's key cannot recover it.",
    "not_started": "No operation exists for this caller and key. Resubmit the exact original apply with that same caller, request, reviewed digest and key.",
    "invalid_request": "This request was not dispatched. Correct the invalid request before applying.",
    "stale_plan": "This request was not dispatched. Refresh and review the exact dry-run before applying.",
    "history_unverifiable": "Authenticated history cannot be uniquely verified. Resolve history verification before retrying; changing keys cannot fix it. Preserve any saved receipt and prior-effect uncertainty.",
    "conflict": "This request was not dispatched. Restore the request that belongs to the original key; do not retry changed input with it.",
    "refused": "Withdrawal was refused. Inspect the current head and saved Client decisions; do not bypass the refusal or remove the label by hand.",
    "replan_required": "The original intent is settled without claiming withdrawal. Inspect the pending request and review a fresh dry run before using a new key.",
    "unavailable": "The requested service evidence or operation is unavailable. Inspect the service trace and retry the same request when prerequisites are available.",
}


def finish(h: Any, *, operation: str, request: dict[str, object], status: str,
           operation_key: str = "", result: dict[str, object] | None = None, diagnostics: dict[str, object] | None = None) -> int:
    """Keep the outcome, preserved receipt and recovery instructions in agreement."""
    output = h.base_payload(status=status, operation=operation, request=request)
    summary = dict(diagnostics or {})
    if operation_key:
        summary["idempotency_key"] = operation_key
    recommendation = RECOVERY.get(status, str(summary.get("recommendation", "Inspect the service refusal before retrying.")))
    reason = summary.get("refusal_reason")
    if status == "refused" and isinstance(reason, str) and reason in REFUSAL_ADVICE:
        recommendation = REFUSAL_ADVICE[reason] + " " + recommendation
    if status == "history_unverifiable" and result and result.get("replan_required") is True:
        recommendation += " " + RECOVERY["replan_required"]
    if result and result.get("prior_effects_unknown") is True:
        recommendation += " A label removal may have happened without a PR history entry; preserve and inspect the saved operation and PR history."
    summary["recommendation"] = recommendation
    output["summary"] = summary
    if result is not None:
        output["result"] = result
    if status != "ok":
        output["warnings"] = [h.warning(str(summary.get("error_code", status)), recommendation)]
    # The standard envelope uses the reserved wrapper key "request"; validate
    # each projected value, including the already redacted request body.
    for value in output.values():
        h.assert_public_safe_shape(value)
    h.emit(output)
    return 0 if status == "ok" else 1


def result_status(result: dict[str, object], *, mode: str) -> str:
    if result.get("history_unverifiable") is True:
        return "history_unverifiable"
    if result.get("replan_required") is True:
        return "replan_required"
    if mode == "dry-run":
        return "ok"
    if result.get("applied") is True:
        return "ok" if result.get("readback_matches") is True else "accepted_unverified"
    return "in_progress" if result.get("operation_state") == "running" else "outcome_unknown"


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    request, body = intent(h, args)
    mode = str(body["mode"])
    operation_key = h.public_identifier(args.idempotency_key) if mode != "dry-run" else ""
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    request["service_url_sha256"] = _digest(settings["service_url"])
    if mode == "apply":
        try:
            evidence = json.loads(Path(args.dry_run_evidence_file).read_text())
            if evidence.get("status") != "ok" or evidence.get("operation") != "product-review-withdrawal-dry-run" or evidence.get("request") != request or evidence.get("result", {}).get("applied") is not False or evidence.get("result", {}).get("plan_sha256") != args.expected_plan_digest:
                raise ValueError("withdrawal_review_evidence_mismatch")
        except (OSError, ValueError, AttributeError):
            return finish(h, operation=operation, request=request, operation_key=operation_key, status="invalid", diagnostics={"error_code": "withdrawal_review_evidence_mismatch", "recommendation": "Save and review a dry run of this exact request before apply."})
    timeout = getattr(args, "timeout", 30)
    known_receipt: dict[str, object] | None = None
    try:
        response = h.request_launchplane(service_url=settings["service_url"], path=h.helper_command_path(operation), settings=settings, body=body, timeout=timeout, idempotency_key=getattr(args, "idempotency_key", ""))
        result = project(h, response, body)
        if mode == "apply" and result["applied"] is True:
            known_receipt = result
            response = h.request_launchplane(service_url=settings["service_url"], path=h.helper_command_path(operation), settings=settings, body={**body, "mode": "readback"}, timeout=timeout, idempotency_key=args.idempotency_key)
            observed = project(h, response, {**body, "mode": "readback"})
            result = observed if observed["applied"] is True else {**known_receipt, "readback_matches": False}
        status = result_status(result, mode=mode)
        return finish(h, operation=operation, request=request, operation_key=operation_key, status=status, result=result, diagnostics=h.service_response_diagnostics(response))
    except urllib.error.HTTPError as error:
        try:
            provider_payload = h._error_payload_from_http(error)
            projected = h.summarize_http_error(operation=operation, request=request, exc=error, provider_payload=provider_payload)
            diagnostics = projected.get("summary", {})
            code = diagnostics.get("error_code")
            if known_receipt is not None:
                status = "accepted_unverified"
            elif mode == "apply" and (error.code >= 500 or code == "product_review_withdrawal_unsettled"):
                status = "outcome_unknown"
            else:
                status = {
                    "product_review_withdrawal_target_busy": "target_busy",
                    "product_review_withdrawal_in_progress": "in_progress",
                    "product_review_withdrawal_conflict": "conflict",
                    "product_review_withdrawal_not_started": "not_started",
                    "product_review_withdrawal_invalid_request": "invalid_request",
                    "product_review_withdrawal_stale_plan": "stale_plan",
                    "product_review_withdrawal_history_unverifiable": "history_unverifiable",
                    "product_review_withdrawal_refused": "refused",
                    "product_review_withdrawal_unsettled": "outcome_unknown",
                }.get(code, projected["status"])
            details = provider_payload.get("error")
            reason = details.get("reason") if isinstance(details, dict) else None
            if (status == "refused" and code == "product_review_withdrawal_refused"
                    and isinstance(reason, str) and reason in REFUSAL_ADVICE):
                diagnostics = {**diagnostics, "refusal_reason": reason}
            return finish(h, operation=operation, request=request, operation_key=operation_key, status=status, result=known_receipt, diagnostics=diagnostics)
        finally:
            error.close()
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException, h.ServiceResponseError):
        status = "accepted_unverified" if known_receipt is not None else "outcome_unknown" if mode in {"apply", "readback"} else "unavailable"
        return finish(h, operation=operation, request=request, operation_key=operation_key, status=status, result=known_receipt, diagnostics={"error_code": "withdrawal_unverified"})
