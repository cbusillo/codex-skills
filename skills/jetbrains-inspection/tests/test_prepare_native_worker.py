#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
native: Any = importlib.import_module("native_backend")

setup: Any = native.sibling_module("prepare_native_worker_test", "prepare-native-worker.py")
host: Any = setup.host


class PrepareWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.app = self.root / "Test IDE.app"
        (self.app / "Contents/bin").mkdir(parents=True)
        (self.app / "Contents/MacOS").mkdir()
        (self.app / "Contents/Resources").mkdir()
        (self.app / "Contents/bin/inspect.sh").touch()
        self.destination = self.root / "new worker"
        worker_override = patch.dict(os.environ, JETBRAINS_INSPECTION_NATIVE_WORKER=str(self.root / "ambient-worker"))
        worker_override.start()
        self.addCleanup(worker_override.stop)
        uv = self.root / "fixture uv"
        uv.write_text(f'#!/bin/sh\nwhile [ "$1" != python ]; do shift; done\nshift\nexec {sys.executable} "$@"\n')
        uv.chmod(0o700)
        resolve_uv = patch.object(shutil, "which", return_value=str(uv))
        resolve_uv.start()
        self.addCleanup(resolve_uv.stop)

    def arguments(self, product_key: str = "pycharm") -> Any:
        product = host.IDE_PRODUCTS[product_key]
        (self.app / "Contents/Resources/product-info.json").write_text(
            json.dumps(
                {
                    "productCode": product.product_codes[0],
                    "envVarBaseName": product.env_var_base_name,
                }
            )
        )
        (self.app / "Contents/bin" / product.vm_options_name).write_text(
            f"-ea\n-Duser.home={self.root / 'other-home'}\n-Djava.io.tmpdir={self.root / 'other-temp'}\n"
            f"-Didea.config.path={self.root / 'other-config'}\n-XX:ErrorFile={self.root / 'other-error.log'}\n"
            "-XX:+HeapDumpOnOutOfMemoryError\n"
        )
        executable = self.app / "Contents/MacOS" / product.launcher_names[0]
        executable.write_text(f"#!{sys.executable}\nimport json,os\nprint(json.dumps(dict(os.environ)))\n")
        executable.chmod(0o700)
        argv = [
            "--product",
            product_key,
            "--destination",
            str(self.destination),
            "--app",
            str(self.app),
            "--cache-root",
            str(self.cache),
        ]
        if product_key == "webstorm":
            for name in ("node", "npm"):
                path = self.root / name
                path.touch()
                path.chmod(0o700)
            argv += ["--node", str(self.root / "node")]
        if product_key == "intellij":
            (self.root / "jdk/bin").mkdir(parents=True)
            (self.root / "jdk/release").touch()
            (self.root / "jdk/bin/java").touch(mode=0o700)
            argv += ["--jdk", str(self.root / "jdk")]
        return setup.build_parser().parse_args(argv)

    def test_each_product_prepares_an_empty_profile_accepted_by_native_validation(self) -> None:
        for product_key in host.IDE_PRODUCTS:
            with self.subTest(product=product_key):
                self.destination = self.root / product_key
                args = self.arguments(product_key)
                with patch.dict(os.environ, JETBRAINS_INSPECTION_NATIVE_WORKER="relative-override"):
                    result = setup.prepare(args)
                config = result["config"]
                configured_host = SimpleNamespace(IDE_PRODUCTS=host.IDE_PRODUCTS, read_global_config=lambda current=config: current)
                selection = host.build_parser().parse_args(["agent-inspect", "--native-worker", str(self.destination)])
                worker = native.worker_settings(selection, configured_host, product_key)
                native.launcher_paths(worker.worker)
                if product_key == "webstorm":
                    native.node_executable(configured_host)
                if product_key == "intellij":
                    native.java_home(configured_host)
                for name in ("config", "plugins", "home"):
                    self.assertEqual(list((self.destination / name).iterdir()), [])
                self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
                effective = {}
                for option in (self.destination / worker.product.vm_options_name).read_text().splitlines():
                    key, separator, value = option.partition("=")
                    if separator:
                        effective[key] = value
                    elif option.startswith(("-XX:+", "-XX:-")):
                        effective[f"-XX:{option[5:]}"] = option[4] == "+"
                self.assertEqual(Path(effective["-Duser.home"]), self.destination / "home")
                self.assertEqual(Path(effective["-Djava.io.tmpdir"]), self.destination / "tmp")
                self.assertTrue(Path(effective["-XX:ErrorFile"]).is_relative_to(self.destination / "log"))
                self.assertFalse(effective["-XX:HeapDumpOnOutOfMemoryError"])
                for key, value in effective.items():
                    if key.startswith("-Didea.") and key.endswith(".path"):
                        self.assertTrue(Path(value).is_relative_to(self.destination))

    def test_existing_paths_are_refused_before_reading_any_input(self) -> None:
        args = self.arguments()
        for kind in ("directory", "file", "dangling_link"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    self.destination.mkdir()
                elif kind == "file":
                    self.destination.touch()
                else:
                    self.destination.symlink_to(self.root / "absent")
                with patch.object(Path, "read_text", side_effect=AssertionError("must not read existing paths")):
                    with self.assertRaises(FileExistsError):
                        setup.prepare(args)
                if kind == "directory":
                    self.destination.rmdir()
                else:
                    self.destination.unlink()

    def test_invalid_product_distribution_and_missing_runtimes_leave_no_profile(self) -> None:
        for product_key in host.IDE_PRODUCTS:
            with self.subTest(product=product_key):
                args = self.arguments(product_key)
                if product_key == "webstorm":
                    args.node = None
                elif product_key == "intellij":
                    args.jdk = None
                else:
                    (self.app / "Contents/Resources/product-info.json").write_text("{}")
                with self.assertRaises((ValueError, native.Blocked)):
                    setup.prepare(args)
                self.assertFalse(self.destination.exists())

    def test_missing_storage_and_properties_injection_leave_no_profile(self) -> None:
        args = self.arguments()
        for destination in (self.root / "unmounted/worker", self.root / "bad\nidea.config.path=other"):
            with self.subTest(destination=destination):
                args.destination = str(destination)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    setup.prepare(args)
                self.assertFalse(destination.exists())
        args.destination = str(self.destination)
        args.cache_root = str(self.root / "absent-cache")
        with self.assertRaises((ValueError, FileNotFoundError)):
            setup.prepare(args)
        self.assertFalse(self.destination.exists())

    def test_launcher_uses_isolated_environment_and_inherits_the_backend_lock(self) -> None:
        args = self.arguments()
        prepared = setup.prepare(args)
        product = host.IDE_PRODUCTS[args.product]
        executable = self.app / "Contents/MacOS" / product.launcher_names[0]
        executable.write_text(
            executable.read_text()
            + """
import fcntl
from pathlib import Path
with (Path(os.environ['HOME']).parent / 'worker.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('lock-held')
"""
        )
        environment = dict(os.environ, IDEA_PROPERTIES="desktop", PYCHARM_VM_OPTIONS="desktop", JDK_JAVA_OPTIONS="desktop")
        launcher = Path(prepared["launcher"])
        result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True, check=True)
        values = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(Path(values["HOME"]), self.destination / "home")
        self.assertEqual(Path(values["TMPDIR"]), self.destination / "tmp")
        self.assertEqual(Path(values[f"{product.env_var_base_name}_PROPERTIES"]), self.destination / "idea.properties")
        self.assertEqual(Path(values[f"{product.env_var_base_name}_VM_OPTIONS"]), self.destination / product.vm_options_name)
        self.assertNotIn("IDEA_PROPERTIES", values)
        self.assertNotIn("JDK_JAVA_OPTIONS", values)
        self.assertIn("lock-held", result.stdout)
        with native.profile_lock(self.destination):
            busy = subprocess.run([sys.executable, str(self.destination / "launch.py")], capture_output=True)
        self.assertNotEqual(busy.returncode, 0)

    def test_write_failure_removes_only_the_unactivated_profile_and_allows_retry(self) -> None:
        args = self.arguments()
        with patch.object(os, "open", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                setup.prepare(args)
        self.assertFalse(self.destination.exists())
        setup.prepare(args)

    def test_ide_requested_restart_keeps_the_lock_and_isolated_environment(self) -> None:
        args = self.arguments()
        prepared = setup.prepare(args)
        executable = self.app / "Contents/MacOS" / host.IDE_PRODUCTS[args.product].launcher_names[0]
        executable.write_text(
            f"#!{sys.executable}\n"
            + """
import fcntl,json,os
from pathlib import Path
worker = Path(os.environ['HOME']).parent
with (worker / 'worker.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('lock-held', flush=True)
marker = worker / 'restarted'
if not marker.exists():
    marker.touch()
    raise SystemExit(int(os.environ['IDEA_RESTART_VIA_EXIT_CODE']))
print(json.dumps(dict(os.environ)))
"""
        )
        result = subprocess.run([prepared["launcher"]], capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(result.stdout.count("lock-held"), 2)
        self.assertEqual(Path(json.loads(result.stdout.splitlines()[-1])["HOME"]), self.destination / "home")


if __name__ == "__main__":
    unittest.main()
