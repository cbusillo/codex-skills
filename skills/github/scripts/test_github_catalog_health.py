#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import datetime as dt
import io
import json
import shlex
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
            with (
                mock.patch.object(health, "self_check", return_value={}),
                mock.patch.object(health, "write_locked_installation", side_effect=OSError),
            ):
                self.assertFalse(health.refresh_health(root))
            with mock.patch.object(health, "self_check", side_effect=RuntimeError):
                self.assertFalse(health.refresh_health(root))
            with mock.patch.object(health, "catalog_version", return_value="a" * 40):
                (root / ".local/catalog-install.json").write_text(
                    json.dumps({"health_status": True, "health": {**fields, "catalog": "a" * 40}})
                )
                self.assertIn("updater=unknown", health.marker_fields(root))

    def test_codex_mixed_sources_and_indexed_disabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path, config_path = root / "hooks.json", root / "config.toml"
            json_path.write_text(
                json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": f"uv run {root}/hooks/command_policy_hook.py"}]}]}})
            )
            inline = f'[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ncommand="uv run {root}/hooks/direction_check_hook.py"\n'
            config_path.write_text(inline)
            self.assertEqual(health.installed_hooks(root, root, "codex"), "pass")
            config_path.write_text(inline + f'[hooks.state."{json_path}:pre_tool_use:0:0"]\nenabled=false\n')
            self.assertEqual(health.installed_hooks(root, root, "codex"), "fail")
            config_path.write_text(inline)
            json_path.unlink()
            self.assertEqual(health.installed_hooks(root, root, "codex"), "fail")
            json_path.write_text("invalid")
            self.assertEqual(health.installed_hooks(root, root, "codex"), "unknown")

    def test_reader_retains_the_historical_observation(self) -> None:
        checked = str(int((dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).timestamp()))
        row = {
            "user": {"login": "fixture"},
            "created_at": "2026-10-29T00:00:00Z",
            "body": f"<!-- github-skill-operation:abc catalog={'a' * 40} updater=pass checked={checked} -->",
        }
        report = health.latest_status([row])[0]
        self.assertEqual(report["status"]["updater"], "pass")
        self.assertEqual(report["status"]["checked"], checked)
        self.assertEqual(report["created_at"], row["created_at"])

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

    def test_hook_commands_bind_the_exact_checkout(self) -> None:
        root = Path("/fixture/catalog")
        for registered in (root, Path(str(root) + "-old")):
            for _event, script in health.HOOK_SCRIPTS:
                direct = shlex.join(["uv", "run", str(registered / "hooks" / script)])
                nested = shlex.join(
                    ["env", f"CLAUDE_PLUGIN_ROOT={registered}", "sh", "-c", f'uv run "${{CLAUDE_PLUGIN_ROOT}}/hooks/{script}"']
                )
                for command in (direct, nested):
                    self.assertEqual(health.catalog_handler(command, root, script), registered == root)

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
