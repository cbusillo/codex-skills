#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Permission profiles, source-surface drift checks and read-only App audits."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote

import github_read

MATRIX = Path(__file__).resolve().parents[1] / "references/operation-matrix.toml"
LEVELS = {"read": 1, "write": 2, "admin": 3}
SOURCE_ROOTS = ("github/scripts", "github-work-rollup/scripts", "babysit-pr/scripts", "direction/scripts")
HTTP_CALLS = {
    "request", "_transport_request", "get_json", "get_text", "paged_json",
    "graphql_json", "call_gh", "call_gh_with_retry", "_request_json",
    "api_json", "request_json", "gh_api",
    "urlopen", "Request",
}
ENDPOINT = re.compile(r"^/?(?:repos|orgs|users|user|app|installation|search|graphql|rate_limit)(?:/|\?|$)")
PROBES = {"metadata", "contents", "issues", "pull_requests", "checks", "statuses",
          "actions", "security_events", "vulnerability_alerts", "secret_scanning_alerts",
          "administration", "deployments", "discussions", "grant_only", "separate_actor"}


def load_matrix(path: Path = MATRIX) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def permission_profile(matrix: dict[str, Any], *, read_only: bool = False) -> dict[str, Any]:
    grants: dict[str, dict[str, str]] = {"repository": {}, "organization": {}}
    separate: list[dict[str, str]] = []
    for identifier, capability in sorted(matrix["capabilities"].items()):
        if capability["actor"] != "agent":
            separate.append({"capability": identifier, "actor": capability["actor"], "reason": capability["degraded_behavior"]})
            continue
        if read_only and not capability["read_supported"]:
            continue
        scope, name = capability["scope"], capability["permission"]
        access = "read" if read_only else capability["access"]
        previous = grants[scope].get(name, "")
        if LEVELS.get(previous, 0) < LEVELS[access]:
            grants[scope][name] = access
    return {
        "name": "read-only" if read_only else "full-operation",
        "permission_schema_version": matrix["permission_schema_version"],
        "permissions": {scope: dict(sorted(values.items())) for scope, values in grants.items()},
        "separate_actors": separate,
        "write_proof": "Permission declarations are not live write verification or authorization.",
    }


def permission_gaps(required: dict[str, str], observed: dict[str, str]) -> dict[str, str]:
    return {name: level for name, level in required.items()
            if LEVELS.get(observed.get(name, ""), 0) < LEVELS[level]}


def _static_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(value.value if isinstance(value, ast.Constant) and isinstance(value.value, str)
                       else "{}" for value in node.values)
    return None


def api_surface(path: Path) -> list[str]:
    """Capture endpoint/method/query declarations, excluding unrelated statements."""
    source = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return sorted(set(line.strip() for line in source.splitlines()
                          if re.search(r"\bgh\b|/repos/|/orgs/|/graphql|--method", line)))
    tree = ast.parse(source, filename=str(path))
    surfaces: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
            text = _static_text(node)
            if text and ENDPOINT.match(text):
                surfaces.add(f"{self.function}:endpoint:{text}")
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    self.visit(value.value)

        def visit_Constant(self, node: ast.Constant) -> None:
            value = node.value
            if isinstance(value, str):
                if ENDPOINT.match(value):
                    surfaces.add(f"{self.function}:endpoint:{value}")
                elif re.match(r"\s*(query|mutation)\b", value) and "{" in value:
                    surfaces.add(f"{self.function}:graphql:{' '.join(value.split())}")

        def visit_Call(self, node: ast.Call) -> None:
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in HTTP_CALLS:
                # Keep routing/method expressions, not request bodies, logging or tests.
                args = [ast.dump(arg, include_attributes=False) for arg in node.args[:2]]
                keywords = [f"{kw.arg}={ast.dump(kw.value, include_attributes=False)}"
                            for kw in node.keywords if kw.arg in {"path", "method", "url", "endpoint"}]
                surfaces.add(f"{self.function}:call:{name}:{'|'.join(args + keywords)}")
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(surfaces)


