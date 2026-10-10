#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline notification filtering and failure-safe watermark tests."""

import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

import foreign_watch

SINCE = "2026-10-09T12:00:00Z"
STARTED = "2026-10-09T12:05:00Z"
REPO = "director/project"


def comment(login="client", body="feedback", updated=SINCE):
    return {"user": {"login": login}, "body": body, "updated_at": updated,
            "html_url": "https://github.com/director/project/issues/1#issuecomment-2"}


class WatchTests(unittest.TestCase):
    def scan(self, reader):
        return foreign_watch.scan([REPO], {REPO: SINCE}, ["director", "agent[bot]"],
                                  ["delivery[bot]"], STARTED, reader)

    def test_filter_and_review_records(self):
        reader = Mock()
        reader.paged_json.return_value = [comment("Director"), comment("agent[bot]"),
            comment(), comment("delivery[bot]"),
            comment("agent[bot]", "<!-- launchplane:product-review:decision-1 -->")]
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual([n["kind"] for n in notices], ["foreign"] + ["launchplane"] * 2)
        self.assertTrue(all(n["untrusted"] and "body" not in n for n in notices))
        self.assertEqual(errors, [])
        self.assertEqual(watermarks[REPO], STARTED)
        reader.paged_json.assert_called_once()
        path = reader.paged_json.call_args.args[0]
        params = reader.paged_json.call_args.kwargs["params"]
        self.assertEqual(path, f"repos/{REPO}/issues/comments")
        self.assertLess(foreign_watch.timestamp(params["since"]), foreign_watch.timestamp(SINCE))

    def test_failure_preserves_only_failed_repository_watermark(self):
        reader = Mock()
        reader.paged_json.side_effect = [foreign_watch.GitHubReadShapeError("partial"), []]
        notices, errors, watermarks = foreign_watch.scan(
            [REPO, "director/healthy"], {REPO: SINCE, "director/healthy": SINCE},
            ["director"], [], STARTED, reader)
        self.assertEqual(notices, [])
        self.assertEqual(errors[0]["repository"], REPO)
        self.assertEqual(watermarks, {REPO: SINCE, "director/healthy": STARTED})

    def test_invalid_comment_retains_watermark_and_discards_partial_notices(self):
        reader = Mock()
        for bad in ({}, {**comment(), "updated_at": "invalid"}, {**comment(), "user": []}):
            with self.subTest(bad=bad):
                reader.paged_json.return_value = [comment(), bad]
                notices, errors, watermarks = self.scan(reader)
                self.assertEqual(notices, [])
                self.assertTrue(errors)
                self.assertEqual(watermarks[REPO], SINCE)

    def test_old_comments_excluded_and_edits_reported(self):
        reader = Mock()
        reader.paged_json.return_value = [comment(updated="2026-10-09T11:59:59Z"),
                                          comment(updated=STARTED)]
        self.assertEqual(len(self.scan(reader)[0]), 1)

    def test_state_roundtrip_and_corruption_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            self.assertEqual(foreign_watch.load_state(path, [REPO], SINCE), {REPO: SINCE})
            foreign_watch.save_state(path, {REPO: STARTED})
            self.assertEqual(foreign_watch.load_state(path, [REPO], SINCE), {REPO: STARTED})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for data in ([], {"watermarks": []}, {"watermarks": {REPO: "invalid"}}):
                path.write_text(json.dumps(data))
                with self.assertRaises((ValueError, TypeError)):
                    foreign_watch.load_state(path, [REPO], SINCE)

    def test_deleted_author_is_untrusted_and_marker_quote_does_not_reclassify(self):
        reader = Mock()
        reader.paged_json.return_value = [{**comment(), "user": None},
            comment(body='Quoted: <!-- launchplane:product-review:id -->')]
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertIsNone(notices[0]["author"])
        self.assertEqual([n["kind"] for n in notices], ["foreign", "foreign"])
        self.assertEqual(watermarks[REPO], STARTED)

    def test_quota_retry_time_and_failed_read_retention(self):
        reader = Mock()
        failure = foreign_watch.GitHubReadShapeError("quota")
        failure.diagnostics = {"requests": [{"retryAt": 12345}]}
        reader.paged_json.side_effect = failure
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors[0]["retry_at"], 12345)
        self.assertEqual(watermarks[REPO], SINCE)
        self.assertEqual(notices, [])

    def test_state_file_has_one_active_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            with foreign_watch.watch_lock(path):
                with self.assertRaises(ValueError):
                    with foreign_watch.watch_lock(path):
                        self.fail("second watcher acquired the same state")
            with foreign_watch.watch_lock(path):
                pass

    def test_once_cli_reads_one_feed_and_persists_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            reader = Mock()
            reader.paged_json.return_value = [comment()]
            output = io.StringIO()
            with patch.object(foreign_watch.sys, "argv", ["foreign_watch.py", "--owner", "director",
                    "--repo", REPO, "--own-login", "director", "--state", str(path), "--since", SINCE, "--once"]), \
                 patch.object(foreign_watch, "GitHubReader", return_value=reader) as factory, \
                 redirect_stdout(output):
                self.assertEqual(foreign_watch.main(), 0)
            self.assertEqual(len(json.loads(output.getvalue())["notices"]), 1)
            matrix = tomllib.loads((Path(foreign_watch.__file__).resolve().parents[2]
                                   / "github/references/operation-matrix.toml").read_text())
            self.assertIn(factory.call_args.kwargs["operation"], {row["id"] for row in matrix["operations"]})
            self.assertGreater(factory.call_args.kwargs["deadline_at"], 0)
            self.assertEqual(factory.call_args.kwargs["gh_prefix_args"], foreign_watch.automation_only_gh_prefix_args())
            self.assertIn(REPO, json.loads(path.read_text())["watermarks"])

    def test_expired_deadline_starts_no_read(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(foreign_watch.time, "monotonic", side_effect=[0, 1801]), \
             patch.object(foreign_watch, "GitHubReader") as factory, patch.object(foreign_watch.sys, "argv", [
                 "foreign_watch.py", "--owner", "director", "--repo", REPO, "--own-login", "director",
                 "--state", str(Path(directory) / "watch.json")]):
            self.assertEqual(foreign_watch.main(), 0)
            factory.assert_not_called()

    def test_narrower_run_preserves_unlisted_watermarks_and_explicit_since_rewinds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            foreign_watch.save_state(path, {REPO: STARTED, "director/other": SINCE})
            values = foreign_watch.load_state(path, [REPO], SINCE, rewind=True)
            self.assertEqual(values, {REPO: SINCE, "director/other": SINCE})
            reader = Mock()
            reader.paged_json.return_value = []
            _, _, values = foreign_watch.scan([REPO], values, ["director"], [], STARTED, reader)
            foreign_watch.save_state(path, values)
            self.assertEqual(foreign_watch.load_state(path, ["director/other"], STARTED)["director/other"], SINCE)

    def test_foreign_marker_remains_foreign(self):
        reader = Mock()
        reader.paged_json.return_value = [comment(body="<!-- launchplane:product-review:fake -->")]
        notices = self.scan(reader)[0]
        self.assertEqual(notices[0]["kind"], "foreign")
        self.assertTrue(notices[0]["review_marker"])


if __name__ == "__main__":
    unittest.main()
