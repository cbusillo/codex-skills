# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from product_summary import main, product_section


class ProductSummaryTests(unittest.TestCase):
    def test_extracts_only_section_and_keeps_nested_content(self) -> None:
        section = "## What Example does\n\nRepairs devices for schools.\n### Users\nSchool staff."
        document = f"# Instructions\nPrivate unrelated guidance.\n\n{section}\n\n## Workflow\nOther guidance."
        self.assertEqual(product_section(document), section)

    def test_fenced_examples_are_not_sections_or_boundaries(self) -> None:
        for marker in ("```", "~~~~"):
            with self.subTest(marker=marker):
                section = f"## What Real product does\nA real description.\n{marker}text\n## Example\n{marker}\nMore facts."
                document = f"{marker}markdown\n## What Fake does\nNot facts.\n{marker}\n{section}\n## Stop\nUnrelated."
                self.assertEqual(product_section(document), section)

    def test_section_at_end_and_optional_heading_closers(self) -> None:
        section = "### What Another product does ###\nA description."
        self.assertEqual(product_section(f"## Instructions\nDetails.\n{section}\n"), section)
        self.assertIsNone(product_section("## What happens next\nNo product description."))

    def test_cli_prints_section_and_reports_missing_section_or_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agents = Path(directory) / "AGENTS.md"
            for document, expected in (
                (None, "does not exist"),
                ("# Instructions\nOnly unrelated guidance.", "No What <product> does section"),
                ("## What Product does\nUseful facts.\n## Other\nHidden.", "## What Product does\nUseful facts.\n"),
            ):
                with self.subTest(document=document):
                    if document is not None:
                        agents.write_text(document, encoding="utf-8")
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main([directory]), 0)
                    self.assertIn(expected, output.getvalue())
                    self.assertNotIn("Hidden", output.getvalue())
                    self.assertNotIn("unrelated", output.getvalue())

    def test_unreadable_input_is_an_error_not_missing_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "AGENTS.md").write_bytes(b"\xff")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main([directory])
            self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
