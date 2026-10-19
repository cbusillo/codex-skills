# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read Launchplane merge-train controller replies through Launchplane's published schema.

Launchplane owns the controller reply contract and publishes it as
`contracts/merge-train-controller-reply.schema.json`. This skill pins a
byte-for-byte copy in `references/`, refreshed with
`sync-merge-train-reply-schema.py`; CI fails when the pin differs from
Launchplane's `main`.

The checker enforces every declared type, required field and value bound. A
field the pin does not declare is newer than the pin: it is dropped from the
projection and named, not treated as a failure, so a Launchplane deploy that
adds a reply field no longer stops a train before the pin is refreshed. A
field name that is not a plain public-safe identifier fails closed here; the
caller's public-safety checks still scan every value.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from launchplane_safety import is_denied_key

CONTROLLER_REPLY_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "references" / "merge-train-controller-reply.schema.json"
)

# Keywords the checker enforces, and annotations it may ignore. Any other
# keyword fails closed when the schema loads, so a pin that needs a keyword the
# checker does not understand is refused instead of half-enforced.
_ENFORCED_KEYWORDS = frozenset({
    "$defs", "$ref", "type", "properties", "required", "additionalProperties",
    "items", "anyOf", "enum", "const", "maxLength",
})
_ANNOTATIONS = frozenset({"$schema", "title", "description", "default"})
_JSON_TYPES = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}
_PLAIN_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
MAX_UNRECOGNIZED_FIELDS = 10


class ReplySchemaError(ValueError):
    """The pinned schema cannot be enforced, or a reply does not match it.

    `code` is a compact public-safe reason the caller can report. This module
    keeps its own error type so callers translate it into theirs.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_schema(schema: object) -> dict[str, Any]:
    """Return the schema when every subschema uses only enforced keywords."""

    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ReplySchemaError("schema_root_not_object")
    _check_subschema(schema, schema, "#")
    return schema


def _check_subschema(node: object, root: dict[str, Any], path: str) -> None:
    if isinstance(node, bool):
        return
    if not isinstance(node, dict):
        raise ReplySchemaError(f"schema_node_not_object:{path}")
    unknown = set(node) - _ENFORCED_KEYWORDS - _ANNOTATIONS
    if unknown:
        raise ReplySchemaError(f"unsupported_schema_keyword:{path}:{min(unknown)}")
    if "$ref" in node:
        _resolve(root, node["$ref"])
    for kind in node.get("type", []) if isinstance(node.get("type"), list) else [node.get("type")]:
        if kind is not None and kind not in _JSON_TYPES:
            raise ReplySchemaError(f"unsupported_schema_type:{path}")
    for name, child in (node.get("$defs") or {}).items():
        _check_subschema(child, root, f"{path}/$defs/{name}")
    for name, child in (node.get("properties") or {}).items():
        _check_subschema(child, root, f"{path}/properties/{name}")
    for index, child in enumerate(node.get("anyOf") or []):
        _check_subschema(child, root, f"{path}/anyOf/{index}")
    for key in ("items", "additionalProperties"):
        if key in node:
            _check_subschema(node[key], root, f"{path}/{key}")


def _resolve(root: dict[str, Any], reference: object) -> dict[str, Any]:
    if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
        raise ReplySchemaError("unsupported_schema_reference")
    target = (root.get("$defs") or {}).get(reference.removeprefix("#/$defs/"))
    if not isinstance(target, dict):
        raise ReplySchemaError("unresolved_schema_reference")
    return target


@cache
def controller_reply_schema() -> dict[str, Any]:
    try:
        return check_schema(json.loads(CONTROLLER_REPLY_SCHEMA_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise ReplySchemaError("controller_reply_schema_unavailable") from exc


def declared_record_keys(schema: dict[str, Any] | None = None) -> frozenset[str]:
    """The `records` keys the schema declares."""

    root = controller_reply_schema() if schema is None else schema
    node = (root.get("properties") or {}).get("records")
    if isinstance(node, dict) and "$ref" in node:
        node = _resolve(root, node["$ref"])
    if not isinstance(node, dict):
        raise ReplySchemaError("controller_reply_schema_unavailable")
    return frozenset(node.get("properties") or {})


def check_controller_reply(reply: object, schema: dict[str, Any] | None = None) -> list[str]:
    """Validate a controller reply; return the dotted names of fields the pin does not declare.

    Raises `ReplySchemaError("invalid_response")` for a declared field with
    the wrong type or bound, or a missing required field, and
    `ReplySchemaError("unsafe_response_shape")` for an undeclared field whose
    name is not a plain, public-safe identifier. Values are not scanned here;
    the caller's public-safety checks own that.
    """

    root = controller_reply_schema() if schema is None else schema
    unrecognized: list[str] = []
    _check_value(reply, root, root, "", unrecognized)
    return sorted(unrecognized)[:MAX_UNRECOGNIZED_FIELDS]


def _check_value(
    value: object, node: object, root: dict[str, Any], path: str, unrecognized: list[str]
) -> None:
    if node is True:
        return
    if not isinstance(node, dict):
        raise ReplySchemaError("invalid_response")
    if "$ref" in node:
        node = _resolve(root, node["$ref"])
    if "anyOf" in node:
        for option in node["anyOf"]:
            found: list[str] = []
            try:
                _check_value(value, option, root, path, found)
            except ReplySchemaError as exc:
                if exc.code == "unsafe_response_shape":
                    raise
                continue
            unrecognized.extend(found)
            break
        else:
            raise ReplySchemaError("invalid_response")
    kind = node.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_JSON_TYPES[item](value) for item in kinds):
            raise ReplySchemaError("invalid_response")
    if "const" in node and value != node["const"]:
        raise ReplySchemaError("invalid_response")
    if "enum" in node and value not in node["enum"]:
        raise ReplySchemaError("invalid_response")
    if "maxLength" in node and isinstance(value, str) and len(value) > node["maxLength"]:
        raise ReplySchemaError("invalid_response")
    if isinstance(value, list) and "items" in node:
        for index, item in enumerate(value):
            _check_value(item, node["items"], root, f"{path}[{index}]", unrecognized)
    if isinstance(value, dict) and (
        "properties" in node or "required" in node or "additionalProperties" in node
    ):
        _check_object(value, node, root, path, unrecognized)


def _check_object(
    value: dict[Any, Any], node: dict[str, Any], root: dict[str, Any], path: str,
    unrecognized: list[str],
) -> None:
    properties: dict[str, Any] = node.get("properties") or {}
    if any(name not in value for name in node.get("required") or []):
        raise ReplySchemaError("invalid_response")
    extra_schema = node.get("additionalProperties", True)
    for key, item in value.items():
        name = f"{path}.{key}" if path else str(key)
        if key in properties:
            _check_value(item, properties[key], root, name, unrecognized)
        elif extra_schema is False:
            if not isinstance(key, str) or not _PLAIN_FIELD_NAME.fullmatch(key) or is_denied_key(key):
                raise ReplySchemaError("unsafe_response_shape")
            unrecognized.append(name)
        else:
            _check_value(item, extra_schema, root, name, unrecognized)
