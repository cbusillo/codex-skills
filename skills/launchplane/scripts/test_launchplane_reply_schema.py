#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
"""Behavior of the pinned controller reply schema checker and its sync."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import launchplane_reply_schema as reply_schema  # noqa: E402

_spec = importlib.util.spec_from_file_location("sync_reply_schema", SCRIPT_DIR / "sync-merge-train-reply-schema.py")
assert _spec is not None and _spec.loader is not None
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status"],
    "properties": {
        "status": {"const": "accepted", "type": "string"},
        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "item": {"$ref": "#/$defs/Item"},
    },
    "$defs": {"Item": {"type": "object", "additionalProperties": False,
                       "properties": {"name": {"type": "string", "maxLength": 4}}}},
}


def test_pinned_schema_is_enforceable() -> None:
    assert reply_schema.controller_reply_schema()["type"] == "object"


def test_declared_types_bounds_and_requirements_are_enforced() -> None:
    reply = {"status": "accepted", "count": None}
    assert reply_schema.check_controller_reply(reply, SCHEMA) == (reply, [])
    for reply in (
        {"count": 1},
        {"status": "pending"},
        {"status": "accepted", "count": True},
        {"status": "accepted", "item": {"name": "too long"}},
    ):
        with pytest.raises(reply_schema.ReplySchemaError) as raised:
            reply_schema.check_controller_reply(reply, SCHEMA)
        assert raised.value.code == "invalid_response"


def test_undeclared_fields_are_removed_and_named_unless_unsafe() -> None:
    reply = {"status": "accepted", "later": 1, "item": {"name": "ok", "later_name": "x"}}
    assert reply_schema.check_controller_reply(reply, SCHEMA) == (
        {"status": "accepted", "item": {"name": "ok"}}, ["item.later_name", "later"],
    )
    for unsafe in ({"status": "accepted", "client_secret": "x"},
                   {"status": "accepted", "https://private.example": "x"}):
        with pytest.raises(reply_schema.ReplySchemaError) as raised:
            reply_schema.check_controller_reply(unsafe, SCHEMA)
        assert raised.value.code == "unsafe_response_shape"


def test_a_schema_constraint_the_checker_would_not_enforce_is_refused() -> None:
    for properties in (
        {"status": {"type": "string", "pattern": "^a"}},
        {"item": {"$ref": "#/$defs/Item", "required": ["name"]}},
    ):
        with pytest.raises(reply_schema.ReplySchemaError):
            reply_schema.check_schema({**SCHEMA, "properties": properties})


def test_check_reports_current_drift_and_unavailable(tmp_path: Path) -> None:
    pin = tmp_path / "pin.json"
    pin.write_bytes(b'{"type": "object"}\n')
    assert sync.run(["check"], fetch=lambda ref: pin.read_bytes(), pin=pin) == 0
    assert sync.run(["check"], fetch=lambda ref: b'{"type": "object" }\n', pin=pin) == 1

    def unavailable(ref: str) -> bytes:
        raise OSError("offline")

    assert sync.run(["check"], fetch=unavailable, pin=pin) == 2


def test_sync_copies_bytes_only_when_enforceable(tmp_path: Path) -> None:
    pin = tmp_path / "pin.json"
    pin.write_bytes(b"old")
    upstream = json.dumps(SCHEMA, indent=2).encode() + b"\n"
    requested: list[str] = []
    assert sync.run(["sync", "--ref", "abc123"], fetch=lambda ref: requested.append(ref) or upstream, pin=pin) == 0
    assert requested == ["abc123"] and pin.read_bytes() == upstream
    refused = json.dumps({"type": "object", "properties": {"x": {"pattern": "a"}}}).encode()
    assert sync.run(["sync"], fetch=lambda ref: refused, pin=pin) == 1
    assert pin.read_bytes() == upstream


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
