#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Exercise initializer preflight and successful same-destination retries."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import yaml

from generate_openai_yaml import write_openai_yaml
from init_skill import init_skill


class InitializerTests(unittest.TestCase):
    def test_invalid_interface_leaves_no_destination_and_corrected_retry_succeeds(self):
        for override in ("short_description=short", "short_description=" + "x" * 65,
                         "unknown=value", "=value", "missing-equals"):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as folder:
                parent = Path(folder).resolve() / "skills"
                destination = parent / "demo-skill"
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    result = init_skill("demo-skill", parent, ["scripts", "references", "assets"], True, [override])
                self.assertIsNone(result)
                self.assertIn("[ERROR]", output.getvalue())
                self.assertFalse(parent.exists())
                with contextlib.redirect_stdout(io.StringIO()):
                    result = init_skill("demo-skill", parent, ["scripts", "references", "assets"], True,
                                        ["short_description=Help with demo tasks and workflows", "brand_color=#123456"])
                self.assertEqual(result, destination)
                for path in ("SKILL.md", "agents/openai.yaml", "scripts/example.py",
                             "references/api_reference.md", "assets/example_asset.txt"):
                    self.assertTrue((destination / path).is_file(), path)
                interface = yaml.safe_load((destination / "agents/openai.yaml").read_text())["interface"]
                self.assertEqual(interface["short_description"], "Help with demo tasks and workflows")
                self.assertEqual(interface["brand_color"], "#123456")

    def test_existing_skill_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "demo-skill"
            destination.mkdir()
            existing = destination / "SKILL.md"
            existing.write_text("Owner content")
            with contextlib.redirect_stdout(io.StringIO()):
                result = init_skill("demo-skill", folder, [], False, [])
            self.assertIsNone(result)
            self.assertEqual(existing.read_text(), "Owner content")
            self.assertEqual(list(destination.iterdir()), [existing])

    def test_generator_preserves_files_when_interface_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)
            agents = destination / "agents"
            agents.mkdir()
            existing = agents / "openai.yaml"
            existing.write_text("Owner content")
            with contextlib.redirect_stdout(io.StringIO()):
                result = write_openai_yaml(destination, "demo-skill", ["unknown=value"])
            self.assertIsNone(result)
            self.assertEqual(existing.read_text(), "Owner content")


if __name__ == "__main__":
    unittest.main()
