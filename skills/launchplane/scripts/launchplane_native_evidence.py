# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import hashlib
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from launchplane_safety import LaunchplaneSafetyError

# https://github.com/cbusillo/launchplane/blob/9e3a5ad982d162a7d149ba68ba60702cce24c64e/control_plane/contracts/native_testing_rebuild_evidence.py
ITEM_SOURCES = {
    "reservation": "native_reservation",
    "original_intent": "native_original_intent",
    "checkpoint": "native_checkpoint",
    "terminal_probe": "checkpoint_probe",
    "candidate_preparation": "checkpoint_preparation",
    "candidate_readiness": "checkpoint_private_readiness",
    "deployment": "deployment_record",
    "forward_route": "forward_route_receipt",
    "inverse_route": "inverse_route_receipt",
    "selection": "publication_receipt",
}
ITEM_CODES = {
    "source": set(ITEM_SOURCES.values()),
    "availability": {"present", "missing", "null", "unavailable", "unsupported", "unbound", "not_retained"},
    "binding": {"matched", "unverified", "mismatched"},
    "coverage": {"not_read", "retained_record_only", "checkpoint_probe_only", "candidate_probe_not_retained"},
    "state": {
        "running",
        "completed",
        "reconcile_required",
        "preparing",
        "prepared",
        "switching",
        "publishing",
        "retiring",
        "complete",
        "rolling_back",
        "failed",
        "pending",
        "pass",
        "fail",
        "skipped",
        "planned",
        "applied",
        "reversed",
    },
}
ITEM_BOOLEANS = {
    "verified",
    "runtime_identity_matches",
    "effect_checkpoint_recorded",
    "not_dispatched",
    "route_write_not_started",
}
VIEW_BOOLEANS = {"publication_started", "retirement_started", "rollback_started"}
OBSERVATION_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?")


