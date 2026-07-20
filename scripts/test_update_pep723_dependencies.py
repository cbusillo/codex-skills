#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.2",
# ]
# ///
"""Focused tests for update_pep723_dependencies.py."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_PATH = Path(__file__).with_name("update_pep723_dependencies.py")
FIXTURE_SCRIPT_MARKER = "# /// " "script"
SPEC = importlib.util.spec_from_file_location("update_pep723_dependencies", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def script_source(dependencies: str, *, extra_metadata: str = "") -> str:
    return f'''#!/usr/bin/env -S uv run --script
{FIXTURE_SCRIPT_MARKER}
# requires-python = ">=3.12"
{extra_metadata}# dependencies = {dependencies}
# ///
print("ok")
'''


def write_script(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.write_text(source, encoding="utf-8")
    return path


def expect_policy_error(callback: Any, text: str) -> None:
    try:
        callback()
    except MODULE.DependencyPolicyError as exc:
        assert text in str(exc), exc
    else:
        raise AssertionError(f"expected DependencyPolicyError containing {text!r}")


def release_payload(version: str, *, yanked: bool = False) -> dict[str, Any]:
    return {"info": {"version": version}, "urls": [{"yanked": yanked}]}


def test_parse_uses_toml_semantics_and_preserves_other_metadata() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        path = write_script(
            root,
            "tool.py",
            script_source(
                '["PyYAML>=6.0.0", "pytest"]',
                extra_metadata='# custom = { owner = "team" }\n',
            ),
        )
        parsed = MODULE.parse_script(path)
        assert parsed is not None
        assert parsed.dependencies == ("PyYAML>=6.0.0", "pytest")
        updated = MODULE.render_script(parsed, ["PyYAML==6.0.3", "pytest==9.1.1"])
        assert '# custom = { owner = "team" }' in updated
        assert '#     "PyYAML==6.0.3",' in updated
        assert '#     "pytest==9.1.1",' in updated
        reparsed = MODULE.parse_script(path, updated)
        assert reparsed is not None
        assert reparsed.dependencies == ("PyYAML==6.0.3", "pytest==9.1.1")


def test_rewrite_targets_only_canonical_top_level_dependencies() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source = script_source('["pytest>=8.0.0"]')
        source = source.replace(
            "# ///\nprint",
            '# [tool.demo]\n# dependencies = ["nested==1.0"]\n# ///\nprint',
        )
        path = write_script(root, "nested.py", source)
        parsed = MODULE.parse_script(path)
        assert parsed is not None
        updated = MODULE.render_script(parsed, ["pytest==9.1.1"])
        assert '# dependencies = ["nested==1.0"]' in updated
        assert updated.count("pytest==9.1.1") == 1

        quoted_source = f'''#!/usr/bin/env python3
{FIXTURE_SCRIPT_MARKER}
# requires-python = ">=3.12"
# "dependencies" = ["pytest==9.1.1"]
# ///
'''
        quoted = write_script(root, "quoted.py", quoted_source)
        expect_policy_error(lambda: MODULE.parse_script(quoted), "canonical top-level")


def test_validation_rejects_unpinned_and_inconsistent_dependencies() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        unpinned_path = write_script(root, "unpinned.py", script_source('["pytest>=8.0.0"]'))
        unpinned = MODULE.parse_script(unpinned_path)
        assert unpinned is not None
        expect_policy_error(
            lambda: MODULE.validate_scripts([unpinned], require_pins=True),
            "must use an exact == pin",
        )

        first_path = write_script(root, "first.py", script_source('["PyYAML==6.0.2"]'))
        second_path = write_script(root, "second.py", script_source('["pyyaml==6.0.3"]'))
        first = MODULE.parse_script(first_path)
        second = MODULE.parse_script(second_path)
        assert first is not None and second is not None
        expect_policy_error(
            lambda: MODULE.validate_scripts([first, second], require_pins=True),
            "repository also uses 6.0.2",
        )


def test_validation_rejects_unsupported_requirement_forms() -> None:
    unsupported = [
        "pytest[testing]==9.1.1",
        "pytest==9.1.1; python_version > '3.12'",
        "pytest @ https://example.invalid/pytest.whl",
        "pytest<10",
    ]
    for requirement in unsupported:
        expect_policy_error(
            lambda requirement=requirement: MODULE.parse_requirement(
                requirement, require_pin=False
            ),
            "unsupported direct dependency",
        )
    expect_policy_error(
        lambda: MODULE.parse_requirement("pytest==not-a-version", require_pin=True),
        "invalid PEP 440 version",
    )
    expect_policy_error(
        lambda: MODULE.parse_requirement("pytest==9.2.0rc1", require_pin=True),
        "stable release",
    )


def test_parser_fails_closed_for_invalid_metadata() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        unclosed = write_script(
            root,
            "unclosed.py",
            '#!/usr/bin/env -S uv run --script\n# /// script\n# dependencies = []\n',
        )
        expect_policy_error(lambda: MODULE.parse_script(unclosed), "unclosed")
        invalid = write_script(root, "invalid.py", script_source('"pytest"'))
        expect_policy_error(lambda: MODULE.parse_script(invalid), "must be a list")
        late = write_script(
            root,
            "late.py",
            '#!/usr/bin/env python3\n"""module"""\n\n\n\n# /// script\n# dependencies = []\n# ///\n',
        )
        expect_policy_error(lambda: MODULE.parse_script(late), "first five lines")
        invalid_comment_source = f'''#!/usr/bin/env python3
{FIXTURE_SCRIPT_MARKER}
#requires-python = ">=3.12"
# dependencies = []
# ///
'''
        invalid_comment = write_script(root, "invalid-comment.py", invalid_comment_source)
        expect_policy_error(lambda: MODULE.parse_script(invalid_comment), "invalid PEP 723")


def test_missing_dependencies_key_is_an_empty_unchanged_list() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source = f'''#!/usr/bin/env python3
{FIXTURE_SCRIPT_MARKER}
# requires-python = ">=3.12"
# ///
print("ok")
'''
        path = write_script(root, "tool.py", source)
        parsed = MODULE.parse_script(path)
        assert parsed is not None
        assert parsed.dependencies == ()
        assert parsed.has_dependencies_key is False
        assert MODULE.render_script(parsed, []) == source
        MODULE.validate_scripts([parsed], require_pins=True)


def test_proposed_updates_pin_and_sort_all_supported_dependencies() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        path = write_script(root, "tool.py", script_source('["pytest", "PyYAML>=6.0.0"]'))
        parsed = MODULE.parse_script(path)
        assert parsed is not None
        captured_requirements: dict[str, tuple[str, ...]] = {}

        def resolver(requirements: Any) -> dict[str, str]:
            captured_requirements.update(requirements)
            return {"pytest": "9.1.1", "pyyaml": "6.0.3"}

        changes, versions = MODULE.proposed_updates(
            [parsed], resolver
        )
        assert versions == {"pytest": "9.1.1", "pyyaml": "6.0.3"}
        assert captured_requirements == {
            "pytest": ("pytest",),
            "pyyaml": ("PyYAML>=6.0.0",),
        }
        updated = changes[path]
        assert updated.index("pytest==9.1.1") < updated.index("PyYAML==6.0.3")
        reparsed = MODULE.parse_script(path, updated)
        assert reparsed is not None
        MODULE.validate_scripts([reparsed], require_pins=True)


def test_uv_resolution_is_pypi_only_direct_and_stable() -> None:
    captured_command: list[str] = []

    def fake_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_command.extend(command)
        output_path = Path(command[command.index("--output-file") + 1])
        output_path.write_text("pytest==9.1.1\npyyaml==6.0.3\n", encoding="utf-8")
        assert kwargs["cwd"] == output_path.parent
        assert "PIP_INDEX_URL" not in kwargs["env"]
        assert "UV_OVERRIDE" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    original_override = os.environ.get("UV_OVERRIDE")
    os.environ["UV_OVERRIDE"] = "override.txt"
    try:
        versions = MODULE.resolve_latest_versions(
            {"pytest": ("pytest",), "pyyaml": ("PyYAML>=6.0.0",)},
            command_runner=fake_runner,
            release_loader=lambda _package, version: release_payload(version),
        )
    finally:
        if original_override is None:
            os.environ.pop("UV_OVERRIDE", None)
        else:
            os.environ["UV_OVERRIDE"] = original_override
    assert versions == {"pytest": "9.1.1", "pyyaml": "6.0.3"}
    assert captured_command[captured_command.index("--index-url") + 1] == MODULE.PYPI_INDEX
    assert captured_command[captured_command.index("--python-version") + 1] == "3.12"
    assert captured_command[captured_command.index("--prerelease") + 1] == "disallow"
    for flag in ("--no-deps", "--no-sources", "--refresh"):
        assert flag in captured_command


def test_resolver_and_release_verification_fail_closed() -> None:
    def failed_runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="no compatible release")

    expect_policy_error(
        lambda: MODULE.resolve_latest_versions(
            {"pytest": ("pytest>=99",)},
            command_runner=failed_runner,
            release_loader=lambda _package, version: release_payload(version),
        ),
        "no compatible release",
    )
    expect_policy_error(
        lambda: MODULE.verify_pypi_releases(
            {"pytest": "9.1.1"},
            release_loader=lambda _package, version: release_payload(version, yanked=True),
        ),
        "no non-yanked files",
    )


def test_crlf_rewrite_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source = script_source('["pytest>=8.0.0"]').replace("\n", "\r\n")
        path = write_script(root, "windows.py", source)
        parsed = MODULE.parse_script(path)
        assert parsed is not None
        updated = MODULE.render_script(parsed, ["pytest==9.1.1"])
        assert "\n" not in updated.replace("\r\n", "")
        reparsed = MODULE.parse_script(path, updated)
        assert reparsed is not None
        assert MODULE.render_script(reparsed, ["pytest==9.1.1"]) == updated


def test_apply_changes_rolls_back_failed_validation() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first = write_script(root, "first.py", "first\n")
        second = write_script(root, "second.py", "second\n")

        def fail_validation() -> None:
            raise MODULE.DependencyPolicyError("validation failed")

        expect_policy_error(
            lambda: MODULE.apply_changes(
                {first: "changed-first\n", second: "changed-second\n"},
                validator=fail_validation,
            ),
            "validation failed",
        )
        assert first.read_text(encoding="utf-8") == "first\n"
        assert second.read_text(encoding="utf-8") == "second\n"


def test_compiled_output_must_match_requested_direct_packages() -> None:
    expect_policy_error(
        lambda: MODULE.parse_compiled_requirements("pytest==9.1.1\n", {"pytest", "pyyaml"}),
        "missing: pyyaml",
    )
    expect_policy_error(
        lambda: MODULE.parse_compiled_requirements(
            "pytest==9.1.1\npluggy==1.6.0\n", {"pytest"}
        ),
        "unexpected: pluggy",
    )


TESTS = [
    test_parse_uses_toml_semantics_and_preserves_other_metadata,
    test_rewrite_targets_only_canonical_top_level_dependencies,
    test_validation_rejects_unpinned_and_inconsistent_dependencies,
    test_validation_rejects_unsupported_requirement_forms,
    test_parser_fails_closed_for_invalid_metadata,
    test_missing_dependencies_key_is_an_empty_unchanged_list,
    test_proposed_updates_pin_and_sort_all_supported_dependencies,
    test_uv_resolution_is_pypi_only_direct_and_stable,
    test_resolver_and_release_verification_fail_closed,
    test_crlf_rewrite_is_idempotent,
    test_apply_changes_rolls_back_failed_validation,
    test_compiled_output_must_match_requested_direct_packages,
]


def main() -> int:
    for test in TESTS:
        test()
    print(f"update PEP 723 dependency tests passed ({len(TESTS)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
