# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bounded, value-free projection for Dokploy inventory pages."""

from __future__ import annotations

import re
from typing import Any

from launchplane_contract import BOUNDED_READ_RESPONSE_FIELDS
from launchplane_safety import (
    CREDENTIAL_ASSIGNMENT_HEAD_RE,
    TOKEN_PATTERNS,
    LaunchplaneSafetyError,
    is_denied_key,
    public_trace_id,
)


def _metadata_text(value: object) -> str:
    """Keep display metadata exact; prose redactors change legitimate names."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise LaunchplaneSafetyError("invalid_response")
    if any(ord(character) < 32 for character in value):
        raise LaunchplaneSafetyError("invalid_response")
    for pattern in TOKEN_PATTERNS:
        # Short prefix fragments occur in nanoids and ordinary display names.
        bounded_pattern = re.compile(
            r"(?<![A-Za-z0-9])(?:" + pattern.pattern + r")", pattern.flags
        )
        for match in bounded_pattern.finditer(value):
            if match.group().lower().startswith("bearer ") or len(match.group()) >= 32:
                raise LaunchplaneSafetyError("unsafe_response_shape")
    if any(
        is_denied_key(match[1])
        for match in CREDENTIAL_ASSIGNMENT_HEAD_RE.finditer(value)
    ):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return value


def _identifier(value: object) -> str:
    text = _metadata_text(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise LaunchplaneSafetyError("invalid_response")
    return text


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return value


def _number(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _list(value: object, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise LaunchplaneSafetyError("invalid_response")
    return value


def project_inventory(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, dict) or not (
        {"status", "trace_id", "inventory"} <= payload.keys() <= BOUNDED_READ_RESPONSE_FIELDS["dokploy-inventory-read"]
    ):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if payload["status"] != "ok":
        raise LaunchplaneSafetyError("invalid_response")
    source = _object(
        payload["inventory"],
        {
            "scope",
            "provider_pagination",
            "inventory_digest",
            "total_projects",
            "total_environments",
            "total_targets",
            "offset",
            "limit",
            "next_offset",
            "projects",
        },
    )
    if (
        source["scope"] != "provider_credential_visible"
        or source["provider_pagination"] != "unpaginated_project_all"
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if not isinstance(source["inventory_digest"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", source["inventory_digest"]
    ):
        raise LaunchplaneSafetyError("invalid_response")
    result = {
        key: source[key] for key in ("scope", "provider_pagination", "inventory_digest")
    }
    for key, maximum in (
        ("total_projects", 2000),
        ("total_environments", 10000),
        ("total_targets", 20000),
        ("offset", 2000),
        ("limit", 100),
    ):
        result[key] = _number(source[key], maximum)
    if result["limit"] < 1:
        raise LaunchplaneSafetyError("invalid_response")
    projects = []
    project_ids: set[str] = set()
    environment_ids: set[str] = set()
    target_ids: set[tuple[str, str]] = set()
    for raw_project in _list(source["projects"], result["limit"]):
        project = _object(raw_project, {"project_id", "name", "environments"})
        project_id = _identifier(project["project_id"])
        if project_id in project_ids:
            raise LaunchplaneSafetyError("invalid_response")
        project_ids.add(project_id)
        environments = []
        for raw_environment in _list(
            project["environments"], result["total_environments"]
        ):
            environment = _object(
                raw_environment, {"environment_id", "name", "targets"}
            )
            environment_id = _identifier(environment["environment_id"])
            if environment_id in environment_ids:
                raise LaunchplaneSafetyError("invalid_response")
            environment_ids.add(environment_id)
            targets = []
            for raw_target in _list(environment["targets"], 500):
                target = _object(
                    raw_target, {"target_id", "category", "name", "placement"}
                )
                category = target["category"]
                if not isinstance(category, str) or category not in {
                    "application",
                    "compose",
                    "postgres",
                    "mysql",
                    "mariadb",
                    "mongo",
                    "redis",
                    "libsql",
                }:
                    raise LaunchplaneSafetyError("invalid_response")
                target_id = _identifier(target["target_id"])
                if (category, target_id) in target_ids:
                    raise LaunchplaneSafetyError("invalid_response")
                target_ids.add((category, target_id))
                placement = _object(
                    target["placement"], {"kind", "server_id", "server_name"}
                )
                if placement["kind"] == "server":
                    placement = {
                        "kind": "server",
                        "server_id": _identifier(placement["server_id"]),
                        "server_name": _metadata_text(placement["server_name"]),
                    }
                elif placement != {
                    "kind": "dokploy_host",
                    "server_id": None,
                    "server_name": None,
                }:
                    raise LaunchplaneSafetyError("invalid_response")
                targets.append(
                    {
                        "target_id": target_id,
                        "category": category,
                        "name": _metadata_text(target["name"]),
                        "placement": placement,
                    }
                )
            environments.append(
                {
                    "environment_id": environment_id,
                    "name": _metadata_text(environment["name"]),
                    "targets": targets,
                }
            )
        projects.append(
            {
                "project_id": project_id,
                "name": _metadata_text(project["name"]),
                "environments": environments,
            }
        )
    end = result["offset"] + len(projects)
    if end > result["total_projects"] or (
        end < result["total_projects"] and not projects
    ):
        raise LaunchplaneSafetyError("invalid_response")
    expected_next = end if end < result["total_projects"] else None
    if source["next_offset"] != expected_next or type(source["next_offset"]) is bool:
        raise LaunchplaneSafetyError("invalid_response")
    if len(environment_ids) > result["total_environments"] or len(target_ids) > min(
        500, result["total_targets"]
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if (
        result["offset"] == 0
        and expected_next is None
        and (
            len(environment_ids) != result["total_environments"]
            or len(target_ids) != result["total_targets"]
        )
    ):
        raise LaunchplaneSafetyError("invalid_response")
    result.update(projects=projects, next_offset=expected_next)
    return result, public_trace_id(payload["trace_id"])
