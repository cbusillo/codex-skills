#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Framing protects event identities without storing engine messages."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("observer_test", Path(__file__).parents[1] / "scripts/native_observer.py")
observer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class ObserverTests(unittest.TestCase):
    def test_determiners_and_split_prompts_keep_their_line_identity(self):
        for text in ("Your license has expired", "The license is missing", "This license is invalid"):
            frame = observer.LineFramer()
            _, prefix = frame.feed(text[:7].encode())
            self.assertFalse(observer.license_boundary(prefix))
            _, prefix = frame.feed(text[7:].encode())
            self.assertTrue(observer.license_boundary(prefix))

    def test_midline_tail_never_becomes_a_prompt(self):
        frame = observer.LineFramer()
        first = b"x" * 5000 + b"Start trial " + b"x" * 4084
        _, prefix = frame.feed(first)
        self.assertFalse(observer.license_boundary(prefix))
        _, prefix = frame.feed(b"")
        self.assertFalse(observer.license_boundary(prefix))
        lines, prefix = frame.feed(b"\nStart trial")
        self.assertFalse(observer.license_boundary(lines[0]))
        self.assertTrue(observer.license_boundary(prefix))

    def test_paths_build_messages_and_quoted_examples_are_not_prompts(self):
        for text in ("Running local inspections on /repo/trial.py", "building license-required-package", 'Example: "Start trial"', "example - Start trial"):
            self.assertFalse(observer.license_boundary(text))

    def test_logged_physical_traversal_is_framed_and_relative(self):
        receipt = {}
        events = observer.EngineObserver(Path("/private/project"), receipt)
        line = "2026-10-20 16:00:00,000 [  100] FINE - #c.i.Inspection - Running local inspections on /private/project/source.py"
        events.observe(line, partial=True)
        self.assertNotIn("inspected_files", receipt)
        events.observe(line)
        self.assertEqual(receipt["inspected_files"], ["source.py"])

    def test_engine_errors_retain_only_the_logger_category(self):
        receipt = {}
        events = observer.EngineObserver(Path("/private/project"), receipt)
        with self.assertRaises(observer.ObservationError):
            events.observe("2026-10-20 16:00:00,000 [  100] ERROR - #c.i.vfs.Store - private customer message")
        self.assertEqual(receipt["engine_error_category"], "#c.i.vfs.Store")
        self.assertNotIn("private customer", str(receipt))

    def test_overlong_line_fails_without_reinterpreting_a_suffix(self):
        frame = observer.LineFramer(limit=8)
        with self.assertRaises(observer.ObservationError):
            frame.feed(b"prefix Start trial")


if __name__ == "__main__":
    unittest.main()
