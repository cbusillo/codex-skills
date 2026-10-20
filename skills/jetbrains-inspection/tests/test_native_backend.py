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
import signal
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


def load(name, path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("Fixture helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


host: Any = load("native_test_host", SCRIPTS / "jb-inspect.py")
native: Any = load("native_test_backend", SCRIPTS / "native_backend.py")


def copy_tracked_fixture(fixture, destination):
    repository = Path(subprocess.check_output(["git", "-C", str(fixture), "rev-parse", "--show-toplevel"], text=True).strip())
    revision = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    prefix = fixture.relative_to(repository).as_posix()
    paths = subprocess.check_output(["git", "-C", str(repository), "ls-tree", "-r", "--name-only", "-z", revision, "--", prefix]).decode().split("\0")
    for path in filter(None, paths):
        relative = (repository / path).relative_to(fixture)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "-C", str(repository), "show", revision + ":" + path]))
    return revision


class NativeTests(unittest.TestCase):
    def test_process_reader_reports_foreign_group_and_command_before_matching(self):
        result = subprocess.CompletedProcess([], 0, b"123456 42 Tue Oct 20 12:00:00 2026 S unrelated-process\n")
        with patch.object(native.common, "command", return_value=result):
            identity = native.process_identity(123456, Path("/private/owned"))
        self.assertEqual(identity["pgid"], 42)
        self.assertIsNone(identity["private"])

    def test_framework_dependency_must_match_the_workspaces_declared_git_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repo, framework, shared = root / "repo", root / "core", root / "shared"
            for path in (repo, framework, shared):
                path.mkdir()
                subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(path)], check=True)
            (framework / "odoo").mkdir()
            (framework / "odoo/release.py").write_text("version_info = (19, 0, 0, FINAL)\n")
            value = shared / "addon.py"
            revisions = []
            for text in ("pass\n", "changed = True\n"):
                value.write_text(text)
                subprocess.run(["git", "-C", str(shared), "add", "addon.py"], check=True)
                subprocess.run(["git", "-C", str(shared), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Fixture"], check=True)
                revisions.append(subprocess.check_output(["git", "-C", str(shared), "rev-parse", "HEAD"], text=True).strip())
            workspace = repo / "workspace.toml"
            declaration = '[build]\nodoo_version = "19.0"\n[repos.shared_addons]\npath = "../shared"\nref = "{}"\n'
            workspace.write_text(declaration.format(revisions[0]))
            with self.assertRaisesRegex(native.Blocked, "native_odoo_dependency_revision_mismatch"):
                native.framework_context(repo, {"model.py"}, [framework, shared])
            workspace.write_text(declaration.format("main"))
            context = native.framework_context(repo, {"model.py"}, [framework, shared])
            self.assertEqual(context["dependency_revisions"]["shared_addons"]["resolved_commit"], revisions[-1])
            workspace.write_text(declaration.format("missing-ref"))
            with self.assertRaisesRegex(native.Blocked, "native_odoo_dependency_unproven"):
                native.framework_context(repo, {"model.py"}, [framework, shared])
            shutil.rmtree(shared / ".git")
            with self.assertRaisesRegex(native.Blocked, "native_odoo_dependency_unproven"):
                native.framework_context(repo, {"model.py"}, [framework, shared])

    def test_framework_series_must_match_the_workspace_and_selected_addon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, framework = root / "repo", root / "core"
            (repo / "addons/probe").mkdir(parents=True)
            (framework / "odoo").mkdir(parents=True)
            (framework / "odoo/release.py").write_text("version_info = (19, 0, 0, FINAL)\n")
            workspace = repo / "workspace.toml"
            manifest = repo / "addons/probe/__manifest__.py"
            workspace.write_text('[build]\nodoo_version = "18.0"\n')
            manifest.write_text('{"version":"19.0.1.0.0"}\n')
            with self.assertRaisesRegex(native.Blocked, "native_odoo_version_mismatch"):
                native.framework_context(repo, {"addons/probe/model.py"}, [framework])
            workspace.write_text('[build]\nodoo_version = "19.0"\n')
            manifest.write_text('{"version":"18.0.1.0.0"}\n')
            with self.assertRaisesRegex(native.Blocked, "native_odoo_version_mismatch"):
                native.framework_context(repo, {"addons/probe/model.py"}, [framework])
            manifest.write_text('{"version":"19.0.1.0.0"}\n')
            proof = native.framework_context(repo, {"addons/probe/model.py"}, [framework])
            self.assertEqual(set(proof["declared_versions"].values()), {proof["framework_series"], proof["framework_series"] + ".1.0.0"})

    def test_fixture_copy_uses_tracked_inputs_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture = root / "fixture"
            fixture.mkdir()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (fixture / "control.py").write_text("pass\n")
            subprocess.run(["git", "-C", str(root), "add", "fixture/control.py"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Fixture"], check=True)
            (fixture / "control.py").write_text("changed locally\n")
            (fixture / "unrelated.py").write_text("must not enter qualification\n")
            output = root / "output"
            copy_tracked_fixture(fixture, output)
            self.assertEqual({p.name for p in output.iterdir()}, {"control.py"})
            self.assertEqual((output / "control.py").read_text(), "pass\n")

    def test_native_is_default_for_odoo_xml_without_desktop_or_docker(self):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", "/fixture"])
        with patch.object(sys, "platform", "darwin"), \
                patch.object(native.common, "scope_selection", return_value=(Path("/fixture"), {}, "files", {"views/form.xml"})), \
                patch.object(native, "eligible", return_value=True), \
                patch.object(native, "is_odoo", return_value=True):
            backend, _ = native.select_backend(args, host)
        self.assertEqual(backend, "native")

    def test_full_python_coverage_uses_native(self):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", "/fixture"])
        with patch.object(sys, "platform", "darwin"), \
                patch.object(native.common, "scope_selection", return_value=(Path("/fixture"), {}, "files", {"src/a.py"})), \
                patch.object(native, "eligible", return_value=True), \
                patch.object(native, "is_odoo", return_value=False):
            self.assertEqual(native.select_backend(args, host)[0], "native")

    def test_odoo_other_languages_and_nested_lanes_keep_their_existing_route(self):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", "/fixture"])
        for selected, lane_gaps in (({"assets/app.js"}, []), ({"nested/model.py"}, ["nested_lane"])):
            with self.subTest(selected=selected), patch.object(sys, "platform", "darwin"), \
                    patch.object(native.common, "scope_selection", return_value=(Path("/fixture"), {}, "files", selected)), \
                    patch.object(native, "eligible", return_value=True), \
                    patch.object(native, "is_odoo", return_value=True), \
                    patch.object(native.common, "lane_gaps", return_value=lane_gaps), \
                    patch.object(native.common, "select_backend", return_value=("api", "existing_lane")):
                self.assertEqual(native.select_backend(args, host), ("api", "existing_lane"))

    def test_explicit_community_options_and_host_choice_still_work(self):
        args = host.build_parser().parse_args(["agent-inspect", "--qodana-recover-only"])
        self.assertEqual(native.select_backend(args, host)[0], "qodana")
        args.qodana_recover_only = False
        self.assertEqual(native.select_backend(args, host, "api"), ("api", "host_default"))

    def test_unconfigured_native_preserves_the_existing_route(self):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", "/fixture"])
        with patch.object(sys, "platform", "darwin"), \
                patch.object(native.common, "scope_selection", return_value=(Path("/fixture"), {}, "files", {"src/a.py"})), \
                patch.object(host, "read_global_config", return_value={}), \
                patch.object(native, "is_odoo", return_value=False), \
                patch.object(native.common, "select_backend", return_value=("qodana", "qualified")):
            backend, reason = native.select_backend(args, host)
            self.assertEqual(backend, "qodana")
            self.assertIn("native_worker_not_configured", reason)

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
        for line in ("No valid license found", "Checking trial availability for product [PY]", "Start trial", "Activate your license", "Your trial has expired", "Trial is active", "You are using a trial license"):
            self.assertTrue(native.license_boundary(line), line)
        self.assertFalse(native.license_boundary("Loaded custom plugins: Odoo IDE"))

    def test_source_names_are_not_license_prompts(self):
        for line in ("Running local inspections on /project/trial.py", "Inspecting trial-balance.xml", "building package license-required-helper"):
            self.assertFalse(native.license_boundary(line), line)


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
        self.script.write_text(f"#!{sys.executable}\ntools={list(native.NAMED_PYTHON)!r}\ncanary_files={native.proof.CANARY_FILES!r}\n" + '''import pathlib,sys,json
report_user_findings=True
out=pathlib.Path(sys.argv[3])
project=pathlib.Path(sys.argv[1])
control=next(project.glob('jb_native_controls_*'))
spans=[{'operationName':'inspectionRun','duration':1,'tags':[{'key':'inspectionId','value':tool},{'key':'file','value':str(project/'source.py')}]} for tool in tools]
(out.parent/'inspection-trace.json').write_text(json.dumps({'data':[{'spans':spans}]}))
print('Running local inspections on '+str(project/'source.py'))
(out/'.descriptions.xml').write_text('<inspections>'+''.join(f'<inspection shortName="{tool}"/>' for tool in tools)+'</inspections>')
for tool in tools:
 paths=[control/canary_files[tool]]
 if report_user_findings: paths.append(project/'source.py')
 problems=''.join(f'<problem><file>{path.as_uri()}</file><line>1</line><problem_class id="{tool}" severity="ERROR"/><description>Planted finding</description></problem>' for path in paths)
 (out/(tool+'.xml')).write_text('<problems>'+problems+'</problems>')
print('Done.')
''')
        self.script.chmod(0o700)
        self.args = host.build_parser().parse_args(["agent-inspect", "--backend", "native", "--repo", str(self.repo),
                    "--scope", "files", "--file", "source.py", "--profile", "Test", "--native-worker", str(self.worker), "--ide-app", str(self.app)])

    def assess(self, loads=None, *, direct=False):
        with patch.object(sys, "platform", "darwin"), \
                patch.object(host, "cache_dir", return_value=self.root / "cache"), \
                patch.object(host, "read_global_config", return_value={}), \
                patch.object(host, "trusted_auto_open_roots", return_value=[str(self.repo)]), \
                patch.object(os, "getloadavg", side_effect=loads or (lambda: (1, 1, 1))):
            return native._run(self.args, host, None) if direct else native.run(self.args, host)

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

    def test_nonroot_preparation_keeps_auto_route_and_explicit_native_uses_ready_venv(self):
        for prepare in ("bash scripts/prepare.sh", {"python": {"version": "3.12", "moduleName": "nested", "projectPath": "nested"}}):
            with self.subTest(prepare=prepare):
                directory = self.repo / ".github"
                directory.mkdir(exist_ok=True)
                (directory / "github.json").write_text(json.dumps({"qualityGate": {"inspection": {"prepare": prepare}}}))
                self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_completed")
                auto = host.build_parser().parse_args(["agent-inspect", "--repo", str(self.repo), "--scope", "files", "--file", "source.py", "--profile", "Test"])
                config = {"jetbrains": {"nativeWorker": {"path": str(self.worker), "app": str(self.app)}}}
                with patch.object(sys, "platform", "darwin"), patch.object(host, "read_global_config", return_value=config):
                    backend, reason = native.select_backend(auto, host)
                self.assertEqual(backend, "api")
                self.assertIn("native_preparation_shape_unsupported", reason)

    def test_cleanup_only_does_not_need_an_installed_ide(self):
        shutil.rmtree(self.app)
        self.args.native_recover_only = True
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_recovery_only_no_inspection")
        self.assertEqual(result["cleanup"]["status"], "closed")

    def test_lane_scope_limit_keeps_its_specific_reason(self):
        error = host.InspectError("fixture scope exceeds limit", payload={"error_reason": "inspection_lane_scope_too_large"})
        with patch.object(native.common, "scope_selection", side_effect=error):
            result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_scope_too_large")
        self.assertNotIn("worker_pid", result["diagnostic"]["native"])

    def test_oversized_file_cannot_borrow_its_controls_execution_proof(self):
        (self.repo / "source.py").write_bytes(b"#" * (native.common.MAX_ANALYZED_FILE_KB * 1024 + 1))
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_selected_file_over_analysis_limit")
        self.assertNotIn("worker_pid", result["diagnostic"]["native"])

    def test_control_findings_and_traversal_do_not_replace_selected_tool_spans(self):
        self.script.write_text(self.script.read_text().replace("report_user_findings=True", "report_user_findings=False")
                               .replace("'value':str(project/'source.py')", "'value':'/synthetic/source.py'"))
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(result["coverage"]["missing_execution"])
        self.assertTrue(all(value["detected"] for value in result["coverage"]["canary_results"].values()))

    def test_registered_tool_without_execution_is_unknown_with_findings_retained(self):
        profile = self.repo / ".idea/inspectionProfiles/Test.xml"
        profile.write_text(profile.read_text().replace('</profile>', '<inspection_tool class="PyCompatibilityInspection" enabled="true"/></profile>'))
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(result["findings"])
        self.assertIn("PyCompatibilityInspection", result["coverage"]["unproven"])

    def test_clean_selected_spans_and_canaries_allow_green_without_control_findings(self):
        self.script.write_text(self.script.read_text().replace("report_user_findings=True", "report_user_findings=False"))
        result = self.assess()
        self.assertEqual(result["agent_result"]["verdict"], "GREEN")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["finding_count"], 0)

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

    def test_error_before_log_prompt_still_persists_the_owner_stop(self):
        self.script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\nPath('logs/idea.log').write_text('No valid license found\\n')\nprint('ERROR: plugin startup failed',flush=True)\ntime.sleep(30)\n")
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_owner_activation_required")
        self.assertTrue((self.worker / "native-activation-stop.json").exists())
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_activation_stop_recorded")

    def test_incomplete_license_observation_cannot_clear_the_outcome_hold(self):
        outside = self.root / "unread.txt"
        outside.write_text("unread fixture")
        self.script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport time\nPath('logs/unsafe.log').symlink_to({str(outside)!r})\nprint('Done.',flush=True)\ntime.sleep(30)\n")
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_license_observation_unproven")
        self.assertFalse(result["diagnostic"]["native"]["terminal_outcome_known"])
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertTrue((self.worker / "native-run.json").exists())
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_interrupted_run_unresolved")

    def test_cancelled_inspection_cannot_borrow_completed_spans_or_done(self):
        self.script.write_text(self.script.read_text() + "print('Cancelling inspection progress')\n")
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_execution_cancelled")

    def test_preparation_output_is_not_an_ide_licensing_event(self):
        private = self.root / "private"
        (private / "logs").mkdir(parents=True)
        receipt = {"load_pause_count": 0}
        with native.profile_lock(self.worker) as lock:
            native.execute([sys.executable, "-c", "print('Start trial\\nERROR: build warning\\nNo modules\\nUsing default project profile')"], {}, private, time.monotonic() + 2,
                           None, receipt, lock_fd=lock.fileno(), require_completion=False)
        self.assertNotIn("license_boundary_observed", receipt)

    def test_timeout_kills_the_group_and_removes_sdk_and_private_files(self):
        self.script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        self.args.timeout_ms = 100
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_timeout")
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertTrue(result["diagnostic"]["native"]["worker_exited"])

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

    def reserve_run(self):
        state = self.root / "cache/native"
        state.mkdir(parents=True, exist_ok=True)
        context = native.new_context(host)
        context.reserve(state, self.worker, self.worker / "config", self.repo / ".venv/bin/python")
        return context

    def test_recovery_removes_only_the_journaled_sdk_and_runtime(self):
        table = self.worker / "config/options/jdk.table.xml"
        table.write_text('<application><!--keep--><component name="ProjectJdkTable"><jdk><name value="owner"/></jdk></component></application>')
        context = self.reserve_run()
        interpreter = self.repo / ".venv/bin/python"
        sdk_name = context.identity["sdk_name"]
        with native.registered_sdk(self.worker / "config", interpreter, "fixture", sdk_name,
                                   on_attempt=lambda: context.sdk_attempt(self.worker / "config", interpreter, sdk_name)):
            saved_sdk = table.read_text()
        table.write_text(saved_sdk)
        context.phase("terminal", outcome="native_parent_exited", license_boundary=False)
        native.recover_run(self.worker)
        self.assertFalse(context.private.exists())
        self.assertIn('value="owner"', table.read_text())
        self.assertIn('<!--keep-->', table.read_text())
        self.assertNotIn(sdk_name, table.read_text())

    def test_atomic_sdk_update_preserves_comments_and_unrelated_entries(self):
        table = self.worker / "config/options/jdk.table.xml"
        original = '<application><!--keep--><component name="ProjectJdkTable"><jdk><name value="owner"/></jdk></component></application>'
        table.write_text(original)
        with patch.object(os, "replace", side_effect=OSError("interrupted write")):
            with self.assertRaises(OSError):
                with native.registered_sdk(self.worker / "config", self.repo / ".venv/bin/python", "fixture", "owned"):
                    self.fail("incomplete write admitted")
        self.assertEqual(table.read_text(), original)
        with native.registered_sdk(self.worker / "config", self.repo / ".venv/bin/python", "fixture", "owned"):
            pass
        self.assertIn("<!--keep-->", table.read_text())
        self.assertIn('value="owner"', table.read_text())
        self.assertNotIn('value="owned"', table.read_text())

    def test_snapshot_already_has_a_recoverable_custody_record(self):
        snapshot = native.common.snapshot

        def check_custody(repo, project):
            record = json.loads((self.worker / "native-run.json").read_text())
            self.assertEqual(json.loads((project.parent / "owner.json").read_text()), native.lifecycle.identity_from(record))
            return snapshot(repo, project)

        with patch.object(native.common, "snapshot", side_effect=check_custody):
            result = self.assess()
            self.assertEqual(result["cleanup"]["status"], "closed")
            self.assertEqual(result["agent_result"]["verdict"], "RED")

    def test_recovery_preserves_unproved_state(self):
        context = self.reserve_run()
        private = context.private
        journal = context.journal
        marker = private / "owner.json"
        marker.write_text("{}")
        with self.assertRaisesRegex(native.Blocked, "native_recovery_ownership_unproven"):
            native.recover_run(self.worker)
        self.assertTrue(journal.exists())
        self.assertTrue(private.exists())
        marker.write_text(json.dumps(context.identity))
        with patch.object(native.common, "storage_identity", return_value={"changed": True}):
            with self.assertRaisesRegex(native.Blocked, "native_recovery_storage_changed"):
                native.recover_run(self.worker)
        self.assertTrue(private.exists())
        with native.profile_lock(self.worker):
            self.args.native_recover_only = True
            self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_worker_busy")
        native.recover_run(self.worker)
        self.assertFalse(private.exists())

    def test_recovery_sdk_failure_retains_state_and_cannot_report_closed(self):
        context = self.reserve_run()
        context.sdk_attempt(self.worker / "config", self.repo / ".venv/bin/python", context.identity["sdk_name"])
        table = self.worker / "config/options/jdk.table.xml"
        table.write_text('<application><component name="ProjectJdkTable"><jdk><name value="' + context.identity["sdk_name"] + '"/><homePath value="different"/></jdk></component></application>')
        self.args.native_recover_only = True
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_recovery_failed")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertTrue(context.journal.exists())
        self.assertTrue(context.private.exists())
        self.assertTrue(table.exists())

    def test_recovery_storage_and_deletion_failures_are_not_closed(self):
        context = self.reserve_run()
        self.args.native_recover_only = True
        with patch.object(native.common, "storage_identity", side_effect=OSError("offline storage")):
            result = self.assess()
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertTrue(context.journal.exists())
        with patch.object(native.common, "delete_private", side_effect=OSError("interrupted deletion")):
            result = self.assess()
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertTrue(context.private.exists())

    def test_reserved_empty_directory_and_partial_marker_are_recoverable(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                context = self.reserve_run()
                context.phase("reserved", private_materialized=False)
                marker = context.private / "owner.json"
                if partial:
                    marker.write_text("{incomplete")
                else:
                    marker.unlink()
                native.recover_run(self.worker)
                self.assertFalse(context.private.exists())
                self.assertFalse(context.journal.exists())

    def test_partial_owner_write_is_disposed_without_an_unjournaled_temporary(self):
        def partial_write(path, _identity):
            path.write_text('{"run_id":')
            raise OSError("interrupted marker write")

        with patch.object(native.lifecycle, "write_owner", side_effect=partial_write):
            result = self.assess()
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertFalse((self.worker / "native-run.json").exists())
        self.assertFalse(list((self.root / "cache/native").glob("private-*")))

    def test_reserved_directory_does_not_adopt_an_unknown_temporary_file(self):
        context = self.reserve_run()
        context.phase("reserved", private_materialized=False)
        (context.private / "owner.json").unlink()
        (context.private / ".owner.json-unknown").write_text("partial")
        with self.assertRaisesRegex(native.Blocked, "native_recovery_ownership_unproven"):
            native.recover_run(self.worker)
        self.assertTrue(context.private.exists())
        self.assertTrue(context.journal.exists())

    def test_malformed_preexisting_sdk_is_preserved_without_fictitious_lease(self):
        table = self.worker / "config/options/jdk.table.xml"
        original = "<application incomplete"
        table.write_text(original)
        result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_sdk_table_malformed")
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertEqual(table.read_text(), original)
        self.assertFalse((self.worker / "native-run.json").exists())

    def test_artifact_failure_does_not_skip_sdk_or_private_disposal(self):
        write = native.write_record

        def fail_artifact(path, payload):
            if path.name.endswith(".findings.json"):
                raise OSError("full output storage")
            write(path, payload)

        with patch.object(native, "write_record", side_effect=fail_artifact):
            result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_persistence_failed")
        self.assertEqual(result["cleanup"]["status"], "closed")
        self.assertFalse((self.worker / "config/options/jdk.table.xml").exists())
        self.assertFalse((self.worker / "native-run.json").exists())

    def test_unrecorded_licensing_event_cannot_relaunch_after_recovery(self):
        self.script.write_text(f"#!{sys.executable}\nprint('Your license has expired',flush=True)\n")
        write = native.write_record

        def fail_terminal_writes(path, payload):
            if path.name == "native-activation-stop.json" or path.name == "native-run.json" and payload.get("phase") == "terminal":
                raise OSError("full profile storage")
            write(path, payload)

        with patch.object(native, "write_record", side_effect=fail_terminal_writes):
            result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_owner_activation_required")
        self.assertEqual(result["cleanup"]["status"], "closed")
        journal = self.worker / "native-run.json"
        self.assertEqual(json.loads(journal.read_text())["run_id"], result["identity"]["inspection_run_id"])
        again = self.assess()
        self.assertEqual(again["inspection_outcome"]["reason"], "native_interrupted_run_unresolved")
        self.assertNotIn("worker_pid", again["diagnostic"]["native"])
        saved = self.root / "terminal-result.json"
        saved.write_text(json.dumps(result))
        self.args.native_recover_only = True
        self.args.native_reconcile_result = str(saved)
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_reconciliation_result_unproven")
        self.assertTrue(journal.exists())

    @staticmethod
    def interrupted_terminal_writer():
        write = native.write_record

        def fail_terminal(path, payload):
            if path.name == "native-run.json" and payload.get("phase") == "terminal":
                raise OSError("terminal write interrupted")
            write(path, payload)
        return fail_terminal

    def reconcile_saved_result(self, result):
        saved = self.root / "terminal-result.json"
        saved.write_text(json.dumps(result))
        self.args.native_recover_only = True
        self.args.native_reconcile_result = str(saved)
        reconciled = self.assess()
        self.assertEqual(reconciled["inspection_outcome"]["reason"], "native_recovery_only_no_inspection")
        self.assertFalse((self.worker / "native-run.json").exists())

    def test_saved_nonlicensing_terminal_result_reconciles_a_persistence_failure(self):
        with patch.object(native, "write_record", side_effect=self.interrupted_terminal_writer()):
            result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_persistence_failed")
        self.reconcile_saved_result(result)

    def test_failed_spawn_can_reconcile_its_known_nonlicensing_outcome(self):
        execute = native.execute

        def failed_spawn(*args, **kwargs):
            with patch.object(native.subprocess, "Popen", side_effect=OSError("spawn failed")):
                return execute(*args, **kwargs)

        with patch.object(native, "execute", side_effect=failed_spawn), \
                patch.object(native, "write_record", side_effect=self.interrupted_terminal_writer()):
            result = self.assess()
        self.assertTrue(result["diagnostic"]["native"]["worker_exited"])
        self.assertNotIn("worker_pid", result["diagnostic"]["native"])
        self.reconcile_saved_result(result)

    def test_explicit_confirmed_release_matches_the_stop_and_never_inspects(self):
        self.script.write_text(f"#!{sys.executable}\nprint('Start trial',flush=True)\n")
        result = self.assess()
        stop = self.worker / "native-activation-stop.json"
        self.assertTrue(stop.exists())
        self.args.native_recover_only = True
        self.args.native_release_run = "wrong"
        self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_release_stop_mismatch")
        self.assertTrue(stop.exists())
        self.args.native_release_run = result["identity"]["inspection_run_id"]
        released = self.assess()
        self.assertEqual(released["inspection_outcome"]["reason"], "native_recovery_only_no_inspection")
        self.assertNotIn("worker_pid", released["diagnostic"]["native"])
        self.assertFalse(stop.exists())

    def test_guardian_fallback_preserves_the_prearmed_run_identity(self):
        contexts = []
        create = native.new_context

        def remember(host_module):
            context = create(host_module)
            contexts.append(context)
            return context

        with patch.object(native, "new_context", side_effect=remember), \
                patch.object(native, "execute", side_effect=RuntimeError("unexpected engine wrapper failure")):
            result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_internal_error")
        self.assertEqual(result["diagnostic"]["native"]["terminal_reason"], "native_internal_error")
        self.assertEqual(result["identity"]["inspection_run_id"], contexts[0].receipt["run_id"])
        journal = json.loads((self.worker / "native-run.json").read_text())
        self.assertEqual(journal["run_id"], result["identity"]["inspection_run_id"])
        self.assertFalse(result["diagnostic"]["native"]["terminal_outcome_known"])

    def test_qodana_flock_excludes_native_execution(self):
        import fcntl
        state = self.root / "state"
        state.mkdir()
        with patch.object(native.common, "state_dir", return_value=state):
            with native.common.worker_lock_path().open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.assess()
        self.assertEqual(result["inspection_outcome"]["reason"], "native_worker_busy")
        self.assertNotIn("worker_pid", result["diagnostic"]["native"])

    def test_structured_preparation_reuses_public_host_caches_across_runs(self):
        shared = self.root / "shared-caches"
        shared.mkdir()
        config = {"jetbrains": {"nativeWorker": {"cacheRoot": str(shared)}}}
        with patch.object(host, "read_global_config", return_value=config):
            with patch.object(host, "cache_dir", return_value=self.root / "run-a"), patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": "override"}):
                first, first_env = native.runtime_storage(host, self.worker)
            with patch.object(host, "cache_dir", return_value=self.root / "run-b"), patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": "override"}):
                second, second_env = native.runtime_storage(host, self.worker)
        self.assertNotEqual(first, second)
        self.assertEqual(first_env, second_env)
        self.assertTrue(all(Path(path).is_relative_to(shared) for path in first_env.values()))
        self.assertFalse(any(Path(path).is_relative_to(self.worker) for path in first_env.values()))

    def test_missing_or_changed_host_cache_storage_is_not_recreated(self):
        missing = self.root / "unmounted/cache"
        config: dict[str, Any] = {"jetbrains": {"nativeWorker": {"cacheRoot": str(missing)}}}
        with patch.object(host, "read_global_config", return_value=config):
            with self.assertRaisesRegex(native.Blocked, "native_cache_root_unavailable"):
                native.runtime_storage(host, self.worker)
        self.assertFalse(missing.parent.exists())
        existing = self.root / "existing-cache"
        existing.mkdir()
        config["jetbrains"]["nativeWorker"].update(cacheRoot=str(existing), cacheStorageIdentity={"different": True})
        with patch.object(host, "read_global_config", return_value=config):
            with self.assertRaisesRegex(native.Blocked, "native_cache_storage_changed"):
                native.runtime_storage(host, self.worker)

    def start_guarded_fixture(self):
        started = self.root / "engine-started"
        self.script.write_text(f"#!{sys.executable}\nimport pathlib,time\npathlib.Path({str(started)!r}).touch()\ntime.sleep(30)\n")
        code = '''import json,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from test_native_backend import native,host
root=Path(sys.argv[2])
args=host.build_parser().parse_args(json.loads(sys.argv[3]))
with patch.object(native.sys,'platform','darwin'), patch.object(native,'state_dir',return_value=root/'state'), patch.object(native.common,'storage_identity',side_effect=lambda anchor:{'fixture':str(anchor)}), patch.object(host,'cache_dir',return_value=root/'cache'), patch.object(host,'read_global_config',return_value={}), patch.object(host,'trusted_auto_open_roots',return_value=[str(root/'repo')]), patch.object(native.os,'getloadavg',return_value=(1,1,1)):
 native.run(args,host)
'''
        argv = ["agent-inspect", "--backend", "native", "--repo", str(self.repo), "--scope", "files", "--file", "source.py", "--profile", "Test", "--native-worker", str(self.worker), "--ide-app", str(self.app)]
        process = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).parent), str(self.root), json.dumps(argv)], start_new_session=True)
        deadline = time.monotonic() + 10
        while not started.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(started.exists(), "engine never entered guarded run")
        return process

    def test_guardian_cleans_up_after_invoking_group_is_killed(self):
        process = self.start_guarded_fixture()
        try:
            deadline = time.monotonic() + 10
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            while (self.worker / "native-run.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse((self.worker / "native-run.json").exists())
            self.assertFalse((self.worker / "config/options/jdk.table.xml").exists())
            self.assertFalse(list((self.root / "cache/native").glob("private-*")))
            with native.profile_lock(self.worker):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    def test_killed_guardian_orphan_is_stopped_before_recovery_and_exact_release(self):
        process = self.start_guarded_fixture()
        journal = self.worker / "native-run.json"
        record = json.loads(journal.read_text())
        try:
            os.kill(record["guardian_pid"], signal.SIGKILL)
            process.wait(timeout=5)
            self.args.native_recover_only = True
            result = self.assess()
            self.assertEqual(result["inspection_outcome"]["reason"], "native_interrupted_run_unresolved")
            self.assertEqual(result["diagnostic"]["native"]["orphan_group_stopped"], record["engine_identity"]["pid"])
            self.assertEqual(result["cleanup"]["status"], "closed")
            self.assertFalse(Path(record["private"]).exists())
            self.assertTrue(journal.exists())
            self.args.native_release_run = "wrong-run"
            self.assertEqual(self.assess()["inspection_outcome"]["reason"], "native_release_run_mismatch")
            self.args.native_release_run = record["run_id"]
            released = self.assess()
            self.assertEqual(released["inspection_outcome"]["reason"], "native_recovery_only_no_inspection")
            self.assertFalse(journal.exists())
            self.assertNotIn("worker_pid", released["diagnostic"]["native"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            try:
                os.killpg(record["engine_identity"]["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_failed_engine_identity_persistence_never_releases_the_launch_gate(self):
        marker = self.root / "must-not-start"
        self.script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
        with patch.object(native.lifecycle.RunContext, "engine_spawned", side_effect=OSError("journal storage full")):
            result = self.assess()
        self.assertFalse(marker.exists())
        self.assertEqual(result["cleanup"]["status"], "closed")

    def test_foreign_launch_identity_never_releases_the_engine_gate(self):
        marker = self.root / "must-not-start"
        self.script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n")
        with patch.object(native, "process_identity", return_value={"pgid": 42, "private": None}):
            result = self.assess()
        self.assertFalse(marker.exists())
        self.assertEqual(result["inspection_outcome"]["reason"], "native_engine_identity_unproven")
        self.assertEqual(result["cleanup"]["status"], "closed")

    def test_reused_pid_recovers_resources_but_keeps_hold_until_exact_release(self):
        for pgid, private in ((123456, None), (123456, "same"), (42, None)):
            with self.subTest(pgid=pgid, private=private):
                context = self.reserve_run()
                identity = {"pid": 123456, "pgid": 123456, "started": "old", "birth_clock": native.PROCESS_BIRTH_CLOCK, "private": str(context.private)}
                context.phase("engine_running", engine_identity=identity)
                self.args.native_recover_only = True
                current = {**identity, "started": "later", "pgid": pgid, "private": str(context.private) if private else None}
                with patch.object(native, "process_identity", return_value=current), patch.object(native.os, "killpg") as kill:
                    result = self.assess(direct=True)
                    self.assertEqual(result["inspection_outcome"]["reason"], "native_interrupted_run_unresolved")
                    self.assertEqual(result["cleanup"]["status"], "closed")
                    self.assertFalse(context.private.exists())
                    self.args.native_release_run = context.identity["run_id"]
                    self.assertEqual(self.assess(direct=True)["inspection_outcome"]["reason"], "native_recovery_only_no_inspection")
                kill.assert_not_called()
                self.args.native_release_run = None

    def test_recovery_refuses_unproved_identity_without_signalling(self):
        for pgid, same_path, started in ((123456, False, "old"), (42, True, "old"), (123456, True, "different-unclocked")):
            with self.subTest(pgid=pgid):
                context = self.reserve_run()
                context.phase("engine_running", engine_identity={"pid": 123456, "pgid": 123456, "started": "old", "private": str(context.private)})
                self.args.native_recover_only = True
                current = {"pid": 123456, "pgid": pgid, "started": started, "private": str(context.private) if same_path else None}
                with patch.object(native, "process_identity", return_value=current), patch.object(native.os, "killpg") as kill:
                    result = self.assess(direct=True)
                self.assertEqual(result["inspection_outcome"]["reason"], "native_recovery_process_identity_changed")
                self.assertEqual(result["cleanup"]["status"], "failed")
                kill.assert_not_called()
                self.assertTrue(context.private.exists())

    def test_both_entry_paths_preserve_resources_under_a_live_unproved_group(self):
        for recover_only in (False, True):
            with self.subTest(recover_only=recover_only):
                context = self.reserve_run()
                context.phase("engine_running", engine_identity={"pid": 123456, "pgid": 123456, "started": "old", "private": str(context.private)})
                self.args.native_recover_only = recover_only
                with patch.object(native, "process_identity", return_value=None), patch.object(native.os, "killpg") as kill:
                    result = self.assess(direct=True)
                self.assertEqual(result["inspection_outcome"]["reason"], "native_recovery_process_group_unproven")
                self.assertEqual(result["cleanup"]["status"], "failed")
                self.assertTrue(context.private.exists())
                kill.assert_called_once_with(123456, 0)

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
            fixture_revision = copy_tracked_fixture(fixture, repo)
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "--all"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Native fixture", "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Committed qualification fixture"], check=True)
            args = host.build_parser().parse_args(["agent-inspect", "--backend", "native", "--repo", str(repo),
                       "--scope", "directory", "--dir", "native_probe", "--timeout-ms", "300000"])
            result = native.run(args, host)
            result["qualification"] = {"fixture_source_commit": fixture_revision}
            if os.environ.get("JB_NATIVE_LIVE_OUT"):
                Path(os.environ["JB_NATIVE_LIVE_OUT"]).write_text(json.dumps(result, indent=2))
            self.assertEqual(result["agent_result"]["verdict"], "RED", result["agent_result"]["agent_report"])
            self.assertTrue(result["coverage"]["complete"])
            self.assertEqual(result["cleanup"]["status"], "closed")
            findings = json.loads(Path(result["findings_artifact"]).read_text())
            self.assertEqual(result["identity"]["native_backend_revision"], native.new_receipt(host)["backend_revision"])
            self.assertIsNotNone(result["diagnostic"]["native"]["repo_head_sha"])
            tree = ast.parse((repo / "native_probe/models.py").read_text())
            functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
            def in_function(finding, node, source_path="native_probe/models.py"):
                start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
                return finding["file"] == source_path and start <= finding["line"] <= node.end_lineno
            wrong, correct = functions["wrong_model"], functions["correct_model"]
            self.assertTrue(any(f["rule_id"] == "PyTypeCheckerInspection" and in_function(f, wrong) for f in findings))
            self.assertFalse(any(in_function(f, correct) for f in findings))
            self.assertTrue(any(f["rule_id"] == "PyUnresolvedReferencesInspection" and in_function(f, functions["broken_dependency"]) for f in findings))
            dependency = functions["valid_dependency"]
            self.assertFalse(any(in_function(f, dependency) for f in findings))
            modern_tree = ast.parse((repo / "native_probe/python_types.py").read_text())
            wrong_type = next(node for node in modern_tree.body if isinstance(node, ast.FunctionDef) and node.name == "wrong_type")
            self.assertTrue(any(f["rule_id"] == "PyTypeCheckerInspection" and in_function(f, wrong_type, "native_probe/python_types.py") for f in findings))
            modern = next(node for node in modern_tree.body if isinstance(node, ast.FunctionDef) and node.name == "valid_modern_annotation")
            self.assertFalse(any(f["rule_id"] in {"PyTypeCheckerInspection", "PyTypeHintsInspection"}
                                 and in_function(f, modern, "native_probe/python_types.py") for f in findings))
            coverage_tree = ast.parse((repo / "native_probe/coverage.py").read_text())
            reference = next(node for node in coverage_tree.body if isinstance(node, ast.FunctionDef))
            self.assertTrue(any(f["rule_id"] == "PyUnresolvedReferencesInspection" and in_function(f, reference, "native_probe/coverage.py") for f in findings))
            for node in (node for node in coverage_tree.body if isinstance(node, ast.Assign)):
                insecure = isinstance(node.value, ast.Constant) and str(node.value.value).startswith("http:")
                detected = any(f["rule_id"] == "HttpUrlsUsage" and f["file"] == "native_probe/coverage.py" and node.lineno <= f["line"] <= node.end_lineno for f in findings)
                self.assertEqual(detected, insecure)
            for tool, source in (("OdooXml", "native_probe/views/defect.xml"), ("DuplicatedCode", "native_probe/duplicates.py"), ("OdooPy", "native_probe/models.py")):
                self.assertTrue(any(f["rule_id"] == tool and f["file"] == source for f in findings), tool)
            duplicate_tree = ast.parse((repo / "native_probe/duplicates.py").read_text())
            blocks = [node for node in duplicate_tree.body if isinstance(node, ast.FunctionDef)]
            self.assertTrue(any(f["rule_id"] == "DuplicatedCode" and f["file"] == "native_probe/duplicates.py"
                                and any(block.body[0].lineno <= f["line"] <= block.end_lineno for block in blocks)
                                for f in findings), "duplicate must be inside the planted blocks, not a cloned file")
            self.assertFalse(any(f["file"] == "native_probe/views/control.xml" for f in findings))


if __name__ == "__main__":
    unittest.main()
