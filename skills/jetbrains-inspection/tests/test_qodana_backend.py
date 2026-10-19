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
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
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
    def test_nested_desktop_preparation_cannot_prepare_qodana_snapshot_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary)
            with self.assertRaises(backend.Blocked):
                backend.preparation({"prepare": {"python": {
                    "version": "3.12", "projectPath": "scripts",
                }}}, private, "python")
            self.assertEqual(list(private.iterdir()), [])

    temp: tempfile.TemporaryDirectory[str]
    root: Path
    repo: Path
    cache: Path
    args: Namespace
    seen: list[Path]
    env: Any
    lock_patch: Any

    def setUp(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
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
                    '<inspection_tool class="PyUnresolvedReferencesInspection" enabled="true"/></profile></component>')
        (self.repo / ".github").mkdir()
        (self.repo / ".github/github.json").write_text(json.dumps({"qualityGate": {"inspection": {
            "prepare": {"python": {"version": "3.12", "moduleName": "fixture"}}}}}))
        self.venv_version = "3.12.2"
        self.type_hints_enabled = "true"
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
        storage_patch = patch.object(backend, "storage_identity", side_effect=lambda anchor: {"fixture_anchor": str(anchor)} if anchor.is_dir() else (_ for _ in ()).throw(backend.Blocked("qodana_recovery_storage_unavailable")))
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
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
        """Stands in for the container run and copy_out: results and pyvenv.cfg come back."""
        self.seen.append(private)
        self.assertEqual((private / "project/src/a.py").read_bytes(), (self.repo / "src/a.py").read_bytes())
        self.assertTrue((private / "project/.git/index").is_file())
        (private / "results").mkdir(exist_ok=True)
        (private / "results/qodana.sarif.json").write_text(json.dumps(sarif([finding(), finding("src/b.py")])) )
        (private / "results/effective.profile.xml").write_text('<profile><inspection_tool class="PyUnresolvedReferencesInspection" enabled="true"/>'
                                                               f'<inspection_tool class="PyTypeHintsInspection" enabled="{self.type_hints_enabled}"/></profile>')
        (private / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {self.venv_version}\n")
        (private / "inspected-sha256.json").write_text(json.dumps({
            str(path.relative_to(private / "project")): backend.digest(path)
            for path in (private / "project").rglob("*") if path.is_file() and ".git" not in path.parts}))
        receipt["container_removed"] = True

    def run_backend(self, execute: Callable[..., Any] | None = None):
        backend.checked_original = backend.checked
        with patch.object(backend, "checked", side_effect=self.prerequisite), \
                patch.object(backend.os, "getloadavg", return_value=(1, 1, 1)), \
                patch.object(backend, "execute", side_effect=lambda *args, **_kwargs: (execute or self.completed)(*args)):
            return backend.run(self.args, host)

    def test_completed_red_has_only_requested_current_findings_and_full_artifact(self):
        self.enable_duplicated_code()
        result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "RED")
        self.assertEqual(result["helper_exit_code"], host.classify_run_exit({"verdict": result["agent_result"]["verdict"]}))
        self.assertEqual(result["findings"][0]["file"], "src/a.py")
        self.assertEqual(result["finding_count"], 1)
        artifact = json.loads(Path(result["findings_artifact"]["path"]).read_text())
        self.assertEqual(artifact["problems"], result["findings"])
        self.assertEqual(result["uncovered_inspections"], ["DuplicatedCode"])
        self.assertTrue(all(not p.exists() for p in self.seen))

    @staticmethod
    def canaries(private):
        return sorted(p.relative_to(private / "project").as_posix()
                      for p in (private / "project").rglob("_jb_qodana_canary_*.py"))

    def clean_with_canary(self, *, proven=("src",), modern_hint_flagged=False, extra=()):
        """Engine results that report the canary's unresolved name in the proven directories."""
        def attempt(*args):
            self.completed(*args)
            results = list(extra)
            for canary in self.canaries(args[2]):
                if canary.rpartition("/")[0] in proven:
                    results.append(finding(canary, line=backend.CANARY_UNRESOLVED_LINE))
                    if modern_hint_flagged:
                        results.append(finding(canary, line=1, rule="PyTypeHintsInspection"))
            (args[2] / "results/qodana.sarif.json").write_text(json.dumps(sarif(results)))
        return attempt

    def enable_duplicated_code(self):
        profile = self.repo / ".idea/inspectionProfiles/Project_Default.xml"
        profile.write_text(profile.read_text().replace(
            "</profile>", '<inspection_tool class="DuplicatedCode" enabled="true"/></profile>'))

    def test_clean_python_run_with_canary_proof_is_green(self):
        result = self.run_backend(self.clean_with_canary())
        self.assertEqual(result["agent_result"]["verdict"], "GREEN")
        self.assertEqual(result["helper_exit_code"], 0)
        self.assertEqual(result["proof_failures"], [])
        self.assertEqual(result["uncovered_inspections"], [])
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(sorted(p.name for p in (self.repo / "src").iterdir()), ["a.py", "b.py"])

    def test_community_uncovered_inspection_blocks_green_until_accepted(self):
        # Q481 pending: the result names DuplicatedCode and stays UNKNOWN on clean code.
        self.enable_duplicated_code()
        with patch.object(backend, "PYTHON_ACCEPTED_UNCOVERED", frozenset()):
            pending = self.run_backend(self.clean_with_canary())
        self.assertEqual(pending["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(pending["uncovered_inspections"], ["DuplicatedCode"])
        self.assertIn("effective_profile_missing:DuplicatedCode", pending["proof_failures"])
        with patch.object(backend, "PYTHON_ACCEPTED_UNCOVERED", frozenset({"DuplicatedCode"})):
            accepted = self.run_backend(self.clean_with_canary())
        self.assertEqual(accepted["agent_result"]["verdict"], "GREEN")
        self.assertEqual(accepted["uncovered_inspections"], ["DuplicatedCode"])

    def test_zero_findings_without_canary_finding_never_green(self):
        result = self.run_backend(self.clean_with_canary(proven=()))
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("python_canary_execution_unproven", result["proof_failures"])
        self.assertFalse(result["agent_result"]["retry_policy"]["retry"])

    def test_every_selected_directory_needs_its_own_canary_finding(self):
        # A module excluding src/ lets a generated/ canary pass while src/a.py goes unread.
        (self.repo / "generated").mkdir()
        (self.repo / "generated/c.py").write_text("print('generated')\n")
        self.args.files = ["src/a.py", "generated/c.py"]
        result = self.run_backend(self.clean_with_canary(proven=("generated",)))
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("python_canary_execution_unproven", result["proof_failures"])

    def test_module_without_attached_interpreter_makes_findings_not_decisive(self):
        # A model without the prepared interpreter flags valid `X | None` hints.
        result = self.run_backend(self.clean_with_canary(modern_hint_flagged=True, extra=[finding()]))
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_python_language_level_mismatch")
        self.assertEqual(result["finding_count"], 0)

    def test_disabled_type_hint_inspection_cannot_vouch_for_the_interpreter(self):
        self.type_hints_enabled = "false"
        result = self.run_backend(self.clean_with_canary())
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("python_canary_rule_not_enabled:PyTypeHintsInspection", result["proof_failures"])

    def test_prepared_interpreter_must_match_the_requested_version(self):
        self.venv_version = "3.11.9"
        result = self.run_backend(self.clean_with_canary(extra=[finding()]))
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_python_interpreter_mismatch")

    def test_no_declared_interpreter_never_green(self):
        (self.repo / ".github/github.json").write_text("{}")
        result = self.run_backend(self.clean_with_canary())
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("python_interpreter_unprepared", result["proof_failures"])

    def test_selected_file_over_the_engine_size_limit_blocks_green(self):
        (self.repo / "src/a.py").write_text("value = 1\n" * 400)
        with patch.object(backend, "MAX_ANALYZED_FILE_KB", 1):
            result = self.run_backend(self.clean_with_canary())
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("qodana_selected_file_over_analysis_limit", result["proof_failures"])

    def test_tracked_module_exclude_pattern_blocks_green(self):
        (self.repo / ".idea/fixture.iml").write_text('<module><component><content><excludePattern pattern="*_gen.py"/></content></component></module>')
        result = self.run_backend(self.clean_with_canary())
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertIn("qodana_tracked_module_exclusions_unproven", result["proof_failures"])

    def test_enabled_python_inspection_missing_from_engine_blocks_green(self):
        profile = self.repo / ".idea/inspectionProfiles/Project_Default.xml"
        profile.write_text(profile.read_text().replace(
            "</profile>", '<inspection_tool class="PyUnusedLocalVariableInspection" enabled="true"/>'
                          '<inspection_tool class="CssUnknownProperty" enabled="true"/></profile>'))
        result = self.run_backend(self.clean_with_canary())
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["proof_failures"], ["effective_profile_missing:PyUnusedLocalVariableInspection"])

    def test_lane_gap_only_for_files_outside_pycharm_lanes(self):
        config = {"lanes": [{"id": "jvm", "ide": "IntelliJ IDEA", "include": ["**/*.kt"]},
                            {"id": "python", "ide": "PyCharm", "include": ["**/*.py"]}]}
        self.assertEqual(backend.lane_gaps(host, config, {"src/a.py"}), [])
        self.assertEqual(backend.lane_gaps(host, config, {"src/a.py", "src/A.kt"}), ["qodana_lane_models_unqualified"])
        nested = {"lanes": [{"id": "python", "ide": "PyCharm", "include": ["**/*.py"], "projectPath": "src"}]}
        self.assertEqual(backend.lane_gaps(host, nested, {"src/a.py"}), ["qodana_lane_models_unqualified"])

    def test_headless_preparation_leaves_the_model_to_qodana_and_provisions_declared_uv(self):
        (self.repo / "uv.toml").write_text('required-version = ">=0.11.29,<1"\n')
        config = {"prepare": {"python": {"version": "3.12", "moduleName": "m"}}}
        private = self.root / "private"
        private.mkdir()
        provisioned = backend.preparation(config, private, "python", self.repo)
        supplied = backend.preparation(config, private, "python", self.repo, uv_supplied=True)
        install, generator = provisioned["commands"]
        self.assertEqual(install[-1], "uv>=0.11.29,<1")
        self.assertEqual(supplied["commands"], [generator])
        self.assertIsNone(supplied["path"])
        # Run the produced preparation on a fresh project: it must leave the IDE model to Qodana.
        project = self.root / "project"
        (project / ".venv/bin").mkdir(parents=True)
        subprocess.run(["git", "init", "--quiet", str(project)], check=True)
        (project / ".gitignore").write_text(".venv/\n")
        interpreter = project / ".venv/bin/python"
        interpreter.write_text("#!/bin/sh\necho 3.12\n")
        interpreter.chmod(0o755)
        script = generator.index(f"{backend.WORK}/prepare-python-project.py")
        argv = [sys.executable, str(private / "prepare-python-project.py"),
                *[str(project) if value == f"{backend.WORK}/project" else value for value in generator[script + 1:]]]
        subprocess.run(argv, check=True, capture_output=True)
        self.assertFalse((project / ".idea").exists())

    def test_uv_requirement_rejects_shell_text(self):
        (self.repo / "uv.toml").write_text('required-version = ">=0.11; touch /tmp/x"\n')
        with self.assertRaises(backend.Blocked):
            backend.uv_requirement(self.repo)

    def test_odoo_generic_xml_or_loaded_profile_does_not_prove_named_coverage(self):
        results = self.root / "results"
        results.mkdir()
        gaps, _uncovered = backend.coverage_gaps("python", {"addon/views.xml"}, {"OdooPy", "OdooXml"}, results)
        self.assertIn("OdooXml_execution_unproven", gaps)
        self.assertIn("odoo_model_roots_interpreter_unproven", gaps)
        self.assertIn("odoo_effective_default_unproven", gaps)

    def test_removed_license_flag_refuses_before_file_or_docker_access(self):
        self.args.qodana_license_file = [str(self.root / "plugin_TEST.license")]
        with patch.object(backend, "checked") as checked, \
                patch.object(backend.shutil, "copyfile") as copy, \
                patch.object(backend, "execute") as execute:
            result = backend.run(self.args, host)
        checked.assert_not_called()
        copy.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_license_import_removed")
        self.assertFalse((self.root / "state").exists())

    def test_snapshot_refuses_credential_locations_before_any_read_or_copy(self):
        for name in ("pycharm.key", "plugin_TEST.license", ".env", ".env.local",
                     ".ssh/id_ed25519", ".aws/credentials", "credentials.json", "account.json"):
            with self.subTest(name=name), \
                    patch.object(backend, "checked", return_value=("src/a.py\0" + name + "\0").encode()), \
                    patch.object(backend.shutil, "copyfile") as copy, \
                    patch.object(backend, "digest") as read:
                with self.assertRaises(backend.Blocked) as refusal:
                    backend.snapshot(self.repo, self.root / "snapshot")
                self.assertEqual(refusal.exception.input_path, name)
                copy.assert_not_called()
                read.assert_not_called()

    def test_explicit_inputs_cannot_disguise_a_credential_as_uv_or_plugin(self):
        alias = self.root / "public-input"
        alias.symlink_to(self.root / "pycharm.key")
        for option in ("qodana_uv", "qodana_plugin_zip"):
            for value in (str(self.root / "pycharm.key"), str(alias)):
                with self.subTest(option=option, value=value):
                    setattr(self.args, option, [value] if option.endswith("zip") else value)
                    with patch.object(backend, "checked") as checked:
                        result = backend.run(self.args, host)
                    checked.assert_not_called()
                    self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
                    self.assertEqual(result["diagnostic"]["error_reason"], "qodana_credential_input_refused")
                    self.assertEqual(result["diagnostic"]["qodana"]["refused_input"], "pycharm.key")
                    setattr(self.args, option, [] if option.endswith("zip") else None)

    def test_plugin_members_are_checked_before_extraction(self):
        from types import SimpleNamespace
        with patch.object(backend.zipfile, "ZipFile") as archive:
            package = archive.return_value.__enter__.return_value
            package.infolist.return_value = [SimpleNamespace(filename="plugin/plugin_TEST.license", external_attr=0)]
            with self.assertRaises(backend.Blocked):
                backend.unpack_plugins([self.root / "public.zip"], self.root / "plugins")
            package.extractall.assert_not_called()

    def test_container_copy_refuses_private_credential_locations(self):
        private = self.root / "private"
        (private / "config/pycharm.key").mkdir(parents=True)
        with patch.object(backend, "bounded") as transfer:
            with self.assertRaises(backend.Blocked):
                backend.copy_in("fixture", private, {})
            transfer.assert_not_called()

    def test_preparation_rewriting_a_selected_source_in_the_container_is_not_decisive(self):
        def rewritten(*args):
            self.completed(*args)
            inspected = args[2] / "inspected-sha256.json"
            hashes = json.loads(inspected.read_text())
            hashes["src/a.py"] = "0" * 64
            inspected.write_text(json.dumps(hashes))
        result = self.run_backend(rewritten)
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_snapshot_inputs_changed")

    def test_rewritten_unselected_import_in_the_container_is_not_decisive(self):
        # src/a.py is selected; src/b.py, which it could import, changed during preparation.
        def rewritten(*args):
            self.completed(*args)
            inspected = args[2] / "inspected-sha256.json"
            hashes = json.loads(inspected.read_text())
            hashes["src/b.py"] = "0" * 64
            inspected.write_text(json.dumps(hashes))
        result = self.run_backend(rewritten)
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_snapshot_inputs_changed")

    def test_engine_rewrite_of_profile_selection_does_not_invalidate_the_run(self):
        settings = self.repo / ".idea/inspectionProfiles/profiles_settings.xml"
        settings.write_text('<component name="InspectionProjectProfileManager"/>')
        def rewritten(*args):
            self.clean_with_canary()(*args)
            inspected = args[2] / "inspected-sha256.json"
            hashes = json.loads(inspected.read_text())
            hashes[".idea/inspectionProfiles/profiles_settings.xml"] = "0" * 64
            inspected.write_text(json.dumps(hashes))
        result = self.run_backend(rewritten)
        self.assertEqual(result["agent_result"]["verdict"], "GREEN")

    def test_copy_step_stops_and_reaps_when_parent_exits_or_deadline_passes(self):
        pid_file = self.root / "copy.pid"
        sleeper = [sys.executable, "-c", f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"]
        for variant in ("parent exits", "deadline passes"):
            with self.subTest(variant=variant):
                pid_file.unlink(missing_ok=True)
                if variant == "parent exits":
                    # The parent's lifetime pipe reads EOF once the copy is running.
                    with patch.object(backend, "parent_gone", side_effect=lambda _fd: pid_file.exists()), \
                            self.assertRaisesRegex(backend.Blocked, "qodana_parent_gone"):
                        backend.bounded(sleeper, backend.safe_env(), parent_fd=0)
                else:
                    with patch.object(backend, "COPY_SECONDS", 1.0), self.assertRaisesRegex(backend.Blocked, "qodana_timeout"):
                        backend.bounded(sleeper, backend.safe_env())
                # Reaped, not merely abandoned: the copy process no longer exists.
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)

    def test_export_hashing_stops_when_parent_exits_or_deadline_passes(self):
        # One large tracked file: the lifetime is valid at the member check and
        # fails only after the first chunk, so the per-chunk check must stop it.
        import tarfile
        archive = self.root / "export.tar"
        payload = self.root / "big.py"
        payload.write_bytes(b"x" * (3 << 20))
        with tarfile.open(archive, "w") as output:
            output.add(payload, arcname="project/big.py")
        for variant in ("parent exits", "deadline passes"):
            with self.subTest(variant=variant):
                checks = []
                def expires_after_first_chunk(*_args):
                    checks.append(1)
                    return len(checks) > 2
                if variant == "parent exits":
                    with patch.object(backend, "parent_gone", side_effect=expires_after_first_chunk), \
                            self.assertRaisesRegex(backend.Blocked, "qodana_parent_gone"):
                        backend.archive_hashes(archive, parent_fd=0)
                else:
                    clock = iter([0.0, 0.0, 10.0, 10.0, 10.0])
                    with patch.object(backend.time, "monotonic", side_effect=lambda: next(clock)), \
                            self.assertRaisesRegex(backend.Blocked, "qodana_timeout"):
                        backend.archive_hashes(archive, None, deadline=5.0)
        self.assertEqual(set(backend.archive_hashes(archive)), {"big.py"})

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

    def test_host_lock_refuses_second_worker_before_execution(self):
        import fcntl
        with (self.root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertFalse(self.seen)

    def test_killed_helper_private_state_is_recovered_before_another_task(self):
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-killed"
        child = '''import importlib.util, os, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("backend", sys.argv[1])
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
b.state_dir = lambda: Path(sys.argv[2])
b.storage_identity = lambda anchor: {"fixture_anchor": str(anchor)}
private = Path(sys.argv[3]); private.mkdir(); (private / "config").mkdir()
receipt = {"run_id": sys.argv[4]}
b.save_run_record(private, receipt)
(private / "config/state.dat").write_bytes(b"synthetic-private-state")
os.kill(os.getpid(), signal.SIGKILL)
'''
        result = subprocess.run([sys.executable, "-c", child, str(SCRIPTS / "qodana_backend.py"),
                                 str(state), str(private), str(uuid.uuid4())], capture_output=True)
        self.assertLess(result.returncode, 0)
        self.assertTrue((private / "config/state.dat").is_file())
        with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 1, b"", b"No such container")):
            result = self.run_backend()
        self.assertFalse(private.exists())
        self.assertEqual(result["diagnostic"]["qodana"]["recovered_run_count"], 1)

    def test_recovery_removes_a_container_proved_by_label_and_log_mount(self):
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-owned"
        (private / "logs").mkdir(parents=True)
        receipt = {"run_id": str(uuid.uuid4())}
        journal = backend.save_run_record(private, receipt)
        info = [{"Config": {"Labels": {backend.RUN_LABEL: receipt["run_id"]}},
                 "Mounts": [{"Destination": backend.SHARED_LOGS, "Source": str(private / "logs")}]}]
        with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(info).encode(), b"")), \
                patch.object(backend, "remove_container", return_value=True) as remove:
            recovered = backend.recover_runs(backend.safe_env())
        remove.assert_called_once()
        self.assertEqual(recovered["recovered_run_count"], 1)
        self.assertFalse(private.exists())
        self.assertFalse(journal.exists())

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
                info = [{"Config": {"Labels": {backend.RUN_LABEL: "other"}},
                         "Mounts": [{"Destination": "/private", "Source": str(private)}]}]
                with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(info).encode(), b"")), \
                        patch.object(backend, "remove_container") as remove:
                    recovered = backend.recover_runs(backend.safe_env())
                    self.assertGreater(recovered["unresolved_record_count"], 0)
                remove.assert_not_called()
                self.assertTrue(private.exists())
                self.assertTrue(journal.exists())

    def test_partial_deletion_keeps_marker_and_next_recovery_finishes(self):
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-partial"
        private.mkdir()
        (private / "config").mkdir()
        (private / "config/state.dat").write_bytes(b"synthetic")
        backend.save_run_record(private, {"run_id": str(uuid.uuid4())})
        with patch.object(backend.shutil, "rmtree", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                backend.delete_private(private)
        self.assertTrue((private / "owner.json").exists())
        with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 1, b"", b"No such container")):
            result = backend.recover_runs(backend.safe_env())
        self.assertEqual(result["unresolved_record_count"], 0)
        self.assertFalse(private.exists())

    def test_missing_storage_retains_journal_and_still_recovers_other_record(self):
        state = self.root / "state"
        state.mkdir()
        storage = self.root / "storage"
        storage.mkdir()
        private = storage / "private-offline"
        private.mkdir()
        journal = backend.save_run_record(private, {"run_id": str(uuid.uuid4())})
        record = json.loads(journal.read_text())
        record["storage_anchor"] = str(storage)
        record["storage_identity"] = backend.storage_identity(storage)
        journal.write_text(json.dumps(record))
        (private / "owner.json").write_text(json.dumps(record))
        storage.rename(self.root / "offline-storage")
        valid = self.root / "private-valid"
        valid.mkdir()
        backend.save_run_record(valid, {"run_id": str(uuid.uuid4())})
        with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 1, b"", b"No such container")):
            result = backend.recover_runs(backend.safe_env())
        self.assertTrue(journal.exists())
        self.assertFalse(valid.exists())
        self.assertEqual(result["recovered_run_count"], 1)
        self.assertEqual(result["unresolved_record_count"], 1)

    def test_deleted_task_directory_on_present_storage_does_not_poison_future_runs(self):
        state = self.root / "state"
        state.mkdir()
        task = self.root / "old-task"
        task.mkdir()
        private = task / "private-gone"
        private.mkdir()
        journal = backend.save_run_record(private, {"run_id": str(uuid.uuid4())})
        backend.delete_private(private)
        task.rmdir()
        with patch.object(backend, "command", return_value=subprocess.CompletedProcess([], 1, b"", b"No such container")):
            result = backend.recover_runs(backend.safe_env())
        self.assertEqual(result["unresolved_record_count"], 0)
        self.assertFalse(journal.exists())

    def test_recovery_uses_journaled_local_docker_endpoint(self):
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-old-daemon"
        private.mkdir()
        endpoint = "unix:///fixture/old.sock"
        backend.save_run_record(private, {"run_id": str(uuid.uuid4())}, {"DOCKER_HOST": endpoint})
        seen = []
        def inspect(_argv, **kwargs):
            seen.append(kwargs["env"]["DOCKER_HOST"])
            return subprocess.CompletedProcess([], 1, b"", b"No such container")
        with patch.object(backend, "command", side_effect=inspect):
            backend.recover_runs({"DOCKER_HOST": "unix:///fixture/new.sock"})
        self.assertEqual(seen, [endpoint])

    def test_unresolved_recovery_is_never_cleanup_clean(self):
        with patch.object(backend, "recover_runs", return_value={"recovered_run_count": 0, "unresolved_record_count": 1}):
            result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertEqual(result["lifecycle_outcome"]["status"], "unknown")

    def test_slow_monitor_cleanup_stays_owned_and_preserves_container_receipt(self):
        def pending(*args):
            self.completed(*args)
            return ("synthetic-monitor", "synthetic-pipe")
        with patch.object(backend, "finish_guardian", return_value=False), patch.object(backend, "delete_private") as delete:
            result = self.run_backend(pending)
        delete.assert_not_called()
        self.assertTrue(result["diagnostic"]["qodana"]["container_removed"])
        self.assertEqual(result["cleanup"]["status"], "deferred")
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")

    def test_macos_firmlink_uses_diskutil_mount_device_not_path_equality(self):
        import plistlib
        original = backend.storage_identity
        # Call the tracked implementation instead of this fixture's identity stub.
        fresh = load("qodana_storage_test", SCRIPTS / "qodana_backend.py")
        anchor = self.root / "users-firmlink"
        mounted = self.root / "data-volume"
        anchor.mkdir(); mounted.mkdir()
        data = plistlib.dumps({"MountPoint": str(mounted), "VolumeUUID": "fixture-volume"})
        with patch.object(fresh.sys, "platform", "darwin"), patch.object(fresh, "command", return_value=subprocess.CompletedProcess([], 0, data, b"")):
            identity = fresh.storage_identity(anchor)
        self.assertEqual(identity["volume_uuid"], plistlib.loads(data)["VolumeUUID"])
        self.assertIs(backend.storage_identity, original)

    def test_diskutil_firmlink_refusal_resolves_device_through_df(self):
        import plistlib
        fresh = load("qodana_firmlink_test", SCRIPTS / "qodana_backend.py")
        data = plistlib.dumps({"MountPoint": str(self.root), "VolumeUUID": "fixture-volume"})
        attempts = []
        def query(argv, **_kwargs):
            attempts.append(argv)
            return subprocess.CompletedProcess(argv, 1 if len(attempts) == 1 else 0, b"" if len(attempts) == 1 else data, b"")
        df = b"Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/fixture 1 1 0 100% /fixture\n"
        with patch.object(fresh.sys, "platform", "darwin"), patch.object(fresh, "command", side_effect=query), patch.object(fresh, "checked", return_value=df):
            identity = fresh.storage_identity(self.root)
        self.assertEqual(identity["volume_uuid"], plistlib.loads(data)["VolumeUUID"])
        self.assertEqual(attempts[-1][-1], df.decode().splitlines()[-1].split()[0])

    def test_qodana_recovery_flag_without_explicit_backend_never_opens_api(self):
        output = io.StringIO()
        with patch.object(host.sys, "argv", [str(SCRIPTS / "jb-inspect.py"), "agent-inspect", "--qodana-recover-only"]), \
                patch.object(host, "build_context") as context, patch.object(host, "command_run") as api, redirect_stdout(output):
            self.assertEqual(host.main(), 0)
        context.assert_not_called()
        api.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["agent_result"]["verdict"], "UNKNOWN")

    def select(self, *argv, image_present=True, host_default=None):
        args = host.build_parser().parse_args(["agent-inspect", "--repo", str(self.repo), *argv])
        real = backend.command
        def docker(command_argv, **kwargs):
            if command_argv[0] != "docker":
                return real(command_argv, **kwargs)
            return subprocess.CompletedProcess(command_argv, 0 if image_present else 1, b"[]", b"")
        with patch.object(backend, "command", side_effect=docker):
            return backend.select_backend(args, host, host_default)

    def test_default_backend_runs_python_headless_and_keeps_the_api_elsewhere(self):
        python = ("--scope", "files", "--file", "src/a.py")
        self.assertEqual(self.select(*python), ("qodana", "python_qualified"))
        self.assertEqual(self.select(*python, image_present=False), ("api", "qodana_image_unavailable"))
        self.assertEqual(self.select(*python, host_default="api"), ("api", "host_default"))
        (self.repo / ".github/github.json").write_text("{}")
        self.assertEqual(self.select(*python), ("api", "python_preparation_not_declared"))
        (self.repo / ".github/github.json").write_text(json.dumps({"qualityGate": {"inspection": {
            "prepare": {"python": {"version": "3.12", "moduleName": "fixture"}}}}}))
        (self.repo / "src/App.kt").write_text("fun main() {}\n")
        self.assertEqual(self.select("--scope", "files", "--file", "src/a.py", "--file", "src/App.kt"),
                         ("api", "selection_not_python_only"))
        (self.repo / "addon").mkdir()
        (self.repo / "addon/__manifest__.py").write_text("{}\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "addon/__manifest__.py"], check=True)
        self.assertEqual(self.select(*python), ("api", "odoo_not_yet_qualified"))

    def test_detached_monitor_deletes_copies_when_parent_is_killed_during_load_pause(self):
        import time
        state = self.root / "state"
        state.mkdir()
        private = self.root / "private-guardian"
        private.mkdir()
        (private / "config").mkdir()
        (private / "config/state.dat").write_bytes(b"synthetic")
        run_id = str(uuid.uuid4())
        backend.save_run_record(private, {"run_id": run_id})
        paused = self.root / "paused"
        child_code = '''import importlib.util, json, os, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("backend", sys.argv[1])
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
b.state_dir = lambda: Path(sys.argv[2])
b.os.getloadavg = lambda: (b.LOAD_LIMIT + 1, 1, 1)
paused = Path(sys.argv[5])
def command(argv, **kwargs):
    if argv[1] == "pause": paused.write_text("paused")
    return subprocess.CompletedProcess(argv, 0, b"", b"")
b.command = command
b.start_argv = lambda _name: [sys.executable, "-c", "import time; time.sleep(20)"]
b.bounded = lambda *_args, **_kwargs: None
receipt = {"run_id": sys.argv[4], "load_pause_count": 0, "container_removed": False}
b.execute(["docker", "create"], "owned", Path(sys.argv[3]), b.safe_env(), 10, receipt)
'''
        parent = subprocess.Popen([sys.executable, "-c", child_code, str(SCRIPTS / "qodana_backend.py"),
                                   str(state), str(private), run_id, str(paused)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not paused.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(paused.exists())
            parent.kill()
            parent.wait(timeout=5)
            deadline = time.monotonic() + 5
            while private.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(private.exists())
            self.assertFalse((state / (run_id + ".json")).exists())
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            parent.stdout.close()
            parent.stderr.close()

    def test_repeated_signals_during_cleanup_do_not_interrupt_profile_deletion(self):
        original = backend.delete_private
        def signal_during_delete(private):
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGHUP)
            original(private)
        with patch.object(backend, "delete_private", side_effect=signal_during_delete):
            result = self.run_backend()
        self.assertTrue(result["diagnostic"]["qodana"]["profile_deleted"])
        self.assertEqual(result["agent_result"]["verdict"], "RED")

    def test_recovery_only_does_not_start_inspection(self):
        self.args.qodana_recover_only = True
        with patch.object(backend, "execute") as execute:
            result = self.run_backend(execute)
        execute.assert_not_called()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")

    def test_scope_modes_and_legacy_config_fallback_resolve_real_file_sets(self):
        (self.repo / ".github/github.json").write_text(json.dumps({"jetbrains": {"scopePreference": "whole_project"}}))
        self.args.scope = None
        broad = self.run_backend()
        self.assertGreater(broad["selected_file_count"], 1)
        self.args.scope = "directory"
        self.args.directory = str(self.repo / "src")
        directory = self.run_backend()
        self.assertEqual(directory["selected_file_count"], 2)
        subprocess.run(["git", "-C", str(self.repo), "add", "--all"], check=True)
        self.args.scope = "changed_files"
        self.args.changed_files_mode = "unstaged"
        self.args.include_unversioned = False
        (self.repo / "src/a.py").write_text("print(other_missing)\n")
        changed = self.run_backend()
        self.assertEqual(changed["selected_file_count"], 1)

    def test_public_plugin_package_is_copied_into_the_disposable_container(self):
        package = self.root / "public-plugin.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("test-plugin/lib/test.jar", b"synthetic-public-artifact")
        self.args.qodana_plugin_zip = [str(package)]
        def inspect_plugins(argv, name, private, *rest):
            copies = []
            with patch.object(backend, "bounded", side_effect=lambda command_argv, *_rest, **_kwargs: copies.append(command_argv)):
                backend.copy_in(name, private, {})
            plugin_copies = [c for c in copies if c[-1].startswith(f"{name}:/opt/idea/plugins/{name}-")]
            self.assertEqual([Path(c[-2]).name for c in plugin_copies], ["test-plugin"])
            self.completed(argv, name, private, *rest)
        result = self.run_backend(inspect_plugins)
        self.assertTrue(result["diagnostic"]["qodana"]["profile_deleted"])

    def test_container_shares_only_its_log_directory_with_the_host(self):
        # Docker Desktop keeps host handles on every shared path the engine touches,
        # after exit (shiny-infra-ops#320); cache, SDK, venv and project stay inside.
        def inspect_launch(argv, name, private, *rest):
            self.assertNotIn(str(self.repo), " ".join(argv))
            shared = []
            for i, value in enumerate(argv[:-1]):
                if value in {"-v", "--volume"}:
                    shared.append(argv[i + 1].split(":")[0])
                elif value == "--mount":
                    fields = dict(field.partition("=")[::2] for field in argv[i + 1].split(","))
                    shared.append(fields.get("source") or fields.get("src", ""))
            self.assertEqual(shared, [str(private / "logs")])
            receipt = rest[-1]
            self.assertEqual(float(argv[argv.index("--cpus") + 1]), receipt["cpu_limit"])
            self.assertEqual(argv[argv.index("--log-driver") + 1], receipt["docker_logging"])
            self.completed(argv, name, private, *rest)
        result = self.run_backend(inspect_launch)
        self.assertEqual(result["agent_result"]["verdict"], "RED")

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
        with patch.object(backend.os, "getloadavg", return_value=(backend.LOAD_LIMIT + 1, 1, 1)), \
                patch.object(backend, "checked", return_value=b"unix:///local/docker.sock"), \
                patch.object(backend, "execute") as execute:
            result = backend.run(self.args, host)
        execute.assert_not_called()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertEqual(result["diagnostic"]["error_reason"], "qodana_host_load_paused")

    def test_mutable_or_paid_images_are_refused(self):
        self.args.qodana_image = "jetbrains/qodana-js:latest"
        result = self.run_backend()
        self.assertEqual(result["agent_result"]["verdict"], "UNKNOWN")
        self.assertFalse(self.seen)

    def test_explicit_qodana_dispatch_never_enters_desktop_lifecycle(self):
        output = io.StringIO()
        with patch.object(host.sys, "argv", [str(SCRIPTS / "jb-inspect.py"), "agent-inspect", "--backend", "qodana", "--repo", str(self.repo)]), \
                patch.object(host, "build_context") as context, patch.object(host, "command_run") as api, \
                patch.object(host, "command_qodana", side_effect=lambda args: backend.run(args, host)), \
                patch.object(backend, "checked", return_value=b"unix:///local/docker.sock"), \
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

    def test_trial_stop_records_hold_and_deletes_profile(self):
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
        with patch.object(backend, "command", side_effect=lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b"")), \
                patch.object(backend, "start_argv", return_value=[sys.executable, "-u", "-c", code]), \
                patch.object(backend, "bounded"):
            with self.assertRaisesRegex(backend.Blocked, "qodana_trial_or_purchase_stop"):
                backend.execute_worker(["docker", "create"], "own-container", self.root, backend.safe_env(), 3, {})
        self.assertTrue(any(call[:3] == ["docker", "rm", "-f"] for call in calls))

    def test_child_environment_does_not_forward_licensing_or_context_overrides(self):
        with patch.dict(os.environ, {"QODANA_TOKEN": "synthetic", "QODANA_LICENSE": "synthetic", "DOCKER_HOST": "foreign", "PYCHARM_VM_OPTIONS": "foreign"}):
            env = backend.safe_env()
        self.assertFalse(set(env) & {"QODANA_TOKEN", "QODANA_LICENSE", "DOCKER_HOST", "PYCHARM_VM_OPTIONS"})


def docker_vm_descriptors():
    """Native descriptor count of Docker Desktop's VM processes (macOS libproc)."""
    import ctypes
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    list_descriptors, descriptor_record_size = 1, 8  # libproc's descriptor-list flavor and record size
    pids = subprocess.run(["pgrep", "-f", "com.apple.Virtualization.VirtualMachine"],
                          capture_output=True, text=True).stdout.split()
    counts = {}
    for pid in map(int, pids):
        size = libproc.proc_pidinfo(pid, list_descriptors, 0, None, 0)
        buffer = ctypes.create_string_buffer(max(size, 0) * 2 + 8)
        used = libproc.proc_pidinfo(pid, list_descriptors, 0, buffer, len(buffer))
        if size <= 0 or used <= 0:
            raise AssertionError(f"descriptor query failed for Docker VM process {pid}")
        counts[pid] = used // descriptor_record_size
    return counts


@unittest.skipUnless(os.environ.get("JB_QODANA_LIVE") == "1" and sys.platform == "darwin",
                     "live Docker Desktop qualification; set JB_QODANA_LIVE=1")
class LiveDescriptorTests(unittest.TestCase):
    """Completed runs must not leave host handles held by Docker Desktop's VM (shiny-infra-ops#320)."""

    def test_completed_runs_do_not_accumulate_docker_vm_descriptors(self):
        repo = SCRIPTS.parents[2]
        before = docker_vm_descriptors()
        self.assertTrue(before, "Docker Desktop's VM is not running")
        for _ in range(2):
            result = subprocess.run(["uv", "run", str(SCRIPTS / "jb-inspect.py"), "agent-inspect", "--backend", "qodana",
                                     "--repo", str(repo), "--scope", "files", "--file",
                                     "skills/jetbrains-inspection/scripts/prepare-python-project.py", "--timeout-ms", "900000"],
                                    capture_output=True, text=True)
            envelope = json.loads(result.stdout)
            # While Q481 is open, Community-uncovered inspections alone may leave a clean run UNKNOWN.
            pending = {"effective_profile_missing:" + rule
                       for rule in backend.PYTHON_COMMUNITY_UNCOVERED - backend.PYTHON_ACCEPTED_UNCOVERED}
            verdict = envelope["agent_result"]["verdict"]
            if verdict == "UNKNOWN":
                self.assertEqual(envelope["diagnostic"]["error_reason"], "qodana_coverage_unproven")
                self.assertTrue(envelope["proof_failures"])
                self.assertLessEqual(set(envelope["proof_failures"]), pending)
            else:
                self.assertIn(verdict, {"GREEN", "RED"})
            self.assertEqual(envelope["cleanup"]["status"], "closed")
        # Only the small shared log directory may remain held, a few handles per run.
        after = docker_vm_descriptors()
        # A restarted VM would reset the count and hide retention.
        self.assertEqual(after.keys(), before.keys())
        self.assertLess(sum(after.values()) - sum(before.values()), 100)


if __name__ == "__main__":
    unittest.main()
