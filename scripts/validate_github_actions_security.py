#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Validate trusted GitHub Actions sources and immutable references."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

ROOT = Path(__file__).resolve().parents[1]
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
VERSION_PROVENANCE_PATTERN = re.compile(r"^v\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
MUTABLE_REFERENCE_ALLOWLIST: Mapping[Path, frozenset[str]] = {}
ReferenceFileKind = Literal["workflow", "action"]


@dataclass(frozen=True)
class ActionClassification:
    trust: str
    privilege: str


@dataclass(frozen=True)
class ActionReference:
    path: Path
    line_number: int
    reference: str
    provenance: str | None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line_number}"


APPROVED_REMOTE_ACTIONS: Mapping[str, ActionClassification] = {
    "actions/checkout": ActionClassification("GitHub-maintained", "repository checkout"),
    "astral-sh/setup-uv": ActionClassification(
        "Approved third-party publisher", "Python tool bootstrap"
    ),
}


def initial_action_reference_files(root: Path) -> tuple[tuple[Path, ReferenceFileKind], ...]:
    workflow_files = sorted((root / ".github/workflows").glob("*.yml"))
    workflow_files.extend(sorted((root / ".github/workflows").glob("*.yaml")))
    composite_action_files = sorted((root / ".github/actions").rglob("action.yml"))
    composite_action_files.extend(sorted((root / ".github/actions").rglob("action.yaml")))
    classified_files = [
        *((path, "workflow") for path in workflow_files),
        *((path, "action") for path in composite_action_files),
    ]
    return tuple(dict.fromkeys(classified_files))


def mapping_entries(
    node: Node, *, visited: set[int] | None = None
) -> Iterator[tuple[ScalarNode, Node]]:
    if not isinstance(node, MappingNode):
        return
    visited = visited if visited is not None else set()
    if id(node) in visited:
        return
    visited.add(id(node))

    direct_entries: list[tuple[ScalarNode, Node]] = []
    merged_nodes: list[MappingNode] = []
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode):
            continue
        if key_node.tag != "tag:yaml.org,2002:merge":
            direct_entries.append((key_node, value_node))
            continue
        if isinstance(value_node, MappingNode):
            merged_nodes.append(value_node)
        elif isinstance(value_node, SequenceNode):
            merged_nodes.extend(
                item for item in value_node.value if isinstance(item, MappingNode)
            )
    yield from direct_entries
    effective_keys = {key_node.value for key_node, _value_node in direct_entries}
    for merged_node in merged_nodes:
        for key_node, value_node in mapping_entries(merged_node, visited=visited):
            if key_node.value in effective_keys:
                continue
            effective_keys.add(key_node.value)
            yield key_node, value_node


def mapping_values(node: Node, key: str) -> Iterator[tuple[ScalarNode, Node]]:
    for key_node, value_node in mapping_entries(node):
        if key_node.value == key:
            yield key_node, value_node


def workflow_uses_nodes(document: Node) -> Iterator[tuple[ScalarNode, Node]]:
    for _jobs_key, jobs_node in mapping_values(document, "jobs"):
        for _job_key, job_node in mapping_entries(jobs_node):
            yield from mapping_values(job_node, "uses")
            for _steps_key, steps_node in mapping_values(job_node, "steps"):
                if not isinstance(steps_node, SequenceNode):
                    continue
                for step_node in steps_node.value:
                    yield from mapping_values(step_node, "uses")


def composite_action_uses_nodes(document: Node) -> Iterator[tuple[ScalarNode, Node]]:
    for _runs_key, runs_node in mapping_values(document, "runs"):
        for _steps_key, steps_node in mapping_values(runs_node, "steps"):
            if not isinstance(steps_node, SequenceNode):
                continue
            for step_node in steps_node.value:
                yield from mapping_values(step_node, "uses")


def uses_nodes(
    document: Node, file_kind: ReferenceFileKind
) -> Iterator[tuple[ScalarNode, Node]]:
    if file_kind == "workflow":
        yield from workflow_uses_nodes(document)
    else:
        yield from composite_action_uses_nodes(document)


def inline_provenance(source_lines: list[str], value_node: ScalarNode) -> str | None:
    line_number = value_node.end_mark.line
    if line_number >= len(source_lines):
        return None
    trailing_text = source_lines[line_number][value_node.end_mark.column :]
    comment_index = trailing_text.find("#")
    if comment_index == -1:
        return None
    provenance = trailing_text[comment_index + 1 :].strip()
    return provenance or None


