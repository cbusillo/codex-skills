#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


TEST_CACHE = tempfile.TemporaryDirectory(prefix="jetbrains-inspection-tests-")


os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = TEST_CACHE.name

os.environ["JETBRAINS_INSPECTION_REGISTRY_DIR"] = str(Path(TEST_CACHE.name) / "registry")

os.environ["JB_INSPECT_OUTCOME_LOG"] = "0"

os.environ["JB_INSPECT_UNKNOWN_LOG"] = "0"


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "jb-inspect.py"


SPEC = importlib.util.spec_from_file_location("jb_inspect", SCRIPT_PATH)


assert SPEC is not None

jb_inspect = cast(Any, importlib.util.module_from_spec(SPEC))


assert SPEC.loader is not None

sys.modules["jb_inspect"] = jb_inspect

SPEC.loader.exec_module(jb_inspect)

NO_LIVE_IDE = patch_attr(
    jb_inspect.urllib.request, "urlopen", side_effect=jb_inspect.urllib.error.URLError(ConnectionRefusedError("connection refused"))
)


def setUpModule() -> None:
    NO_LIVE_IDE.start()


def tearDownModule() -> None:
    NO_LIVE_IDE.stop()


def write_json(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)


class OfflineIdeProcessTests(unittest.TestCase):
    def test_process_tree_keeps_ancestors_and_siblings_but_excludes_observers(self) -> None:
        process_list = ("123 1 /Applications/PyCharm.app/Contents/MacOS/pycharm\n124 123 /bin/zsh\n"
                        "125 124 /usr/bin/python3\n126 125 /bin/ps\n127 124 /usr/bin/node\n")
        with patch.object(os, "getpid", return_value=125), patch.object(subprocess, "check_output", return_value=process_list):
            self.assertEqual(jb_inspect.offline_ide_processes(), [123, 124, 127])

    def test_running_ide_is_visible_without_a_plugin_registry(self):
        process_list = "123 1 /Applications/PyCharm.app/Contents/MacOS/pycharm\n124 1 /bin/sh\n"
        with patch.object(subprocess, "check_output", return_value=process_list):
            self.assertEqual(jb_inspect.offline_ide_processes(), [123])
        with patch.object(subprocess, "check_output", side_effect=OSError("fixture")):
            with self.assertRaises(jb_inspect.InspectError):
                jb_inspect.offline_ide_processes()


class RunningIdeRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        primary = self.home / "primary"
        self.root = self.home / "task"
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "task", str(self.root)], check=True)
        self.processes = "123 1 /Applications/PyCharm.app/Contents/MacOS/pycharm\n"
        self.open_files = "p123\0\nfcwd\0n/another/project\0\n"
        self.discovery_error: Exception | None = None
        self.file_error: Exception | None = None
        self.file_stderr = ""
        self.args = jb_inspect.build_parser().parse_args(["remove-worktree", "--repo", str(self.root)])
        check_output = subprocess.check_output
        run = subprocess.run

        def discover(command: list[str], **kwargs: Any) -> str:
            if command[0] != "ps":
                return check_output(command, **kwargs)
            if self.discovery_error:
                raise self.discovery_error
            return self.processes

        def observe(command: list[str], **kwargs: Any) -> Any:
            if Path(command[0]).name != "lsof":
                return run(command, **kwargs)
            if self.file_error:
                raise self.file_error
            return subprocess.CompletedProcess(command, 0, self.open_files, self.file_stderr)

        for context in (patch_attr(jb_inspect.Path, "home", return_value=self.home),
                        patch.object(sys, "platform", "darwin"),
                        patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""}),
                        patch.object(subprocess, "check_output", side_effect=discover),
                        patch.object(subprocess, "run", side_effect=observe),
                        patch_attr(jb_inspect, "discover_identities", side_effect=AssertionError("desktop discovery")),
                        patch_attr(jb_inspect, "http_request", side_effect=AssertionError("desktop request"))):
            context.start()
            self.addCleanup(context.stop)

    def retire(self) -> None:
        self.assertTrue(jb_inspect.command_remove_worktree(self.args)["offline_cleanup_proved"])
        self.assertTrue(self.root.is_dir())
        self.args.dry_run = False
        self.assertTrue(jb_inspect.command_remove_worktree(self.args)["worktree_removed"])
        self.assertFalse(self.root.exists())

    def test_native_workers_on_private_snapshots_allow_retirement(self) -> None:
        self.processes += "124 1 /Applications/IntelliJ IDEA.app/Contents/MacOS/idea\n"
        self.open_files = ("p123\0\nfcwd\0n/host-caches/native/run/project\0\n"
                           "p124\0\nfcwd\0n/host-caches/native/another-run/project\0\n")
        self.retire()

    def test_desktop_ide_without_worktree_allows_retirement_including_offline_only(self) -> None:
        self.args.offline_only = True
        self.retire()

    def test_unreadable_unrelated_mount_allows_retirement(self) -> None:
        self.file_stderr = self.unstattable_mount("/Volumes/.timemachine/NAS._smb._tcp.local./backup/TimeMachine")
        self.retire()

    def test_open_file_through_another_spelling_of_the_worktree_refuses_retirement(self) -> None:
        alias = self.home / "alias"
        alias.symlink_to(self.root.parent)
        self.file_stderr = self.unstattable_mount("/Volumes/backup")
        self.args.dry_run = False
        for spelling in (alias / self.root.name, Path(str(self.root).swapcase())):
            with self.subTest(spelling=spelling):
                if not spelling.exists():
                    continue
                self.open_files = f"p123\0\nfcwd\0n{spelling}\0\n"
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.command_remove_worktree(self.args)
                self.assertTrue(self.root.is_dir())

    @staticmethod
    def unstattable_mount(mount: Path | str) -> str:
        return (f"lsof: WARNING: can't stat() smbfs file system {mount}\n"
                "      Output information may be incomplete.\n"
                '      assuming "dev=36000019" from mount table\n')

    def test_desktop_ide_open_or_recorded_worktree_refuses_retirement(self) -> None:
        self.open_files = f"p123\0\nfcwd\0n{self.root}-other/source.py\0\n"
        self.assertTrue(jb_inspect.command_remove_worktree(self.args)["offline_cleanup_proved"])
        self.args.dry_run = False
        self.open_files = f"p123\0\nfcwd\0n{self.root}\0\n"
        with self.assertRaises(jb_inspect.InspectError):
            jb_inspect.command_remove_worktree(self.args)
        self.assertTrue(self.root.is_dir())
        self.open_files = "p123\0\nfcwd\0n/another/project\0\n"
        record = self.home / "Library/Application Support/JetBrains/PyCharm/options/recentProjects.xml"
        record.parent.mkdir(parents=True)
        record.write_text(f'<application><option value="{self.root}" /></application>')
        with self.assertRaises(jb_inspect.InspectError) as caught:
            jb_inspect.command_remove_worktree(self.args)
        self.assertEqual(caught.exception.payload["offline_retirement_record"], str(record))
        self.assertTrue(self.root.is_dir())

    def test_ide_descendant_with_open_worktree_refuses_retirement(self) -> None:
        self.processes += "124 123 /bin/zsh\n125 124 /usr/bin/python3\n"
        self.open_files += f"p124\0\nfcwd\0n/another/project\0\np125\0\nf7\0n{self.root}/source.py\0\n"
        self.args.dry_run = False
        with self.assertRaises(jb_inspect.InspectError) as caught:
            jb_inspect.command_remove_worktree(self.args)
        self.assertEqual(caught.exception.payload["offline_retirement_ide_pids"], [125])
        self.assertTrue(self.root.is_dir())

    def test_process_discovery_failure_refuses_retirement(self) -> None:
        self.assertTrue(jb_inspect.command_remove_worktree(self.args)["offline_cleanup_proved"])
        self.args.dry_run = False
        self.discovery_error = subprocess.CalledProcessError(1, "ps")
        with self.assertRaises(jb_inspect.InspectError):
            jb_inspect.command_remove_worktree(self.args)
        self.assertTrue(self.root.is_dir())

    def test_incomplete_open_file_proof_refuses_retirement(self) -> None:
        for output, error, stderr in (("", None, ""), ("p123\0\nfNOFD\0\n", None, ""),
                                      ("p123\0\nfcwd\0tDIR\0n/another/project\0\nf7\0tREG\0nunknown\0\n", None, ""),
                                      ("p123\0\nfcwd\0n/Volumes/Developer-Artifacts -- /source.py\0\n", None, ""),
                                      ("p123\0\nfcwd\0n/Volumes/Developer-Artifacts (/dev/disk9s1)\0\n", None, ""),
                                      (self.open_files, None, "permission denied"),
                                      (self.open_files, None, self.unstattable_mount(self.root.parent)),
                                      (self.open_files, None, self.unstattable_mount(self.root / "mounted")),
                                      (self.open_files, None, self.unstattable_mount("/Volumes/backup") + "lsof: WARNING: other\n"),
                                      (self.open_files, None, self.unstattable_mount("/Volumes/back\\\\up")),
                                      (self.open_files, None, "      Output information may be incomplete.\n"),
                                      (self.open_files, FileNotFoundError("lsof"), ""),
                                      (self.open_files, subprocess.TimeoutExpired("lsof", 5), "")):
            with self.subTest(output=output, error=error, stderr=stderr):
                self.open_files, self.file_error, self.file_stderr = output, error, stderr
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.command_remove_worktree(self.args)
                self.assertTrue(self.root.is_dir())

    def test_malformed_process_discovery_refuses_retirement(self) -> None:
        self.processes = "unreadable process list\n"
        with self.assertRaises(jb_inspect.InspectError):
            jb_inspect.command_remove_worktree(self.args)
        self.assertTrue(self.root.is_dir())