def _closed_object(value: object, required: set[str], optional: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return value


def _code(value: object, allowed: set[str]) -> object:
    if not isinstance(value, str) or value not in allowed:
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _integer(value: object, minimum: int) -> object:
    if type(value) is not int or value < minimum:
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _nullable_booleans(source: dict[str, Any], names: set[str]) -> dict[str, object]:
    for name in names:
        if source.get(name) is not None and type(source[name]) is not bool:
            raise LaunchplaneSafetyError("invalid_response")
    return {name: source.get(name) for name in names}


def _project_item(value: object, *, name: str) -> dict[str, object]:
    probe_fields = {"http_status", "failure_kind"} if name == "terminal_probe" else set()
    source = _closed_object(value, {"source"}, {*ITEM_CODES, *ITEM_BOOLEANS, "observed_at", "attempt", *probe_fields})
    projected = {
        "source": _code(source["source"], ITEM_CODES["source"]),
        **{
            name: _code(source.get(name, default), ITEM_CODES[name])
            for name, default in (
                ("availability", "unsupported"),
                ("binding", "unverified"),
                ("coverage", "not_read"),
            )
        },
        "state": None if source.get("state") is None else _code(source["state"], ITEM_CODES["state"]),
        "attempt": None if source.get("attempt") is None else _integer(source["attempt"], 1),
        **_nullable_booleans(source, ITEM_BOOLEANS),
    }
    projected["observed_at"] = _observation_time(source.get("observed_at"))
    if "http_status" in source:
        status = source["http_status"]
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise LaunchplaneSafetyError("invalid_response")
        projected["http_status"] = status
    if "failure_kind" in source:
        failure = source["failure_kind"]
        projected["failure_kind"] = None if failure is None else _code(
            failure, {"connection", "tls", "timeout", "invalid_response", "redirect"}
        )
    return projected


def project_native_evidence(value: object, *, operation: dict[str, Any], operation_id: object) -> dict[str, object]:
    source = _closed_object(
        value,
        {*ITEM_SOURCES, "operation_attempt"},
        {"schema_version", "needed_copies", "predecessor_snapshot", *VIEW_BOOLEANS},
    )
    version = source.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise LaunchplaneSafetyError("invalid_response")
    attempt = _integer(source["operation_attempt"], 0)
    if operation.get("operation_id") != operation_id or type(operation.get("attempt")) is not int or operation["attempt"] != attempt:
        raise LaunchplaneSafetyError("invalid_response")
    predecessor = source.get("predecessor_snapshot")
    if predecessor is not None:
        _code(predecessor, {"identity_recorded", "stopped", "absent"})
    return {
        "schema_version": version,
        "operation_attempt": attempt,
        **{name: _project_item(source[name], name=name) for name in ITEM_SOURCES},
        "predecessor_snapshot": predecessor,
        **_nullable_booleans(source, VIEW_BOOLEANS),
        "needed_copies": _code(source.get("needed_copies", "retain_all"), {"retain_all"}),
    }


def _observation_time(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str) or not OBSERVATION_TIME.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LaunchplaneSafetyError("invalid_response") from None
    return value


# https://github.com/cbusillo/launchplane/blob/2550cf18a20bbdc93c277a62ae0914ad20ab3ce4/control_plane/contracts/native_testing_rebuild_evidence.py
SCOPE_FIELDS = ("operation_id", "product", "context", "instance")
PRIVATE_STATUS = {
    "availability": "unsupported",
    "binding": "unverified",
    "coverage": "not_read",
}
PRIVATE_ITEMS = ("candidate", "forward_route", "inverse_route", "failure_reference")
PRIVATE_DIGESTS = ("intent_sha256", "checkpoint_plan_sha256")
ATTRIBUTION_FIELDS = ("terminal_account", "execution_trace", "submitted_program")
DIGEST_PATTERN = r"[a-f0-9]{64}"
SELECTOR_ID_PATTERN = r"[a-zA-Z0-9_-]+"
FAILURE_STAGES = {
    "host_preparation", "source_transfer", "website_settings", "integration_readback", "snapshot",
    "runtime_start", "cleanup", "restore", "attachments", "pre_registry_sanitize",
    "module_update", "core_schema", "users", "post_sanitize",
}


def _pattern(value: object, pattern: str) -> None:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise LaunchplaneSafetyError("invalid_response")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private_item(value: object, extra: set[str]) -> dict[str, Any]:
    item = _closed_object(value, set(), {*PRIVATE_STATUS, "observed_at", "attempt", *extra})
    _code(item.get("availability", "unsupported"), ITEM_CODES["availability"])
    _code(item.get("binding", "unverified"), ITEM_CODES["binding"])
    _code(item.get("coverage", "not_read"), {"not_read", "retained_record_only"})
    _observation_time(item.get("observed_at"))
    if item.get("attempt") is not None:
        _integer(item["attempt"], 1)
    return item


def _runtime_identity(value: object, operation: dict[str, Any]) -> None:
    required = {"context", "instance", "deployment_record_id", "artifact_id", "source_git_ref"}
    optional = {
        "product", "environment_kind", "image_reference", "release_tuple_id", "preview_id",
        "preview_generation_id", "deployed_at", "slot", "release_id", "image_digest", "database_uuid",
    }
    identity = _closed_object(value, required, {*optional, "schema_version"})
    _integer(identity.get("schema_version", 1), 1)
    if any(not isinstance(identity[name], str) for name in identity.keys() & (required | optional)):
        raise LaunchplaneSafetyError("invalid_response")
    if any(not identity[name].strip() for name in required):
        raise LaunchplaneSafetyError("invalid_response")
    _pattern(identity.get("image_digest", ""), r"|sha256:[a-f0-9]{64}")
    if (
        any(identity[name] != operation[name] for name in ("context", "instance"))
        or (identity.get("product") or operation["product"]) != operation["product"]
    ):
        raise LaunchplaneSafetyError("invalid_response")
    request = operation.get("request")
    if isinstance(request, dict) and any(
        request.get(name) and identity[name] != request[name] for name in ("artifact_id", "source_git_ref")
    ):
        raise LaunchplaneSafetyError("invalid_response")


def _candidate_selector(value: object, operation: dict[str, Any], namespace: str) -> dict[str, Any]:
    ids = {"server_id", "project_id", "environment_id", "compose_id"}
    selector = _closed_object(value, {*ids, "namespace", "resources_binding_sha256", "runtime_identity"}, set())
    for name in ids:
        _pattern(selector[name], SELECTOR_ID_PATTERN)
    _pattern(selector["resources_binding_sha256"], DIGEST_PATTERN)
    if selector["namespace"] != namespace:
        raise LaunchplaneSafetyError("invalid_response")
    _runtime_identity(selector["runtime_identity"], operation)
    return selector


def _route_selector(value: object, route_id: str) -> dict[str, Any]:
    selector = _closed_object(
        value, {"server_id", "copy_id", "route_id", "route_digest", "document_sha256"}, {"backend"},
    )
    _pattern(selector["server_id"], SELECTOR_ID_PATTERN)
    _pattern(selector["route_digest"], DIGEST_PATTERN)
    _pattern(selector["document_sha256"], f"absent|{DIGEST_PATTERN}")
    if selector["route_id"] != route_id or not isinstance(selector["copy_id"], str) or not selector["copy_id"]:
        raise LaunchplaneSafetyError("invalid_response")
    backend = selector.get("backend")
    if backend is not None:
        if not isinstance(backend, str):
            raise LaunchplaneSafetyError("invalid_response")
        try:
            parsed = urlsplit(backend)
            valid = (
                parsed.scheme in {"http", "https"} and parsed.hostname
                and parsed.username is None and parsed.password is None
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise LaunchplaneSafetyError("invalid_response")
    return selector


def _validate_failure_reference(value: object) -> None:
    item = _private_item(value, {"evidence_id", "stage", "kind", "exit_status"})
    if item.get("evidence_id") is not None:
        _pattern(item["evidence_id"], r"unavailable|[a-f0-9]{32}")
    if item.get("stage") is not None:
        _code(item["stage"], FAILURE_STAGES)
    if item.get("kind") is not None:
        _code(item["kind"], {"command_failed", "refused", "unavailable"})
    exit_status = item.get("exit_status")
    if exit_status is not None and (type(exit_status) is not int or not -255 <= exit_status <= 255):
        raise LaunchplaneSafetyError("invalid_response")
    if any(item.get(name) is not None for name in ("evidence_id", "stage", "kind", "exit_status")):
        _require_retained_item(item, {"matched"})


def _require_retained_item(item: dict[str, Any], allowed_bindings: set[str]) -> None:
    if (
        item.get("availability") != "present" or item.get("coverage") != "retained_record_only"
        or item.get("binding") not in allowed_bindings
    ):
        raise LaunchplaneSafetyError("invalid_response")


def _validate_private_route(
    item: dict[str, Any], *, route_id: str, namespace: str, inverse: bool, evidence: dict[str, Any],
) -> dict[str, Any] | None:
    _nullable_booleans(item, {"route_digest_matches", "observed_backend_matches"})
    _observation_time(item.get("backend_observed_at"))
    if item.get("selector") is None:
        return None
    selector = _route_selector(item["selector"], route_id)
    _require_retained_item(item, {"matched", "unverified"})
    _require_retained_item(evidence, {"matched"})
    if (
        (selector["copy_id"] == namespace) == inverse
        or (item.get("attempt") is not None and evidence.get("attempt") is not None
            and item["attempt"] != evidence["attempt"])
        or item.get("route_digest_matches") is False
        or (item.get("route_digest_matches") is True and item["binding"] != "matched")
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return selector


def _validate_selectors(binding: dict[str, Any], operation: dict[str, Any], evidence: dict[str, Any]) -> None:
    scope = {name: binding[name] for name in ("product", "context")}
    namespace = "lp-testing-" + _digest({**scope, "operation": binding["operation_id"]})[:32]
    route_id = "lp-testing-route-" + _digest(scope)[:32]
    candidate = _private_item(binding.get("candidate", {}), {"selector"})
    selectors = []
    if candidate.get("selector") is not None:
        _require_retained_item(candidate, {"matched"})
        selectors.append(_candidate_selector(candidate["selector"], operation, namespace))
    for name in ("forward_route", "inverse_route"):
        item = _private_item(
            binding.get(name, {}), {"selector", "route_digest_matches", "observed_backend_matches", "backend_observed_at"},
        )
        selector = _validate_private_route(
            item, route_id=route_id, namespace=namespace, inverse=name == "inverse_route", evidence=evidence[name],
        )
        if selector is not None:
            selectors.append(selector)
    if selectors:
        _require_retained_item(evidence["checkpoint"], {"matched"})
        servers = {selector["server_id"] for selector in selectors}
        if binding.get("execution_server_id") is not None:
            servers.add(binding["execution_server_id"])
        if len(servers) != 1:
            raise LaunchplaneSafetyError("invalid_response")


def validate_native_execution_binding(
    value: object, *, operation: dict[str, Any], operation_id: object, public_evidence: object,
) -> dict[str, Any]:
    binding = _closed_object(
        value,
        {*SCOPE_FIELDS, "native_evidence"},
        {"schema_version", "reservation_id", "execution_server_id", *PRIVATE_ITEMS, *PRIVATE_DIGESTS, *ATTRIBUTION_FIELDS},
    )
    version = binding.get("schema_version", 1)
    if type(version) is not int or version != 1 or any(
        not isinstance(binding[name], str) or not binding[name].strip() or binding[name] != operation.get(name)
        for name in SCOPE_FIELDS
    ):
        raise LaunchplaneSafetyError("invalid_response")
    evidence: dict[str, Any] = project_native_evidence(binding["native_evidence"], operation=operation, operation_id=operation_id)
    if public_evidence is not None and public_evidence != evidence:
        raise LaunchplaneSafetyError("invalid_response")
    for name, source in ITEM_SOURCES.items():
        if evidence[name]["source"] != source:
            raise LaunchplaneSafetyError("invalid_response")
    for name in ATTRIBUTION_FIELDS:
        _code(binding.get(name, "not_retained"), {"not_retained"})
    for name in PRIVATE_DIGESTS:
        if binding.get(name) is not None:
            _pattern(binding[name], DIGEST_PATTERN)
    if binding.get("execution_server_id") is not None:
        _pattern(binding["execution_server_id"], SELECTOR_ID_PATTERN)
    if binding.get("reservation_id") is not None:
        reservation_key = "\x1f".join((
            "launchplane:testing-native-rebuild:v1", "worker:testing-native-rebuild", binding["operation_id"],
        ))
        expected_reservation = "mutation-reservation-" + hashlib.sha256(reservation_key.encode()).hexdigest()
        if binding["reservation_id"] != expected_reservation:
            raise LaunchplaneSafetyError("invalid_response")
        _require_retained_item(evidence["reservation"], {"matched"})
    reservation_attempt = evidence["reservation"]["attempt"]
    probe_attempt = evidence["terminal_probe"]["attempt"]
    if reservation_attempt is not None and probe_attempt is not None and probe_attempt > reservation_attempt:
        raise LaunchplaneSafetyError("invalid_response")
    _validate_selectors(binding, operation, evidence)
    _validate_failure_reference(binding.get("failure_reference", {}))
    return binding


def native_binding_receipt(binding: dict[str, Any]) -> dict[str, object]:
    try:
        private_json = json.dumps(binding, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except UnicodeEncodeError:
        raise LaunchplaneSafetyError("invalid_response") from None
    return {
        "status": "written",
        "binding_sha256": hashlib.sha256(private_json).hexdigest(),
        **{
            name: {key: binding.get(name, {}).get(key, default) for key, default in PRIVATE_STATUS.items()}
            for name in PRIVATE_ITEMS
        },
        **{name: binding.get(name, "not_retained") for name in ATTRIBUTION_FIELDS},
    }
