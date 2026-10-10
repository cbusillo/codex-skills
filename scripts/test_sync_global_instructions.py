#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "tomlkit==0.15.1",
# ]
# ///
"""Global instructions preserve private sections and existing files on adoption."""

import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("sync_global_instructions", Path(__file__).with_name("sync-global-instructions.py"))
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


class GlobalInstructionsTests(unittest.TestCase):
    def test_explicit_upgrade_adopts_legacy_catalog_hook_and_preserves_other_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            path = codex / "hooks.json"
            unrelated = {"command": "other-startup"}
            legacy = {"command": f"uv run --quiet --no-python-downloads {sync.ROOT / 'hooks/direction_check_hook.py'}", "timeout_sec": 15}
            path.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [legacy, unrelated]}]}}))
            native_state = f'[hooks.state."{path}:session_start:0:1"]\nenabled=false\ntrusted_hash="owner-state"\n'
            (codex / "config.toml").write_text(native_state)
            before = path.read_bytes()
            outputs = sync.prepare_codex_hooks(codex, upgrade_session_start=True)
            self.assertEqual(path.read_bytes(), before)
            rendered = json.loads(outputs[path])["hooks"]["SessionStart"]
            self.assertEqual(len(rendered), 1)
            self.assertEqual(rendered[0]["matcher"], "startup")
            self.assertEqual(rendered[0]["hooks"][1], unrelated)
            sync.write_codex_hooks(outputs, codex)
            self.assertEqual((codex / "config.toml").read_text(), native_state)
            self.assertEqual(sync.prepare_codex_hooks(codex, upgrade_session_start=True)[path], outputs[path])

    def test_explicit_upgrade_refuses_custom_wrappers_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            path = codex / "hooks.json"
            path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": f"echo before; uv run {sync.ROOT / 'hooks/direction_check_hook.py'}"}]}]}}))
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Custom catalog session hook preserved"):
                sync.prepare_codex_hooks(codex, upgrade_session_start=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertIn("echo before", sync.prepare_codex_hooks(codex)[path])

    def test_upgrade_preserves_empty_groups_and_recognizes_plain_catalog_shell_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            path = codex / "hooks.json"
            command = shlex.join(["env", f"CLAUDE_PLUGIN_ROOT={sync.ROOT}", "sh", "-c",
                                  'uv run --quiet --no-python-downloads "${CLAUDE_PLUGIN_ROOT}/hooks/direction_check_hook.py"'])
            legacy = {"command": command, "timeout_sec": 15}
            empty = {"matcher": "startup", "hooks": []}
            path.write_text(json.dumps({"hooks": {"SessionStart": [empty, {"hooks": [legacy]}]}}))
            outputs = sync.prepare_codex_hooks(codex, upgrade_session_start=True)
            groups = json.loads(outputs[path])["hooks"]["SessionStart"]
            self.assertEqual(groups[0], empty)
            self.assertNotIn("timeout_sec", groups[1]["hooks"][0])
            sync.write_codex_hooks(outputs, codex)
            self.assertEqual(sync.prepare_codex_hooks(codex, upgrade_session_start=True)[path], outputs[path])

    def test_compact_only_direction_hook_keeps_managed_startup_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            command = f"uv run {sync.ROOT / 'hooks' / 'direction_check_hook.py'} --skills-only"
            group = {"matcher": "compact", "hooks": [{"command": command}]}
            (codex / "hooks.json").write_text(json.dumps({"hooks": {"SessionStart": [group]}}))
            outputs = sync.prepare_codex_hooks(codex)
            groups = json.loads(outputs[codex / "hooks.json"])["hooks"]["SessionStart"]
            self.assertIn(group, groups)
            self.assertTrue(any(handler.get("statusMessage") == "codex-skills session start" for group in groups for handler in group["hooks"]))

    def test_disabled_toml_duplicate_does_not_disable_existing_json_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config = codex / "config.toml"
            group = {"hooks": [{"command": "existing-stop"}]}
            (codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [group]}}))
            state_key = f"{config}:stop:0:0"
            config.write_text(f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="existing-stop"\n[hooks.state."{state_key}"]\nenabled=false\n')
            outputs = sync.prepare_codex_hooks(codex)
            self.assertEqual(json.loads(outputs[codex / "hooks.json"])["hooks"]["Stop"][0], group)
            self.assertEqual(outputs.disabled_migrated_handlers, [{"event": "Stop", "source_group": 0, "source_handler": 0, "deduplicated": True, "destination_group": None, "destination_handler": None}])

    def test_identical_inline_groups_report_the_correct_disabled_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config = codex / "config.toml"
            state_key = f"{config}:stop:1:0"
            config.write_text('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="same-stop"\n' * 2 + f'[hooks.state."{state_key}"]\nenabled=false\n')
            outputs = sync.prepare_codex_hooks(codex)
            self.assertEqual(outputs.disabled_migrated_handlers[0]["destination_group"], 1)

    def test_disabled_migrated_hook_is_reported_without_copying_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config = codex / "config.toml"
            state_key = f"{config}:stop:0:0"
            original = f'[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="disabled-hook"\n[hooks.state."{state_key}"]\nenabled=false\ntrusted_hash="old-hash"\n'
            config.write_text(original)
            outputs = sync.prepare_codex_hooks(codex)
            self.assertEqual(outputs.disabled_migrated_handlers, [{"event": "Stop", "source_group": 0, "source_handler": 0, "destination_group": 0, "destination_handler": 0}])
            migrated = json.loads(outputs[codex / "hooks.json"])
            self.assertNotIn("state", migrated["hooks"])
            self.assertEqual(config.read_text(), original)
            sync.write_codex_hooks(outputs, codex)
            self.assertEqual(tomllib.loads(config.read_text())["hooks"]["state"], tomllib.loads(original)["hooks"]["state"])

    def test_disabled_handler_reports_destination_after_existing_json_group_through_home_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / "actual-codex"
            codex.mkdir()
            alias = root / ".codex"
            alias.symlink_to(codex, target_is_directory=True)
            path = codex / "hooks.json"
            path.write_text(sync.render_codex_hook(path))
            state_key = f"{alias / 'config.toml'}:pre_tool_use:0:0"
            original = f'[[hooks.PreToolUse]]\nmatcher="Read"\n[[hooks.PreToolUse.hooks]]\ncommand="disabled-reader"\n[hooks.state."{state_key}"]\nenabled=false\n'
            (codex / "config.toml").write_text(original)
            outputs = sync.prepare_codex_hooks(codex.resolve())
            self.assertEqual(outputs.disabled_migrated_handlers, [{"event": "PreToolUse", "source_group": 0, "source_handler": 0, "destination_group": 1, "destination_handler": 0}])
            destination = json.loads(outputs[path.resolve()])["hooks"]["PreToolUse"][1]
            self.assertEqual(destination, tomllib.loads(original)["hooks"]["PreToolUse"][0])

    def test_hook_cli_preview_write_and_diff_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = root / ".codex"
            codex.mkdir()
            (codex / "config.toml").write_text('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="my-stop"\n')
            argv = [sys.executable, str(Path(sync.__file__)), "--home-dir", str(root), "--codex-hook", "--hooks-only"]
            def call(*options):
                result = subprocess.run([*argv, *options], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            preview = call()
            self.assertEqual(preview["migrated_events"], {"Stop": 1})
            self.assertEqual(preview["hook_trust"], sync.HOOK_TRUST_NOTICE)
            self.assertTrue(all("diff" not in item for item in preview["outputs"]))
            self.assertTrue(all("diff" in item for item in call("--show-diff")["outputs"]))
            self.assertFalse((codex / "hooks.json").exists())
            call("--write")
            self.assertFalse((codex / "AGENTS.md").exists())
            self.assertFalse((root / ".claude" / "CLAUDE.md").exists())
            self.assertTrue(all(item["state"] == "current" for item in call("--write")["outputs"]))
            invalid = subprocess.run([sys.executable, str(Path(sync.__file__)), "--home-dir", str(root), "--hooks-only"], capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)

    def test_instruction_preview_still_shows_diff_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.md"
            source.write_text("Shared fixture rules.\n")
            result = subprocess.run([sys.executable, str(Path(sync.__file__)), "--home-dir", str(root), "--source", str(source),
                                     "--local-source", str(root / "absent.md")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(all("Shared fixture rules." in item["diff"] for item in json.loads(result.stdout)["outputs"]))

    def test_instruction_cli_recovers_regular_output_while_preserving_linked_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local, target = root / "shared.md", root / "private.md", root / "dotfiles.md"
            source.write_text("Shared rules.\n")
            local.write_text("Reconciled personal rule.\n")
            target.write_text("Linked personal rule.\n")
            codex, claude = root / ".codex", root / ".claude"
            codex.mkdir()
            claude.mkdir()
            link = codex / "AGENTS.md"
            link.symlink_to(target)
            regular = claude / "CLAUDE.md"
            regular.write_text(sync.HEADER + "\n\nHand-edited shared rule.\n")
            original = regular.read_bytes()
            args = [sys.executable, str(Path(sync.__file__)), "--home-dir", str(root),
                    "--source", str(source), "--local-source", str(local)]
            preview = subprocess.run(args, capture_output=True, text=True, check=True)
            self.assertEqual(regular.read_bytes(), original)
            self.assertEqual(json.loads(preview.stdout)["outputs"][1]["state"], "preserved")
            subprocess.run([*args, "--write"], capture_output=True, text=True, check=True)
            self.assertEqual(regular.read_text(), sync.render(source, local))
            self.assertEqual(target.read_text(), "Linked personal rule.\n")
            self.assertEqual(link.readlink(), target)
            repeat = subprocess.run([*args, "--write"], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(repeat.stdout)["outputs"][0]["state"], "current")

    def test_instruction_refresh_runs_without_site_packages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.md"
            source.write_text("Shared rules.\n")
            result = subprocess.run([sys.executable, "-S", str(Path(sync.__file__)), "--home-dir", str(root),
                                     "--source", str(source), "--local-source", str(root / "absent.md"), "--write"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / ".codex" / "AGENTS.md").read_bytes(), (root / ".claude" / "CLAUDE.md").read_bytes())

    def test_user_direction_hook_replaces_redundant_managed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            path = codex / "hooks.json"
            path.write_text(sync.render_codex_hook(path, include_session_start=True))
            original = f'[[hooks.SessionStart]]\nmatcher="startup"\n[[hooks.SessionStart.hooks]]\ncommand="uv run {sync.ROOT / "hooks" / "direction_check_hook.py"}"\n'
            (codex / "config.toml").write_text(original)
            outputs = sync.prepare_codex_hooks(codex)
            sessions = json.loads(outputs[path])["hooks"]["SessionStart"]
            self.assertEqual(sessions, tomllib.loads(original)["hooks"]["SessionStart"])
            sync.write_codex_hooks(outputs, codex)
            self.assertEqual(sync.prepare_codex_hooks(codex)[path], outputs[path])

    def test_existing_json_policy_position_survives_inline_event_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            path = codex / "hooks.json"
            path.write_text(sync.render_codex_hook(path))
            policy = json.loads(path.read_text())["hooks"]["PreToolUse"][0]
            (codex / "config.toml").write_text('[[hooks.PreToolUse]]\nmatcher="Read"\n[[hooks.PreToolUse.hooks]]\ncommand="another-policy"\n')
            migrated = json.loads(sync.prepare_codex_hooks(codex)[path])["hooks"]["PreToolUse"]
            self.assertEqual(migrated[0], policy)
            self.assertEqual(migrated[1]["hooks"][0]["command"], "another-policy")

    def test_interrupted_migration_preserves_new_trust_and_reconciles_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config = codex / "config.toml"
            original = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="my-stop"\n[hooks.state.existing]\ntrusted_hash="old"\n'
            changed = original.replace('"old"', '"owner-reviewed"')
            config.write_text(original)
            outputs = sync.prepare_codex_hooks(codex)
            synchronize = sync.synchronize
            def interrupted(content, destinations, **kwargs):
                result = synchronize(content, destinations, **kwargs)
                if destinations == [codex / "hooks.json"]:
                    config.write_text(changed)
                return result
            with mock.patch.object(sync, "synchronize", side_effect=interrupted):
                with self.assertRaisesRegex(ValueError, "Destination changed"):
                    sync.write_codex_hooks(outputs, codex)
            self.assertEqual(config.read_text(), changed)
            recovered = sync.prepare_codex_hooks(codex)
            self.assertEqual(sum(handler.get("command") == "my-stop" for group in json.loads(recovered[codex / "hooks.json"])["hooks"]["Stop"] for handler in group["hooks"]), 1)
            sync.write_codex_hooks(recovered, codex)
            self.assertEqual(tomllib.loads(config.read_text())["hooks"]["state"]["existing"]["trusted_hash"], "owner-reviewed")
    def test_mixed_hooks_migrate_with_preview_backup_trust_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config = codex / "config.toml"
            original = f'''# Personal setting\nmodel = "personal-model"\n
[[hooks.SessionStart]]
matcher = "startup|resume|clear"
[[hooks.SessionStart.hooks]]
type = "command"
command = "uv run {sync.ROOT / 'hooks' / 'direction_check_hook.py'}"
timeout = 15

[[hooks.Stop]]
matcher = "*"
[[hooks.Stop.hooks]]
type = "command"
command = "my-stop"

# Codex-managed trust, preserve it verbatim.
[hooks.state."source-bound-id"]
approved = true
hash = "existing-hash"
'''
            config.write_text(original)
            hook_path = codex / "hooks.json"
            hook_path.write_text(sync.render_codex_hook(hook_path))
            old_json = hook_path.read_bytes()
            expected = tomllib.loads(original)
            plan = sync.prepare_codex_hooks(codex)
            self.assertEqual(config.read_text(), original)
            self.assertEqual(hook_path.read_bytes(), old_json)
            hooks = json.loads(plan[hook_path])["hooks"]
            self.assertEqual(hooks["SessionStart"], expected["hooks"]["SessionStart"])
            self.assertEqual(hooks["Stop"], json.loads(old_json)["hooks"]["Stop"] + expected["hooks"]["Stop"])
            self.assertEqual(len(hooks["PreToolUse"]), 1)
            receipt = sync.write_codex_hooks(plan, codex)
            for item in receipt:
                if "backup" in item:
                    self.assertEqual(Path(item["backup"]).stat().st_mode & 0o777, 0o600)
            cleaned = tomllib.loads(config.read_text())
            self.assertEqual(cleaned["hooks"], {"state": expected["hooks"]["state"]})
            self.assertEqual(cleaned["model"], expected["model"])
            self.assertIn(original[original.index('[hooks.state.'):], config.read_text())
            self.assertIn('# Codex-managed trust, preserve it verbatim.', config.read_text())
            second = sync.prepare_codex_hooks(codex)
            self.assertEqual(list(second), [hook_path])
            self.assertTrue(all(item["state"] == "current" for item in sync.write_codex_hooks(second, codex)))
            self.assertEqual(len(list(codex.glob("*.backup-*"))), 2)

    def test_fresh_hook_setup_registers_catalog_events_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            first = sync.prepare_codex_hooks(codex)
            hook_path = codex / "hooks.json"
            self.assertEqual(json.loads(first[hook_path]), json.loads(sync.render_codex_hook(hook_path, include_session_start=True)))
            sync.write_codex_hooks(first, codex)
            self.assertEqual(sync.prepare_codex_hooks(codex), first)
            self.assertFalse((codex / "config.toml").exists())

    def test_conflicts_malformed_hooks_and_symlink_migration_refuse_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            config, hook_path = codex / "config.toml", codex / "hooks.json"
            inline = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = "my-stop"\n'
            config.write_text(inline)
            duplicate = {"hooks": {"Stop": [{"hooks": [{"command": "my-stop", "timeout": 30}]}]}}
            hook_path.write_text(json.dumps(duplicate))
            before = hook_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                sync.prepare_codex_hooks(codex)
            self.assertEqual(hook_path.read_bytes(), before)
            self.assertEqual(config.read_text(), inline)
            hook_path.write_text('{"hooks":{"Stop":[null]}}')
            with self.assertRaises(ValueError):
                sync.prepare_codex_hooks(codex)
            hook_path.write_text('{}')
            target = codex / "actual.toml"
            config.rename(target)
            config.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                sync.prepare_codex_hooks(codex)
            self.assertEqual(target.read_text(), inline)

    def test_hook_sources_changed_after_preview_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory)
            outputs = sync.prepare_codex_hooks(codex)
            config = codex / "config.toml"
            changed = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand = "added-after-preview"\n'
            config.write_text(changed)
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                sync.write_codex_hooks(outputs, codex)
            self.assertFalse((codex / "hooks.json").exists())
            self.assertEqual(config.read_text(), changed)

    def test_explicit_host_directories_receive_identical_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex, claude = root / "codex-config", root / "claude-config"
            subprocess.run([sys.executable, str(Path(sync.__file__)), "--codex-dir", str(codex), "--claude-dir", str(claude), "--local-source", str(root / "absent.md"), "--write"], check=True, capture_output=True)
            self.assertEqual((codex / "AGENTS.md").read_bytes(), (claude / "CLAUDE.md").read_bytes())

    def test_codex_registration_preserves_other_hooks_and_reuses_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            other = {"matcher": "Read", "hooks": [{"type": "command", "command": "true"}]}
            path.write_text(json.dumps({"hooks": {"PreToolUse": [other], "Stop": [other]}}))
            first = sync.render_codex_hook(path)
            path.write_text(first)
            self.assertEqual(first, sync.render_codex_hook(path))
            hooks = json.loads(first)["hooks"]
            self.assertEqual(hooks["Stop"][0], other)
            self.assertEqual(len(hooks["Stop"]), 2)
            self.assertEqual(len(hooks["Interrupt"]), 1)
            self.assertEqual(hooks["PreToolUse"][0], other)
            self.assertIn("command_policy_hook.py", hooks["PreToolUse"][1]["hooks"][0]["command"])

    def test_existing_commands_and_group_positions_preserve_trust_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hooks.json"
            source = json.loads((sync.ROOT / "hooks" / "hooks.json").read_text())["hooks"]
            legacy = {}
            for event, label in (("PreToolUse", sync.HOOK_LABEL), ("SessionStart", "codex-skills session start")):
                groups = [group for group in source[event] if group.get("matcher") != "compact"]
                for group in groups:
                    for handler in group["hooks"]:
                        handler["command"] = shlex.join(["env", f"CLAUDE_PLUGIN_ROOT={sync.ROOT}", "sh", "-c", handler["command"]])
                        handler["statusMessage"] = label
                legacy[event] = groups
            path.write_text(json.dumps({"hooks": legacy}))
            first = sync.render_codex_hook(path, include_session_start=True)
            rendered = json.loads(first)["hooks"]
            self.assertEqual(rendered["PreToolUse"], legacy["PreToolUse"])
            self.assertEqual(rendered["SessionStart"], legacy["SessionStart"])
            other = {"hooks": [{"type": "command", "command": "user-hook"}]}
            for event in ("Stop", "Interrupt"):
                rendered[event].append(other)
            path.write_text(json.dumps({"hooks": rendered}))
            self.assertEqual(json.loads(sync.render_codex_hook(path, include_session_start=True))["hooks"], rendered)

    def test_preview_adoption_backup_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local = root / "shared.md", root / "private.md"
            source.write_text("# Shared rules\n")
            local.write_text("## Host rules\nKeep this local.\n")
            targets = [root / "claude.md", root / "codex.md"]
            targets[0].write_text("original\n")
            content = sync.render(source, local)
            self.assertIn("Keep this local.", content)
            self.assertTrue(all(entry["state"] == "would_write" for entry in sync.synchronize(content, targets, write=False)))
            self.assertEqual(targets[0].read_text(), "original\n")
            self.assertFalse(targets[1].exists())
            receipt = sync.synchronize(content, targets, write=True)
            self.assertEqual(Path(receipt[0]["backup"]).read_text(), "original\n")
            self.assertEqual(targets[0].read_bytes(), targets[1].read_bytes())
            self.assertTrue(all(entry["state"] == "current" for entry in sync.synchronize(content, targets, write=True)))
            self.assertEqual(len(list(root.glob("*.backup-*"))), 1)

    def test_unsafe_second_destination_leaves_first_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            first.write_text("preserve\n")
            second.symlink_to(first)
            with self.assertRaises(ValueError):
                sync.synchronize("new\n", [first, second], write=True)
            self.assertEqual(first.read_text(), "preserve\n")



    
    def test_missing_local_source_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local = root / "shared.md", root / "private.md"
            source.write_text("# Shared rules\n")
            targets = [root / "claude.md"]
            targets[0].write_text("HEADER\n\n# Shared rules\n\n## Host rules\nKeep this local.\n")

            # Preview reports refusal
            content_str = sync.render(source, local)
            preview = sync.synchronize(content_str, targets, write=False, local_source_missing=True, allow_missing_local=False)
            self.assertEqual(preview[0]["state"], "refused")
            self.assertIn("refusal", preview[0])

            # Write is refused
            with self.assertRaises(ValueError):
                sync.synchronize(content_str, targets, write=True, local_source_missing=True, allow_missing_local=False)

            # Runtime checkout (local source exists) writes normally
            # In a real run, content_str would include the local file content if it existed.
            # Here we just verify the flag allows the write to proceed.
            receipt = sync.synchronize(content_str, targets, write=True, local_source_missing=False, allow_missing_local=False)
            self.assertEqual(receipt[0]["state"], "written")

            # Reset target
            targets[0].write_text("HEADER\n\n# Shared rules\n\n## Host rules\nKeep this local.\n")

            # Explicit override proceeds even if local source is missing
            receipt = sync.synchronize(content_str, targets, write=True, local_source_missing=True, allow_missing_local=True)
            self.assertEqual(receipt[0]["state"], "written")

    def test_missing_local_source_refusal_leaves_first_destination_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, local = root / "shared.md", root / "private.md"
            source.write_text("# Shared rules\n")
            targets = [root / "claude.md", root / "codex.md"]
            # First target doesn't exist. Second target differs.
            targets[1].write_text("HEADER\n\n# Shared rules\n\n## Host rules\nKeep this local.\n")

            content_str = sync.render(source, local)
            with self.assertRaises(ValueError):
                sync.synchronize(content_str, targets, write=True, local_source_missing=True, allow_missing_local=False)
            self.assertFalse(targets[0].exists())


if __name__ == "__main__":
    unittest.main()