class SdkRetirementTests(unittest.TestCase):
    def setUp(self):
        processes = patch_attr(jb_inspect, "offline_ide_processes", return_value=[])
        processes.start()
        self.addCleanup(processes.stop)

    def test_offline_records_hold_registered_sdks_projects_and_caches(self):
        for kind in ("jdk.table.xml", jb_inspect.HELPER_SDK_RECORD_FILENAME, "recentProjects.xml", "cache", "finalizing", "idea", "receipt"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                root = home / "task"
                root.mkdir()
                with (patch_attr(jb_inspect.Path, "home", return_value=home),
                      patch.object(sys, "platform", "darwin"),
                      patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""})):
                    if kind in {"cache", "finalizing"}:
                        projects = home / "Library/Caches/JetBrains/PyCharm2026.2/projects"
                        record = (projects / f"{jb_inspect.HELPER_FINALIZING_CACHE_PREFIX}id.json" if kind == "finalizing" else
                                  projects / f"{jb_inspect.HELPER_SEALED_CACHE_PREFIX}id" / jb_inspect.HELPER_PROJECT_CACHE_MARKER)
                        record.parent.mkdir(parents=True)
                        record.write_text(json.dumps({"worktree_path": str(root)}))
                    elif kind == "idea":
                        (root / ".idea").mkdir()
                        (root / ".idea/workspace.xml").write_text("<project />")
                    elif kind == "receipt":
                        record = jb_inspect.repository_preparation_receipt_path({"worktree_root": str(root)})
                        record.parent.mkdir(parents=True, exist_ok=True)
                        record.write_text("{}")
                        self.addCleanup(record.unlink)
                    else:
                        record = home / "Library/Application Support/JetBrains/PyCharm2026.2/options" / kind
                        record.parent.mkdir(parents=True)
                        record.write_text(f'<application><option value="{root}/.venv/bin/python" /></application>')
                    args = Namespace(command="remove-worktree", repo=str(root), dry_run=False, lifecycle_lock_timeout_ms=1000)
                    with (patch_attr(jb_inspect, "retirement_worktree", return_value=root),
                          patch_attr(jb_inspect, "discover_identities", return_value=[]),
                          patch.object(subprocess, "run") as removal):
                        with self.assertRaises(jb_inspect.InspectError) as caught:
                            jb_inspect.command_retire_sdks(args)
                        self.assertEqual(caught.exception.payload["offline_retirement_record"],
                                         str(root / ".idea/workspace.xml") if kind == "idea" else str(record))
                        removal.assert_not_called()
                        self.assertTrue(root.is_dir())

    def test_offline_record_scan_preserves_scope_and_fails_closed_on_unreadable_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            root = home / "task"
            options = home / "Library/Application Support/JetBrains/PyCharm2026.2/options"
            options.mkdir(parents=True)
            (home / "another-config").mkdir()
            table = options / "jdk.table.xml"
            with (patch_attr(jb_inspect.Path, "home", return_value=home),
                  patch.object(sys, "platform", "darwin"),
                  patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": str(home / "another-config")})):
                table.write_text('<application><homePath value="$USER_HOME$/task-other/.venv/bin/python" /></application>')
                jb_inspect.offline_retirement_records(root)
                table.write_text('<application><homePath value="$USER_HOME$/task/.venv/bin/python" /></application>')
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.offline_retirement_records(root)
                table.write_text("broken xml")
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.offline_retirement_records(root)
                with patch_attr(jb_inspect.Path, "iterdir", side_effect=PermissionError("fixture")):
                    with self.assertRaises(jb_inspect.InspectError):
                        jb_inspect.offline_retirement_records(root)

    def test_custom_storage_and_unresolved_cache_ownership_require_live_cleanup(self):
        for kind in ("custom-storage", "missing-cache-marker", "bad-cache-marker"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                with (patch_attr(jb_inspect.Path, "home", return_value=home),
                      patch.object(sys, "platform", "darwin"),
                      patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""})):
                    if kind == "custom-storage":
                        config = home / "Library/Application Support/JetBrains/PyCharm2026.2"
                        config.mkdir(parents=True)
                        (config / "idea.properties").write_text("idea.system.path=/custom/cache\n")
                    else:
                        cache = home / "Library/Caches/JetBrains/PyCharm2026.2/projects" / f"{jb_inspect.HELPER_SEALED_CACHE_PREFIX}id"
                        cache.mkdir(parents=True)
                        if kind == "bad-cache-marker":
                            (cache / jb_inspect.HELPER_PROJECT_CACHE_MARKER).write_text("{}")
                    with self.assertRaises(jb_inspect.InspectError) as caught:
                        jb_inspect.offline_retirement_records(home / "task")
                    expected = config / "idea.properties" if kind == "custom-storage" else cache / jb_inspect.HELPER_PROJECT_CACHE_MARKER
                    self.assertEqual(caught.exception.payload["offline_retirement_record"], str(expected))

    def test_offline_cache_inventory_ignores_ordinary_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            projects = home / "Library/Caches/JetBrains/PyCharm2026.2/projects"
            projects.mkdir(parents=True)
            (projects / ".DS_Store").write_bytes(b"Finder metadata")
            (projects / "cache-index").write_text("ordinary cache metadata")
            with (patch_attr(jb_inspect.Path, "home", return_value=home),
                  patch.object(sys, "platform", "darwin"),
                  patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""})):
                jb_inspect.offline_retirement_records(home / "task")

    def test_offline_apply_rechecks_records_after_preview(self):
        for offline_only, new_process in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(offline_only=offline_only, new_process=new_process):
                self.check_offline_apply_rechecks(offline_only, new_process)

    def check_offline_apply_rechecks(self, offline_only, new_process):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            root = home / "task"
            root.mkdir()
            calls = []
            def retire(_root: Path, *_args: Any, **_kwargs: Any) -> Path:
                calls.append(True)
                if len(calls) == 2 and not new_process:
                    (root / ".idea").mkdir()
                    (root / ".idea/workspace.xml").write_text("<project />")
                return root
            args = Namespace(command="remove-worktree", repo=str(root), dry_run=False,
                             offline_only=offline_only, lifecycle_lock_timeout_ms=1000)
            with (patch_attr(jb_inspect.Path, "home", return_value=home),
                  patch.object(sys, "platform", "darwin"),
                  patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""}),
                  patch_attr(jb_inspect, "retirement_worktree", side_effect=retire),
                  patch_attr(jb_inspect, "discover_identities", return_value=[]) as discover,
                  patch_attr(jb_inspect, "offline_ide_processes", side_effect=[[], [123]] if new_process else None,
                               return_value=[]),
                  patch.object(subprocess, "run") as removal):
                with self.assertRaises(jb_inspect.InspectError) as caught:
                    jb_inspect.command_retire_sdks(args)
                if new_process:
                    self.assertEqual(caught.exception.payload["offline_retirement_ide_pids"], [123])
                else:
                    self.assertEqual(caught.exception.payload["offline_retirement_record"], str(root / ".idea/workspace.xml"))
                discover.assert_not_called()
                removal.assert_not_called()

    def test_worktree_with_only_preparation_retires_unless_ide_records_reference_it(self) -> None:
        for preparation, offline_only in (("none", False), ("virtualenv", False), ("project-model", False),
                                          ("none", True), ("virtualenv", True), ("project-model", True)):
            with self.subTest(preparation=preparation, offline_only=offline_only), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                primary, root = home / "primary", home / "task"
                subprocess.run(["git", "init", "-q", str(primary)], check=True)
                subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                                "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "docs"], check=True)
                (primary / ".idea/inspectionProfiles").mkdir(parents=True)
                (primary / ".idea/inspectionProfiles/shared.xml").write_text("<component />")
                (primary / ".idea/vcs.xml").write_text("<project />")
                subprocess.run(["git", "-C", str(primary), "add", ".idea"], check=True)
                subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                                "user.email=fixture@example.invalid", "commit", "-q", "-m", "shared inspection policy"], check=True)
                (primary / ".git" / "info" / "exclude").write_text(".venv/\n.idea/*.xml\n.idea/*.iml\n")
                subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "task", str(root)], check=True)
                if preparation != "none":
                    (root / ".venv").mkdir()
                if preparation == "project-model":
                    for name in ("misc.xml", "modules.xml", "fixture.iml"):
                        (root / ".idea" / name).write_text("<project />")
                table = home / "Library/Application Support/JetBrains/PyCharm2026.2/options/jdk.table.xml"
                table.parent.mkdir(parents=True)
                table.write_text('<application />')
                args = jb_inspect.build_parser().parse_args(["remove-worktree", "--repo", str(root)] +
                                                           (["--offline-only"] if offline_only else []))
                with (patch_attr(jb_inspect.Path, "home", return_value=home),
                      patch.object(sys, "platform", "darwin"),
                      patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""}),
                      patch_attr(jb_inspect, "discover_identities", side_effect=AssertionError("desktop discovery")) as discovery,
                      patch_attr(jb_inspect, "http_post", side_effect=AssertionError("desktop request")) as post):
                    if preparation == "project-model":
                        table.write_text(f'<application><option value="{root}/.venv/bin/python" /></application>')
                        args.dry_run = False
                        with self.assertRaises(jb_inspect.InspectError) as caught:
                            jb_inspect.command_retire_sdks(args)
                        self.assertEqual(caught.exception.payload["offline_retirement_record"], str(table))
                        self.assertTrue(root.is_dir())
                        table.write_text('<application />')
                    args.dry_run = True
                    self.assertFalse(jb_inspect.command_retire_sdks(args)["worktree_removed"])
                    self.assertTrue(root.is_dir())
                    args.dry_run = False
                    result = jb_inspect.command_retire_sdks(args)
                    self.assertTrue(result["worktree_removed"])
                    self.assertTrue(result["offline_cleanup_proved"])
                    self.assertIn(str(table), result["offline_records_checked"])
                    post.assert_not_called()
                    discovery.assert_not_called()
                self.assertFalse(root.exists())
                subprocess.run(["git", "-C", str(primary), "show-ref", "--verify", "--quiet", "refs/heads/task"], check=True)

    @staticmethod
    def identity():
        return {"port": 63342, "session_id": "session", "helper_sdk_lifecycle_version": 1}

    @staticmethod
    def response(root, dry_run):
        return jb_inspect.HttpResult(200, {"status": "ok", "session_id": "session",
            "sdk_lifecycle_version": 1, "dry_run": dry_run, "sdks": [{
                "status": "would_remove" if dry_run else "removed", "reason": "helper_owned",
                "worktree_path": str(root), "sdk_name": "owned"}]}, "http://fixture")


    def test_missing_capability_refuses_before_any_request(self):
        for identities in ([], [self.identity(), {"port": 63343, "session_id": "old"}]):
            with self.subTest(identities=identities), patch_attr(jb_inspect, "http_post") as post:
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.unregister_helper_sdks(identities, None, False)
                post.assert_not_called()

    def test_session_drift_refuses(self):
        response = self.response(Path("/fixture/task"), True)
        response.body["session_id"] = "replacement"
        with patch_attr(jb_inspect, "http_post", return_value=response):
            with self.assertRaises(jb_inspect.InspectError):
                jb_inspect.unregister_helper_sdks([self.identity()], Path("/fixture/task"), True)

    def test_orphans_use_reviewed_paths_before_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "removed"
            args = Namespace(command="cleanup-helper-sdks", dry_run=False, lifecycle_lock_timeout_ms=1000,
                             worktree_path=[str(root)])
            applied = False
            def post(_port, _endpoint, params, **_kwargs):
                nonlocal applied
                if params["dry_run"] == "true":
                    self.assertEqual(params["orphans"], "true")
                    self.assertNotIn("worktree_path", params)
                else:
                    self.assertEqual(params["worktree_path"], str(root.resolve()))
                    self.assertNotIn("orphans", params)
                response = self.response(root, params["dry_run"] == "true")
                if applied:
                    response.body["sdks"] = []
                if params["dry_run"] == "false":
                    applied = True
                return response
            with patch_attr(jb_inspect, "discover_identities", return_value=[self.identity()]), patch_attr(jb_inspect, "http_post", side_effect=post) as request:
                result = jb_inspect.command_retire_sdks(args)
                self.assertEqual([call.args[2]["dry_run"] for call in request.call_args_list], ["true", "false", "true"])
                self.assertFalse(result["worktree_removed"])

    def test_open_project_refuses_even_without_sdk_records(self):
        root = Path("/fixture/task")
        identity = self.identity() | {"open_projects": [{"base_path": str(root / "nested")}]}
        with patch_attr(jb_inspect, "http_post") as post:
            with self.assertRaisesRegex(jb_inspect.InspectError, "Close every IDE project"):
                jb_inspect.unregister_helper_sdks([identity], root, False)
            post.assert_not_called()


    def test_refused_orphan_is_listed_without_blocking_unrelated_reviewed_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "removed"
            held = Path(tmp).resolve() / "renamed"
            args = Namespace(command="cleanup-helper-sdks", dry_run=False, lifecycle_lock_timeout_ms=1000, worktree_path=[str(root)])
            applied = False
            def post(_port, _endpoint, params, **_kwargs):
                nonlocal applied
                response = self.response(root, params["dry_run"] == "true")
                if params["dry_run"] == "true":
                    response.body["status"] = "refused"
                    if applied:
                        response.body["sdks"] = []
                    response.body["sdks"].append({"worktree_path": str(held), "status": "refused", "reason": "ownership_mismatch", "sdk_name": "held"})
                    raise jb_inspect.InspectError("HTTP 409", 3, response.body | {"http_status": 409})
                self.assertEqual(params["worktree_path"], str(root))
                applied = True
                return response
            with patch_attr(jb_inspect, "discover_identities", return_value=[self.identity()]), patch_attr(jb_inspect, "http_post", side_effect=post):
                result = jb_inspect.command_retire_sdks(args)
                self.assertEqual(result["sdk_preview"][0]["result"]["sdks"][1]["status"], "refused")
                self.assertEqual(len(result["sdk_cleanup"]), 1)
                args.worktree_path = [str(held)]
                with self.assertRaisesRegex(jb_inspect.InspectError, "absent from the current"):
                    jb_inspect.command_retire_sdks(args)

    def test_empty_orphan_apply_cannot_hide_a_remaining_owned_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "removed"
            args = Namespace(command="cleanup-helper-sdks", dry_run=False, lifecycle_lock_timeout_ms=1000, worktree_path=[str(root)])
            def post(_port, _endpoint, params, **_kwargs):
                response = self.response(root, params["dry_run"] == "true")
                if params["dry_run"] == "false":
                    response.body["sdks"] = []
                return response
            with patch_attr(jb_inspect, "discover_identities", return_value=[self.identity()]), patch_attr(jb_inspect, "http_post", side_effect=post):
                with self.assertRaisesRegex(jb_inspect.InspectError, "remain registered"):
                    jb_inspect.command_retire_sdks(args)


    def remove_racing_finder(self, root: Path, kept: list[str], written: list[str], unregister: bool = True, edited: tuple[str, ...] = ()):
        """Run remove-worktree while Git stops partway, as it does when Finder refills a folder (#844)."""
        primary = root.parent / "primary"
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        (primary / ".gitignore").write_text(".venv/\n")
        (primary / "pkg").mkdir()
        (primary / "pkg" / "module.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(primary), "add", "."], check=True)
        subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "task", str(root)], check=True)
        (root / ".venv" / "lib").mkdir(parents=True)
        (root / ".venv" / "lib" / "site.py").write_text("generated\n")
        real_run = subprocess.run
        def run(command, *args, **kwargs):
            if command[3:5] != ["worktree", "remove"]:
                return real_run(command, *args, **kwargs)
            if unregister:
                # Git deletes files, then drops the registration even though a folder stayed non-empty.
                git_dir = real_run(["git", "-C", str(root), "rev-parse", "--absolute-git-dir"],
                                   capture_output=True, text=True, check=True).stdout.strip()
                shutil.rmtree(git_dir)
                for path in root.rglob("*"):
                    if path.is_file() and str(path.relative_to(root)) not in kept:
                        path.unlink()
                for relative in written:
                    (root / relative).write_text("written during removal\n")
                for relative in edited:
                    with (root / relative).open("a") as handle:
                        handle.write("edited during removal\n")
            return subprocess.CompletedProcess(command, 128, "", f"error: failed to delete '{root}': Directory not empty\n")
        args = Namespace(command="remove-worktree", repo=str(root), dry_run=False, lifecycle_lock_timeout_ms=1000)
        with patch_attr(jb_inspect, "offline_retirement_records", return_value=[]), patch.object(subprocess, "run", side_effect=run):
            return jb_inspect.command_retire_sdks(args)

    def test_removal_finishes_when_only_finder_metadata_refills_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "task"
            result = self.remove_racing_finder(root, [], ["pkg/.DS_Store", ".DS_Store"])
            self.assertTrue(result["worktree_removed"])
            self.assertFalse(root.exists())

    def test_removal_finishes_when_verified_checkout_files_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "task"
            result = self.remove_racing_finder(root, ["pkg/module.py", ".venv/lib/site.py"], ["pkg/.DS_Store"])
            self.assertTrue(result["worktree_removed"])
            self.assertFalse(root.exists())

    def test_removal_keeps_and_reports_a_file_written_during_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "task"
            with self.assertRaisesRegex(jb_inspect.InspectError, "not in the verified checkout") as caught:
                self.remove_racing_finder(root, ["pkg/module.py"], ["pkg/.DS_Store", "pkg/notes.txt"])
            self.assertEqual(caught.exception.payload["retained_entries"], ["pkg/notes.txt"])
            self.assertEqual((root / "pkg" / "notes.txt").read_text(), "written during removal\n")
            self.assertTrue((root / "pkg" / "module.py").exists())

    def test_removal_keeps_a_verified_file_edited_in_place_during_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "task"
            with self.assertRaisesRegex(jb_inspect.InspectError, "not in the verified checkout") as caught:
                self.remove_racing_finder(root, ["pkg/module.py"], ["pkg/.DS_Store"], edited=("pkg/module.py",))
            self.assertEqual(caught.exception.payload["retained_entries"], ["pkg/module.py"])
            self.assertIn("edited during removal", (root / "pkg" / "module.py").read_text())

    def test_removal_keeps_a_file_written_while_leftovers_are_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "task"
            real_unlink = os.unlink
            def unlink(path, *args, **kwargs):
                real_unlink(path, *args, **kwargs)
                late = root / "pkg" / "late.txt"
                if not late.exists():
                    late.write_text("written after the leftovers were listed\n")
            with patch.object(os, "unlink", side_effect=unlink):
                with self.assertRaisesRegex(jb_inspect.InspectError, "not in the verified checkout") as caught:
                    self.remove_racing_finder(root, ["pkg/module.py"], ["pkg/.DS_Store"])
            self.assertEqual(caught.exception.payload["retained_entries"], ["pkg/late.txt"])
            self.assertTrue((root / "pkg" / "late.txt").exists())

    def test_refused_removal_that_keeps_the_registration_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "task"
            with self.assertRaisesRegex(jb_inspect.InspectError, "Git worktree removal failed"):
                self.remove_racing_finder(root, [], [], unregister=False)
            self.assertTrue((root / ".venv" / "lib" / "site.py").exists())


    def test_busy_empty_response_never_counts_as_cleanup_success(self):
        root = Path("/fixture/task")
        busy = {"status": "refused", "reason": "sdk_lifecycle_busy", "http_status": 409,
                "session_id": "session", "sdk_lifecycle_version": 1, "dry_run": False, "sdks": []}
        with patch_attr(jb_inspect, "http_post", side_effect=jb_inspect.InspectError("HTTP 409", 3, busy)) as post:
            with self.assertRaises(jb_inspect.InspectError) as caught:
                jb_inspect.unregister_helper_sdks([self.identity()], root, False)
            self.assertEqual(caught.exception.payload["reason"], "sdk_lifecycle_busy")
            self.assertGreater(post.call_args.kwargs["timeout"], 10)


    def test_malformed_orphan_paths_refuse_before_apply(self):
        for value in (None, "", "relative/path", 42):
            response = self.response(Path("/fixture/task"), True)
            response.body["sdks"][0]["worktree_path"] = value
            with self.subTest(value=value), patch_attr(jb_inspect, "http_post", return_value=response):
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.unregister_helper_sdks([self.identity()], None, True)

    def test_orphan_apply_requires_reviewed_paths_and_accessible_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "unmounted" / "removed"
            args = Namespace(command="cleanup-helper-sdks", dry_run=False, lifecycle_lock_timeout_ms=1000)
            for selected in ([], [str(root)]):
                args.worktree_path = selected
                with self.subTest(selected=selected), patch_attr(jb_inspect, "discover_identities", return_value=[self.identity()]), patch_attr(jb_inspect, "http_post", return_value=self.response(root, True)) as post:
                    with self.assertRaises(jb_inspect.InspectError):
                        jb_inspect.command_retire_sdks(args)
                    self.assertEqual(post.call_count, 1)

    def test_preflight_preserves_primary_locked_dirty_and_leased_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "primary"
            root = Path(tmp) / "task"
            subprocess.run(["git", "init", "-q", str(primary)], check=True)
            subprocess.run(["git", "-C", str(primary), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
            subprocess.run(["git", "-C", str(primary), "worktree", "add", "-q", "-b", "task", str(root)], check=True)
            with self.assertRaises(jb_inspect.InspectError):
                jb_inspect.retirement_worktree(primary, True)
            subprocess.run(["git", "-C", str(primary), "worktree", "lock", str(root)], check=True)
            self.assertEqual(jb_inspect.retirement_worktree(root, True), root.resolve())
            with self.assertRaises(jb_inspect.InspectError):
                jb_inspect.retirement_worktree(root, False)
            subprocess.run(["git", "-C", str(primary), "worktree", "unlock", str(root)], check=True)
            (root / "valuable").write_text("keep")
            with self.assertRaises(jb_inspect.InspectError):
                jb_inspect.retirement_worktree(root, True)
            (root / "valuable").unlink()
            with patch_attr(jb_inspect, "read_local_leases", return_value=[(Path("lease.json"), {"worktree_root": str(root)})]):
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.retirement_worktree(root, True)
            with patch_attr(jb_inspect, "git_common_worktree", return_value=None):
                with self.assertRaisesRegex(jb_inspect.InspectError, "primary checkout"):
                    jb_inspect.retirement_worktree(root, True)
            self.assertTrue(root.is_dir())


class RetiredDesktopCommandTests(unittest.TestCase):
    def test_open_worktree_names_agent_inspect_without_contacting_an_ide(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "open-worktree", "--repo", "."],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn(jb_inspect.RETIRED_COMMAND_MESSAGE.format(command="open-worktree"), result.stderr)


class GlobalConfigTests(unittest.TestCase):
    def test_shared_config_is_independent_of_cli_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shared = home / ".config/jetbrains-inspection/config.json"
            shared.parent.mkdir(parents=True)
            write_json(shared, {"jetbrains": {"trustedAutoOpenRoots": [str(home / "worktrees")]}})
            for variable in ("CODE_HOME", "CODEX_HOME", "CODEX_LAB_HOME"):
                with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(home / "cli")}, clear=True), patch.object(Path, "home", return_value=home):
                    self.assertEqual(jb_inspect.global_config_path(), shared)
                    self.assertEqual(jb_inspect.trusted_auto_open_roots(), [str((home / "worktrees").resolve())])

    def test_explicit_override_wins_even_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            shared = home / ".config/jetbrains-inspection/config.json"
            shared.parent.mkdir(parents=True)
            write_json(shared, {"shared": True})
            override = home / "override.json"
            with patch.dict(os.environ, {"JETBRAINS_INSPECTION_GLOBAL_CONFIG": str(override)}, clear=True), patch.object(Path, "home", return_value=home):
                self.assertEqual(jb_inspect.global_config_path(), override)
                self.assertEqual(jb_inspect.read_global_config(), {})

    def test_legacy_config_remains_available_until_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".code/jetbrains-inspection.json"
            legacy.parent.mkdir()
            write_json(legacy, {"legacy": True})
            for environment in ({}, {"CODE_HOME": str(legacy.parent)}, {"CODEX_HOME": str(legacy.parent)}):
                with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), patch.object(Path, "home", return_value=home):
                    self.assertEqual(jb_inspect.read_global_config(), {"legacy": True})
                    shared = home / ".config/jetbrains-inspection/config.json"
                    shared.parent.mkdir(parents=True, exist_ok=True)
                    write_json(shared, {"shared": True})
                    self.assertEqual(jb_inspect.read_global_config(), {"shared": True})
                    shared.unlink()

    def test_missing_configs_default_to_shared_location(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=Path(tmp)):
            self.assertEqual(jb_inspect.global_config_path(), Path(tmp) / ".config/jetbrains-inspection/config.json")
            self.assertEqual(jb_inspect.read_global_config(), {})

    def test_invalid_shared_config_does_not_fall_back_to_legacy_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".code/jetbrains-inspection.json"
            legacy.parent.mkdir()
            write_json(legacy, {"legacy": True})
            shared = home / ".config/jetbrains-inspection/config.json"
            shared.parent.mkdir(parents=True)
            shared.write_text("invalid json")
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, "home", return_value=home):
                with self.assertRaises(jb_inspect.InspectError):
                    jb_inspect.read_global_config()


