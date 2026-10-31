#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import github_catalog_health as health


class HealthTests(unittest.TestCase):
    def test_opt_in_controls_public_snapshot_and_disable_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / ".local"
            local.mkdir()
            receipt = local / "catalog-install.json"
            snapshot = {
                "catalog": "a" * 40,
                "updater": "fail",
                "checked": str(int(dt.datetime.now(dt.UTC).timestamp())),
                "private": "/secret/home",
                "codex": "private-name",
                "behind": "12",
            }
            with mock.patch.object(health, "catalog_version", return_value=snapshot["catalog"]):
                receipt.write_text(json.dumps({"health": snapshot}))
                self.assertEqual(health.marker_fields(root), f"catalog={snapshot['catalog']}")
                receipt.write_text(json.dumps({"health_status": True, "health": snapshot}))
                shared = health.marker_fields(root)
                self.assertIn("updater=fail", shared)
                self.assertNotIn("/secret", shared)
                self.assertNotIn("private-name", shared)
                receipt.write_text(json.dumps({"health_status": False, "health": snapshot}))
                self.assertNotIn("updater=", health.marker_fields(root))
                snapshot["catalog"] = "b" * 40
                receipt.write_text(json.dumps({"health_status": True, "health": snapshot}))
                self.assertNotIn("updater=", health.marker_fields(root))

    def test_missing_or_failed_evidence_never_passes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(health, "output", return_value=""),
        ):
            root = Path(temporary)
            fields = health.self_check(root, {"scheduled_updater": True})
            self.assertNotIn("pass", fields.values())
            self.assertEqual(fields["approval"], "unknown")
            self.assertEqual(fields["behind"], "unknown")
            self.assertNotIn(str(root), json.dumps(fields))

    def test_hooks_require_both_catalog_bindings_and_not_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "settings.json"
            hooks = {
                event: [{"hooks": [{"command": f"uv run {root}/hooks/{script}"}]}]
                for event, script in [
                    ("PreToolUse", "command_policy_hook.py"),
                    ("SessionStart", "direction_check_hook.py"),
                ]
            }
            path.write_text(json.dumps({"hooks": hooks}))
            self.assertEqual(health.hooks_status(path, root), "pass")
            path.write_text(json.dumps({"hooks": hooks, "disableAllHooks": True}))
            self.assertEqual(health.hooks_status(path, root), "fail")
            hooks.pop("PreToolUse")
            path.write_text(json.dumps({"hooks": hooks}))
            self.assertEqual(health.hooks_status(path, root), "fail")

    def test_supported_plugin_and_inline_codex_installations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "catalog"
            claude = Path(temporary) / "claude"
            root.mkdir()
            (root / ".claude-plugin").mkdir()
            (root / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "fixture"}))
            (root / "hooks").mkdir()
            source = Path(__file__).resolve().parents[3] / "hooks/hooks.json"
            (root / "hooks/hooks.json").write_bytes(source.read_bytes())
            (claude / "skills").mkdir(parents=True)
            (claude / "skills/shared").symlink_to(root, target_is_directory=True)
            self.assertEqual(health.installed_hooks(root, claude, "claude"), "pass")
            (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"fixture@skills-dir": False}}))
            self.assertEqual(health.installed_hooks(root, claude, "claude"), "fail")
            config = claude / "config.toml"
            config.write_text(
                "".join(
                    f'[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ncommand="uv run {root}/hooks/{script}"\n'
                    for event, script in (("PreToolUse", "command_policy_hook.py"), ("SessionStart", "direction_check_hook.py"))
                )
            )
            self.assertEqual(health.installed_hooks(root, claude, "codex"), "pass")
            config.write_text(config.read_text() + "[features]\nhooks=false\n")
            self.assertEqual(health.installed_hooks(root, claude, "codex"), "fail")

    def test_old_snapshots_and_optional_write_failure(self) -> None:
        fields = {
            "checked": str(int((dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).timestamp())),
            "updater": "pass",
            "codex_hooks": "pass",
            "behind": "0",
        }
        observed = health.observed_fields(fields)
        self.assertEqual(observed["updater"], "unknown")
        self.assertEqual(observed["codex_hooks"], "unknown")
        self.assertEqual(observed["behind"], "unknown")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".local").mkdir()
            (root / ".local/catalog-install.json").write_text(json.dumps({"health_status": True}))
            (root / ".local/catalog-install.health.tmp").mkdir()
            with mock.patch.object(health, "self_check", return_value={}):
                self.assertFalse(health.refresh_health(root))

    def test_reader_pagination_bounds_and_malformed_response(self) -> None:
        row = {"user": {"login": "fixture"}, "body": "", "created_at": "2026-10-31"}
        responses = [subprocess.CompletedProcess([], 0, json.dumps([row] * count), "") for count in (100, 1)]
        with (
            mock.patch.object(health.sys, "argv", ["reader", "--repo", "fixture/repo", "--pages", "2"]),
            mock.patch.object(health.subprocess, "run", side_effect=responses) as request,
            mock.patch("sys.stdout", new_callable=io.StringIO) as stream,
        ):
            self.assertEqual(health.main(), 0)
            report = json.loads(stream.getvalue())
            self.assertEqual(report["comments_read"], 101)
            self.assertFalse(report["older_comments_omitted"])
            self.assertEqual(request.call_count, 2)
        with (
            mock.patch.object(health.sys, "argv", ["reader", "--repo", "fixture/repo", "--pages", "1"]),
            mock.patch.object(health.subprocess, "run", return_value=responses[0]),
            mock.patch("sys.stdout", new_callable=io.StringIO) as stream,
        ):
            health.main()
            self.assertTrue(json.loads(stream.getvalue())["older_comments_omitted"])
        with (
            mock.patch.object(health.sys, "argv", ["reader", "--repo", "fixture/repo"]),
            mock.patch.object(health.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "invalid", "")),
        ):
            with self.assertRaises(SystemExit):
                health.main()

    def test_updater_requires_recent_success(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(health, "output", return_value="loaded"),
            mock.patch.object(health.sys, "platform", "darwin"),
        ):
            root = Path(temporary)
            (root / ".local").mkdir()
            plist = root / "job.plist"
            plist.touch()
            receipt = root / ".local/catalog-update.json"
            for age, state, expected in ((1, "current", "pass"), (24, "current", "fail"), (1, "error", "fail")):
                checked = dt.datetime.now(dt.UTC) - dt.timedelta(hours=age)
                receipt.write_text(json.dumps({"checked_at": checked.isoformat(), "state": state}))
                self.assertEqual(
                    health.self_check(root, {"scheduled_updater": True, "updater_plist": str(plist)})["updater"], expected
                )

    def test_non_boolean_opt_in_never_shares(self) -> None:
        for value in (None, "true", "yes", 1, {}):
            self.assertFalse(health.health_enabled({"health_status": value}))

    def test_reader_preserves_latest_version_without_reusing_old_health(self) -> None:
        marker = "<!-- github-skill-operation:abc"
        rows = [
            {
                "user": {"login": "director"},
                "created_at": "2026-10-30",
                "body": f"{marker} catalog={'a' * 40} updater=fail -->",
            },
            {
                "user": {"login": "director"},
                "created_at": "2026-10-31",
                "body": f"{marker} catalog={'b' * 40} -->",
            },
            {
                "user": {"login": "legacy"},
                "created_at": "2026-10-31",
                "body": f"{marker} -->",
            },
        ]
        found = {row["author"]: row["status"] for row in health.latest_status(rows)}
        self.assertEqual(found["director"], {"catalog": "b" * 40})
        self.assertEqual(found["legacy"], {"catalog": "unknown"})


if __name__ == "__main__":
    unittest.main()
