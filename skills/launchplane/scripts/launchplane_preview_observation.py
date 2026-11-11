# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
from typing import Any

from launchplane_inventory_readback import (
    boolean,
    execute_private_read,
    identifier,
    require,
    text,
)
from launchplane_native_evidence import _closed_object, _code, _observation_time

COMMAND = "preview-container-observation"
EXACT_FIELDS = {"product", "pr_number", "source_git_ref", "image_digest", "preview_id", "generation_id", "target_id"}
ORIGINAL_PATTERN = r"mutation-reservation-[a-f0-9]{64}"
# https://github.com/cbusillo/launchplane/blob/fc2a03fdf64d11fd922ebea7c1e2ff150eb235ec/control_plane/odoo_runtime_reads.py
OBSERVATION_CODES = {
    "selection": {"unknown", "unique", "missing", "ambiguous", "missing_or_unavailable"},
    "inventory_coverage": {"unknown", "complete", "invalid"},
    "existence_coverage": {"unknown", "observed"},
    "inspection_coverage": {"unknown", "observed", "invalid"},
    "running_coverage": {"unknown", "observed"},
    "runtime_identity": {"not_reached", "unknown", "verified", "mismatch"},
    "image_agreement": {"not_reached", "unknown", "verified", "mismatch"},
    "stage": {
        "selection",
        "target",
        "inventory",
        "inspection",
        "container_state",
        "container_binding",
        "runtime_identity",
        "image",
        "complete",
    },
}
OBSERVATION_TIMES = {"inventory_received_at", "inspection_attempted_at", "inspection_received_at"}
OBSERVATION_BOOLEANS = {"exists", "config_valid", "state_valid", "running"}


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        COMMAND,
        help="Save an exact preview's typed container observation privately without verifying runtime or import eligibility.",
    )
    parser.add_argument(
        "--payload-file",
        required=True,
        help="Private JSON with exact preview/source/target and original reservation or controller plan selectors.",
    )
    parser.add_argument("--result-file", required=True, help="New private result file outside the repository.")


def validate_intent(value: object) -> dict[str, Any]:
    source = _closed_object(value, {*EXACT_FIELDS, "mode"}, {"original_reservation_id", "plan_id"})
    require(source["mode"] == "container_observation")
    require(type(source["pr_number"]) is int and source["pr_number"] >= 1)
    text(source["product"], maximum=128, pattern=r"[A-Za-z0-9_.:-]+")
    text(source["source_git_ref"], pattern=r"[a-f0-9]{40}")
    text(source["image_digest"], pattern=r"sha256:[a-f0-9]{64}")
    for field in ("preview_id", "generation_id"):
        text(source[field], maximum=256)
        require(bool(source[field]))
    text(source["target_id"], maximum=256, pattern=r"[A-Za-z0-9_-]+")
    original = source.get("original_reservation_id", "")
    plan = source.get("plan_id", "")
    text(original, pattern=rf"(?:{ORIGINAL_PATTERN})?")
    text(plan, maximum=128, pattern=r"(?:odoo-preview-plan-[A-Za-z0-9_-]+)?")
    require(bool(original) != bool(plan))
    return source


def validate_observation(value: object) -> None:
    source = _closed_object(value, set(), {*OBSERVATION_CODES, *OBSERVATION_TIMES, *OBSERVATION_BOOLEANS, "container_id"})
    for field, codes in OBSERVATION_CODES.items():
        if field in source:
            _code(source[field], codes)
    for field in OBSERVATION_TIMES:
        if field in source:
            _observation_time(source[field])
    for field in OBSERVATION_BOOLEANS:
        if source.get(field) is not None:
            boolean(source[field])
    if source.get("container_id") is not None:
        text(source["container_id"], maximum=64, pattern=r"[a-f0-9]{12,64}")


def validate_response(value: object, intent: dict[str, Any]) -> dict[str, Any]:
    source = _closed_object(value, {"trace_id", "result"}, {"status"})
    require(source.get("status", "ok") == "ok")
    identifier(source["trace_id"])
    result = _closed_object(
        source["result"],
        {"observed_at", "binding", "current_binding", "observation"},
        {"mode", "read_only", "runtime_verification", "import_eligibility"},
    )
    require(result.get("mode", "container_observation") == "container_observation")
    require(result.get("read_only", True) is True)
    require(result.get("runtime_verification", "not_performed") == "not_performed")
    require(result.get("import_eligibility", "not_evaluated") == "not_evaluated")
    require(result["observed_at"] is not None)
    _observation_time(result["observed_at"])
    _code(result["current_binding"], {"current", "changed", "unavailable"})
    binding = _closed_object(result["binding"], {*EXACT_FIELDS, "artifact_id", "original_reservation_id"}, set())
    require(type(binding["pr_number"]) is int)
    for field in EXACT_FIELDS:
        require(binding[field] == intent[field])
    identifier(binding["artifact_id"])
    text(binding["original_reservation_id"], pattern=ORIGINAL_PATTERN)
    if intent.get("original_reservation_id"):
        require(binding["original_reservation_id"] == intent["original_reservation_id"])
    validate_observation(result["observation"])
    return source


def execute(h: Any, args: argparse.Namespace) -> int:
    return execute_private_read(
        h,
        args,
        command=COMMAND,
        intent_validator=validate_intent,
        response_validator=validate_response,
        invalid_transport_code="invalid_observation_transport",
        result_summary=lambda _: {"runtime_verification": "not_performed", "import_eligibility": "not_evaluated"},
        recommendation="Read the private partial container observations and current binding. Runtime verification and import eligibility remain unestablished; existing holds remain.",
    )
