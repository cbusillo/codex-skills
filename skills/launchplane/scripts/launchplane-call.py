#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography==50.0.2", "jsonschema==4.26.0", "rfc3339-validator==0.1.4", "websockets==17.4"]
# ///
from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_safety import validate_service_url  # noqa: E402
from launchplane_testing_state import open_private_response, save_private_response as save_evidence  # noqa: E402

SPEC = importlib.util.spec_from_file_location("caller_operator", Path(__file__).with_name("launchplane-write-action.py"))
assert SPEC is not None and SPEC.loader is not None
operator: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)
SAFE_OUTPUT = "x-launchplane-safe-output"
GENERIC_SURFACE = "generic_caller"
WITHHELD = object()
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


class Operation(NamedTuple):
    method: str
    path: str
    description: dict[str, Any]


def open_evidence(path: str) -> BinaryIO:
    return open_private_response(operator, path)


def resolve_schema(schema: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    visited: set[str] = set()
    while "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/") or reference in visited:
            return {}
        visited.add(reference)
        target: Any = document
        for part in reference[2:].split("/"):
            if not isinstance(target, dict):
                return {}
            target = target.get(part.replace("~1", "/").replace("~0", "~"))
        if not isinstance(target, dict):
            return {}
        schema = {**target, **{key: value for key, value in schema.items() if key != "$ref"}}
    return schema


def safe_fields(value: Any, schema: dict[str, Any], document: dict[str, Any], depth: int = 0) -> Any:
    if depth > 32:
        return WITHHELD
    schema = resolve_schema(schema, document)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value if schema.get(SAFE_OUTPUT) is True else WITHHELD
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        result = {}
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                projected = safe_fields(value[key], child, document, depth + 1)
                if projected is not WITHHELD:
                    result[key] = projected
        return result if result else WITHHELD
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        projected_items = [safe_fields(item, schema["items"], document, depth + 1) for item in value]
        result_items = [item for item in projected_items if item is not WITHHELD]
        return result_items if result_items else WITHHELD
    return WITHHELD


def service_output(payload: Any, schema: dict[str, Any], document: dict[str, Any], status: int) -> dict[str, Any]:
    projected = safe_fields(payload, schema, document)
    output = projected if isinstance(projected, dict) else {}
    output["http_status"] = status
    if not isinstance(payload, dict):
        return output
    if isinstance(payload.get("status"), str):
        output["status"] = operator.public_code(payload["status"])
    diagnostics = operator.service_response_diagnostics(payload)
    if diagnostics.get("trace_id"):
        output["trace_id"] = diagnostics["trace_id"]
    for key in ("error", "refusal"):
        value = payload.get(key)
        if isinstance(value, str):
            output[key] = operator.public_service_text(value)
        elif isinstance(value, dict):
            selected = diagnostics if key == "error" else operator.service_response_diagnostics({"error": value})
            output[key] = {
                field: selected["error_" + field] for field in ("code", "message", "reason") if selected.get("error_" + field)
            }
    return output


def exchange(
    settings: dict[str, str], method: str, path: str, query: dict[str, str], body: Any, key: str, timeout: float
) -> tuple[int, bytes]:
    try:
        response = operator.request_launchplane_response(
            service_url=settings["service_url"], path=path, settings=settings, method=method,
            body=body, query=query, timeout=timeout, idempotency_key=key, no_store=True,
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.code, raw


def pairs(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or name in result:
            raise ValueError("invalid_parameter")
        result[name] = item
    return result


def find_operation(document: dict[str, Any], operation_id: str) -> Operation:
    operations = [
        Operation(method.upper(), path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS and isinstance(operation, dict) and operation.get("operationId") == operation_id
    ]
    if len(operations) != 1:
        raise ValueError("operation_not_unique")
    return operations[0]


def call(args: argparse.Namespace, evidence: BinaryIO) -> tuple[int, dict[str, Any], bool]:
    settings = operator.resolve_settings(args)
    validate_service_url(settings["service_url"])
    if not settings["token"]:
        raise ValueError("operator_config_required")
    body = operator.read_payload_file(args.body_file) if args.body_file else None
    if args.mode is not None:
        body = {**(body or {}), "mode": args.mode}
    status, raw = exchange(settings, "GET", "/openapi.json", {}, None, "", args.timeout)
    if status != 200:
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            payload = {}
        output = service_output(payload, {}, {}, status)
        if not save_evidence(evidence, raw):
            output["status"] = "unavailable"
        return 1, output, False
    document = json.loads(raw)
    contract = document["x-launchplane-agent-contract"]["operations"]
    method, path, operation = find_operation(document, args.operation_id)
    entries = [entry for entry in contract if entry["operation_id"] == args.operation_id]
    if len(entries) != 1 or GENERIC_SURFACE not in entries[0]["supported_surfaces"]:
        raise ValueError("generic_caller_not_supported")
    if (entries[0]["method"], entries[0]["path"]) != (method, path):
        raise ValueError("description_disagreement")
    for name, value in pairs(args.path).items():
        path = path.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
    query = pairs(args.query)
    mutating = method not in {"GET", "HEAD", "OPTIONS"}
    try:
        status, raw = exchange(settings, method, path, query, body, args.idempotency_key, args.timeout)
    except (OSError, ValueError, http.client.HTTPException) as error:
        save_evidence(evidence, json.dumps({"transport_error": type(error).__name__, "detail": str(error)}).encode())
        return 1, {"status": "outcome_unknown" if mutating else "unavailable"}, mutating
    stored = save_evidence(evidence, raw)
    code = 0 if 200 <= status < 300 else 1
    try:
        payload = json.loads(raw)
        response = operation.get("responses", {}).get(str(status), operation.get("responses", {}).get("default", {}))
        response = resolve_schema(response, document)
        schema = response.get("content", {}).get("application/json", {}).get("schema", {})
        output = service_output(payload, schema, document, status)
    except (ValueError, UnicodeDecodeError, TypeError, KeyError, AttributeError):
        output = {"http_status": status, "status": "accepted_unverified" if 200 <= status < 300 else "unverified"}
        code = 1
    if not stored or (mutating and (status >= 500 or status == 408)):
        output["status"] = "outcome_unknown" if mutating else "unavailable"
        code = 1
    return code, output, mutating


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call an operation from Launchplane's live API description; save the full reply privately."
    )
    parser.add_argument("operation_id")
    parser.add_argument("--body-file")
    parser.add_argument("--mode")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--query", action="append", default=[])
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--response-file", required=True)
    parser.add_argument("--config")
    parser.add_argument("--env-config")
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    evidence = None
    code, output, mutating = 1, {"status": "private_evidence_unavailable"}, False
    try:
        evidence = open_evidence(args.response_file)
        try:
            code, output, mutating = call(args, evidence)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException) as error:
            save_evidence(evidence, json.dumps({"caller_error": type(error).__name__, "detail": str(error)}).encode())
            output = {"status": "unavailable"}
    except (OSError, ValueError):
        pass
    finally:
        if evidence is not None:
            try:
                evidence.close()
            except OSError:
                code = 1
                output["status"] = "outcome_unknown" if mutating else "unavailable"
    print(json.dumps(output))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
