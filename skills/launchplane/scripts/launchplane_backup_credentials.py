# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
import urllib.parse
from typing import Any

PREFLIGHT = "production-backup-preflight-read"
PREFLIGHT_TIMEOUT_SECONDS = 45.0
BINDINGS = "backup-worker-bindings"
COMMANDS = {PREFLIGHT, *(BINDINGS + "-" + mode for mode in ("read", "dry-run", "apply"))}
CREDENTIAL_KEYS = ("PRODUCTION_BACKUP_SSH_PRIVATE_KEY", "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS")


def add_parser(subparsers: Any) -> None:
    for command in sorted(COMMANDS):
        parser = subparsers.add_parser(command, help="Read backup metadata or review reference-only worker rebinding.")
        for field in ("product", "context", "instance"):
            parser.add_argument("--" + field, required=True)
        parser.add_argument("--promotion-action", required=command == PREFLIGHT, default="")
        if command == PREFLIGHT:
            parser.add_argument("--proposal-id", default="")
        elif not command.endswith("-read"):
            parser.add_argument("--private-key-binding", required=True)
            parser.add_argument("--known-hosts-binding", required=True)
            parser.add_argument("--reason", required=True)
            parser.add_argument("--idempotency-key", required=True)
            if command.endswith("-apply"):
                parser.add_argument("--reviewed-dry-run", required=True, action="store_true")
                parser.add_argument("--dry-run-evidence-file", required=True)
                parser.add_argument("--expected-plan-digest", required=True)


def identifier(h: Any, value: object) -> str:
    projected = h.public_identifier(value)
    if projected != value:
        raise ValueError("invalid_backup_reference")
    return projected


def project_metadata(h: Any, response: object, scope: dict[str, str]) -> dict[str, Any]:
    source = h._require_dict(response)
    result = h._require_dict(source.get("result"))
    if source.get("status") != "accepted" or any(result.get(key) != value for key, value in scope.items()):
        raise ValueError("backup_lane_mismatch")
    credentials = result.get("credentials")
    if not isinstance(credentials, list) or len(credentials) > 500:
        raise ValueError("invalid_backup_metadata")
    projected = [
        {key: identifier(h, h._require_dict(item).get(key)) for key in ("binding_id", "binding_key", "version_id")}
        for item in credentials
    ]
    if len({item["binding_id"] for item in projected}) != len(projected):
        raise ValueError("ambiguous_backup_metadata")
    return {**scope, "bindings": sorted(projected, key=lambda item: item["binding_id"])}


def project_preflight(h: Any, response: object) -> dict[str, object]:
    source = h._require_dict(response)
    result = h._require_dict(source.get("result"))
    if (
        source.get("status") != "accepted"
        or result.get("capture_started") is not False
        or result.get("status") not in {"pass", "fail"}
    ):
        raise ValueError("invalid_backup_preflight")
    revision = result.get("policy_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid_backup_preflight")
    return {
        "status": result["status"],
        "capture_started": False,
        "policy_revision": revision,
        "policy_record_id": identifier(h, result.get("policy_record_id")),
        "boundary_binding_id": identifier(h, result.get("credential_binding_id")),
        "error_code": h.public_code(result["error_code"]) if result.get("error_code") else "",
        "boundary_status": h.public_code(result.get("boundary_status")),
        "storage_status": h.public_code(result.get("storage_status")),
        "finished_at": h.public_timestamp(result.get("finished_at")),
    }


def selected_metadata(metadata: dict[str, Any], bindings: dict[str, str]) -> list[dict[str, str]]:
    by_id = {item["binding_id"]: item for item in metadata["bindings"]}
    if not set(bindings.values()) <= by_id.keys():
        raise ValueError("backup_binding_unavailable")
    return [by_id[value] for value in sorted(bindings.values())]


