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
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_safety import LaunchplaneSafetyError, build_launchplane_url, safe_urlopen, validate_service_url  # noqa: E402

SPEC = importlib.util.spec_from_file_location("caller_operator", Path(__file__).with_name("launchplane-write-action.py"))
assert SPEC is not None and SPEC.loader is not None
operator: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)
SAFE_OUTPUT = "x-launchplane-safe-output"
WITHHELD = object()
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


class Operation(NamedTuple):
    method: str
    path: str
    description: dict[str, Any]


def open_evidence(path: str) -> BinaryIO:
    target = Path(path).expanduser().absolute()
    if "task-evidence" not in target.parent.parts or target.parent.resolve(strict=True) != target.parent:
        raise ValueError("private_task_evidence_required")
    if target.is_relative_to(operator.active_repo_root()):
        raise ValueError("private_task_evidence_required")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "wb")


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
    for key in ("status", "trace_id"):
        if isinstance(payload.get(key), str):
            output[key] = payload[key]
    for key in ("error", "refusal"):
        value = payload.get(key)
        if isinstance(value, str):
            output[key] = value
        elif isinstance(value, dict):
            output[key] = {field: value[field] for field in ("code", "message", "reason") if isinstance(value.get(field), str)}
    return output


def exchange(
    settings: dict[str, str], method: str, path: str, query: dict[str, str], body: Any, key: str, timeout: float
) -> tuple[int, bytes]:
    url = build_launchplane_url(settings["service_url"], path, query=urllib.parse.urlencode(query))
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {settings['token']}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Idempotency-Key", key)
    try:
        response = safe_urlopen(request, timeout=timeout)
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


def call(args: argparse.Namespace, evidence: BinaryIO) -> int:
    settings = operator.resolve_settings(args)
    validate_service_url(settings["service_url"])
    if not settings["token"]:
        raise ValueError("operator_config_required")
    body = operator.read_payload_file(args.body_file) if args.body_file else None
    if args.mode is not None:
        body = {**(body or {}), "mode": args.mode}
    status, raw = exchange(settings, "GET", "/openapi.json", {}, None, "", args.timeout)
    if status != 200:
        evidence.write(raw)
        print(json.dumps(service_output(json.loads(raw), {}, {}, status)))
        return 1
    document = json.loads(raw)
    contract = document["x-launchplane-agent-contract"]["operations"]
    method, path, operation = find_operation(document, args.operation_id)
    for entry in contract:
        if entry["operation_id"] == args.operation_id and (entry["method"], entry["path"]) != (method, path):
            raise ValueError("description_disagreement")
    for name, value in pairs(args.path).items():
        path = path.replace("{" + name + "}", urllib.parse.quote(value, safe=""))
    try:
        status, raw = exchange(settings, method, path, pairs(args.query), body, args.idempotency_key, args.timeout)
    except (OSError, ValueError, http.client.HTTPException) as error:
        evidence.write(json.dumps({"transport_error": type(error).__name__, "detail": str(error)}).encode())
        print(json.dumps({"status": "outcome_unknown" if method not in {"GET", "HEAD", "OPTIONS"} else "unavailable"}))
        return 1
    evidence.write(raw)
    evidence.flush()
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        print(json.dumps({"http_status": status, "status": "unverified"}))
        return 1
    response = operation.get("responses", {}).get(str(status), operation.get("responses", {}).get("default", {}))
    response = resolve_schema(response, document)
    schema = response.get("content", {}).get("application/json", {}).get("schema", {})
    print(json.dumps(service_output(payload, schema, document, status)))
    return 0 if 200 <= status < 300 else 1


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
    try:
        with open_evidence(args.response_file) as evidence:
            try:
                return call(args, evidence)
            except (OSError, ValueError, KeyError, TypeError, LaunchplaneSafetyError, http.client.HTTPException) as error:
                evidence.write(json.dumps({"caller_error": type(error).__name__, "detail": str(error)}).encode())
                print(json.dumps({"status": "unavailable"}))
                return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "private_evidence_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
