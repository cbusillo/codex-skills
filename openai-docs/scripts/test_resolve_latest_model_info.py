#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline tests for the latest-model metadata resolver entrypoints."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


RESOLVER = Path(__file__).with_name("resolve-latest-model-info.cjs")
POSIX_RESOLVER = Path(__file__).with_name("resolve-latest-model-info")


def run_resolver(markdown: str, *, base_url: str = "https://developers.openai.com") -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(markdown)
        source = Path(handle.name)

    try:
        return subprocess.run(
            ["node", str(RESOLVER), "--source", str(source), "--base-url", base_url],
            capture_output=True,
            text=True,
        )
    finally:
        source.unlink(missing_ok=True)


class ResolveLatestModelInfoTests(unittest.TestCase):
    def test_parses_frontmatter_metadata(self) -> None:
        result = run_resolver(
            """---
latestModelInfo:
  model: gpt-5.6-sol
  migrationGuide: /api/docs/guides/upgrading-to-gpt-5p6-sol.md
  promptingGuide: /api/docs/guides/prompt-guidance-gpt-5p6.md
---
"""
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "model": "gpt-5.6-sol",
                "modelSlug": "gpt-5p6-sol",
                "migrationGuideUrl": "https://developers.openai.com/api/docs/guides/upgrading-to-gpt-5p6-sol.md",
                "promptingGuideUrl": "https://developers.openai.com/api/docs/guides/prompt-guidance-gpt-5p6.md",
            },
        )

    def test_parses_comment_metadata_and_relative_urls(self) -> None:
        result = run_resolver(
            """<!-- latestModelInfo
model: gpt-5.6-terra
migrationGuide: migrate.md
promptingGuide: prompt.md
-->
""",
            base_url="https://example.test/docs/",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["modelSlug"], "gpt-5p6-terra")
        self.assertEqual(payload["migrationGuideUrl"], "https://example.test/docs/migrate.md")
        self.assertEqual(payload["promptingGuideUrl"], "https://example.test/docs/prompt.md")

    def test_parses_quoted_crlf_metadata(self) -> None:
        result = run_resolver(
            "latestModelInfo:\r\n"
            '  model: "gpt-5.6-luna"\r\n'
            "  migrationGuide: 'migrate.md'\r\n"
            '  promptingGuide: "prompt.md"\r\n',
            base_url="https://example.test/docs/",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["model"], "gpt-5.6-luna")
        self.assertEqual(payload["modelSlug"], "gpt-5p6-luna")

    def test_rejects_missing_required_metadata(self) -> None:
        result = run_resolver(
            """latestModelInfo:
  model: gpt-5.6-sol
  migrationGuide: migrate.md
"""
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must include model, migrationGuide, and promptingGuide", result.stderr)

    def test_rejects_missing_metadata_block(self) -> None:
        result = run_resolver("# Latest model\n")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latestModelInfo block not found", result.stderr)

    def test_entrypoints_preserve_astra_anchors_inside_module_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text('{"type":"module"}\n')
            resolver = root / RESOLVER.name
            wrapper = root / POSIX_RESOLVER.name
            shutil.copyfile(RESOLVER, resolver)
            shutil.copyfile(POSIX_RESOLVER, wrapper)
            source = root / "model.md"
            source.write_text(
                "---\nlatestModelInfo:\n"
                "  model: gpt-6-astra\n"
                "  migrationGuide: /api/docs/guides/latest-model/gpt-6-astra.md#migration-quickstart\n"
                "  promptingGuide: /api/docs/guides/latest-model/gpt-6-astra.md#prompting-best-practices\n"
                "---\n"
            )
            expected = {
                "model": "gpt-6-astra",
                "modelSlug": "gpt-6-astra",
                "migrationGuideUrl": "https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md#migration-quickstart",
                "promptingGuideUrl": "https://developers.openai.com/api/docs/guides/latest-model/gpt-6-astra.md#prompting-best-practices",
            }
            for command in (["node", str(resolver)], ["sh", str(wrapper)]):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [*command, "--source", str(source)],
                        cwd=root,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), expected)


if __name__ == "__main__":
    unittest.main()
