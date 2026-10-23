#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline shortcut definition and install tests; no Shortcuts or Reminders access."""
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import reminder_shortcut  # noqa: E402


class ReminderShortcutTests(unittest.TestCase):
    def test_each_reminder_field_reads_its_own_input_key(self):
        workflow = plistlib.loads(plistlib.dumps(reminder_shortcut.definition(), fmt=plistlib.FMT_BINARY))
        actions = [a["WFWorkflowActionParameters"] for a in workflow["WFWorkflowActions"]]
        key_by_uuid = {a["UUID"]: a.get("WFDictionaryKey") for a in actions}
        reminder = actions[-1]
        for key, parameter in reminder_shortcut.REMINDER_PARAMETERS.items():
            with self.subTest(key=key):
                source = reminder[parameter]["Value"]["attachmentsByRange"]["{0, 1}"]["OutputUUID"]
                self.assertEqual(key_by_uuid[source], key)
        for parameters in actions[1:-1]:
            self.assertIn(parameters["WFInput"]["Value"]["OutputUUID"], key_by_uuid)

    def test_install_signs_then_opens_the_signed_file(self):
        calls = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(reminder_shortcut.subprocess, "run", side_effect=lambda args, **_: calls.append(args)):
            signed = reminder_shortcut.install(Path(folder))
            unsigned = Path(folder) / f"{reminder_shortcut.SHORTCUT_NAME}.unsigned.shortcut"
            self.assertEqual(plistlib.loads(unsigned.read_bytes())["WFWorkflowActions"][-1]["WFWorkflowActionIdentifier"],
                             "is.workflow.actions.addnewreminder")
        self.assertEqual([c[:2] for c in calls], [["shortcuts", "sign"], ["open", str(signed)]])
        self.assertEqual(calls[0][calls[0].index("--output") + 1], str(signed))


if __name__ == "__main__":
    unittest.main()
