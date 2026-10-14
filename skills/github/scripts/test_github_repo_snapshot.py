# /// script
# requires-python = ">=3.12"
# ///
"""Exercise the snapshot CLI offline with repository metadata fixtures."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from github_launchplane import configuration_status

SCRIPT = Path(__file__).with_name("github-repo-snapshot.sh")


def helper_metadata():
    return {"launchplane": {
        "enabled": True,
        "service": {"contextUrlEnv": "CONTEXT_URL", "operatorUrlEnv": "OPERATOR_URL"},
        "context": {"enabled": True, "helper": "tools/context.py"},
        "operator": {"enabled": True, "helper": "tools/operator.py", "requiresPrivateConfig": True},
    }}


class SnapshotTests(unittest.TestCase):
    @staticmethod
    def snapshot(metadata, *, text=False, policy=None, policy_exit=0,
                 private_config=True, launchplane_env=None, real_policy=False, isolated_home=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin",
                            "https://github.com/example/project.git"], check=True)
            config = root / "metadata.json"
            if metadata is not None:
                config.write_text(json.dumps(metadata))
            fixture_home = root / "home"
            fixture_home.mkdir()
            child_home = fixture_home / "isolated" if isolated_home else fixture_home
            child_home.mkdir(exist_ok=True)
            # The detector runs against a fixture user's native home, independent
            # of this host's installed configuration. Its CLI and source are real.
            configuration_helper = root / "configuration.py"
            configuration_helper.write_text(
                "import sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
                "import github_launchplane\n"
                f"github_launchplane.director_home = lambda: Path({str(fixture_home)!r})\n"
                "github_launchplane.main()\n"
            )
            if private_config:
                operator_config = fixture_home / ".config" / "launchplane" / "local-operator.json"
                operator_config.parent.mkdir(parents=True)
                operator_config.write_text("invalid configuration")
            policy_helper = root / "policy.py"
            policy_called = root / "policy-called"
            if policy is not None:
                policy_helper.write_text(
                    "import sys\nfrom pathlib import Path\n"
                    f"Path({str(policy_called)!r}).touch()\n"
                    f"print({json.dumps(json.dumps(policy))})\n"
                    f"sys.exit({policy_exit})\n"
                )
            if real_policy:
                policy_helper = SCRIPT.parents[2] / "launchplane" / "scripts" / "launchplane-write-action.py"
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("LAUNCHPLANE_")}
            result = subprocess.run(
                ["bash", str(SCRIPT), *([] if metadata is None else ["--config", str(config)]),
                 *([] if text else ["--json"])],
                cwd=root,
                env={**env, "HOME": str(child_home), **(launchplane_env or {}),
                     "GITHUB_REPO_SNAPSHOT_GH": str(root / "missing-gh"),
                     "GITHUB_REPO_SNAPSHOT_PYTHON": sys.executable,
                     "GITHUB_REPO_SNAPSHOT_CONFIGURATION_HELPER": str(configuration_helper),
                     "GITHUB_REPO_SNAPSHOT_POLICY_HELPER": str(policy_helper)},
                capture_output=True, text=True,
            )
            result.policy_called = policy_called.exists()
            return result

    def test_product_routing_context(self):
        routing = {"product": "example-product", "context": "example_context", "publicName": "Example product"}
        result = self.snapshot({"launchplane": routing})
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)["launchplane"]
        self.assertEqual(summary["routing"], routing)
        self.assertFalse(summary["context"]["enabled"])
        self.assertIsNone(summary["context"]["helper"])
        self.assertEqual(summary["warnings"], [])
        result = self.snapshot({"launchplane": routing}, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("routingContext: example_context", result.stdout)
        self.assertNotIn("Cannot index", result.stderr)

    def test_helper_context(self):
        context = {"enabled": True, "helper": "tools/context.py"}
        result = self.snapshot({"launchplane": {"context": context}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["launchplane"]["context"], context)

    def test_invalid_context_is_diagnosed(self):
        result = self.snapshot({"launchplane": {"context": ["bad"]}})
        self.assertEqual(result.returncode, 0, result.stderr)
        warnings = json.loads(result.stdout)["launchplane"]["warnings"]
        self.assertTrue(any(warning["code"] == "invalid_launchplane_context" for warning in warnings))

    def test_invalid_launchplane_is_diagnosed(self):
        result = self.snapshot({"launchplane": "bad"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["launchplane"]["status"], "invalid")

    def test_projection_failure_is_not_success(self):
        result = self.snapshot({"cleanup": {"commands": [42]}})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot index", result.stderr)

    def test_policy_overrides_stale_metadata_and_absent_routing(self):
        for enrolled in (True, False):
            targets = [{"baseBranch": "release", "readyLabel": "ship"}] if enrolled else []
            policy = {
                "status": "available", "operation": "merge-train-policy-read",
                "result": {
                    "source": "launchplane", "enabled": enrolled,
                    "status": "enrolled" if enrolled else "not_enrolled",
                    "targets": targets,
                    "policy": {"record_id": "fixture-policy"},
                },
            }
            for metadata in ({}, helper_metadata(),
                             {"launchplane": {"mergeTrain": {"enabled": not enrolled,
                                                            "baseBranch": "stale", "readyLabel": "stale"}}}):
                result = self.snapshot(metadata, policy=policy)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["launchplane"]["mergeTrain"], policy["result"])
                if metadata == helper_metadata():
                    summary = json.loads(result.stdout)["launchplane"]
                    self.assertEqual(summary["service"], {
                        **metadata["launchplane"]["service"], "localConfigExample": None,
                    })
                    self.assertEqual(summary["context"], metadata["launchplane"]["context"])
                    self.assertEqual(summary["operator"], metadata["launchplane"]["operator"])
                    self.assertEqual(summary["warnings"], [])
                result = self.snapshot(metadata, text=True, policy=policy)
                self.assertIn(f'mergeTrainStatus: {policy["result"]["status"]}', result.stdout)
                self.assertIn(f'mergeTrainEnabled: {str(enrolled).lower()}', result.stdout)
                if enrolled:
                    for target in targets:
                        self.assertIn(f'mergeTrainTarget: {target["baseBranch"]} ({target["readyLabel"]})',
                                      result.stdout)

    def test_unavailable_or_invalid_policy_never_means_not_enrolled(self):
        for response, exit_code in ((None, 0), ({}, 0), ({"status": "unauthorized"}, 1)):
            for metadata in ({}, helper_metadata(),
                             *({"launchplane": {"mergeTrain": {"enabled": enabled,
                                                             "baseBranch": "stale", "readyLabel": "stale"}}}
                               for enabled in (True, False))):
                result = self.snapshot(metadata, policy=response, policy_exit=exit_code)
                summary = json.loads(result.stdout)["launchplane"]["mergeTrain"]
                self.assertEqual(summary["status"], "unknown")
                self.assertIsNone(summary["enabled"])
                self.assertEqual(summary["targets"], [])
                self.assertIsNone(summary["policy"])
                text_result = self.snapshot(
                    metadata, text=True, policy=response, policy_exit=exit_code,
                )
                self.assertIn("mergeTrainEnabled: unknown", text_result.stdout)
                self.assertNotIn("mergeTrainTarget:", text_result.stdout)


    def test_unknown_reason_drops_provider_prose(self):
        for reason, expected in (("authorization_denied", "authorization_denied"),
                                 ("fixture private diagnostic", "invalid_response")):
            response = {"status": "denied", "warnings": [{"code": reason}]}
            result = self.snapshot({}, policy=response, policy_exit=1)
            summary = json.loads(result.stdout)["launchplane"]["mergeTrain"]
            self.assertEqual(summary["reason"], expected)
            self.assertEqual(summary["status"], "unknown")
            self.assertIsNone(summary["enabled"])
            if reason != expected:
                self.assertNotIn(reason, result.stdout)

    def test_no_launchplane_skips_policy_read_without_a_failure(self):
        for text in (False, True):
            result = self.snapshot(None, text=text, private_config=False,
                                   policy={"status": "unauthorized"}, policy_exit=1)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(result.policy_called)
            if text:
                self.assertIn("mergeTrainStatus: not_configured", result.stdout)
                self.assertNotIn("mergeTrainReason:", result.stdout)
            else:
                summary = json.loads(result.stdout)["launchplane"]
                self.assertIsNone(summary["mergeTrain"].get("reason"))
                self.assertEqual(summary["mergeTrain"]["status"], "not_configured")
                self.assertFalse(summary["mergeTrain"]["enabled"])

    def test_configured_but_unreadable_never_takes_absent_path(self):
        for metadata, private_config, environment in (
            ({}, True, {}),
            ({}, False, {"LAUNCHPLANE_OPERATOR_URL": "https://example.invalid"}),
            ({}, False, {"LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": ""}),
            (helper_metadata(), False, {}),
            ({"launchplane": {"enabled": False}}, False, {}),
        ):
            for code in ("missing_service_url", "missing_operator_config", "authorization_denied"):
                with self.subTest(metadata=metadata, private_config=private_config, code=code):
                    result = self.snapshot(
                        metadata, private_config=private_config, launchplane_env=environment,
                        policy={"status": "unavailable", "warnings": [{"code": code}]}, policy_exit=1,
                    )
                    self.assertTrue(result.policy_called)
                    route = json.loads(result.stdout)["launchplane"]["mergeTrain"]
                    self.assertEqual(route["status"], "unknown")
                    self.assertIsNone(route["enabled"])

    def test_real_policy_helper_with_invalid_private_configuration_stays_unknown(self):
        result = self.snapshot({}, real_policy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        route = json.loads(result.stdout)["launchplane"]["mergeTrain"]
        self.assertEqual(route["status"], "unknown")
        self.assertIsNone(route["enabled"])
        helper = SCRIPT.parents[2] / "launchplane" / "scripts" / "launchplane-write-action.py"
        spec = importlib.util.spec_from_file_location("policy_helper_under_test", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "invalid.json"
            config.write_text("invalid configuration")
            with self.assertRaises(ValueError) as error:
                module.load_config(str(config))
        self.assertEqual(route["reason"], str(error.exception))

    def test_isolated_home_snapshot_never_takes_absent_path(self):
        result = self.snapshot({}, isolated_home=True,
                               policy={"status": "unavailable"}, policy_exit=1)
        self.assertTrue(result.policy_called)
        route = json.loads(result.stdout)["launchplane"]["mergeTrain"]
        self.assertEqual(route["status"], "unknown")
        self.assertIsNone(route["enabled"])

    def test_text_without_jq_keeps_local_snapshot_and_unknown_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            bins = root / "bin"
            bins.mkdir()
            for name in ("git", "dirname"):
                (bins / name).symlink_to(shutil.which(name))
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)], cwd=root,
                env={**os.environ, "PATH": str(bins), "GITHUB_REPO_SNAPSHOT_PYTHON": sys.executable,
                     "GITHUB_REPO_SNAPSHOT_GH": str(root / "missing-gh")},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("== Repository ==", result.stdout)
            self.assertIn("mergeTrainEnabled: unknown", result.stdout)


class ConfigurationTests(unittest.TestCase):
    def test_unreadable_private_directory_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(Path, "lstat", side_effect=PermissionError):
                self.assertEqual(configuration_status({}, home=home, environ={}, expected_home=home), "unknown")

    def test_broken_private_symlinks_do_not_prove_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".config").symlink_to(home / "missing")
            self.assertEqual(configuration_status({}, home=home, environ={}, expected_home=home), "unknown")
            (home / ".config").unlink()
            (home / ".config").mkdir()
            (home / ".config" / "launchplane").symlink_to(home / "missing")
            self.assertEqual(configuration_status({}, home=home, environ={}, expected_home=home), "configured")

    def test_isolated_or_missing_home_never_proves_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            isolated = home / "isolated"
            isolated.mkdir()
            for requested in (isolated, home / "missing"):
                self.assertEqual(configuration_status({}, home=requested, environ={}, expected_home=home), "unknown")

    def test_empty_private_directory_keeps_configured_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".config" / "launchplane").mkdir(parents=True)
            self.assertEqual(configuration_status({}, home=home, environ={}, expected_home=home), "configured")


if __name__ == "__main__":
    unittest.main()