def project_plan(h: Any, response: object, body: dict[str, Any], selected: list[dict[str, str]]) -> dict[str, Any]:
    source = h._require_dict(response)
    result = h._require_dict(source.get("result"))
    digest = result.get("plan_sha256")
    if source.get("status") != "accepted" or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("invalid_backup_plan")
    if type(result.get("changed")) is not bool or result.get("applied") is not (body["mode"] == "apply"):
        raise ValueError("invalid_backup_plan")
    expected = sorted(
        [
            {"binding_id": value, "from_key": item["binding_key"], "to_key": key}
            for key, value in body["bindings"].items()
            for item in selected
            if item["binding_id"] == value
        ],
        key=lambda item: item["binding_id"],
    )
    changes = result.get("changes")
    if not isinstance(changes, list) or len(changes) != len(expected):
        raise ValueError("invalid_backup_plan")
    projected = sorted(
        [{key: identifier(h, h._require_dict(item).get(key)) for key in ("binding_id", "from_key", "to_key")} for item in changes],
        key=lambda item: item["binding_id"],
    )
    if projected != expected or body["mode"] == "apply" and digest != body["reviewed_plan_sha256"]:
        raise ValueError("backup_plan_mismatch")
    return {
        "plan_sha256": digest,
        "changed": result["changed"],
        "applied": result["applied"],
        "changes": projected,
        "selected_bindings": selected,
    }


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    scope = {key: identifier(h, getattr(args, key)) for key in ("product", "context", "instance")}
    if scope["instance"] != "prod":
        raise ValueError("backup_production_lane_required")
    request: dict[str, Any] = dict(scope)
    body: dict[str, Any] = {}
    mode = operation.removeprefix(BINDINGS + "-")
    if operation == PREFLIGHT:
        request["promotion_action"] = identifier(h, args.promotion_action)
        if args.proposal_id:
            request["proposal_id"] = identifier(h, args.proposal_id)
    elif mode != "read":
        bindings = dict(
            zip(CREDENTIAL_KEYS, (identifier(h, args.private_key_binding), identifier(h, args.known_hosts_binding)), strict=True)
        )
        if len(set(bindings.values())) != len(CREDENTIAL_KEYS) or not args.reason.strip() or len(args.reason) > 500:
            raise ValueError("invalid_backup_bindings")
        identifier(h, args.idempotency_key)
        body = {
            "mode": mode,
            "bindings": bindings,
            "reason": args.reason,
            "promotion_action": identifier(h, args.promotion_action) if args.promotion_action else "",
        }
        request.update(
            intent_sha256=h.metadata_review_digest({key: value for key, value in body.items() if key != "mode"}),
            idempotency_key_sha256=h.metadata_review_digest({"key": args.idempotency_key}),
        )
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    request["service_url_sha256"] = h.metadata_review_digest({"service_url": settings["service_url"]})
    payload = h.base_payload(status="ok", operation=operation, request=request)
    response = None
    attempted = accepted = False
    read_path = h.helper_command_path(BINDINGS + "-read").format(product=urllib.parse.quote(scope["product"], safe=""))

    def read_metadata() -> dict[str, Any]:
        return project_metadata(
            h,
            h.request_launchplane_read(
                service_url=settings["service_url"], path=read_path, settings=settings, query={}, timeout=args.timeout
            ),
            scope,
        )

    def failure(status: str, diagnostics: dict[str, object]) -> dict[str, Any]:
        factory = {"accepted_unverified": h._apply_response_unverified, "outcome_unknown": h._apply_outcome_unknown}.get(status)
        failed = (
            factory(operation=operation, request=request, label="backup worker bindings")
            if factory
            else h.base_payload(status=status, operation=operation, request=request)
        )
        failed["result"] = payload["result"]
        failed["summary"].update(diagnostics)
        if accepted:
            failed["summary"].update({"read_back_" + key: value for key, value in diagnostics.items()})
            failed["summary"].update(h.service_response_diagnostics(response))
        if response and response.get("replayed") is True:
            failed["summary"]["original_trace_id"] = h.public_trace_id(response.get("original_trace_id"))
        return failed

    try:
        if operation == PREFLIGHT or mode == "read":
            path = h.helper_command_path(operation) if operation == PREFLIGHT else read_path
            response = h.request_launchplane_read(
                service_url=settings["service_url"],
                path=path,
                settings=settings,
                query={key: value for key, value in request.items() if key != "service_url_sha256"}
                if operation == PREFLIGHT
                else {},
                timeout=args.timeout,
            )
            payload["result"] = project_preflight(h, response) if operation == PREFLIGHT else project_metadata(h, response, scope)
        else:
            if mode == "apply":
                digest = h._reviewed_apply_digest(args)
                evidence = h.read_payload_file(args.dry_run_evidence_file)
                reviewed = h._require_dict(evidence.get("result"))
                selected = reviewed.get("selected_bindings")
                if (
                    evidence.get("operation") != BINDINGS + "-dry-run"
                    or evidence.get("status") != "ok"
                    or evidence.get("request") != request
                    or reviewed.get("applied") is not False
                    or reviewed.get("plan_sha256") != digest
                    or not isinstance(selected, list)
                ):
                    raise ValueError("backup_review_evidence_mismatch")
                metadata = project_metadata(h, {"status": "accepted", "result": {**scope, "credentials": selected}}, scope)
                if selected_metadata(metadata, body["bindings"]) != selected or len(selected) != len(CREDENTIAL_KEYS):
                    raise ValueError("backup_review_evidence_mismatch")
                project_plan(h, {"status": "accepted", "result": reviewed}, {**body, "mode": "dry-run"}, selected)
                body["reviewed_plan_sha256"] = digest
            else:
                selected = selected_metadata(read_metadata(), body["bindings"])
            attempted = mode == "apply"
            response = h.request_launchplane(
                service_url=settings["service_url"],
                path=h.helper_command_path(operation).format(product=urllib.parse.quote(scope["product"], safe="")),
                settings=settings,
                body=body,
                timeout=args.timeout,
                idempotency_key=args.idempotency_key,
            )
            accepted = mode == "apply" and response.get("status") == "accepted"
            payload["result"] = project_plan(h, response, body, selected)
            if mode == "apply":
                observed = selected_metadata(read_metadata(), body["bindings"])
                expected = [
                    {**item, "binding_key": next(key for key, value in body["bindings"].items() if value == item["binding_id"])}
                    for item in selected
                ]
                payload["result"]["readback_matches"] = observed == expected
                if observed != expected:
                    raise ValueError("backup_readback_mismatch")
        payload["summary"] = h.service_response_diagnostics(response)
    except urllib.error.HTTPError as error:
        try:
            diagnostics = h.summarize_http_error(operation=operation, request=request, exc=error)["summary"]
            unresolved = diagnostics.get("error_code") in {
                "mutation_in_progress",
                "mutation_reconciliation_required",
                "reconciliation_required",
            }
            status = (
                "accepted_unverified"
                if accepted
                else "outcome_unknown"
                if attempted and (error.code >= 500 or error.code == 408 or unresolved)
                else "refused"
            )
            payload = failure(status, diagnostics)
        finally:
            error.close()
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        payload = failure(
            "accepted_unverified" if accepted else "outcome_unknown" if attempted else "unavailable",
            h.service_response_diagnostics(error),
        )
    except (ValueError, h.LaunchplaneSafetyError) as error:
        payload = failure(
            "accepted_unverified" if accepted else "outcome_unknown" if attempted else "invalid",
            h.service_response_diagnostics(error),
        )
    if payload["status"] != "ok":
        payload["summary"]["recommendation"] = (
            "Preserve the saved review, receipt, caller, exact request and original operation key. Read binding metadata before any same-key retry; never switch credentials, keys or workflows to bypass a refusal."
        )
    for part in ("request", "result", "summary"):
        h.assert_public_safe_shape(payload[part])
    h.emit(payload)
    return 0 if payload["status"] == "ok" else 1
