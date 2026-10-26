# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only inspection and inert plans for retiring selected workflow authz rules."""

from __future__ import annotations

import argparse
import http.client
import re
import urllib.error
from typing import Any

COMMAND_MODES = {
    "workflow-rule-retirement-inspect": "inspect",
    "workflow-rule-retirement-plan": "plan",
}
COMMANDS = frozenset(COMMAND_MODES)
# Launchplane's WorkflowRuleRetirementRequest accepts at most this many selections.
MAX_SELECTION = 50
MAX_ID_LENGTH = 256
REQUEST_FIELDS = frozenset({"policy_record_id", "policy_revision", "policy_sha256", "selection"})
SELECTION_FIELDS = frozenset({"rule_sha256", "managed_set_id", "managed_rule_id"})
RESPONSE_FIELDS = frozenset({"status", "trace_id", "result"})
RESULT_FIELDS = frozenset({
    "policy_record_id", "policy_revision", "policy_sha256", "selected_rules",
    "dependency_verification", "apply_supported", "blockers",
})
PLAN_HASH_FIELDS = ("candidate_policy_sha256", "survivor_rules_sha256", "plan_sha256")
PLAN_FIELDS = frozenset({
    *PLAN_HASH_FIELDS, "surviving_workflow_rule_count", "other_principal_collections_preserved",
})
RULE_FIELDS = frozenset({"rule_sha256", "management", "scope", "broad_or_shared_scope"})
SCOPE_TEXT_FIELDS = ("repository", "repository_id", "repository_owner_id")
SCOPE_MANAGED_FIELDS = ("managed_set_id", "managed_rule_id")
SCOPE_LIST_FIELDS = (
    "workflow_refs", "job_workflow_refs", "event_names", "refs", "environments",
    "products", "contexts", "instances", "actions",
)
SCOPE_FIELDS = frozenset({*SCOPE_TEXT_FIELDS, *SCOPE_MANAGED_FIELDS, *SCOPE_LIST_FIELDS})
HEX64_RE = re.compile(r"[a-f0-9]{64}")
# Identifier characters plus the glob characters a selector may use.
SELECTOR_RE = re.compile(r"[A-Za-z0-9_.:/#@+*?\[\]!-][A-Za-z0-9_.:/#@+*?\[\]!-]{0,255}")
DECIMAL_RE = re.compile(r"[0-9]{1,32}")
STALE_CODES = frozenset({"workflow_rule_selection_changed", "active_authz_policy_ambiguous"})
STALE_RECOMMENDATION = (
    "The active policy or the selected rules changed. Re-read the active policy record id, "
    "revision and digest, and each rule's fingerprint and managed ids. Then rerun with the fresh payload."
)


def add_parser(subparsers: Any) -> None:
    for command, mode in COMMAND_MODES.items():
        parser = subparsers.add_parser(
            command,
            help=f"Read-only workflow authz rule retirement {mode}; there is no apply mode.",
        )
        parser.add_argument("--payload-file", required=True, help="Private request JSON outside the repository.")


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and HEX64_RE.fullmatch(value) is not None


def _bounded_identifier(h: Any, value: object, code: str) -> str:
    try:
        projected = h.public_identifier(value)
    except h.LaunchplaneSafetyError:
        raise ValueError(code) from None
    if projected != value or len(projected) > MAX_ID_LENGTH:
        raise ValueError(code)
    return projected