def parse_action_file(
    path: Path, root: Path, file_kind: ReferenceFileKind
) -> tuple[list[ActionReference], list[str]]:
    relative_path = path.relative_to(root)
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    try:
        documents = tuple(yaml.compose_all(source))
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line_number = mark.line + 1 if mark is not None else 1
        problem = getattr(error, "problem", None) or "invalid YAML"
        return [], [f"{relative_path}:{line_number}: {problem}."]

    references: list[ActionReference] = []
    violations: list[str] = []
    for document in documents:
        if document is None:
            continue
        for key_node, value_node in uses_nodes(document, file_kind):
            line_number = key_node.start_mark.line + 1
            if not isinstance(value_node, ScalarNode) or not value_node.value.strip():
                violations.append(
                    f"{relative_path}:{line_number}: uses must be a non-empty scalar reference."
                )
                continue
            references.append(
                ActionReference(
                    path=relative_path,
                    line_number=line_number,
                    reference=value_node.value.strip(),
                    provenance=inline_provenance(source_lines, value_node),
                )
            )
    return references, violations


def local_reference_files(
    root: Path, reference: str
) -> tuple[tuple[Path, ReferenceFileKind], ...]:
    if not reference.startswith("./") or "@" in reference:
        return ()
    target = (root / reference[2:]).resolve()
    if not target.is_relative_to(root.resolve()):
        return ()
    if target.is_file() and target.suffix in {".yml", ".yaml"}:
        return ((target, "workflow"),)
    if not target.is_dir():
        return ()
    action_files = [target / "action.yml", target / "action.yaml"]
    return tuple((path, "action") for path in action_files if path.is_file())


def parse_action_references(root: Path) -> tuple[list[ActionReference], list[str]]:
    root = root.resolve()
    references: list[ActionReference] = []
    violations: list[str] = []
    pending = list(initial_action_reference_files(root))
    visited: set[tuple[Path, ReferenceFileKind]] = set()
    while pending:
        path, file_kind = pending.pop()
        file_identity = (path, file_kind)
        if file_identity in visited:
            continue
        visited.add(file_identity)
        file_references, file_violations = parse_action_file(path, root, file_kind)
        references.extend(file_references)
        violations.extend(file_violations)
        for action in file_references:
            pending.extend(local_reference_files(root, action.reference))
    return references, violations


def validate_repository(root: Path = ROOT, *, require_exact_sources: bool = True) -> list[str]:
    root = root.resolve()
    references, violations = parse_action_references(root)
    observed_sources: set[str] = set()
    observed_allowlist_entries: set[tuple[Path, str]] = set()

    for action in references:
        if action.reference.startswith("./"):
            if "@" in action.reference:
                violations.append(
                    f"{action.location}: local action reference must not include a revision."
                )
            elif not (root / action.reference[2:]).resolve().is_relative_to(root):
                violations.append(
                    f"{action.location}: local action reference must remain inside the repository."
                )
            continue

        source, separator, revision = action.reference.rpartition("@")
        if not separator:
            violations.append(
                f"{action.location}: remote action reference must include a full commit SHA."
            )
            continue

        classification = APPROVED_REMOTE_ACTIONS.get(source)
        if classification is None:
            violations.append(
                f"{action.location}: unapproved remote action source {source!r}."
            )
        else:
            observed_sources.add(source)

        is_allowlisted = action.reference in MUTABLE_REFERENCE_ALLOWLIST.get(
            action.path, frozenset()
        )
        if is_allowlisted:
            observed_allowlist_entries.add((action.path, action.reference))
        if not is_allowlisted and FULL_SHA_PATTERN.fullmatch(revision) is None:
            violations.append(
                f"{action.location}: remote action {source!r} must use a 40-character SHA."
            )
        if action.provenance is None:
            violations.append(
                f"{action.location}: remote action {source!r} must document its release tag."
            )
        elif VERSION_PROVENANCE_PATTERN.fullmatch(action.provenance) is None:
            violations.append(
                f"{action.location}: release provenance must be a version tag, not "
                f"{action.provenance!r}."
            )

    if require_exact_sources:
        unused_sources = sorted(set(APPROVED_REMOTE_ACTIONS) - observed_sources)
        if unused_sources:
            violations.append(
                "approved remote action sources are unused: " + ", ".join(unused_sources)
            )

    configured_allowlist_entries = {
        (path, reference)
        for path, references_for_path in MUTABLE_REFERENCE_ALLOWLIST.items()
        for reference in references_for_path
    }
    unused_allowlist_entries = configured_allowlist_entries - observed_allowlist_entries
    if unused_allowlist_entries:
        formatted_entries = ", ".join(
            f"{path}={reference}"
            for path, reference in sorted(
                unused_allowlist_entries, key=lambda entry: (str(entry[0]), entry[1])
            )
        )
        violations.append("mutable reference allowlist entries are unused: " + formatted_entries)

    return violations


def main() -> int:
    violations = validate_repository()
    if violations:
        print("GitHub Actions security validation failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("ok github-actions-security")
    return 0


if __name__ == "__main__":
    sys.exit(main())
