#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import tempfile
import unittest
from pathlib import Path

from handoff_record import explicit_config, handoff_record


class HandoffRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def config(self, name: str, text: str) -> Path:
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_unset_record_defaults_to_the_directors_own_direction_repository(self) -> None:
        accounts_only = self.config("supervisor.toml", "[accounts]\n")
        record = handoff_record("someone", [self.folder / "missing.toml", accounts_only])
        self.assertEqual({"repository": "someone/direction", "source": "default"}, record)

    def test_first_config_with_a_handoff_table_wins(self) -> None:
        first = self.config("first.toml", '[handoff]\nissue = "someone/direction#12"\n')
        second = self.config("second.toml", '[handoff]\nissue = "someone/other#3"\n')
        record = handoff_record("Someone", [first, second])
        self.assertEqual("someone/direction#12", record["issue"])
        self.assertEqual("https://github.com/someone/direction/issues/12", record["url"])
        self.assertEqual(str(first), record["source"])

    def test_issue_in_another_persons_account_is_refused(self) -> None:
        foreign = self.config("supervisor.toml", '[handoff]\nissue = "cbusillo/codex-skills#1043"\n')
        with self.assertRaisesRegex(ValueError, "outside someone's account"):
            handoff_record("someone", [foreign])

    def test_malformed_handoff_issue_is_refused(self) -> None:
        for text in ('[handoff]\nissue = "someone/direction"\n', "[handoff]\nissue = 12\n", 'handoff = "x"\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                handoff_record("someone", [self.config("supervisor.toml", text)])

    def test_missing_explicit_config_is_refused_instead_of_defaulting(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not exist"):
            explicit_config(self.folder / "moved.toml")


if __name__ == "__main__":
    unittest.main()
