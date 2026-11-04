#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

import check_insecure_tls as tls_checker
import check_new_comments as checker

BASE = "def total(values: list[int]) -> int:\n    return sum(values)\n"


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.git("init", "--quiet", "--initial-branch", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def commit(self, files: Mapping[str, str | None]) -> None:
        for name, content in files.items():
            self.replace(self.root / name, content)
        self.git("add", "--all")
        self.git("commit", "--quiet", "--allow-empty", "--message", "change")

    @staticmethod
    def replace(path: Path, content: str | None) -> None:
        if content is None:
            path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class NewCommentsCheck(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Repository(Path(directory.name))

    def findings(self, before: Mapping[str, str | None], after: Mapping[str, str | None]) -> set[tuple[str, int, str]]:
        self.repository.commit(before)
        self.repository.commit(after)
        return {(item.path, item.line, item.kind) for item in checker.scan(self.repository.root, "HEAD^1")}

    def assert_allowed(self, source: str) -> None:
        self.assertEqual(self.findings({"app.py": BASE}, {"app.py": source}), set())

    def test_new_comments_fail(self) -> None:
        source = "# Sum the values.\ndef total(values: list[int]) -> int:\n    return sum(values)  # fast path\n"
        self.assertEqual(
            self.findings({"app.py": BASE}, {"app.py": source}),
            {("app.py", 1, "comment"), ("app.py", 3, "comment")},
        )

    def test_rendered_docstring_does_not_bypass_comment_lint(self) -> None:
        source = (
            '"""Command help rendered by argparse."""\nimport argparse\nargparse.ArgumentParser(description=__doc__).parse_args()\n'
        )
        self.assert_allowed(source)
        self.assertEqual(
            self.findings({"app.py": source}, {"app.py": "# Explain this command.\n" + source}), {("app.py", 1, "comment")}
        )

    def test_unchanged_comments_pass(self) -> None:
        before = "# Existing note.\n" + BASE
        self.assertEqual(self.findings({"app.py": before}, {"app.py": before + "\n\nLIMIT = 3\n"}), set())

    def test_tool_directives_pass(self) -> None:
        self.assert_allowed(
            "import os  # noqa: F401\n"
            "import sys  # noqa: F401, E401\n"
            "value: int = os.environ  # type: ignore[assignment]\n"
            "other: int = sys.argv  # pyright: ignore[reportAssignmentType]\n"
            "# noinspection PyUnresolvedReferences\n"
            "# isort: split\n"
            "import missing  # ruff: noqa: F401  # noqa: I001\n"
        )

    def test_unscoped_or_explained_directives_fail(self) -> None:
        source = (
            "import os  # noqa\nvalue: int = os.sep  # type: ignore\nimport sys  # noqa: F401  # imported for side effects\n"
            "# isort: split because the base loads first\n"
        )
        self.assertEqual(
            self.findings({"app.py": BASE}, {"app.py": source}),
            {("app.py", 1, "comment"), ("app.py", 2, "comment"), ("app.py", 3, "comment"), ("app.py", 4, "comment")},
        )

    def test_tls_annotation_passes_comments_without_bypassing_tls_reason(self) -> None:
        cases = (
            ("isolated loopback test peer.", False),
            ("isolated peer; decision https://example.invalid/issues/1#comment", False),
            ("", True),
            ("Q418", True),
            ("https://example.invalid/decision", True),
        )
        for spacing in (" ", "", "   "):
            for reason, rejected in cases:
                with self.subTest(reason=reason, spacing=spacing):
                    source = f"import requests\n#{spacing}insecure-tls: {reason}\nrequests.get(url, verify=False)  # noqa: S501\n"
                    self.assertEqual(
                        self.findings({"app.py": BASE}, {"app.py": source}),
                        {("app.py", 2, "comment")} if rejected else set(),
                    )
                    self.assertEqual(bool(tls_checker.findings(source)), rejected)

    def test_tls_annotation_block_passes_only_at_its_choke_point(self) -> None:
        source = (
            "def fixture():\n"
            "    # insecure-tls: isolated peer exercises certificate rejection.\n"
            "    # Only this fixture disables verification.\n"
            "    # Decision: https://example.invalid/issues/1#comment\n"
            "    ctx.check_hostname = False\n"
            "    ctx.verify_mode = ssl.CERT_NONE\n"
        )
        self.assert_allowed(source)
        for unrelated in (
            "# insecure-tls: calculate totals\nvalue = 1\n",
            "value = 1  # insecure-tls: calculate totals\n",
            "# insecure-tls: isolated peer\ndef fixture():\n    ctx.check_hostname = False\n",
        ):
            with self.subTest(source=unrelated):
                self.assertTrue(self.findings({"app.py": BASE}, {"app.py": unrelated}))

    def test_shebang_and_script_metadata_pass(self) -> None:
        self.assert_allowed(
            '#!/usr/bin/env python3\n# /// script\n# requires-python = ">=3.12"\n# dependencies = []\n# ///\n' + BASE
        )

    def test_metadata_block_other_than_script_fails(self) -> None:
        source = "# /// notes\n# Explain business logic here\n# ///\n" + BASE
        self.assertEqual(
            self.findings({"app.py": BASE}, {"app.py": source}),
            {("app.py", 1, "comment"), ("app.py", 2, "comment"), ("app.py", 3, "comment")},
        )

    def test_license_and_generated_headers_pass(self) -> None:
        self.assert_allowed("# Part of Odoo. See LICENSE file for full copyright and licensing details.\n" + BASE)
        self.assert_allowed("# @generated by protoc\n# Source: ledger.proto\n" + BASE)
        self.assert_allowed("# Copyright 2026 Example\n# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).\n" + BASE)

    def test_header_marker_allows_only_its_own_line(self) -> None:
        cases = {
            "# SPDX-License-Identifier: MIT\n# Explain business logic here\n": 2,
            "# SPDX-License-Identifier: MIT\n\n# Sum the values.\n": 3,
            "# Sync license keys from the vendor.\n": 1,
        }
        for header, line in cases.items():
            with self.subTest(header=header):
                self.assertEqual(self.findings({"app.py": BASE}, {"app.py": header + BASE}), {("app.py", line, "comment")})

    def test_link_only_comment_passes_and_link_with_prose_fails(self) -> None:
        self.assert_allowed("# https://www.rfc-editor.org/rfc/rfc9110#section-15.5\n" + BASE)
        source = "# Retry per https://www.rfc-editor.org/rfc/rfc9110\n" + BASE
        self.assertEqual(self.findings({"app.py": BASE}, {"app.py": source}), {("app.py", 1, "comment")})

    def test_moved_lines_are_not_new(self) -> None:
        before = {
            "a.py": "def total() -> int:\n    # Existing note.\n    return 0\n",
            "b.py": BASE,
        }
        after = {
            "a.py": None,
            "b.py": BASE + "\n\nclass Ledger:\n    def total(self) -> int:\n        # Existing note.\n        return 0\n",
        }
        self.assertEqual(self.findings(before, after), set())

    def test_existing_inline_comment_on_edited_line_is_not_new(self) -> None:
        before = "LIMIT = 1  # Existing explanation\n"
        self.assertEqual(self.findings({"app.py": before}, {"app.py": before.replace("1", "2")}), set())

    def test_each_removed_comment_covers_one_addition(self) -> None:
        before = {"app.py": "LIMIT = 1  # Existing explanation\n"}
        after = {"app.py": "LIMIT = 2  # Existing explanation\nSIZE = 3  # Existing explanation\n"}
        self.assertEqual(self.findings(before, after), {("app.py", 2, "comment")})

    def test_quoted_paths_are_checked(self) -> None:
        for name in ("café.py", "tab\tname.py", 'quote"name.py'):
            with self.subTest(name=name):
                self.assertEqual(self.findings({name: BASE}, {name: "# Sum.\n" + BASE}), {(name, 1, "comment")})

    def test_unparsable_file_keeps_comment_findings_and_reports_itself(self) -> None:
        self.assertEqual(
            self.findings({"app.py": BASE}, {"app.py": "# New explanation\nx = (\n"}),
            {("app.py", 1, "comment"), ("app.py", 1, "unparsed")},
        )

    def test_renamed_file_is_not_new(self) -> None:
        before = {"old.py": "# Existing note.\n" + BASE}
        self.assertEqual(self.findings(before, {"old.py": None, "new.py": "# Existing note.\n" + BASE}), set())

    def test_comment_inside_string_is_not_a_comment(self) -> None:
        self.assert_allowed(BASE + 'PATTERN = "# not a comment"\n')

    def test_non_python_files_are_ignored(self) -> None:
        self.assertEqual(self.findings({"app.py": BASE}, {"run.sh": "# Shell note.\n"}), set())

    def test_base_ahead_of_branch_compares_from_merge_base(self) -> None:
        self.repository.commit({"app.py": "# Existing note.\n" + BASE})
        self.repository.git("branch", "work")
        self.repository.commit({"app.py": BASE})
        self.repository.git("checkout", "--quiet", "work")
        self.repository.commit({"other.py": BASE})
        self.assertEqual(checker.scan(self.repository.root, "main"), [])

    def test_cli_exits_one_with_github_annotation(self) -> None:
        self.repository.commit({"app.py": BASE})
        self.repository.commit({"app.py": "# Sum.\n" + BASE})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = checker.main(["--repo", str(self.repository.root), "--base", "HEAD^1", "--format", "github"])
        self.assertEqual(status, 1)
        self.assertTrue(output.getvalue().startswith("::error file=app.py,line=1,"))

    def test_cli_reports_unknown_base(self) -> None:
        self.repository.commit({"app.py": BASE})
        with contextlib.redirect_stderr(io.StringIO()):
            status = checker.main(["--repo", str(self.repository.root), "--base", "HEAD^1"])
        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()
