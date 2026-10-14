#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavior checks for the opt-in local backend; no Docker/IDE/license needed."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from argparse import Namespace
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


host = cast(Any, load("qodana_test_host", SCRIPTS / "jb-inspect.py"))
backend = cast(Any, load("qodana_test_backend", SCRIPTS / "qodana_backend.py"))


def sarif(findings):
    return {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "Qodana"}}, "results": findings}]}


def finding(file="src/a.py", line=3, rule="PyUnresolvedReferencesInspection", message="Unresolved reference 'missing'"):
    return {"ruleId": rule, "level": "error", "message": {"text": message},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": file, "uriBaseId": "SRCROOT"},
                                                 "region": {"startLine": line, "startColumn": 2}}}]}


class QodanaTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory[str]
    root: Path
    repo: Path
    cache: Path
    args: Namespace
    seen: list[Path]
    env: Any
    lock_patch: Any

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src/a.py").write_text("print(missing)\n")
        (self.repo / "src/b.py").write_text("print('other')\n")
        profiles = self.repo / ".idea/inspectionProfiles"
        profiles.mkdir(parents=True)
        (profiles / "Project_Default.xml").write_text('<component><profile><option name="myName" value="Test Profile"/>'
                    '<inspection_tool class="PyUnresolvedReferencesInspection" enabled="true"/>'
                    '<inspection_tool class="DuplicatedCode" enabled="true"/></profile></component>')
        self.cache = self.root / "cache"
        self.env = patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": str(self.cache), "JB_INSPECT_OUTCOME_LOG": "0", "JB_INSPECT_UNKNOWN_LOG": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lock_patch = patch.object(backend, "worker_lock_path", return_value=self.root / "worker.lock")
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        state_patch = patch.object(backend, "state_dir", return_value=self.root / "state")
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.args = host.build_parser().parse_args(["agent-inspect", "--backend", "qodana", "--repo", str(self.repo),
                                                    "--scope", "files", "--file", "src/a.py", "--profile", "Test Profile"])
        self.seen = []

    @staticmethod
    def prerequisite(argv, **_kwargs):
        if argv[:3] == ["docker", "context", "inspect"]:
            return b"unix:///local/docker.sock"
        if argv[:3] == ["docker", "image", "inspect"]:
            return b"[]"
        if argv[:2] == ["docker", "ps"]:
            return b""
        return backend.checked_original(argv, **_kwargs)

    def completed(self, _argv, _name, private, _env, _timeout, receipt) -> None:
        self.seen.append(private)
        self.assertEqual((private / "project/src/a.py").read_bytes(), (self.repo / "src/a.py").read_bytes())
        self.assertTrue((private / "project/.git/index").is_file())
        (private / "results/qodana.sarif.json").write_text(json.dumps(sarif([finding(), finding("src/b.py")])) )
        (private / "results/effective.profile.xml").write_text('<profile><inspection_tool class="PyUnresolvedReferencesInspection" enabled="true"/></profile>')
        receipt["container_removed"] = True

    def run_backend(self, execute: Callable[..., Any] | None = None):
        backend.checked_original = backend.checked
        with patch.object(backend, "checked", side_effect=self.prerequisite), \
                patch.object(backend.os, "getloadavg", return_value=(1, 1, 1)), \
                patch.object(backend, "execute", side_effect=execute or self.completed):
            return backend.run(self.args, host)

    def test_completed_red_has_only_requested_current_findings_and_full_artifact(self):
        result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "RED")
        self.assertEqual(result["helper_exit_code"], host.classify_run_exit({"verdict": result["agent_result"]["verdict"]}))
        self.assertEqual(result["findings"][0]["file"], "src/a.py")
        self.assertEqual(result["finding_count"], 1)
        artifact = json.loads(Path(result["findings_artifact"]["path"]).read_text())
        self.assertEqual(artifact["problems"], result["findings"])
        self.assertIn("effective_profile_missing:DuplicatedCode", result["proof_failures"])
        self.assertTrue(all(not p.exists() for p in self.seen))

    def test_zero_findings_with_measured_profile_gap_never_green(self):
        def empty(*args):
            self.completed(*args)
            (args[2] / "results/qodana.sarif.json").write_text(json.dumps(sarif([])))
        result = self.run_backend(empty)
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("effective_profile_missing:DuplicatedCode", result["proof_failures"])
        self.assertFalse(result["agent_result"]["retry_policy"]["retry"])

    def test_odoo_generic_xml_or_loaded_profile_does_not_prove_named_coverage(self):
        results = self.root / "results"
        results.mkdir()
        gaps = backend.coverage_gaps("python", {"addon/views.xml"}, {"OdooPy", "OdooXml"}, results)
        self.assertIn("OdooXml_execution_unproven", gaps)
        self.assertIn("odoo_model_roots_interpreter_unproven", gaps)
        self.assertIn("odoo_effective_default_unproven", gaps)

    def test_license_copies_private_read_only_and_deleted_for_success_failure_and_interrupt(self):
        source = self.root / "plugin_TEST.license"
        source.write_bytes(b"synthetic-private-license-do-not-print")
        before = source.stat()
        self.args.qodana_license_file = [str(source)]
        for error in (None, backend.Blocked("qodana_timeout"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                def attempt(*args):
                    private = args[2]
                    copy = private / "config" / source.name
                    self.assertEqual(copy.stat().st_mode & 0o777, 0o400)
                    self.assertEqual(copy.read_bytes(), source.read_bytes())
                    self.completed(*args)
                    if error:
                        raise error
                result = self.run_backend(attempt)
                self.assertTrue(result["diagnostic"]["qodana"]["profile_deleted"])
                self.assertEqual(result["diagnostic"]["qodana"]["license_copy_count"], 1)
                self.assertNotIn(source.read_text(), json.dumps(result))
                self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertTrue(all(not p.exists() for p in self.seen))
                if error:
                    self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")

    def test_input_change_invalidates_decisive_findings(self):
        def mutate(*args):
            self.completed(*args)
            (self.repo / "src/a.py").write_text("print('fixed')\n")
        result = self.run_backend(mutate)
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_source_inputs_changed")

    def test_cleanup_failure_overrides_decisive_red_and_reports_retained_profile(self):
        with patch.object(backend.shutil, "rmtree", side_effect=PermissionError):
            result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertFalse(result["diagnostic"]["qodana"]["profile_deleted"])

    def test_host_lock_refuses_second_worker_before_license_copy(self):
        import fcntl
        with (self.root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["qodana"]["license_copy_count"], 0)
        self.assertFalse(self.seen)

    def test_killed_helper_license_state_is_recovered_before_another_task(self):
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-killed"
        source = self.root / "plugin_TEST.license"
        source.write_bytes(b"synthetic-license-copy")
        child = '''import importlib.util, os, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("backend", sys.argv[1])
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
b.state_dir = lambda: Path(sys.argv[2])
private = Path(sys.argv[3]); private.mkdir(); (private / "config").mkdir()
receipt = {"run_id": sys.argv[5], "license_copy_count": 0}
b.save_run_record(private, receipt)
b.copy_licenses([sys.argv[4]], private / "config", receipt)
os.kill(os.getpid(), signal.SIGKILL)
'''
        result = subprocess.run([sys.executable, "-c", child, str(SCRIPTS / "qodana_backend.py"),
                                 str(state), str(private), str(source), str(uuid.uuid4())], capture_output=True)
        self.assertLess(result.returncode, 0)
        self.assertTrue((private / "config" / source.name).is_file())
        with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 1, b"", b"No such container")):
            result = self.run_backend()
        self.assertFalse(private.exists())
        self.assertEqual(result["diagnostic"]["qodana"]["recovered_run_count"], 1)
        self.assertTrue(source.is_file())

    def test_orphan_recovery_keeps_container_with_wrong_label_or_missing_marker(self):
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-foreign"
        private.mkdir()
        receipt = {"run_id": str(uuid.uuid4())}
        journal = backend.save_run_record(private, receipt)
        for variant in ("wrong_label", "missing_marker"):
            with self.subTest(variant=variant):
                if variant == "missing_marker":
                    (private / "owner.json").unlink()
                info = [{"Config": {"Labels": {"codex-skills.qodana.run": "other"}},
                         "Mounts": [{"Destination": "/private", "Source": str(private)}]}]
                with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(info).encode(), b"")), \
                        patch.object(backend, "remove_container") as remove:
                    with self.assertRaises(backend.Blocked):
                        backend.recover_runs(backend.safe_env())
                remove.assert_not_called()
                self.assertTrue(private.exists())
                self.assertTrue(journal.exists())

    def test_launch_mounts_licenses_read_only_and_keeps_host_project_outside_container(self):
        def inspect_launch(argv, *rest):
            self.assertNotIn(str(self.repo), " ".join(argv))
            license_mounts = [value for value in argv if value.startswith(str(source) + ":")]
            # The host input is copied, so identify the actual private copy by its basename.
            if not license_mounts:
                license_mounts = [value for value in argv if Path(value.split(":", 1)[0]).name == source.name]
            self.assertTrue(license_mounts)
            self.assertTrue(all(value.rsplit(":", 1)[-1] == "ro" for value in license_mounts))
            receipt = rest[-1]
            self.assertEqual(float(argv[argv.index("--cpus") + 1]), receipt["cpu_limit"])
            self.assertEqual(argv[argv.index("--log-driver") + 1], receipt["docker_logging"])
            self.completed(argv, *rest)
        source = self.root / "plugin_TEST.license"
        source.write_bytes(b"synthetic")
        self.args.qodana_license_file = [str(source)]
        self.run_backend(inspect_launch)

    def test_sarif_failure_missing_and_ambiguous_do_not_report_green_or_red(self):
        for kind in ("missing", "failure", "ambiguous"):
            with self.subTest(kind=kind):
                def invalid(*args):
                    self.completed(*args)
                    path = args[2] / "results/qodana.sarif.json"
                    if kind == "missing":
                        path.unlink()
                    elif kind == "failure":
                        data = json.loads(path.read_text())
                        data["runs"][0]["invocations"] = [{"executionSuccessful": False}]
                        path.write_text(json.dumps(data))
                    else:
                        (path.parent / "other").mkdir()
                        (path.parent / "other/qodana.sarif.json").write_bytes(path.read_bytes())
                result = self.run_backend(invalid)
                self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
                self.assertTrue(result["diagnostic"]["qodana"]["profile_deleted"])

    def test_load_admission_prevents_copy_and_execution(self):
        with patch.object(backend.os, "getloadavg", return_value=(backend.LOAD_LIMIT + 1, 1, 1)), patch.object(backend, "execute") as execute:
            result = backend.run(self.args, host)
        execute.assert_not_called()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["qodana"]["license_copy_count"], 0)

    def test_mutable_or_paid_image_and_account_stores_are_refused(self):
        self.args.qodana_image = "jetbrains/qodana-js:latest"
        result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertFalse(self.seen)
        with self.assertRaises(backend.Blocked):
            backend.copy_licenses([str(self.root / "account.json")], self.root, {"license_copy_count": 0})

    def test_default_api_dispatch_does_not_load_qodana(self):
        output = io.StringIO()
        with patch.object(host.sys, "argv", [str(SCRIPTS / "jb-inspect.py"), "agent-inspect", "--repo", str(self.repo)]), \
                patch.object(host, "build_context", return_value={}), patch.object(host, "command_run", return_value={}) as api, \
                patch.object(host, "emit_agent_result", return_value=0), redirect_stdout(output):
            self.assertEqual(host.main(), 0)
        api.assert_called_once()

    def test_explicit_qodana_dispatch_never_enters_desktop_lifecycle(self):
        output = io.StringIO()
        with patch.object(host.sys, "argv", [str(SCRIPTS / "jb-inspect.py"), "agent-inspect", "--backend", "qodana", "--repo", str(self.repo)]), \
                patch.object(host, "build_context") as context, patch.object(host, "command_run") as api, \
                patch.object(host, "command_qodana", side_effect=lambda args: backend.run(args, host)), \
                patch.object(os, "getloadavg", return_value=(backend.LOAD_LIMIT + 1, 1, 1)), redirect_stdout(output):
            self.assertEqual(host.main(), 0)
        context.assert_not_called()
        api.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["agent_result"]["verdict"], "UNKNOWN")

    def test_trial_guard_allows_availability_and_stops_issuance(self):
        for line in ("Checking trial availability for product [QDL]", "Trial dry-run: request was declined", "Trial dry-run: request failed"):
            self.assertFalse(backend.boundary_event(line))
        for line in ("Starting trial", "Trial activated", "Obtained trial license", "Issuing trial license", "Purchase a license"):
            self.assertTrue(backend.boundary_event(line))
        monitor = backend.Monitor(self.root)
        monitor.observe(b"Starting tr")
        monitor.observe(b"ial")
        self.assertTrue(monitor.boundary)
        dry_run = backend.Monitor(self.root)
        dry_run.observe(b"Trial dry-run: request was declined. Error code = EXISTING_LICENSE_IS_EXPIRED\n")
        self.assertFalse(dry_run.license_refused)

    def test_trial_stop_records_hold_and_deletes_license_profile(self):
        def stopped(*args):
            self.completed(*args)
            raise backend.Blocked("qodana_trial_or_purchase_stop")
        result = self.run_backend(stopped)
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(result["diagnostic"]["qodana"]["profile_deleted"])
        self.assertTrue((self.root / "state/boundary-stop.json").is_file())
        # Selecting a new per-task cache never lifts the same user's stop.
        with patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": str(self.root / "another-cache")}):
            again = self.run_backend()
        self.assertEqual(again["diagnostic"]["error_reason"], "qodana_stop_recorded")

    def test_live_process_guard_discards_output_and_terminates_worker(self):
        calls = []
        code = "import time; print('Trial activated', flush=True); time.sleep(30)"
        with patch.object(backend, "command", side_effect=lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b"")):
            with self.assertRaisesRegex(backend.Blocked, "qodana_trial_or_purchase_stop"):
                backend.execute([sys.executable, "-u", "-c", code], "own-container", self.root, backend.safe_env(), 3, {})
        self.assertTrue(any(call[:3] == ["docker", "rm", "-f"] for call in calls))

    def test_child_environment_does_not_forward_licensing_or_context_overrides(self):
        with patch.dict(os.environ, {"QODANA_TOKEN": "synthetic", "QODANA_LICENSE": "synthetic", "DOCKER_HOST": "foreign", "PYCHARM_VM_OPTIONS": "foreign"}):
            env = backend.safe_env()
        self.assertFalse(set(env) & {"QODANA_TOKEN", "QODANA_LICENSE", "DOCKER_HOST", "PYCHARM_VM_OPTIONS"})


if __name__ == "__main__":
    unittest.main()
