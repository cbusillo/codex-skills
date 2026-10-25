#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Fixture helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


host: Any = load("native_common_test_host", SCRIPTS / "jb-inspect.py")
common: Any = load("native_common_test_module", SCRIPTS / "native_common.py")


class NativeCommonTests(unittest.TestCase):
    root: Path
    repo: Path

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src/a.py").write_text("print(missing)\n")
        (self.repo / "src/b.py").write_text("print('other')\n")
        (self.repo / ".github").mkdir()
        environment = patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": str(self.root / "cache"),
                                              "JB_INSPECT_OUTCOME_LOG": "0", "JB_INSPECT_UNKNOWN_LOG": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def declare_inspection(self, inspection: dict[str, Any]) -> None:
        (self.repo / ".github/github.json").write_text(json.dumps({"qualityGate": {"inspection": inspection}}))

    def test_nested_desktop_preparation_cannot_prepare_the_snapshot_root(self) -> None:
        private = self.root / "private"
        private.mkdir()
        with self.assertRaises(common.Blocked):
            common.preparation({"prepare": {"python": {"version": "3.12", "projectPath": "scripts"}}}, private)
        self.assertEqual(list(private.iterdir()), [])

    def test_preparation_runs_in_the_snapshot_and_leaves_the_model_to_native(self) -> None:
        private = self.root / "private"
        project = private / "project"
        (project / ".venv/bin").mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        (project / ".gitignore").write_text(".venv/\n")
        interpreter = project / ".venv/bin/python"
        interpreter.write_text("#!/bin/sh\necho 3.12\n")
        interpreter.chmod(0o755)
        argv = common.preparation({"prepare": {"python": {"version": "3.12", "moduleName": "m"}}}, private)
        generator = argv.index(str(private / "prepare-python-project.py"))
        self.assertEqual(argv[argv.index("--repo") + 1], str(project))
        subprocess.run([sys.executable, *argv[generator:]], check=True, capture_output=True)
        self.assertFalse((project / ".idea").exists())

    def test_lane_gap_only_for_files_outside_root_pycharm_lanes(self) -> None:
        config = {"lanes": [{"id": "jvm", "ide": "IntelliJ IDEA", "include": ["**/*.kt"]},
                            {"id": "python", "ide": "PyCharm", "include": ["**/*.py"]}]}
        self.assertEqual(common.lane_gaps(host, config, {"src/a.py"}), [])
        self.assertTrue(common.lane_gaps(host, config, {"src/a.py", "src/A.kt"}))
        nested = {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "projectPath": "src"}]}
        self.assertTrue(common.lane_gaps(host, nested, {"src/a.py"}))

    def test_snapshot_refuses_credential_locations_before_any_read_or_copy(self) -> None:
        for name in ("pycharm.key", "plugin_TEST.license", ".env", ".env.local",
                     ".ssh/id_ed25519", ".aws/credentials", "credentials.json", "account.json"):
            with self.subTest(name=name), \
                    patch_attr(common, "checked", return_value=("src/a.py\0" + name + "\0").encode()), \
                    patch.object(shutil, "copyfile") as copy, \
                    patch_attr(common, "digest") as read:
                with self.assertRaises(common.Blocked) as refusal:
                    common.snapshot(self.repo, self.root / "snapshot")
                self.assertEqual(refusal.exception.input_path, name)
                copy.assert_not_called()
                read.assert_not_called()

    def test_macos_firmlink_uses_diskutil_mount_device_not_path_equality(self) -> None:
        anchor = self.root / "users-firmlink"
        mounted = self.root / "data-volume"
        anchor.mkdir()
        mounted.mkdir()
        data = plistlib.dumps({"MountPoint": str(mounted), "VolumeUUID": "fixture-volume"})
        with patch.object(sys, "platform", "darwin"), \
                patch_attr(common, "command", return_value=subprocess.CompletedProcess([], 0, data, b"")):
            identity = common.storage_identity(anchor)
        self.assertEqual(identity["volume_uuid"], plistlib.loads(data)["VolumeUUID"])

    def test_diskutil_firmlink_refusal_resolves_device_through_df(self) -> None:
        data = plistlib.dumps({"MountPoint": str(self.root), "VolumeUUID": "fixture-volume"})
        attempts = []

        def query(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            attempts.append(argv)
            return subprocess.CompletedProcess(argv, 1 if len(attempts) == 1 else 0, b"" if len(attempts) == 1 else data, b"")

        df = b"Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/fixture 1 1 0 100% /fixture\n"
        with patch.object(sys, "platform", "darwin"), patch_attr(common, "command", side_effect=query), \
                patch_attr(common, "checked", return_value=df):
            identity = common.storage_identity(self.root)
        self.assertEqual(identity["volume_uuid"], plistlib.loads(data)["VolumeUUID"])
        self.assertEqual(attempts[-1][-1], df.decode().splitlines()[-1].split()[0])

    def test_test_and_control_manifests_do_not_make_the_repository_odoo(self) -> None:
        for directory in ("assets/control", "tests/fixtures/addon"):
            path = self.repo / directory / "__manifest__.py"
            path.parent.mkdir(parents=True)
            path.write_text("{}\n")
            subprocess.run(["git", "-C", str(self.repo), "add", str(path)], check=True)
        self.assertFalse(common.is_odoo(self.repo))
        (self.repo / "addon").mkdir()
        (self.repo / "addon/__manifest__.py").write_text("{}\n")
        self.assertTrue(common.is_odoo(self.repo))

    def test_scope_honors_lane_exclusions_and_explicit_file_override(self) -> None:
        self.declare_inspection({"lanes": [{"id": "python", "ide": "PyCharm", "required": True,
                                            "include": ["**/*.py"], "exclude": ["fixtures/**"]}]})
        fixture = self.repo / "fixtures/defect.py"
        fixture.parent.mkdir()
        fixture.write_text("print(missing)\n")
        args = host.build_parser().parse_args(["agent-inspect", "--repo", str(self.repo), "--scope", "whole_project"])
        self.assertNotIn("fixtures/defect.py", common.scope_selection(args, host)[3])
        args.scope, args.files = "files", ["fixtures/defect.py"]
        self.assertIn("fixtures/defect.py", common.scope_selection(args, host)[3])

    def test_mixed_selection_uses_declared_lanes(self) -> None:
        self.declare_inspection({"lanes": [{"id": "python", "ide": "PyCharm", "required": True, "include": ["**/*.py"]}]})
        (self.repo / "README.md").write_text("Fixture documentation\n")
        args = host.build_parser().parse_args(["agent-inspect", "--repo", str(self.repo), "--scope", "changed_files"])
        with patch_attr(host, "inspection_scope_paths", return_value=["src/a.py", "README.md"]):
            self.assertEqual(common.scope_selection(args, host)[3], {"src/a.py"})
        args.scope, args.files = "files", ["src/a.py", "README.md"]
        self.assertEqual(common.scope_selection(args, host)[3], {"src/a.py"})
        receipt: dict[str, Any] = {}
        common.scope_selection(args, host, receipt)
        self.assertEqual(receipt["unmatched_files"], ["README.md"])
        self.assertEqual(receipt["excluded_files"], [])


if __name__ == "__main__":
    unittest.main()
