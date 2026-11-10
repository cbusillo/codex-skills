# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import re
from datetime import datetime
from typing import Any

from launchplane_safety import LaunchplaneSafetyError

# https://github.com/cbusillo/launchplane/blob/dbda589bd6872f5a68337e68b18ae1b692272d74/control_plane/contracts/native_testing_rebuild_evidence.py
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


def _project_item(value: object) -> dict[str, object]:
    source = _closed_object(value, {"source"}, {*ITEM_CODES, *ITEM_BOOLEANS, "observed_at", "attempt"})
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
    observed = source.get("observed_at")
    if observed is not None:
        if not isinstance(observed, str) or not OBSERVATION_TIME.fullmatch(observed):
            raise LaunchplaneSafetyError("invalid_response")
        try:
            datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            raise LaunchplaneSafetyError("invalid_response") from None
    projected["observed_at"] = observed
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
        **{name: _project_item(source[name]) for name in ITEM_SOURCES},
        "predecessor_snapshot": predecessor,
        **_nullable_booleans(source, VIEW_BOOLEANS),
        "needed_copies": _code(source.get("needed_copies", "retain_all"), {"retain_all"}),
    }
