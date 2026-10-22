"""Parse and render PEP 723 script metadata with the standard library only.

The updater and the trusted proposal job share these semantics. The proposal
job runs without installed dependencies, so nothing here may import a
third-party package.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence


SCRIPT_MARKER = "# /// script"
BLOCK_END_MARKER = "# ///"
UV_SCRIPT_SHEBANG = "#!/usr/bin/env -S uv run --script"
PACKAGE_NAME_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
DEPENDENCIES_ASSIGNMENT = re.compile(r"(?m)^[ \t]*dependencies[ \t]*=")
# PEP 503 name normalization, as packaging.utils.canonicalize_name documents.
NAME_SEPARATORS = re.compile(r"[-_.]+")


class DependencyPolicyError(RuntimeError):
    """Raised when repository metadata cannot be handled safely."""


@dataclass(frozen=True)
class ScriptMetadata:
    path: Path
    source: str
    source_lines: tuple[str, ...]
    block_start: int
    block_end: int
    metadata_text: str
    metadata: Mapping[str, object]
    has_dependencies_key: bool
    dependencies: tuple[str, ...]



def normalize_package_name(name: str) -> str:
    return NAME_SEPARATORS.sub("-", name).lower()


def _line_body(line: str) -> str:
    return line.rstrip("\r\n")


def read_source_text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _metadata_line(line: str, path: Path) -> str:
    line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    body = _line_body(line)
    if body == "#":
        return line_ending
    if not body.startswith("# "):
        raise DependencyPolicyError(f"{path}: invalid PEP 723 metadata line {body!r}")
    return body[2:] + line_ending


def _top_level_dependencies_span(metadata_text: str) -> tuple[int, int]:
    matches: list[tuple[int, int]] = []
    table_started = False
    offset = 0
    for line in metadata_text.splitlines(keepends=True):
        body = _line_body(line)
        stripped = body.lstrip()
        if stripped.startswith("["):
            table_started = True
        if not table_started:
            match = DEPENDENCIES_ASSIGNMENT.match(metadata_text, offset, offset + len(body))
            if match is not None:
                matches.append((match.start(), match.end()))
        offset += len(line)
    if len(matches) != 1:
        raise DependencyPolicyError(
            "PEP 723 metadata must contain exactly one canonical top-level dependencies key"
        )
    return matches[0]


def parse_script(path: Path, source: str | None = None) -> ScriptMetadata | None:
    source = read_source_text(path) if source is None else source
    source_lines = tuple(source.splitlines(keepends=True))
    header_lines = tuple(_line_body(line) for line in source_lines[:5])
    marker_positions = tuple(
        line_number
        for line_number, line in enumerate(source_lines)
        if _line_body(line) == SCRIPT_MARKER
    )
    is_uv_script = bool(header_lines and header_lines[0] == UV_SCRIPT_SHEBANG)
    if not marker_positions:
        if is_uv_script:
            raise DependencyPolicyError(f"{path}: uv script is missing a header PEP 723 block")
        return None
    if len(marker_positions) != 1:
        raise DependencyPolicyError(f"{path}: expected exactly one PEP 723 script metadata block")
    if marker_positions[0] >= len(header_lines):
        raise DependencyPolicyError(f"{path}: PEP 723 metadata block must start in the first five lines")

    block_start = marker_positions[0]
    block_end = next(
        (
            line_number
            for line_number in range(block_start + 1, len(source_lines))
            if _line_body(source_lines[line_number]) == BLOCK_END_MARKER
        ),
        None,
    )
    if block_end is None:
        raise DependencyPolicyError(f"{path}: unclosed PEP 723 script metadata block")

    metadata_text = "".join(
        _metadata_line(line, path) for line in source_lines[block_start + 1 : block_end]
    )
    try:
        metadata = tomllib.loads(metadata_text)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyPolicyError(f"{path}: invalid PEP 723 TOML: {exc}") from exc

    has_dependencies_key = "dependencies" in metadata
    dependencies = metadata.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise DependencyPolicyError(f"{path}: PEP 723 dependencies must be a list")
    if not all(isinstance(item, str) for item in dependencies):
        raise DependencyPolicyError(f"{path}: PEP 723 dependencies must contain only strings")
    if has_dependencies_key:
        try:
            _top_level_dependencies_span(metadata_text)
        except DependencyPolicyError as exc:
            raise DependencyPolicyError(f"{path}: {exc}") from exc
    return ScriptMetadata(
        path=path,
        source=source,
        source_lines=source_lines,
        block_start=block_start,
        block_end=block_end,
        metadata_text=metadata_text,
        metadata=metadata,
        has_dependencies_key=has_dependencies_key,
        dependencies=tuple(dependencies),
    )



def _skip_space_and_comments(text: str, position: int) -> int:
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        if text[position] == "#":
            newline = text.find("\n", position)
            return len(text) if newline < 0 else _skip_space_and_comments(text, newline + 1)
        return position
    return position


def _array_end(text: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    triple = False
    escaped = False
    in_comment = False
    position = start
    while position < len(text):
        character = text[position]
        if in_comment:
            if character == "\n":
                in_comment = False
            position += 1
            continue
        if quote is not None:
            delimiter = quote * (3 if triple else 1)
            if not escaped and text.startswith(delimiter, position):
                position += len(delimiter)
                quote = None
                triple = False
                continue
            if quote == '"' and not escaped and character == "\\":
                escaped = True
            else:
                escaped = False
            position += 1
            continue
        if character == "#":
            in_comment = True
            position += 1
            continue
        if character in {'"', "'"}:
            quote = character
            triple = text.startswith(character * 3, position)
            position += 3 if triple else 1
            continue
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    raise DependencyPolicyError("dependencies array is not closed")


def replace_dependencies(metadata_text: str, dependencies: Sequence[str]) -> str:
    assignment_start, assignment_end = _top_level_dependencies_span(metadata_text)
    array_start = _skip_space_and_comments(metadata_text, assignment_end)
    if array_start >= len(metadata_text) or metadata_text[array_start] != "[":
        raise DependencyPolicyError("PEP 723 dependencies value must be an array")
    array_end = _array_end(metadata_text, array_start)
    line_end = metadata_text.find("\n", array_end)
    replacement_end = len(metadata_text) if line_end < 0 else line_end
    if replacement_end > array_end and metadata_text[replacement_end - 1] == "\r":
        replacement_end -= 1
    trailing = metadata_text[array_end:replacement_end].strip()
    if trailing and not trailing.startswith("#"):
        raise DependencyPolicyError("unsupported content after the dependencies array")
    newline = "\r\n" if "\r\n" in metadata_text else "\n"
    rendered = "dependencies = ["
    if dependencies:
        rendered += newline + "".join(
            f"    {json.dumps(value)},{newline}" for value in dependencies
        )
    rendered += "]"
    return metadata_text[:assignment_start] + rendered + metadata_text[replacement_end:]


def _comment_metadata(metadata_text: str) -> str:
    if metadata_text and not metadata_text.endswith(("\n", "\r")):
        metadata_text += "\n"
    output: list[str] = []
    for line in metadata_text.splitlines(keepends=True):
        line_ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        body = _line_body(line)
        output.append((f"# {body}" if body else "#") + line_ending)
    return "".join(output)


def render_script(script: ScriptMetadata, dependencies: Sequence[str]) -> str:
    if not script.has_dependencies_key:
        if dependencies:
            raise DependencyPolicyError(
                f"{script.path}: cannot add dependencies to metadata without a dependencies key"
            )
        return script.source
    metadata_text = replace_dependencies(script.metadata_text, dependencies)
    try:
        rendered_metadata = tomllib.loads(metadata_text)
    except tomllib.TOMLDecodeError as exc:
        raise DependencyPolicyError(
            f"{script.path}: rewritten PEP 723 metadata is invalid TOML: {exc}"
        ) from exc
    expected_metadata = dict(script.metadata)
    expected_metadata["dependencies"] = list(dependencies)
    if rendered_metadata != expected_metadata:
        raise DependencyPolicyError(
            f"{script.path}: dependency rewrite changed unrelated PEP 723 metadata"
        )
    return "".join(script.source_lines[: script.block_start + 1]) + _comment_metadata(
        metadata_text
    ) + "".join(script.source_lines[script.block_end :])

