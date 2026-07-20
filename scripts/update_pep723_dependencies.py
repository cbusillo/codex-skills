#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.2",
# ]
# ///
"""Check and update direct dependencies declared in PEP 723 script metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


ROOT = Path(__file__).resolve().parents[1]
PYPI_INDEX = "https://pypi.org/simple"
MINIMUM_PYTHON = "3.12"
SCRIPT_MARKER = "# /// script"
BLOCK_END_MARKER = "# ///"
UV_SCRIPT_SHEBANG = "#!/usr/bin/env -S uv run --script"
PACKAGE_NAME_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
VERSION_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9.!+_-]*[A-Za-z0-9])?"
SUPPORTED_REQUIREMENT = re.compile(
    rf"^(?P<name>{PACKAGE_NAME_PATTERN})(?:(?P<operator>==|>=)(?P<version>{VERSION_PATTERN}))?$"
)
DEPENDENCIES_ASSIGNMENT = re.compile(r"(?m)^[ \t]*dependencies[ \t]*=")
RESOLVER_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CI",
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class DependencyPolicyError(RuntimeError):
    """Raised when repository metadata cannot be handled safely."""


@dataclass(frozen=True)
class DirectRequirement:
    raw: str
    name: str
    normalized_name: str
    operator: str | None
    version: str | None


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
    return canonicalize_name(name)


def parse_requirement(value: str, *, require_pin: bool) -> DirectRequirement:
    match = SUPPORTED_REQUIREMENT.fullmatch(value)
    if match is None:
        raise DependencyPolicyError(
            f"unsupported direct dependency {value!r}; use a bare name, one >= lower bound, "
            "or an exact == pin without extras, markers, URLs, or environment-specific syntax"
        )
    operator = match.group("operator")
    version = match.group("version")
    if require_pin and operator != "==":
        raise DependencyPolicyError(f"direct dependency {value!r} must use an exact == pin")
    if version is not None:
        try:
            parsed_version = Version(version)
        except InvalidVersion as exc:
            raise DependencyPolicyError(
                f"direct dependency {value!r} contains an invalid PEP 440 version"
            ) from exc
        if str(parsed_version) != version:
            raise DependencyPolicyError(
                f"direct dependency {value!r} must use canonical PEP 440 version {str(parsed_version)!r}"
            )
        if parsed_version.local is not None:
            raise DependencyPolicyError(
                f"direct dependency {value!r} must not use a local version"
            )
        if require_pin and (parsed_version.is_prerelease or parsed_version.is_devrelease):
            raise DependencyPolicyError(
                f"direct dependency {value!r} must pin a stable release"
            )
    name = match.group("name")
    return DirectRequirement(
        raw=value,
        name=name,
        normalized_name=normalize_package_name(name),
        operator=operator,
        version=version,
    )


def _line_body(line: str) -> str:
    return line.rstrip("\r\n")


def _read_text(path: Path) -> str:
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
    source = _read_text(path) if source is None else source
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


def discover_python_files(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resolved_root = root.resolve()
    paths: list[Path] = []
    for relative in sorted(line for line in result.stdout.splitlines() if line):
        path = root / relative
        if not path.resolve().is_relative_to(resolved_root):
            raise DependencyPolicyError(f"tracked Python path escapes repository root: {relative}")
        paths.append(path)
    return tuple(paths)


def load_script_metadata(paths: Sequence[Path]) -> tuple[ScriptMetadata, ...]:
    scripts: list[ScriptMetadata] = []
    errors: list[str] = []
    for path in paths:
        try:
            metadata = parse_script(path)
        except (DependencyPolicyError, OSError, UnicodeError) as exc:
            errors.append(str(exc))
            continue
        if metadata is not None:
            scripts.append(metadata)
    if errors:
        raise DependencyPolicyError("\n".join(errors))
    return tuple(scripts)


def validate_scripts(scripts: Sequence[ScriptMetadata], *, require_pins: bool) -> dict[str, str]:
    versions: dict[str, str] = {}
    display_names: dict[str, str] = {}
    errors: list[str] = []
    for script in scripts:
        observed_in_script: set[str] = set()
        for value in script.dependencies:
            try:
                requirement = parse_requirement(value, require_pin=require_pins)
            except DependencyPolicyError as exc:
                errors.append(f"{script.path}: {exc}")
                continue
            if requirement.normalized_name in observed_in_script:
                errors.append(
                    f"{script.path}: duplicate direct dependency {requirement.normalized_name!r}"
                )
                continue
            observed_in_script.add(requirement.normalized_name)
            display_names.setdefault(requirement.normalized_name, requirement.name)
            if not require_pins or requirement.version is None:
                continue
            prior_version = versions.setdefault(requirement.normalized_name, requirement.version)
            if prior_version != requirement.version:
                errors.append(
                    f"{script.path}: {requirement.normalized_name} uses {requirement.version}, "
                    f"but the repository also uses {prior_version}"
                )
    if errors:
        raise DependencyPolicyError("\n".join(errors))
    return display_names


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


def parse_compiled_requirements(text: str, expected: set[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        requirement = parse_requirement(value, require_pin=True)
        if requirement.normalized_name in resolved:
            raise DependencyPolicyError(
                f"uv resolver returned duplicate package {requirement.normalized_name!r}"
            )
        assert requirement.version is not None
        resolved[requirement.normalized_name] = requirement.version
    missing = sorted(expected - set(resolved))
    unexpected = sorted(set(resolved) - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise DependencyPolicyError("uv resolver output did not match direct dependencies (" + "; ".join(details) + ")")
    return resolved


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
ReleaseLoader = Callable[[str, str], Mapping[str, Any]]


def resolver_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    return {
        name: value
        for name, value in source.items()
        if name in RESOLVER_ENVIRONMENT_ALLOWLIST
    }


def load_pypi_release(package: str, version: str) -> Mapping[str, Any]:
    url = (
        "https://pypi.org/pypi/"
        f"{urllib.parse.quote(package, safe='')}/{urllib.parse.quote(version, safe='')}/json"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "codex-skills-pep723-updater/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            final_url = urllib.parse.urlparse(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "pypi.org":
                raise DependencyPolicyError(
                    f"PyPI release lookup for {package} redirected outside pypi.org"
                )
            payload = json.loads(response.read().decode("utf-8"))
    except DependencyPolicyError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise DependencyPolicyError(
            f"failed to verify PyPI release {package}=={version}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DependencyPolicyError(
            f"PyPI release lookup for {package}=={version} returned an invalid payload"
        )
    return payload


def verify_pypi_releases(
    versions: Mapping[str, str], *, release_loader: ReleaseLoader = load_pypi_release
) -> None:
    for package, version in sorted(versions.items()):
        selected_version = Version(version)
        if (
            selected_version.is_prerelease
            or selected_version.is_devrelease
            or selected_version.local is not None
        ):
            raise DependencyPolicyError(
                f"resolver selected non-stable release {package}=={version}"
            )
        payload = release_loader(package, version)
        info = payload.get("info")
        files = payload.get("urls")
        if not isinstance(info, dict) or not isinstance(info.get("version"), str):
            raise DependencyPolicyError(
                f"PyPI release lookup for {package}=={version} omitted version metadata"
            )
        try:
            reported_version = Version(info["version"])
        except InvalidVersion as exc:
            raise DependencyPolicyError(
                f"PyPI reported an invalid version for {package}=={version}"
            ) from exc
        if reported_version != selected_version:
            raise DependencyPolicyError(
                f"PyPI release lookup returned {info['version']} for requested {package}=={version}"
            )
        if not isinstance(files, list) or not any(
            isinstance(file, dict) and file.get("yanked") is False for file in files
        ):
            raise DependencyPolicyError(
                f"PyPI release {package}=={version} has no non-yanked files"
            )


def build_resolution_requirements(
    scripts: Sequence[ScriptMetadata],
) -> dict[str, tuple[str, ...]]:
    display_names = validate_scripts(scripts, require_pins=False)
    lower_bounds: dict[str, set[str]] = {name: set() for name in display_names}
    for script in scripts:
        for value in script.dependencies:
            requirement = parse_requirement(value, require_pin=False)
            if requirement.operator == ">=":
                assert requirement.version is not None
                lower_bounds[requirement.normalized_name].add(
                    f"{display_names[requirement.normalized_name]}>={requirement.version}"
                )
    return {
        package: tuple(sorted(bounds)) if bounds else (display_names[package],)
        for package, bounds in sorted(lower_bounds.items())
    }


def resolve_latest_versions(
    requirements: Mapping[str, Sequence[str]],
    *,
    uv_command: str = "uv",
    command_runner: CommandRunner = subprocess.run,
    release_loader: ReleaseLoader = load_pypi_release,
) -> dict[str, str]:
    if not requirements:
        return {}
    with tempfile.TemporaryDirectory(prefix="pep723-update-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        input_path = temporary_root / "requirements.in"
        output_path = temporary_root / "requirements.txt"
        config_path = temporary_root / "uv.toml"
        input_path.write_text(
            "".join(
                f"{requirement}\n"
                for package in sorted(requirements)
                for requirement in requirements[package]
            ),
            encoding="utf-8",
        )
        config_path.write_text("", encoding="utf-8")
        command = [
            uv_command,
            "pip",
            "compile",
            str(input_path),
            "--output-file",
            str(output_path),
            "--config-file",
            str(config_path),
            "--index-url",
            PYPI_INDEX,
            "--python-version",
            MINIMUM_PYTHON,
            "--resolution",
            "highest",
            "--prerelease",
            "disallow",
            "--no-deps",
            "--no-header",
            "--no-annotate",
            "--no-sources",
            "--refresh",
            "--no-progress",
        ]
        try:
            command_runner(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=temporary_root,
                env=resolver_environment(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise DependencyPolicyError(f"uv failed to resolve PEP 723 dependencies: {detail.strip()}") from exc
        versions = parse_compiled_requirements(
            output_path.read_text(encoding="utf-8"), set(requirements)
        )
        verify_pypi_releases(versions, release_loader=release_loader)
        return versions


Resolver = Callable[[Mapping[str, Sequence[str]]], dict[str, str]]


def proposed_updates(
    scripts: Sequence[ScriptMetadata], resolver: Resolver
) -> tuple[dict[Path, str], dict[str, str]]:
    resolution_requirements = build_resolution_requirements(scripts)
    resolved_versions = resolver(resolution_requirements)
    changes: dict[Path, str] = {}
    for script in scripts:
        requirements = [parse_requirement(value, require_pin=False) for value in script.dependencies]
        updated_dependencies = [
            f"{requirement.name}=={resolved_versions[requirement.normalized_name]}"
            for requirement in sorted(requirements, key=lambda item: item.normalized_name)
        ]
        updated_source = render_script(script, updated_dependencies)
        if updated_source != script.source:
            changes[script.path] = updated_source
    return changes, resolved_versions


def check_repository(root: Path) -> tuple[int, int]:
    scripts = load_script_metadata(discover_python_files(root))
    display_names = validate_scripts(scripts, require_pins=True)
    return len(scripts), len(display_names)


def _stage_file(path: Path, source: str, mode: int) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".pep723", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(source)
        os.chmod(temporary_path, mode)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def apply_changes(
    changes: Mapping[Path, str], *, validator: Callable[[], object]
) -> None:
    originals = {
        path: (_read_text(path), path.stat().st_mode & 0o7777)
        for path in changes
    }
    staged: dict[Path, Path] = {}
    try:
        for path, source in changes.items():
            staged[path] = _stage_file(path, source, originals[path][1])
        for path in sorted(staged):
            os.replace(staged[path], path)
        validator()
    except BaseException as exc:
        rollback_errors: list[str] = []
        for path, (source, mode) in originals.items():
            try:
                os.replace(_stage_file(path, source, mode), path)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise DependencyPolicyError(
                "PEP 723 update failed and rollback was incomplete:\n"
                + "\n".join(rollback_errors)
            ) from exc
        raise
    finally:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)


def update_repository(
    root: Path,
    *,
    dry_run: bool,
    resolver: Resolver,
) -> tuple[tuple[Path, ...], dict[str, str]]:
    scripts = load_script_metadata(discover_python_files(root))
    changes, versions = proposed_updates(scripts, resolver)
    if not dry_run:
        apply_changes(changes, validator=lambda: check_repository(root))
    return tuple(sorted(changes)), versions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Validate exact, consistent direct pins")
    mode.add_argument("--update", action="store_true", help="Resolve and rewrite direct pins from PyPI")
    parser.add_argument("--dry-run", action="store_true", help="Show update results without writing files")
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root")
    parser.add_argument("--uv-command", default="uv", help="uv executable used by update mode")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run and not args.update:
        raise DependencyPolicyError("--dry-run requires --update")
    root = args.root.resolve()
    if args.update:
        changed_paths, versions = update_repository(
            root,
            dry_run=args.dry_run,
            resolver=lambda packages: resolve_latest_versions(
                packages, uv_command=args.uv_command
            ),
        )
        for package, version in sorted(versions.items()):
            print(f"{package}=={version}")
        action = "would update" if args.dry_run else "updated"
        print(f"{action} {len(changed_paths)} PEP 723 script(s)")
        for path in changed_paths:
            print(path.relative_to(root))
        return 0

    script_count, package_count = check_repository(root)
    print(
        f"PEP 723 dependency policy passed for {script_count} script(s) "
        f"and {package_count} direct package(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DependencyPolicyError as exc:
        print(f"PEP 723 dependency policy failed:\n{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
