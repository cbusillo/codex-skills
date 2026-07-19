#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline tests for the latest-model metadata resolver and fallback bundle."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = Path(__file__).with_name("resolve-latest-model-info.js")


def run_resolver(markdown: str, *, base_url: str = "https://developers.openai.com") -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(markdown)
        source = Path(handle.name)

    try:
        return subprocess.run(
            ["node", str(RESOLVER), "--source", str(source), "--base-url", base_url],
            check=False,
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

    def test_bundled_fallback_targets_gpt56(self) -> None:
        latest = (SKILL_ROOT / "references" / "latest-model.md").read_text()
        upgrade = (SKILL_ROOT / "references" / "upgrade-guide.md").read_text()
        prompting = (SKILL_ROOT / "references" / "prompting-guide.md").read_text()
        normalized_latest = " ".join(latest.split())

        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            self.assertIn(model, latest)
        for invented_model in ("gpt-5.6-pro", "gpt-5.6-mini", "gpt-5.6-nano"):
            self.assertNotIn(f"| `{invented_model}` |", latest)
        self.assertIn("gpt-5.5-pro", latest)
        self.assertIn("defaults to `medium`", normalized_latest)
        self.assertIn("# Upgrading to GPT-5.6", upgrade)
        self.assertIn('modelSlug: "gpt-5p6-sol"', upgrade)
        self.assertIn('`gpt-5.6-sol` plus `reasoning.mode: "pro"`', upgrade)
        self.assertIn('reasoning_effort: "none"', upgrade)
        self.assertIn("Chat Completions routes that use function tools", upgrade)
        self.assertIn("# Prompting guidance for GPT-5.6", prompting)
        self.assertNotIn("# Upgrading to GPT-5.5", upgrade)
        self.assertNotIn("GPT-5.5 works best", prompting)


if __name__ == "__main__":
    unittest.main()
