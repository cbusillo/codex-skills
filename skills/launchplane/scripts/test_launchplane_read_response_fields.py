#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
"""Launchplane's declared read response fields fit the caller's allowlists."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from launchplane_contract import BOUNDED_READ_RESPONSE_FIELDS  # noqa: E402

_spec = importlib.util.spec_from_file_location("sync_read_fields", SCRIPT_DIR / "sync-read-response-fields.py")
assert _spec is not None and _spec.loader is not None
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def test_every_field_launchplane_declares_is_accepted_by_its_read() -> None:
    pinned = json.loads(sync.PIN_PATH.read_text())
    assert pinned.keys() == BOUNDED_READ_RESPONSE_FIELDS.keys()
    unaccepted = {
        command: sorted(set(fields) - BOUNDED_READ_RESPONSE_FIELDS[command])
        for command, fields in pinned.items()
        if set(fields) - BOUNDED_READ_RESPONSE_FIELDS[command]
    }
    assert unaccepted == {}


def _openapi(extra_environment_field: str = "") -> bytes:
    """A synthetic OpenAPI document declaring every bounded read route."""
    environment = {"status": {}, "trace_id": {}, "environment": {}}
    if extra_environment_field:
        environment[extra_environment_field] = {}
    paths = {}
    for command in BOUNDED_READ_RESPONSE_FIELDS:
        path = sync.bounded_read_path(command)
        name = "Environment" if command == "product-environment-read" else command
        paths[path] = {
            "get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}}}
        }
    schemas = {command: {"properties": {"status": {}, "trace_id": {}}} for command in BOUNDED_READ_RESPONSE_FIELDS}
    schemas["Environment"] = {"properties": environment}
    return json.dumps({"paths": paths, "components": {"schemas": schemas}}).encode()


def test_check_reports_current_drift_and_unavailable(tmp_path: Path) -> None:
    pin = tmp_path / "pin.json"
    schema = tmp_path / "openapi.json"
    schema.write_bytes(_openapi())
    arguments = ["--openapi", str(schema)]
    assert sync.run(["sync", *arguments], pin=pin) == 0
    expected_environment = sorted(json.loads(schema.read_bytes())["components"]["schemas"]["Environment"]["properties"])
    assert json.loads(pin.read_bytes())["product-environment-read"] == expected_environment
    assert sync.run(["check", *arguments], pin=pin) == 0
    schema.write_bytes(_openapi("added_field"))
    assert sync.run(["check", *arguments], pin=pin) == 1
    missing_route = json.loads(_openapi())
    del missing_route["paths"][sync.bounded_read_path("product-environment-read")]
    schema.write_text(json.dumps(missing_route))
    assert sync.run(["check", *arguments], pin=pin) == 2
    schema.unlink()
    assert sync.run(["check", *arguments], pin=pin) == 2


def test_sync_reads_the_requested_document(tmp_path: Path) -> None:
    pin = tmp_path / "pin.json"
    for name, payload in (("first.json", _openapi()), ("second.json", _openapi("added_field"))):
        schema = tmp_path / name
        schema.write_bytes(payload)
        assert sync.run(["sync", "--openapi", str(schema)], pin=pin) == 0
        assert pin.read_bytes() == sync.render_pin(json.loads(payload))


@pytest.mark.parametrize("payload", [b"invalid", b"[]", b"null", b"{}"])
def test_invalid_documents_preserve_the_pin(tmp_path: Path, payload: bytes) -> None:
    schema = tmp_path / "openapi.json"
    pin = tmp_path / "pin.json"
    pin.write_bytes(sync.render_pin(json.loads(_openapi())))
    original = pin.read_bytes()
    schema.write_bytes(payload)
    assert sync.run(["sync", "--openapi", str(schema)], pin=pin) == 2
    assert pin.read_bytes() == original


def test_oversized_document_preserves_the_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    schema = tmp_path / "openapi.json"
    pin = tmp_path / "pin.json"
    payload = _openapi()
    schema.write_bytes(payload)
    pin.write_bytes(sync.render_pin(json.loads(payload)))
    original = pin.read_bytes()
    monkeypatch.setattr(sync, "MAX_OPENAPI_BYTES", len(payload) - 1)
    assert sync.run(["sync", "--openapi", str(schema)], pin=pin) == 2
    assert pin.read_bytes() == original


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
