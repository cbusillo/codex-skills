#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_backend as native

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
        uv = self.root / "fixture uv"
        uv.write_text(f'#!/bin/sh\nshift 5\nexec {sys.executable} "$@"\n')
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
        (self.app / "Contents/bin" / product.vm_options_name).write_text("-ea\n-Xmx1024m\n")
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
                result = setup.prepare(args)
                config = result["config"]
                with patch.object(host, "read_global_config", return_value=config):
                    worker = native.worker_settings(host.build_parser().parse_args(["agent-inspect"]), host, product_key)
                    native.launcher_paths(worker.worker)
                    if product_key == "webstorm":
                        native.node_executable(host)
                    if product_key == "intellij":
                        native.java_home(host)
                for name in ("config", "plugins", "home"):
                    self.assertEqual(list((self.destination / name).iterdir()), [])
                self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)

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
        setup.prepare(args)
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
        launcher = self.destination / f"Activate {product.display_name} worker.command"
        result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True, check=True)
        values = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(Path(values["HOME"]), self.destination / "home")
        self.assertEqual(Path(values[f"{product.env_var_base_name}_PROPERTIES"]), self.destination / "idea.properties")
        self.assertNotIn("IDEA_PROPERTIES", values)
        self.assertNotIn("JDK_JAVA_OPTIONS", values)
        self.assertIn("lock-held", result.stdout)
        with native.profile_lock(self.destination):
            busy = subprocess.run([sys.executable, str(self.destination / "launch.py")], capture_output=True)
        self.assertNotEqual(busy.returncode, 0)


if __name__ == "__main__":
    unittest.main()
