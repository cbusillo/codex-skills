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
    body: dict[str, object] = {"product": product, "repository": args.repo, "pull_request": args.pr, "expected_head_sha": args.head, "reason": reason, "director_decision_url": args.director_decision, "mode": mode}
    request = {key: value for key, value in body.items() if key not in {"reason", "director_decision_url", "mode"}}
    request.update(reason_sha256=_digest(reason), director_decision_sha256=_digest(args.director_decision))
    if mode != "dry-run":
        if not re.fullmatch(r"[a-f0-9]{64}", args.expected_plan_digest) or not args.idempotency_key.strip() or len(args.idempotency_key) > 200:
            raise ValueError("invalid_withdrawal_review_binding")
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
        projected["history_url"] = h.public_url(url)
    if "replan_required" in result:
        if type(result["replan_required"]) is not bool or result["applied"]:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["replan_required"] = result["replan_required"]
    if mode == "readback":
        if type(result.get("readback_matches")) is not bool:
            raise h.LaunchplaneSafetyError("invalid_response")
        projected["readback_matches"] = result["readback_matches"]
    h.assert_public_safe_shape(projected)
    return projected


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    request, body = intent(h, args)
    mode = body["mode"]
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
            h.emit(h.unavailable_payload(operation=operation, request=request, status="invalid", code="withdrawal_review_evidence_mismatch", message="Save and review a dry run of this exact request before apply."))
            return 1
    timeout = getattr(args, "timeout", 30)
    attempted = False
    applied = False
    try:
        attempted = mode == "apply"
        response = h.request_launchplane(service_url=settings["service_url"], path=h.helper_command_path(operation), settings=settings, body=body, timeout=timeout, idempotency_key=getattr(args, "idempotency_key", ""))
        result = project(h, response, body)
        if result.get("replan_required") is True:
            output = h.unavailable_payload(operation=operation, request=request, status="replan_required", code="withdrawal_replan_required", message="The original operation is settled without claiming withdrawal. Inspect the pending request; use a fresh reviewed dry run and new key for a new withdrawal.")
            output["result"] = result
            h.emit(output)
            return 1
        if mode == "apply":
            applied = result["applied"] is True
            if not applied:
                raise h.LaunchplaneSafetyError("invalid_response")
            readback = h.request_launchplane(service_url=settings["service_url"], path=h.helper_command_path(operation), settings=settings, body={**body, "mode": "readback"}, timeout=timeout, idempotency_key=args.idempotency_key)
            result = project(h, readback, {**body, "mode": "readback"})
            response = readback
        if mode != "dry-run" and result.get("readback_matches") is not True:
            raise h.LaunchplaneSafetyError("withdrawal_readback_mismatch")
    except urllib.error.HTTPError as error:
        try:
            output = h.summarize_http_error(operation=operation, request=request, exc=error, provider_payload=h._error_payload_from_http(error))
            if applied:
                output["status"] = "accepted_unverified"
            elif attempted and output.get("summary", {}).get("error_code") == "product_review_withdrawal_unsettled":
                output["status"] = "outcome_unknown"
            h.emit(output)
        finally:
            error.close()
        return 1
    except (ValueError, OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException, h.ServiceResponseError):
        status = "accepted_unverified" if applied else "outcome_unknown" if attempted else "unavailable"
        h.emit(h.unavailable_payload(operation=operation, request=request, status=status, code="withdrawal_unverified", message="Preserve the original request, reviewed digest and operation key. Read back before retrying; never remove the label by hand."))
        return 1
    output = h.base_payload(status="ok", operation=operation, request=request)
    output["result"] = result
    output["summary"] = {**h.service_response_diagnostics(response), "recommendation": "Pending request only. Client decisions and release acceptance remain unchanged."}
    h.emit(output)
    return 0
