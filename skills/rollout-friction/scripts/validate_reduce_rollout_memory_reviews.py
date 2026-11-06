#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import tempfile
import unittest
from pathlib import Path

from reduce_rollout_memory_reviews import split_child_parents


class SplitChildParentsTests(unittest.TestCase):
    def test_only_child_prompts_identify_superseded_parents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (
                "batch-006.prompt.json",
                "batch-006-a.prompt.json",
                "batch-006-b.prompt.json",
                "batch-007-a-a.prompt.json",
                "batch-008-a.result.json",
                "batch-009.prompt.json",
            ):
                (root / name).touch()
            self.assertEqual(split_child_parents(root), {"006", "007"})

    def test_empty_review_directory_has_no_split_parents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(split_child_parents(Path(temporary)), set())


if __name__ == "__main__":
    unittest.main()