def intent(h: Any, args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    mode = COMMAND_MODES[args.command]
    payload = h.read_payload_file(args.payload_file)
    if payload.keys() != REQUEST_FIELDS:
        raise ValueError("invalid_workflow_rule_retirement_fields")
    payload["policy_record_id"] = _bounded_identifier(h, payload["policy_record_id"], "invalid_workflow_rule_retirement_policy_record_id")
    revision = payload["policy_revision"]
    if type(revision) is not int or revision < 1:
        raise ValueError("invalid_workflow_rule_retirement_policy_revision")
    if not _is_hex64(payload["policy_sha256"]):
        raise ValueError("invalid_workflow_rule_retirement_policy_sha256")
    selection = payload["selection"]
    if not isinstance(selection, list) or not 1 <= len(selection) <= MAX_SELECTION:
        raise ValueError("invalid_workflow_rule_retirement_selection")
    seen: set[str] = set()
    for item in selection:
        if not isinstance(item, dict) or item.keys() != SELECTION_FIELDS:
            raise ValueError("invalid_workflow_rule_retirement_selection")
        if not _is_hex64(item["rule_sha256"]):
            raise ValueError("invalid_workflow_rule_retirement_rule_sha256")
        if item["rule_sha256"] in seen:
            raise ValueError("duplicate_workflow_rule_retirement_selection")
        seen.add(item["rule_sha256"])
        managed = (item["managed_set_id"], item["managed_rule_id"])
        if managed != (None, None):
            for field in SCOPE_MANAGED_FIELDS:
                item[field] = _bounded_identifier(h, item[field], "invalid_workflow_rule_retirement_managed_identity")
    request: dict[str, object] = {
        "mode": mode,
        "payload_source": "private_file",
        "selection_count": len(selection),
    }
    h.assert_public_safe_shape(request)
    return request, {**payload, "mode": mode}


def _selector(h: Any, value: object) -> str:
    if not isinstance(value, str) or SELECTOR_RE.fullmatch(value) is None:
        raise h.LaunchplaneSafetyError("invalid_response")
    return h.public_identifier(value, pattern=SELECTOR_RE)


def _decimal(h: Any, value: object) -> str:
    if not isinstance(value, str) or DECIMAL_RE.fullmatch(value) is None:
        raise h.LaunchplaneSafetyError("invalid_response")
    return value


def _project_scope(h: Any, value: object, selection: dict[str, Any], drops: Any) -> tuple[dict[str, object], bool]:
    scope = h._require_dict(value)
    before = drops.count
    projected: dict[str, object] = {}
    for key in SCOPE_MANAGED_FIELDS:
        if scope.get(key) != selection[key]:
            raise h.LaunchplaneSafetyError("invalid_response")
        if selection[key] is not None:
            projected[key] = h.public_identifier(selection[key])
    # Launchplane dumps every rule field except null managed ids: text fields
    # default to "" and selector lists to [], so a missing one is drift, not
    # an unconstrained rule.
    for key in SCOPE_TEXT_FIELDS:
        if not isinstance(scope.get(key), str):
            raise h.LaunchplaneSafetyError("invalid_response")
        path = f"selected_rules[].scope.{key}"
        if scope[key] == "":
            projected[key] = ""
            continue
        validate = (lambda item: _decimal(h, item)) if key != "repository" else (lambda item: _selector(h, item))
        kept = drops.keep(path, validate, scope[key])
        if kept:
            projected[key] = kept
    for key in SCOPE_LIST_FIELDS:
        path = f"selected_rules[].scope.{key}"
        items = scope.get(key)
        if not isinstance(items, list):
            raise h.LaunchplaneSafetyError("invalid_response")
        kept_items: list[str] = []
        for item in items:
            kept = drops.keep(path, lambda entry: _selector(h, entry), item)
            if kept:
                kept_items.append(kept)
            elif item in (None, ""):
                drops.drop(path)
        projected[key] = kept_items
    for key, value in scope.items():
        if key not in SCOPE_FIELDS:
            h.assert_public_safe_shape({key: value})
        if key not in SCOPE_FIELDS and value not in (None, "", [], {}):
            drops.drop("selected_rules[].scope.<unlisted field>")
    return projected, drops.count == before


# The service cannot prove which workflows use a rule; any other value is drift.
SOURCE_REVIEW_REQUIRED = "unknown_requires_source_review"


def _dependency_verification(h: Any, value: object) -> str:
    if value != SOURCE_REVIEW_REQUIRED:
        raise h.LaunchplaneSafetyError("invalid_response")
    return SOURCE_REVIEW_REQUIRED


def _project_rules(h: Any, value: object, body: dict[str, Any], drops: Any) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise h.LaunchplaneSafetyError("invalid_response")
    requested = {item["rule_sha256"]: item for item in body["selection"]}
    returned: set[str] = set()
    rules: list[dict[str, object]] = []
    for raw in value:
        rule = h._require_dict(raw)
        digest = rule.get("rule_sha256")
        if not isinstance(digest, str) or digest not in requested or digest in returned:
            raise h.LaunchplaneSafetyError("invalid_response")
        returned.add(digest)
        selection = requested[digest]
        expected = "unmanaged" if selection["managed_set_id"] is None else "managed"
        broad = rule.get("broad_or_shared_scope")
        if rule.get("management") != expected or type(broad) is not bool:
            raise h.LaunchplaneSafetyError("invalid_response")
        scope, complete = _project_scope(h, rule.get("scope"), selection, drops)
        h._report_dropped_fields(rule, prefix="selected_rules[]", kept=set(RULE_FIELDS),
                                 known=RULE_FIELDS, drops=drops)
        rules.append({
            "rule_sha256": digest,
            "management": expected,
            "managed_set_id": selection["managed_set_id"],
            "managed_rule_id": selection["managed_rule_id"],
            "broad_or_shared_scope": broad,
            "scope": scope,
            "scope_complete": complete,
        })
    if returned != set(requested):
        raise h.LaunchplaneSafetyError("invalid_response")
    return rules


def project(h: Any, response: object, body: dict[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    source = h._require_dict(response)
    if source.keys() - RESPONSE_FIELDS or source.get("status") != "ok":
        raise h.LaunchplaneSafetyError("invalid_response")
    result = h._require_dict(source.get("result"))
    present = PLAN_FIELDS & result.keys()
    if present != (PLAN_FIELDS if body["mode"] == "plan" else frozenset()):
        raise h.LaunchplaneSafetyError("invalid_response")
    if (result.get("policy_record_id") != body["policy_record_id"]
            or type(result.get("policy_revision")) is not int
            or result.get("policy_revision") != body["policy_revision"]
            or result.get("policy_sha256") != body["policy_sha256"]
            or result.get("apply_supported") is not False):
        raise h.LaunchplaneSafetyError("invalid_response")
    blockers = result.get("blockers")
    if not isinstance(blockers, list):
        raise h.LaunchplaneSafetyError("invalid_response")
    drops = h._FieldDrops()
    rules = _project_rules(h, result.get("selected_rules"), body, drops)
    projected: dict[str, object] = {
        "mode": body["mode"],
        "policy_record_id": body["policy_record_id"],
        "policy_revision": body["policy_revision"],
        "policy_sha256": body["policy_sha256"],
        "apply_supported": False,
        "dependency_verification": _dependency_verification(h, result.get("dependency_verification")),
        "blockers": [h.public_code(code) for code in blockers],
        "selected_rules": rules,
    }
    if body["mode"] == "plan":
        count = result.get("surviving_workflow_rule_count")
        preserved = result.get("other_principal_collections_preserved")
        if (any(not _is_hex64(result.get(key)) for key in PLAN_HASH_FIELDS)
                or type(count) is not int or count < 0 or type(preserved) is not bool):
            raise h.LaunchplaneSafetyError("invalid_response")
        projected.update({key: result[key] for key in PLAN_HASH_FIELDS})
        projected["surviving_workflow_rule_count"] = count
        projected["other_principal_collections_preserved"] = preserved
    h._report_dropped_fields(result, prefix="result", kept=set(RESULT_FIELDS | PLAN_FIELDS),
                             known=RESULT_FIELDS | PLAN_FIELDS, drops=drops)
    projected["dropped_field_paths"] = sorted(drops.paths)
    h.assert_public_safe_shape(projected)
    summary: dict[str, object] = {
        "launchplane_status": "ok",
        "trace_id": h.service_response_diagnostics(source).get("trace_id", ""),
        "selected_rule_count": len(rules),
        "broad_rule_count": sum(1 for rule in rules if rule["broad_or_shared_scope"]),
        "blocker_codes": projected["blockers"],
        "recommendation": (
            "Review each rule's scope and broad flag. Launchplane does not know which workflows "
            "depend on these rules, so review workflow sources before any retirement. "
            "This helper has no apply mode; retirement needs a separate reviewed policy change."
        ),
    }
    h.assert_public_safe_shape(summary)
    return projected, summary


def _rejection(h: Any, operation: str, request: dict[str, object], error: urllib.error.HTTPError) -> dict[str, object]:
    provider = h._error_payload_from_http(error)
    output = h.summarize_http_error(operation=operation, request=request, exc=error, provider_payload=provider)
    summary = output["summary"]
    if error.code == 409 and summary.get("error_code") in STALE_CODES:
        summary["recommendation"] = STALE_RECOMMENDATION
    h.assert_public_safe_shape(summary)
    return output


def execute(h: Any, args: argparse.Namespace) -> int:
    operation = args.command
    request, body = intent(h, args)
    settings = h.prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        # Pure read: the service writes nothing, so no idempotency key is sent.
        response = h.request_launchplane(service_url=settings["service_url"],
            path=h.helper_command_path(operation), settings=settings, body=body, timeout=args.timeout)
    except urllib.error.HTTPError as error:
        try:
            h.emit(_rejection(h, operation, request, error))
        except h.LaunchplaneSafetyError:
            h.emit_invalid_response(operation=operation, request=request)
        finally:
            error.close()
        return 1
    except h.ServiceResponseError as error:
        output = h.unavailable_payload(operation=operation, request=request, status="unavailable",
            code="invalid_response", message="The workflow rule retirement response was unavailable.")
        output["summary"] = h.service_response_diagnostics(error)
        h.emit(output)
        return 1
    except h.LaunchplaneSafetyError as error:
        h.emit_safety_error_payload(operation=operation, request=request, exc=error)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as error:
        h.emit_read_transport_error(operation=operation, request=request, timeout=args.timeout, exc=error)
        return 1
    try:
        result, summary = project(h, response, body)
    except ValueError:
        h.emit_invalid_response(operation=operation, request=request,
            diagnostics=h.service_response_diagnostics(response))
        return 1
    output = h.base_payload(status="ok", operation=operation, request=request)
    output["result"] = result
    output["summary"] = summary
    h.emit(output)
    return 0