def source_fingerprints(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory in SOURCE_ROOTS:
        for path in sorted((root / directory).glob("*")):
            if not path.is_file() or path.name.startswith(("test", "validate-")):
                continue
            if path.suffix not in {".py", ".sh", ""}:
                continue
            surface = api_surface(path)
            if surface:
                result[str(path.relative_to(root))] = hashlib.sha256(
                    json.dumps(surface, separators=(",", ":")).encode()
                ).hexdigest()
    return result


def validate_permissions(matrix: dict[str, Any], root: Path, *, check_sources: bool = True) -> list[str]:
    errors: list[str] = []
    if matrix.get("permission_schema_version") != 1:
        errors.append("permission_schema_version must be 1")
    capabilities = matrix.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        return [*errors, "capabilities must be a non-empty table"]
    required = {"scope", "permission", "access", "actor", "read_supported", "probe", "degraded_behavior", "documentation"}
    for identifier, row in capabilities.items():
        if not isinstance(row, dict) or set(row) != required:
            errors.append(f"capability {identifier} must declare {', '.join(sorted(required))}")
            continue
        if row["scope"] not in ("repository", "organization", "account", "registry"):
            errors.append(f"capability {identifier} has invalid permission scope")
        if row["access"] not in tuple(LEVELS) or row["actor"] not in ("agent", "operator", "workflow", "project", "registry"):
            errors.append(f"capability {identifier} has invalid access or actor")
        if not isinstance(row["read_supported"], bool):
            errors.append(f"capability {identifier} must declare read_supported")
        for field in ("permission", "probe", "degraded_behavior", "documentation"):
            if not isinstance(row[field], str) or not row[field].strip():
                errors.append(f"capability {identifier} requires {field}")
        if isinstance(row["documentation"], str) and not row["documentation"].startswith("https://docs.github.com/"):
            errors.append(f"capability {identifier} requires primary GitHub documentation")
        if row["probe"] not in tuple(PROBES):
            errors.append(f"capability {identifier} has no implemented safe probe")
        if row["actor"] == "agent" and (row["scope"] != "repository" or row["probe"] == "separate_actor"):
            errors.append(f"capability {identifier} has unsupported agent scope or probe")
        if row["actor"] != "agent" and row["probe"] != "separate_actor":
            errors.append(f"capability {identifier} must preserve its separate actor")
    used: set[str] = set()
    for row in matrix.get("operations", []):
        if not isinstance(row, dict):
            errors.append("operation must be a table")
            continue
        refs = row.get("capabilities")
        mode = row.get("permission_mode")
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in capabilities for ref in refs):
            errors.append(f"operation {row.get('id')} has missing or unknown capabilities")
        else:
            used.update(refs)
        if mode not in ("fixed", "input_dependent", "local", "delegated"):
            errors.append(f"operation {row.get('id')} requires a permission_mode")
        if mode == "fixed" and not refs:
            errors.append(f"operation {row.get('id')} must declare its required capabilities")
        if not isinstance(row.get("permission_note"), str) or not row["permission_note"].strip():
            errors.append(f"operation {row.get('id')} requires permission/degraded/profile-impact notes")
        if mode == "local" and refs:
            errors.append(f"operation {row.get('id')} is local but declares remote grants")
    for identifier in sorted(set(capabilities) - used):
        errors.append(f"capability {identifier} has no supported operation")
    if check_sources:
        expected = matrix.get("api_surface_fingerprints", {})
        if not isinstance(expected, dict):
            return [*errors, "api_surface_fingerprints must be a table"]
        actual = source_fingerprints(root)
        for path in sorted(set(expected) | set(actual)):
            if expected.get(path) != actual.get(path):
                errors.append(f"API surface changed: {path}; review its permissions, probes, degraded behavior and profile impact in the operation matrix")
    return errors


def classify_probe(status: int, *, granted: bool, feature_enabled: bool | None = None,
                   message: str = "", accepted: str = "") -> dict[str, Any]:
    """A 403/404 never proves that a security signal is clean."""
    if feature_enabled is False:
        state = "not_enabled"
    elif not granted:
        state = "permission_missing"
    elif 200 <= status < 300:
        state = "available"
    elif status == 404 and message.casefold() == "no analysis found":
        state = "no_data"
    else:
        state = "unavailable"
    return {"state": state, "http_status": status,
            "reason": ("ambiguous_404" if status == 404 and state == "unavailable"
                       else "granted_but_denied" if status == 403 and granted and state == "unavailable"
                       else None),
            "accepted_permissions": accepted or None}


def _get_probe(reader: github_read.GitHubReader, path: str, step: str) -> dict[str, Any]:
    try:
        result = reader.request("GET", path, step=step)
    except github_read.GitHubReadError as error:
        result = error.result
    if not result.ok and (not result.status or 200 <= result.status < 300):
        return {"state": "unavailable", "reason": "read_failed", "http_status": result.status}
    body = result.body
    message = body.get("message", "") if isinstance(body, dict) else ""
    return classify_probe(int(result.status or 0), granted=True,
                          message=message if isinstance(message, str) else "",
                          accepted=result.headers.get("x-accepted-github-permissions", ""))