def helper_args(**overrides):
    values = {
        "ide": None,
        "project_key": None,
        "project_path": None,
        "worktree_path": None,
        "cwd": None,
        "project": None,
        "session_id": None,
        "port": None,
        "open": False,
        "no_worktree_check": False,
    }
    values.update(overrides)
    return Namespace(**values)


class InspectionLaneConfigTest(unittest.TestCase):
    def test_parses_ordered_lanes_and_preserves_legacy_without_lanes(self):
        legacy = jb_inspect.parse_inspection_lanes({"ide": "PyCharm"})
        lanes = jb_inspect.parse_inspection_lanes(
            {
                "lanes": [
                    {
                        "id": "jvm",
                        "ide": "IntelliJ IDEA",
                        "required": True,
                        "include": ["**/*.kt", "**/*.gradle.kts"],
                        "exclude": ["test-fixtures/red-lane/**"],
                        "projectPath": "services/jvm",
                    },
                    {
                        "id": "python",
                        "ide": "PyCharm",
                        "required": False,
                        "include": ["**/*.py"],
                    },
                ]
            }
        )

        self.assertEqual(legacy, ())
        self.assertEqual([lane.lane_id for lane in lanes], ["jvm", "python"])
        self.assertTrue(lanes[0].required)
        self.assertFalse(lanes[1].required)
        self.assertEqual(lanes[0].exclude, ("test-fixtures/red-lane/**",))
        self.assertEqual(lanes[0].project_path, "services/jvm")
        self.assertEqual(lanes[0].public()["projectPath"], "services/jvm")
        self.assertNotIn("projectPath", lanes[1].public())

    def test_rejects_duplicate_ids_invalid_patterns_and_unknown_fields(self):
        cases = [
            {
                "lanes": [
                    {"id": "jvm", "ide": "IntelliJ IDEA", "include": ["**/*.kt"]},
                    {"id": "jvm", "ide": "PyCharm", "include": ["**/*.py"]},
                ]
            },
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["../**/*.py"]}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/[abc.py"]}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "profile": "strict"}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "projectPath": "../python"}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "projectPath": "/tmp/python"}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "projectPath": "python/*"}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "projectPath": "."}]},
            {"lanes": [{"id": 1, "ide": "PyCharm", "include": ["**/*.py"]}]},
            {"lanes": [{"id": "python", "ide": "PyCharm", "include": [42]}]},
        ]

        for inspection in cases:
            with self.subTest(inspection=inspection), self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.parse_inspection_lanes(inspection)
            self.assertEqual(raised.exception.payload["error_reason"], "inspection_lane_config_invalid")

