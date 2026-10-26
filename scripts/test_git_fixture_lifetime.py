# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import test_codeql_languages
import test_select_validation_tests


class FixtureLifetimeTests(unittest.TestCase):
    def test_selection_fixtures_do_not_start_background_git_writers(self) -> None:
        for fixture_type in (test_codeql_languages.SelectionTests, test_select_validation_tests.SelectionTests):
            with self.subTest(family=fixture_type.__module__), TemporaryDirectory() as directory:
                trace = Path(directory) / "git.jsonl"
                fixture = fixture_type()
                with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}):
                    try:
                        fixture.setUp()
                        fixture.write("extra.txt", "changed\n")
                        fixture.commit()
                        fixture.git("checkout", "-b", "other")
                        fixture.write("other.txt", "other\n")
                        fixture.commit()
                        fixture.git("checkout", "-")
                        fixture.git("merge", "--no-ff", "-m", "merge", "other")
                        children = [event["argv"] for event in map(json.loads, trace.read_text().splitlines())
                                    if event.get("event") == "child_start"]
                        self.assertTrue(children)
                        self.assertFalse([argv for argv in children if "maintenance" in argv or "gc" in argv])
                    finally:
                        fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
