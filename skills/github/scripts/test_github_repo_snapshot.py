# /// script
# requires-python = ">=3.12"
# ///
"""Exercise the snapshot CLI offline with repository metadata fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("github-repo-snapshot.sh")


class SnapshotTests(unittest.TestCase):
    def snapshot(self, metadata, *, text=False, policy=None, policy_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            config = root / "metadata.json"
            config.write_text(json.dumps(metadata))
            policy_helper = root / "policy.py"
            if policy is not None:
                policy_helper.write_text(
                    "import sys\n"
                    f"print({json.dumps(json.dumps(policy))})\n"
                    f"sys.exit({policy_exit})\n"
                )
            return subprocess.run(
                ["bash", str(SCRIPT), "--config", str(config), *([] if text else ["--json"])],
                cwd=root,
                env={**os.environ, "GITHUB_REPO_SNAPSHOT_GH": str(root / "missing-gh"),
                     "GITHUB_REPO_SNAPSHOT_POLICY_HELPER": str(policy_helper)},
                capture_output=True, text=True, check=False,
            )

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
            policy = {
                "status": "available", "operation": "merge-train-policy-read",
                "result": {
                    "source": "launchplane", "enabled": enrolled,
                    "status": "enrolled" if enrolled else "not_enrolled",
                    "targets": [{"baseBranch": "release", "readyLabel": "ship"}] if enrolled else [],
                    "policy": {"record_id": "fixture-policy"},
                },
            }
            for metadata in ({}, {"launchplane": {"mergeTrain": {"enabled": not enrolled}}}):
                result = self.snapshot(metadata, policy=policy)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["launchplane"]["mergeTrain"], policy["result"])
                result = self.snapshot(metadata, text=True, policy=policy)
                self.assertIn(f'mergeTrainStatus: {policy["result"]["status"]}', result.stdout)
                self.assertIn(f'mergeTrainEnabled: {str(enrolled).lower()}', result.stdout)

    def test_unavailable_or_invalid_policy_never_means_not_enrolled(self):
        for response, exit_code in ((None, 0), ({}, 0), ({"status": "unauthorized"}, 1)):
            for enabled in (True, False):
                metadata = {"launchplane": {"mergeTrain": {"enabled": enabled}}}
                result = self.snapshot(metadata, policy=response, policy_exit=exit_code)
                summary = json.loads(result.stdout)["launchplane"]["mergeTrain"]
                self.assertEqual(summary["status"], "unknown")
                self.assertIsNone(summary["enabled"])
                self.assertIn("mergeTrainEnabled: unknown", self.snapshot(
                    metadata, text=True, policy=response, policy_exit=exit_code,
                ).stdout)


if __name__ == "__main__":
    unittest.main()