class InspectionLaneSelectionTest(unittest.TestCase):
    @staticmethod
    def make_context(root: Path, lanes: tuple[Any, ...], scope: str = "files") -> dict[str, Any]:
        return {
            "repo_path": str(root),
            "worktree_root": str(root),
            "project_path": str(root),
            "exact_route_path": str(root),
            "lifecycle_target_path": str(root),
            "scope": scope,
            "_inspection_lanes": lanes,
        }

    def test_first_match_assignment_exclusions_and_unmatched_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            files = [
                root / "build.gradle.kts",
                root / "src" / "Example.kt",
                root / "tools" / "check.py",
                root / "test-fixtures" / "red-lane" / "Broken.py",
                root / "README.md",
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            lanes = jb_inspect.parse_inspection_lanes(
                {
                    "lanes": [
                        {
                            "id": "jvm",
                            "ide": "IntelliJ IDEA",
                            "include": ["**/*.kt", "**/*.gradle.kts"],
                        },
                        {
                            "id": "python",
                            "ide": "PyCharm",
                            "include": ["**/*.py", "tools/**"],
                            "exclude": ["test-fixtures/red-lane/**"],
                        },
                    ]
                }
            )
            args = helper_args(
                scope="files",
                files=[str(path) for path in files],
                include_unversioned=True,
                changed_files_mode="all",
                max_files=None,
            )

            selection = jb_inspect.resolve_inspection_lane_selection(args, self.make_context(root, lanes))

        self.assertEqual(
            [item["file"] for item in selection["lane_files"]["jvm"]],
            ["build.gradle.kts", "src/Example.kt"],
        )
        self.assertEqual(
            [item["file"] for item in selection["lane_files"]["python"]],
            ["tools/check.py", "test-fixtures/red-lane/Broken.py"],
        )
        self.assertEqual(selection["excluded_files"], [])
        self.assertEqual(selection["explicit_exclusion_overrides"][0]["file"], "test-fixtures/red-lane/Broken.py")
        self.assertEqual(selection["unmatched_files"], ["README.md"])

    def test_overlap_is_deterministic_first_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "tools" / "check.py"
            target.parent.mkdir()
            target.write_text("fixture\n", encoding="utf-8")
            lanes = jb_inspect.parse_inspection_lanes(
                {
                    "lanes": [
                        {"id": "first", "ide": "PyCharm", "include": ["tools/**"]},
                        {"id": "second", "ide": "PyCharm", "include": ["**/*.py"]},
                    ]
                }
            )
            args = helper_args(scope="files", files=[str(target)], max_files=None)

            selection = jb_inspect.resolve_inspection_lane_selection(args, self.make_context(root, lanes))

        self.assertEqual(len(selection["lane_files"]["first"]), 1)
        self.assertEqual(selection["lane_files"]["second"], [])

    def test_single_star_does_not_cross_path_segments(self):
        self.assertTrue(jb_inspect.inspection_lane_pattern_matches("src/check.py", "src/*.py"))
        self.assertFalse(jb_inspect.inspection_lane_pattern_matches("src/nested/check.py", "src/*.py"))
        self.assertTrue(jb_inspect.inspection_lane_pattern_matches("src/nested/check.py", "src/**/*.py"))
        self.assertTrue(jb_inspect.inspection_lane_pattern_matches("check.py", "**/*.py"))

    def test_rejects_file_outside_exact_worktree(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.NamedTemporaryFile() as outside:
            root = Path(tmp).resolve()
            lanes = jb_inspect.parse_inspection_lanes(
                {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"]}]}
            )
            args = helper_args(scope="files", files=[outside.name], max_files=None)

            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.resolve_inspection_lane_selection(args, self.make_context(root, lanes))

        self.assertEqual(raised.exception.payload["error_reason"], "inspection_lane_path_outside_worktree")

    def test_changed_files_are_resolved_once_and_partitioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            kotlin = root / "src" / "Example.kt"
            python = root / "tools" / "check.py"
            kotlin.parent.mkdir()
            python.parent.mkdir()
            kotlin.write_text("class Example\n", encoding="utf-8")
            python.write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
                cwd=root,
                check=True,
            )
            kotlin.write_text("class Updated\n", encoding="utf-8")
            python.write_text("print('updated')\n", encoding="utf-8")
            red_fixture = root / "test-fixtures" / "red-lane" / "Broken.py"
            red_fixture.parent.mkdir(parents=True)
            red_fixture.write_text("broken =\n", encoding="utf-8")
            lanes = jb_inspect.parse_inspection_lanes(
                {
                    "lanes": [
                        {"id": "jvm", "ide": "IntelliJ IDEA", "include": ["**/*.kt"]},
                        {
                            "id": "python",
                            "ide": "PyCharm",
                            "include": ["**/*.py"],
                            "exclude": ["test-fixtures/red-lane/**"],
                        },
                    ]
                }
            )
            args = helper_args(
                scope="changed_files",
                files=[],
                changed_files_mode="all",
                include_unversioned=True,
                max_files=None,
            )

            selection = jb_inspect.resolve_inspection_lane_selection(
                args,
                self.make_context(root, lanes, scope="changed_files"),
            )

        self.assertEqual(
            selection["selected_files"],
            ["src/Example.kt", "tools/check.py", "test-fixtures/red-lane/Broken.py"],
        )
        self.assertEqual([item["file"] for item in selection["lane_files"]["jvm"]], ["src/Example.kt"])
        self.assertEqual([item["file"] for item in selection["lane_files"]["python"]], ["tools/check.py"])
        self.assertEqual(selection["excluded_files"][0]["file"], "test-fixtures/red-lane/Broken.py")


class UnknownVerdictLogTest(unittest.TestCase):
    def test_routing_lock_uses_windows_file_lock_fallback(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = []

            def locking(self, _descriptor, mode, size):
                self.calls.append((mode, size))

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": tmp}),
                patch_attr(jb_inspect, jb_inspect.fcntl.__name__, None),
                patch_attr(jb_inspect, "msvcrt", fake_msvcrt),
            ):
                with jb_inspect.outcome_routing_lock(timeout_ms=0):
                    pass

        self.assertEqual(fake_msvcrt.calls, [(fake_msvcrt.LK_NBLCK, 1), (fake_msvcrt.LK_UNLCK, 1)])

    @unittest.skipIf(jb_inspect.fcntl is None, "requires POSIX file locking")
    def test_outcome_append_lock_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            with patch_attr(jb_inspect.fcntl, "flock", side_effect=BlockingIOError()):
                with self.assertRaisesRegex(TimeoutError, "outcome log lock"):
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "blocked"}, lock_timeout_ms=0)

    def test_outcome_append_uses_windows_file_lock_fallback(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = []

            def locking(self, _descriptor, mode, size):
                self.calls.append((mode, size))

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            with (
                patch_attr(jb_inspect, jb_inspect.fcntl.__name__, None),
                patch_attr(jb_inspect, "msvcrt", fake_msvcrt),
            ):
                jb_inspect.append_jsonl_record(log_path, {"event_id": "windows"}, lock_timeout_ms=0)

            self.assertEqual(json.loads(log_path.read_text(encoding="utf-8")), {"event_id": "windows"})

        self.assertEqual(fake_msvcrt.calls, [(fake_msvcrt.LK_NBLCK, 1), (fake_msvcrt.LK_UNLCK, 1)])

    def test_outcome_append_windows_lock_timeout_is_bounded(self):
        class BlockingMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def locking(self, _descriptor, mode, size):
                raise OSError("busy")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            with (
                patch_attr(jb_inspect, jb_inspect.fcntl.__name__, None),
                patch_attr(jb_inspect, "msvcrt", BlockingMsvcrt()),
            ):
                with self.assertRaisesRegex(TimeoutError, "outcome log lock"):
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "blocked"}, lock_timeout_ms=0)

            self.assertEqual(log_path.read_bytes(), b"")

    def test_summarize_outcome_log_counts_agent_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcomes.jsonl"
            outcome_path.write_text(
                "\n".join(
                    [
                        json.dumps({
                            "timestamp": "2026-07-04T00:00:00Z",
                            "command": "inspect-closeout",
                            "verdict": "UNKNOWN",
                            "bucket": "ide_not_ready",
                            "retry": True,
                            "ide_channel": "stable",
                            "cleanup_status": "deferred",
                            "repo_path": "/secret/repo",
                        }),
                        "not-json",
                        json.dumps({
                            "timestamp": "2026-07-04T00:01:00Z",
                            "command": "inspect-closeout",
                            "verdict": "GREEN",
                            "bucket": "clean",
                            "retry": False,
                            "ide_channel": "stable",
                            "cleanup_status": "closed",
                            "worktree_root": "/secret/worktree",
                        }),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = jb_inspect.summarize_outcome_log(outcome_path, limit=1)

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["events"], 2)
            self.assertEqual(summary["invalid_lines"], 1)
            self.assertEqual(summary["summary"]["by_verdict"], {"GREEN": 1, "UNKNOWN": 1})
            self.assertEqual(summary["summary"]["by_bucket"], {"clean": 1, "ide_not_ready": 1})
            self.assertEqual(summary["summary"]["by_retry"], {"false": 1, "true": 1})
            self.assertEqual(summary["summary"]["by_cleanup_status"], {"closed": 1, "deferred": 1})
            self.assertEqual(summary["summary"]["retryable_unknowns"], 1)
            self.assertEqual(len(summary["recent"]), 1)
            self.assertEqual(summary["recent"][0]["bucket"], "clean")
            self.assertNotIn("repo_path", summary["recent"][0])
            self.assertNotIn("worktree_root", json.dumps(summary))

    def test_summarize_outcome_log_missing_path_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = jb_inspect.summarize_outcome_log(Path(tmp) / "missing.jsonl")

            self.assertEqual(summary["status"], "missing")
            self.assertEqual(summary["events"], 0)
            self.assertEqual(summary["summary"], jb_inspect.empty_outcome_summary())

    def test_command_summarize_outcomes_respects_disabled_env(self):
        with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: "0"}):
            summary = jb_inspect.command_summarize_outcomes(Namespace(log_path=None, limit=10))

        self.assertEqual(summary["status"], "disabled")
        self.assertEqual(summary["events"], 0)

    def test_command_summarize_outcomes_reads_explicit_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "custom-outcomes.jsonl"
            outcome_path.write_text(
                json.dumps({"verdict": "GREEN", "bucket": "clean", "command": "inspect-closeout"}) + "\n",
                encoding="utf-8",
            )

            summary = jb_inspect.command_summarize_outcomes(Namespace(log_path=str(outcome_path), limit=5))

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["summary"]["by_verdict"], {"GREEN": 1})

    def test_jsonl_append_uses_one_locked_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            with patch.object(os, "write", wraps=jb_inspect.os.write) as write_call:
                if jb_inspect.fcntl is None:
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "one"})
                    lock_calls = []
                else:
                    with patch_attr(jb_inspect.fcntl, "flock", wraps=jb_inspect.fcntl.flock) as lock_call:
                        jb_inspect.append_jsonl_record(log_path, {"event_id": "one"})
                    lock_calls = [call.args[1] for call in lock_call.call_args_list]
            written_record = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(write_call.call_count, 1)
        if jb_inspect.fcntl is not None:
            self.assertEqual(
                lock_calls,
                [jb_inspect.fcntl.LOCK_EX | jb_inspect.fcntl.LOCK_NB, jb_inspect.fcntl.LOCK_UN],
            )
        self.assertEqual(written_record, {"event_id": "one"})

    def test_jsonl_append_rolls_back_partial_write_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            log_path.write_bytes(b'{"event_id":"existing"}\n')
            original = log_path.read_bytes()
            real_write = jb_inspect.os.write
            call_count = 0

            def partial_then_fail(descriptor, payload):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return real_write(descriptor, bytes(payload[:5]))
                raise OSError("simulated append failure")

            with patch.object(os, "write", side_effect=partial_then_fail):
                with self.assertRaisesRegex(OSError, "simulated append failure"):
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "new"})

            self.assertEqual(log_path.read_bytes(), original)


class ScopedLeaseCleanupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="scoped-lease-cleanup-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.selected_id = "190abb77-89c0-458b-84ce-39a2d638dd91"
        self.foreign_id = "04f75cb7-770c-481c-91fc-67509436dafd"
        self.lease = {
            "lease_id": self.selected_id, "state": "cleanup_failed", "pid": 68466,
            "opened_by_helper": True, "updated_at_ms": 1,
            "worktree_root": "/tmp/selected", "project_key": "path:/tmp/selected",
            "project_instance_id": "session:1", "session_id": "session",
        }
        self.selected_path = self.directory / f"{self.selected_id}.json"
        self.foreign_path = self.directory / f"{self.foreign_id}.json"
        write_json(self.selected_path, self.lease)
        write_json(self.foreign_path, self.lease | {"lease_id": self.foreign_id})
        self.foreign_bytes = self.foreign_path.read_bytes()
        self.enterContext(patch_attr(jb_inspect, "lease_dir", return_value=self.directory))
        self.enterContext(patch_attr(jb_inspect, "now_ms", return_value=100_000_000))
        self.enterContext(patch_attr(jb_inspect, "pid_alive", return_value=False))
        self.discovery = self.enterContext(patch_attr(
            jb_inspect, "discover_routes_for_cleanup", return_value=([], {"session"}),
        ))

    def run_cleanup(self, *selectors, dry_run=False):
        args = jb_inspect.build_parser().parse_args([
            "cleanup-helper-leases", "--dry-run" if dry_run else "--no-dry-run",
            *[value for selector in selectors for value in ("--lease-id", selector)],
        ])
        return jb_inspect.command_cleanup_leases(args)

    def test_selected_stale_lease_is_reconciled_without_touching_foreign_lease(self):
        # Discovery proves the original session exists but its exact project is gone.
        result = self.run_cleanup(self.selected_id)
        self.assertEqual(result["removed"], [str(self.selected_path)])
        self.assertFalse(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)
        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 0)

    def test_scoped_dry_run_proves_selected_removal_without_mutation(self):
        result = self.run_cleanup(self.selected_id, dry_run=True)
        self.assertEqual(result["stale"], [str(self.selected_path)])
        self.assertEqual(result["removed"], [])
        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 0)
        self.discovery.assert_called_once()
        self.assertEqual(result["would_remove"], [str(self.selected_path)])
        self.assertTrue(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def test_invalid_missing_and_multiple_selectors_fail_without_discovery(self):
        cases = [
            (("../other",), "invalid_lease_selector"),
            (("",), "invalid_lease_selector"),
            (("00000000-0000-0000-0000-000000000000",), "lease_not_found"),
            ((self.selected_id, self.foreign_id), "invalid_lease_selector"),
            ((self.selected_id, self.selected_id), "invalid_lease_selector"),
        ]
        for selectors, reason in cases:
            with self.subTest(selectors=selectors):
                result = self.run_cleanup(*selectors)
                self.assertEqual(result["failed"][0]["reason"], reason)
                self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)
                self.assertEqual(result["removed"], [])
        self.discovery.assert_not_called()
        self.assertTrue(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def test_duplicate_payload_identity_and_mismatched_filename_fail_closed(self):
        write_json(self.foreign_path, self.lease)
        result = self.run_cleanup(self.selected_id)
        self.assertEqual(result["failed"][0]["reason"], "lease_identity_ambiguous")
        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)
        self.selected_path.unlink()
        result = self.run_cleanup(self.selected_id)
        self.assertEqual(result["failed"][0]["reason"], "lease_identity_mismatch")
        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)
        self.discovery.assert_not_called()
        self.assertTrue(self.foreign_path.exists())

    def test_live_pid_with_old_cleanup_failed_lease_is_not_forced_stale(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), patch_attr(jb_inspect, "pid_alive", return_value=True):
                result = self.run_cleanup(self.selected_id, dry_run=dry_run)
                self.assertEqual(result["unresolved"][0]["reason"], "lease_not_stale")
                self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)
        self.discovery.assert_not_called()
        self.assertTrue(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def test_scoped_discovery_failure_preserves_both_leases(self):
        self.discovery.side_effect = jb_inspect.InspectError("unavailable", 3)
        result = self.run_cleanup(self.selected_id)
        self.assertEqual(result["failed"][0]["reason"], "route_discovery_failed")
        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)
        self.assertTrue(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def test_cleanup_failed_lease_in_foreign_session_remains_unresolved(self):
        self.discovery.return_value = ([{
            "base_path": "/tmp/selected", "project_instance_id": "foreign:1",
            "session_id": "foreign", "project_key": "path:/tmp/selected",
        }], {"foreign"})
        with patch_attr(jb_inspect, "reclaim_lifecycle_claim") as claim:
            result = self.run_cleanup(self.selected_id)
        claim.assert_not_called()
        self.assertEqual(result["unresolved"][0]["reason"], "ownership_route_unavailable")
        self.assertTrue(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def write_never_opened_lease(self, **changes):
        # project_open_blocked: a live IDE accepted the open, but no exact route ever appeared.
        attempt = {"accepted": True, "ownership_registered": True, "lease_id": self.selected_id,
                   "identity": {"session_id": "session", "port": 63342}}
        write_json(self.selected_path, {
            "lease_id": self.selected_id, "state": "cleanup_pending", "pid": 68466,
            "opened_by_helper": True, "open_request_may_have_been_accepted": False,
            "lifecycle_target_path": "/tmp/selected", "worktree_root": "/tmp/selected",
            "session_id": "session", "ide_port": 63342, "updated_at_ms": 1,
            "open_attempts": [attempt],
        } | changes)

    def probe_ide(self, status, reason, session="session", diagnostic_version=1):
        identity = {"session_id": "session", "port": 63342, "lifecycle_open_diagnostic_version": diagnostic_version}
        body = {"status": status, "reason": reason, "opened": False, "opening_scheduled": False,
                "probe": True, "session_id": session}
        return (
            patch_attr(jb_inspect, "identity_for_port", return_value=identity),
            patch_attr(jb_inspect, "http_get", return_value=jb_inspect.HttpResult(200, body, "http://fixture")),
        )

    def test_live_original_session_reporting_project_not_open_releases_lease(self):
        acknowledged = {"method": "bootstrapped_ide", "accepted": True, "ownership_registered": True,
                        "lease_id": self.selected_id, "identity": {"session_id": "session", "port": 63342}}
        histories = {
            "running_ide": None,
            # The IDE was started first; startup and "no matching IDE" entries sent no open request.
            "bootstrapped_ide": [
                {"method": "running_ide", "accepted": False, "reason": "no_matching_running_ide"},
                {"method": "bootstrap_ide", "accepted": True},
                acknowledged,
            ],
        }
        for name, attempts in histories.items():
            with self.subTest(name):
                self.write_never_opened_lease(**({"open_attempts": attempts} if attempts else {}))
                identity, get = self.probe_ide("not_open", "project_not_open")
                with identity, get as http_get, patch_attr(jb_inspect, "cleanup_lifecycle") as close:
                    preview = self.run_cleanup(self.selected_id, dry_run=True)
                    self.assertEqual(preview["would_remove"], [str(self.selected_path)])
                    self.assertTrue(self.selected_path.exists())
                    result = self.run_cleanup(self.selected_id)
                close.assert_not_called()
                endpoint, params = http_get.call_args.args[1:3]
                self.assertEqual((endpoint, params["probe"], params["lease_id"]),
                                 ("lifecycle/open", "true", self.selected_id))
                self.assertEqual(result["removed"], [str(self.selected_path)])
                self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 0)
                self.assertFalse(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def test_live_original_session_still_opening_or_unprovable_retains_lease(self):
        # The probe cannot fence a request the IDE has not registered yet, so only acknowledged opens qualify.
        ambiguous = {"open_request_may_have_been_accepted": True, "state": "open_requesting", "open_attempts": [{
            "accepted": False, "request_may_have_been_accepted": True, "ownership_registered": False,
            "lease_id": self.selected_id, "identity": {"session_id": "session", "port": 63342},
        }]}
        cases = {
            "opening": ("opening", "already_opening", "session", 1, {}),
            "other_session_answered": ("not_open", "project_not_open", "other-session", 1, {}),
            "plugin_without_probe": ("not_open", "project_not_open", "session", 0, {}),
            "ambiguous_open_request": ("not_open", "project_not_open", "session", 1, ambiguous),
        }
        for name, (status, reason, session, version, changes) in cases.items():
            with self.subTest(name):
                self.write_never_opened_lease(**changes)
                identity, get = self.probe_ide(status, reason, session, version)
                with identity, get as http_get:
                    result = self.run_cleanup(self.selected_id)
                unresolved = result["unresolved"][0]
                self.assertEqual(unresolved["reason"], "ownership_route_unavailable")
                self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)
                self.assertTrue(self.selected_path.exists())
                if name in {"opening", "ambiguous_open_request"}:
                    self.assertEqual(unresolved["open_probe"], {"status": status, "reason": reason})
                if name == "plugin_without_probe":
                    # An older plugin would treat the probe as a real open request.
                    http_get.assert_not_called()

    def test_plain_output_names_unresolved_lease_reason_without_inspection_verdict(self):
        self.discovery.return_value = ([{
            "base_path": "/tmp/selected", "project_instance_id": "foreign:1",
            "session_id": "foreign", "project_key": "path:/tmp/selected",
        }], {"foreign"})
        output = io.StringIO()
        argv = [str(SCRIPT_PATH), "cleanup-helper-leases", "--lease-id", self.selected_id, "--no-dry-run"]
        with patch.object(sys, "argv", argv), redirect_stdout(output):
            code = jb_inspect.main()
        text = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn(f"LEASE_UNRESOLVED: lease_id={self.selected_id} reason=ownership_route_unavailable", text)
        self.assertNotIn("VERDICT:", text)
        self.assertTrue(self.selected_path.exists())

    def test_selection_and_cleanup_remain_inside_lifecycle_lock(self):
        held = []
        class Lock:
            def __enter__(self):
                held.append(True)
            def __exit__(self, *_):
                held.pop()
        original_read = jb_inspect.read_local_leases
        def read_locked():
            self.assertEqual(held, [True])
            return original_read()
        with (
            patch_attr(jb_inspect, "lifecycle_lock", return_value=Lock()),
            patch_attr(jb_inspect, "read_local_leases", side_effect=read_locked),
        ):
            result = self.run_cleanup(self.selected_id)
        self.assertEqual(result["removed"], [str(self.selected_path)])

    def test_default_cleanup_still_processes_all_stale_leases(self):
        result = self.run_cleanup()
        self.assertCountEqual(result["removed"], [str(self.selected_path), str(self.foreign_path)])
        self.assertFalse(self.selected_path.exists())
        self.assertFalse(self.foreign_path.exists())

    def test_normalized_selector_identifies_the_same_lease(self):
        for selector in (self.selected_id.upper(), self.selected_id.replace("-", "")):
            with self.subTest(selector=selector):
                result = self.run_cleanup(selector, dry_run=True)
                self.assertEqual(result["stale"], [str(self.selected_path)])
                self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 0)
        self.assertEqual(self.discovery.call_count, 2)
        self.assertTrue(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)

    def test_preexisting_project_releases_only_selected_local_lease_without_discovery(self):
        write_json(self.selected_path, self.lease | {"opened_by_helper": False})
        result = self.run_cleanup(self.selected_id)
        self.assertEqual(result["removed"], [str(self.selected_path)])
        self.assertEqual(result["closed"], [])
        self.discovery.assert_not_called()
        self.assertFalse(self.selected_path.exists())
        self.assertEqual(self.foreign_path.read_bytes(), self.foreign_bytes)


if __name__ == "__main__":
    unittest.main()
