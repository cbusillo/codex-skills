#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Native routing, proof and exclusive-profile tests without a licensed IDE."""
import importlib.util
import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from contextlib import redirect_stdout

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Fixture helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


host: Any = load("native_test_host", SCRIPTS / "jb-inspect.py")
native: Any = load("native_test_backend", SCRIPTS / "native_backend.py")


class NativeTests(unittest.TestCase):
    def test_native_is_default_for_odoo_xml_without_desktop_or_docker(self):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", "/fixture"])
        with patch.object(native.sys, "platform", "darwin"), \
                patch.object(native.common, "scope_selection", return_value=(Path("/fixture"), {}, "files", {"views/form.xml"})), \
                patch.object(native, "is_odoo", return_value=True):
            backend, _ = native.select_backend(args, host)
        self.assertEqual(backend, "native")

    def test_full_python_coverage_uses_native(self):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", "/fixture"])
        with patch.object(native.sys, "platform", "darwin"), \
                patch.object(native.common, "scope_selection", return_value=(Path("/fixture"), {}, "files", {"src/a.py"})), \
                patch.object(native, "is_odoo", return_value=False):
            self.assertEqual(native.select_backend(args, host)[0], "native")

    def test_explicit_community_options_and_host_choice_still_work(self):
        args = host.build_parser().parse_args(["agent-inspect", "--qodana-recover-only"])
        self.assertEqual(native.select_backend(args, host)[0], "qodana")
        args.qodana_recover_only = False
        self.assertEqual(native.select_backend(args, host, "api"), ("api", "host_default"))

    def test_cli_dispatch_does_not_enter_desktop_lifecycle(self):
        with patch.object(host, "command_native", return_value={"backend": "native"}) as run, \
                patch.object(host, "build_context", side_effect=AssertionError("desktop opened")), \
                patch.object(sys, "argv", ["jb-inspect", "agent-inspect", "--backend", "native"]):
            self.assertEqual(host.main(), 0)
        run.assert_called_once()

    def test_backend_option_conflicts_never_open_a_desktop_or_start_either_worker(self):
        cases = (["--backend", "api", "--native-recover-only"],
                 ["--backend", "native", "--qodana-recover-only"],
                 ["--native-recover-only", "--qodana-recover-only"])
        for options in cases:
            with self.subTest(options=options), \
                    patch.object(sys, "argv", ["jb-inspect", "agent-inspect", *options]), \
                    patch.object(host, "build_context", side_effect=AssertionError("desktop opened")), \
                    patch.object(host, "command_native", side_effect=AssertionError("worker started")), \
                    patch.object(host, "command_qodana", side_effect=AssertionError("container started")), \
                    redirect_stdout(io.StringIO()) as output:
                host.main()
                self.assertEqual(json.loads(output.getvalue())["agent_result"]["verdict"], "UNKNOWN")

    def test_enabled_or_registered_without_execution_never_proves_coverage(self):
        expected = {"OdooPy", "OdooXml", "DuplicatedCode", "HttpUrlsUsage"}
        coverage = native.coverage(expected, expected, {"OdooPy", "HttpUrlsUsage"})
        self.assertEqual(set(coverage["unproven"]), {"OdooXml", "DuplicatedCode"})
        self.assertFalse(coverage["complete"])
        self.assertTrue(native.coverage(expected, expected, expected)["complete"])

    def test_profile_lock_excludes_a_second_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = Path(temporary)
            with native.profile_lock(worker):
                child = subprocess.run([sys.executable, "-c",
                    "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                    str(worker / "worker.lock")], capture_output=True)
                self.assertNotEqual(child.returncode, 0)
            with native.profile_lock(worker):
                pass

    def test_existing_profile_lock_and_symlink_lock_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            worker = Path(temporary)
            with native.profile_lock(worker):
                with self.assertRaises(native.Blocked):
                    with native.profile_lock(worker):
                        self.fail("concurrent run admitted")
            (worker / "worker.lock").unlink()
            target = worker / "unrelated"
            target.write_text("preserve")
            (worker / "worker.lock").symlink_to(target)
            with self.assertRaises(native.Blocked):
                with native.profile_lock(worker):
                    self.fail("symlink admitted")
            self.assertEqual(target.read_text(), "preserve")

    def test_license_prompt_and_trial_availability_stop_without_returning_contents(self):
        for line in ("No valid license found", "Checking trial availability for product [PY]", "Start trial", "Activate your license"):
            self.assertTrue(native.license_boundary(line), line)
        self.assertFalse(native.license_boundary("Loaded custom plugins: Odoo IDE"))


class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        state_patch = patch.object(native, "state_dir", return_value=self.root / "state")
        state_patch.start()
        self.addCleanup(state_patch.stop)
        storage_patch = patch.object(native.common, "storage_identity", side_effect=lambda anchor: {"fixture": str(anchor)})
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        (self.repo / ".gitignore").write_text(".venv/\n")
        (self.repo / "source.py").write_text("print(missing)\n")
        profile = self.repo / ".idea/inspectionProfiles/Test.xml"
        profile.parent.mkdir(parents=True)
        profile.write_text('<profile><option name="myName" value="Test"/>'
                           '<inspection_tool class="PyUnresolvedReferencesInspection" enabled="true"/></profile>')
        (self.repo / ".venv/bin").mkdir(parents=True)
        (self.repo / ".venv/bin/python").symlink_to(sys.executable)
        self.worker = self.root / "worker"
        for name in ("config/options", "plugins", "home"):
            (self.worker / name).mkdir(parents=True)
        (self.worker / "idea.properties").write_text(
            f"idea.config.path={self.worker}/config\nidea.plugins.path={self.worker}/plugins\n")
        self.app = self.root / "Fake.app"
        (self.app / "Contents/bin").mkdir(parents=True)
        (self.app / "Contents/Resources").mkdir()
        (self.app / "Contents/Resources/product-info.json").write_text(json.dumps({"productCode": "PY", "envVarBaseName": "PYCHARM"}))
        (self.app / "Contents/bin/pycharm.vmoptions").write_text("")
        self.script = self.app / "Contents/bin/inspect.sh"
        self.script.write_text(f"#!{sys.executable}\ntools={list(native.NAMED_PYTHON)!r}\n" + '''import pathlib,sys
out=pathlib.Path(sys.argv[3])
print('Running local inspections on '+str(pathlib.Path(sys.argv[1])/'source.py'))
(out/'.descriptions.xml').write_text('<inspections>'+''.join(f'<inspection shortName="{tool}"/>' for tool in tools)+'</inspections>')
for tool in tools:
 (out/(tool+'.xml')).write_text(f'<problems><problem><file>file://$PROJECT_DIR$/source.py</file><line>1</line><problem_class id="{tool}" severity="ERROR"/><description>Planted finding</description></problem></problems>')
print('Done.')
''')
        self.script.chmod(0o700)
        self.args = host.build_parser().parse_args(["agent-inspect", "--backend", "native", "--repo", str(self.repo),
                    "--scope", "files", "--file", "source.py", "--profile", "Test", "--native-worker", str(self.worker), "--ide-app", str(self.app)])

    def assess(self, loads=None):
        with patch.object(native.sys, "platform", "darwin"), \
                patch.object(host, "cache_dir", return_value=self.root / "cache"), \
                patch.object(host, "read_global_config", return_value={}), \
                patch.object(host, "trusted_auto_open_roots", return_value=[str(self.repo)]), \
                patch.object(native.os, "getloadavg", side_effect=loads or (lambda: (1, 1, 1))):
            return native._run(self.args, host, None)

    def test_findings_require_current_reports_and_cleanup_sdk_in_place(self):
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "RED")
        self.assertEqual(result["findings"][0]["file"], "source.py")
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertFalse((self.worker / "config/options/jdk.table.xml").exists())
        self.assertFalse(list((self.root / "cache/native").glob("private-*")))
        self.assertEqual((self.repo / "source.py").read_text(), "print(missing)\n")

    def test_another_ide_distribution_is_refused_before_launch(self):
        (self.app / "Contents/Resources/product-info.json").write_text(json.dumps({"productCode": "WS", "envVarBaseName": "WEBSTORM"}))
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_pycharm_distribution_required")
        self.assertNotIn("worker_pid", result["diagnostic"]["native"])

    def test_cleanup_only_does_not_need_an_installed_ide(self):
        shutil.rmtree(self.app)
        self.args.native_recover_only = True
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_recovery_only_no_inspection")
        self.assertEqual(result["cleanup"]["status"], "closed")

    def test_oversized_file_cannot_borrow_its_controls_execution_proof(self):
        (self.repo / "source.py").write_bytes(b"#" * (native.common.MAX_ANALYZED_FILE_KB * 1024 + 1))
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_selected_file_over_analysis_limit")
        self.assertNotIn("worker_pid", result["diagnostic"]["native"])

    def test_scoped_disabled_tools_do_not_borrow_unrestricted_controls_proof(self):
        profile = self.repo / ".idea/inspectionProfiles/Test.xml"
        profile.write_text(profile.read_text().replace('enabled="true"/>', 'enabled="true"><scope name="Generated" enabled="false"/></inspection_tool>'))
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(any(gap.startswith("scoped_profile_execution_unproven:") for gap in result["proof_failures"]))

    def test_registered_tool_without_execution_is_unknown_with_findings_retained(self):
        profile = self.repo / ".idea/inspectionProfiles/Test.xml"
        profile.write_text(profile.read_text().replace('</profile>', '<inspection_tool class="OdooXml" enabled="true"/></profile>'))
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(result["findings"])
        self.assertIn("OdooXml", result["coverage"]["unproven"])

    def test_clean_scope_can_use_private_execution_controls_without_reporting_them(self):
        original = self.script.read_text()
        original = original.replace("for tool in tools:",
            "control=next(pathlib.Path(sys.argv[1]).glob('jb_native_controls_*'))/'coverage.py'\nfor tool in tools:")
        original = original.replace("file://$PROJECT_DIR$/source.py</file>", "{control.as_uri()}</file>")
        self.script.write_text(original)
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "GREEN")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["finding_count"], 0)
        self.assertFalse(result["findings"])

    def test_license_prompt_stops_worker_removes_runtime_and_persists_stop(self):
        self.script.write_text(f"#!{sys.executable}\nimport time\nprint('No valid license found',flush=True)\ntime.sleep(30)\n")
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_owner_activation_required")
        self.assertEqual(result["cleanup"]["status"], "closed")
        stop = json.loads((self.worker / "native-activation-stop.json").read_text())
        self.assertEqual(stop["boundary_event_count"], 1)
        self.assertNotIn("No valid license", json.dumps(stop))
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_activation_stop_recorded")

    def test_partial_license_prompt_without_newline_is_stopped(self):
        self.script.write_text(f"#!{sys.executable}\nimport sys,time\nsys.stdout.write('Start trial');sys.stdout.flush()\ntime.sleep(30)\n")
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_owner_activation_required")

    def test_timeout_kills_the_group_and_removes_sdk_and_private_files(self):
        self.script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        self.args.timeout_ms = 100
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_timeout")
        self.assertEqual(result["cleanup"]["status"], "closed")
        with self.assertRaises(ProcessLookupError):
            os.kill(result["diagnostic"]["native"]["worker_pid"], 0)

    def test_load_pause_resumes_and_exits_under_the_same_deadline(self):
        original = self.script.read_text()
        self.script.write_text(original.replace("out=pathlib.Path", "import time;time.sleep(0.4)\nout=pathlib.Path"))
        loads = iter([(1, 1, 1), (native.LOAD_LIMIT + 1, 1, 1)])
        result = self.assess(lambda: next(loads, (1, 1, 1)))
        self.assertEqual(result["diagnostic"]["native"]["load_pause_count"], 1)
        self.assertEqual(result["agent_result"]["verdict"], "RED")

    def test_parent_eof_stops_its_owned_worker(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        private = self.root / "private"
        (private / "logs").mkdir(parents=True)
        receipt = {"load_pause_count": 0}
        try:
            with native.profile_lock(self.worker) as lock:
                with self.assertRaisesRegex(native.Blocked, "native_parent_exited"):
                    native.execute([sys.executable, "-c", "import time;time.sleep(30)"], {}, private,
                                   time.monotonic() + 2, read_fd, receipt, lock_fd=lock.fileno())
            self.assertTrue(receipt["worker_exited"])
        finally:
            os.close(read_fd)

    def test_recovery_removes_only_the_journaled_sdk_and_runtime(self):
        private = self.root / "cache/native/private-interrupted"
        private.mkdir(parents=True)
        run_id = str(native.uuid.uuid4())
        sdk_name = "jb-native-" + run_id
        interpreter = self.repo / ".venv/bin/python"
        record = {"run_id": run_id, "private": str(private), "sdk_name": sdk_name,
                  "interpreter": str(interpreter), "table_existed": False,
                  "storage_anchor": str(self.root), "storage_identity": {"fixture": str(self.root)}}
        (private / "owner.json").write_text(json.dumps(record))
        (self.worker / "native-run.json").write_text(json.dumps(record))
        with native.registered_sdk(self.worker / "config", interpreter, "fixture", sdk_name):
            # Simulate a hard-killed guardian before its normal context cleanup.
            table = self.worker / "config/options/jdk.table.xml"
            saved_sdk = table.read_text()
        table.write_text(saved_sdk)
        unrelated = self.root / "keep"
        unrelated.write_text("preserved")
        native.recover_run(self.worker)
        self.assertFalse(private.exists())
        self.assertFalse(table.exists())
        self.assertEqual(unrelated.read_text(), "preserved")

    def test_credential_named_source_is_refused_without_opening_it(self):
        credential = self.repo / "do-not-read.license"
        credential.write_text("fixture content must not be read")
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path == credential:
                self.fail("credential file opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_credential_input_refused")


@unittest.skipUnless(os.environ.get("JB_NATIVE_LIVE") == "1", "independently activated native worker required")
class LiveNativeTests(unittest.TestCase):
    def test_real_odoo_defects_and_corrected_controls(self):
        cache = host.cache_dir() / "native-qualification"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fixture-", dir=cache) as temporary:
            repo = Path(temporary) / "project"
            fixture = Path(__file__).parent / "fixtures/native-odoo"
            shutil.copytree(fixture, repo)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
            args = host.build_parser().parse_args(["agent-inspect", "--backend", "native", "--repo", str(repo),
                       "--scope", "directory", "--dir", "native_probe", "--timeout-ms", "300000"])
            result = native.run(args, host)
            if os.environ.get("JB_NATIVE_LIVE_OUT"):
                Path(os.environ["JB_NATIVE_LIVE_OUT"]).write_text(json.dumps(result, indent=2))
            self.assertEqual(result["agent_result"]["verdict"], "RED", result["agent_result"]["agent_report"])
            self.assertTrue(result["coverage"]["complete"])
            self.assertEqual(result["cleanup"]["status"], "closed")
            findings = json.loads(Path(result["findings_artifact"]).read_text())
            tree = ast.parse((fixture / "native_probe/models.py").read_text())
            functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
            wrong, correct = functions["wrong_model"], functions["correct_model"]
            self.assertTrue(any(f["rule_id"] == "PyTypeCheckerInspection" and f["file"] == "native_probe/models.py"
                                and wrong.lineno <= f["line"] <= wrong.end_lineno for f in findings))
            self.assertFalse(any(f["file"] == "native_probe/models.py" and correct.lineno <= f["line"] <= correct.end_lineno for f in findings))
            for tool, source in (("OdooXml", "native_probe/views/defect.xml"), ("DuplicatedCode", "native_probe/duplicates.py"), ("OdooPy", "native_probe/models.py")):
                self.assertTrue(any(f["rule_id"] == tool and f["file"] == source for f in findings), tool)
            self.assertFalse(any(f["file"] == "native_probe/views/control.xml" for f in findings))


if __name__ == "__main__":
    unittest.main()
