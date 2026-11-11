#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.2.0",
# ]
# ///
"""Launchplane's declared read response fields fit the caller's allowlists."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from launchplane_contract import BOUNDED_READ_RESPONSE_FIELDS  # noqa: E402

_spec = importlib.util.spec_from_file_location("check_read_fields", SCRIPT_DIR / "check-read-response-fields.py")
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _openapi(extra_environment_field: str = "") -> bytes:
    paths = {}
    schemas = {}
    for command, accepted in BOUNDED_READ_RESPONSE_FIELDS.items():
        name = "Environment" if command == "product-environment-read" else command
        paths[check.bounded_read_path(command)] = {
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}}}
        }
        schemas[name] = {"type": "object", "additionalProperties": False, "properties": {field: {} for field in accepted}}
    if extra_environment_field:
        schemas["Environment"]["properties"][extra_environment_field] = {}
    return json.dumps({"paths": paths, "components": {"schemas": schemas}}).encode()


def _check(tmp_path: Path, payload: bytes) -> int:
    schema = tmp_path / "openapi.json"
    schema.write_bytes(payload)
    return check.run(["--openapi", str(schema)])


def test_declared_fields_inside_the_allowlist_pass(tmp_path: Path) -> None:
    assert _check(tmp_path, _openapi()) == 0


def test_a_declared_field_outside_the_allowlist_names_the_read_and_field(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check(tmp_path, _openapi("added_field")) == 1
    assert "product-environment-read declares added_field" in capsys.readouterr().err


def test_an_accepted_field_the_producer_no_longer_declares_still_passes(tmp_path: Path) -> None:
    document = json.loads(_openapi())
    del document["components"]["schemas"]["Environment"]["properties"]["host_network_capacity"]
    assert _check(tmp_path, json.dumps(document).encode()) == 0


def test_a_missing_read_route_is_unavailable(tmp_path: Path) -> None:
    document = json.loads(_openapi())
    del document["paths"][check.bounded_read_path("product-environment-read")]
    assert _check(tmp_path, json.dumps(document).encode()) == 2


def _environment_route_schema(document: dict[str, Any]) -> dict[str, Any]:
    route = document["paths"][check.bounded_read_path("product-environment-read")]
    return route["get"]["responses"]["200"]["content"]["application/json"]


@pytest.mark.parametrize(
    "hide_a_field",
    [
        lambda document: document["components"]["schemas"]["Environment"].update(allOf=[{"properties": {"added_field": {}}}]),
        lambda document: document["components"]["schemas"]["Environment"].update(additionalProperties=True),
        lambda document: document["components"]["schemas"]["Environment"].pop("additionalProperties"),
        lambda document: _environment_route_schema(document)["schema"].update(properties={"added_field": {}}),
        lambda document: _environment_route_schema(document).update(schema={"$ref": 17}),
        lambda document: document["components"]["schemas"].update(Environment={"$ref": "#/components/schemas/Environment"}),
    ],
    ids=["composition", "open-object", "unstated-additional-properties", "reference-siblings", "non-string-reference", "reference-cycle"],
)
def test_a_schema_that_could_hide_a_field_is_unavailable(tmp_path: Path, hide_a_field: Callable[[dict[str, Any]], object]) -> None:
    document = json.loads(_openapi())
    hide_a_field(document)
    assert _check(tmp_path, json.dumps(document).encode()) == 2


@pytest.mark.parametrize("payload", [b"invalid", b"[]", b"null", b"{}"])
def test_invalid_documents_are_unavailable(tmp_path: Path, payload: bytes) -> None:
    assert _check(tmp_path, payload) == 2


def test_missing_document_is_unavailable(tmp_path: Path) -> None:
    assert check.run(["--openapi", str(tmp_path / "absent.json")]) == 2


def test_oversized_document_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _openapi()
    monkeypatch.setattr(check, "MAX_OPENAPI_BYTES", len(payload) - 1)
    assert _check(tmp_path, payload) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