def audit_repository(reader: github_read.GitHubReader, repository: str, *,
                     installation: dict[str, Any], membership: set[str],
                     capabilities: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must use owner/name")
    if repository.casefold() not in membership:
        return {"repository": repository, "state": "not_installed", "capabilities": {}}
    if installation.get("suspended"):
        return {"repository": repository, "state": "unavailable", "reason": "installation_suspended", "capabilities": {}}
    try:
        metadata = reader.get_json(f"/repos/{repository}", step="capability_repository")
    except github_read.GitHubReadError:
        return {"repository": repository, "state": "unavailable", "capabilities": {}}
    if not isinstance(metadata, dict) or str(metadata.get("full_name", "")).casefold() != repository.casefold():
        return {"repository": repository, "state": "unavailable", "reason": "repository_identity_mismatch", "capabilities": {}}
    if metadata.get("archived") or metadata.get("disabled"):
        return {"repository": repository, "state": "excluded_archived_or_disabled", "capabilities": {}}
    default_ref = quote(str(metadata.get("default_branch") or ""), safe="")
    base = f"/repos/{repository}"
    paths = {
        "metadata": base, "contents": f"{base}/commits?per_page=1",
        "issues": f"{base}/issues?per_page=1", "pull_requests": f"{base}/pulls?per_page=1",
        "checks": f"{base}/commits/{default_ref}/check-runs?per_page=1",
        "statuses": f"{base}/commits/{default_ref}/status",
        "actions": f"{base}/actions/workflows?per_page=1",
        "security_events": f"{base}/code-scanning/alerts?per_page=1",
        "vulnerability_alerts": f"{base}/dependabot/alerts?per_page=1",
        "administration": f"{base}/actions/runners?per_page=1",
        "deployments": f"{base}/deployments?per_page=1",
    }
    findings: dict[str, Any] = {}
    read_probes: dict[str, dict[str, Any]] = {}
    for identifier, capability in capabilities.items():
        actor = capability["actor"]
        if actor != "agent":
            findings[identifier] = {"state": "requires_separate_actor", "actor": actor,
                                    "reason": capability["degraded_behavior"]}
            continue
        name, level = capability["permission"], capability["access"]
        if permission_gaps({name: level}, installation["permissions"]):
            findings[identifier] = {"state": "permission_missing", "permission": name, "required": level}
            continue
        probe = capability["probe"]
        if probe == "secret_scanning_alerts":
            signal = github_read.redacted_secret_scanning_status(reader, repository, limit=1)
            state = signal["status"]
            findings[identifier] = {"state": "available" if state in {"clean", "findings"} else "unavailable",
                                    "reason": signal.get("reason"), "literal_values_hidden": True}
            continue
        if probe == "discussions":
            if metadata.get("has_discussions") is False:
                findings[identifier] = {"state": "not_enabled", "permission_granted": True}
            else:
                owner, repo_name = repository.split("/")
                result = reader.graphql_json(
                    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){discussions(first:1){totalCount}}}",
                    {"owner": owner, "name": repo_name}, step="capability_discussions")
                if not result.ok or not isinstance(result.body, dict) or result.body.get("errors"):
                    findings[identifier] = {"state": "unavailable", "reason": "discussion_read_failed"}
                else:
                    data = result.body.get("data")
                    repo_data = data.get("repository") if isinstance(data, dict) else None
                    discussions = repo_data.get("discussions") if isinstance(repo_data, dict) else None
                    count = discussions.get("totalCount") if isinstance(discussions, dict) else None
                    findings[identifier] = ({"state": "permission_granted" if level == "write" else "available", "read_probe": "available", "write_probe": "not_exercised"}
                                            if isinstance(count, int) and not isinstance(count, bool)
                                            else {"state": "unavailable", "reason": "discussion_shape_invalid"})
            continue
        if probe == "grant_only":
            findings[identifier] = {"state": "permission_granted", "write_probe": "not_exercised"}
            continue
        if probe in {"checks", "statuses"} and not default_ref:
            findings[identifier] = {"state": "no_data", "reason": "no_default_branch"}
            continue
        if probe == "issues" and metadata.get("has_issues") is False:
            findings[identifier] = {"state": "not_enabled", "permission_granted": True}
            continue
        if probe not in read_probes:
            read_probes[probe] = (_get_probe(reader, paths[probe], f"capability_{probe}")
                                  if probe != "metadata" else {"state": "available", "http_status": 200})
        outcome = dict(read_probes[probe])
        if level == "write" and outcome["state"] == "available":
            outcome.update(state="permission_granted", read_probe="available", write_probe="not_exercised")
        findings[identifier] = outcome
    return {"repository": repository, "state": "audited", "visibility": metadata.get("visibility"),
            "capabilities": findings}
