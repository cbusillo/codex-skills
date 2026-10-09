#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run the updater's publication shell offline with real Bash and jq."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/update-pep723-dependencies.yml"


class PublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sha = "a" * 40
        self.record = {
            "author": {"is_bot": True},
            "baseRefName": "main",
            "body": "<!-- pep723-dependency-updater -->\nPrevious validation",
            "headRefName": "automation/pep723-dependencies",
            "headRefOid": self.sha,
            "headRepository": {"nameWithOwner": "fixture/catalog"},
            "number": 42,
        }
        workflow = yaml.safe_load(WORKFLOW.read_text())
        self.shell = next(
            step["run"]
            for step in workflow["jobs"]["update"]["steps"]
            if step.get("name") == "Create or refresh pull request"
        )
        self.environment = {
            **os.environ,
            "PATH": f"{self.root}:{os.environ['PATH']}",
            "TMPDIR": str(self.root),
            "FIXTURE_ROOT": str(self.root),
            "HEAD_SHA": self.sha,
            "AUTOMATION_BRANCH": self.record["headRefName"],
            "PR_MARKER": "<!-- pep723-dependency-updater -->",
            "GITHUB_REPOSITORY": "fixture/catalog",
            "GITHUB_SERVER_URL": "https://github.example.test",
            "VALIDATION_RUN_ID": "123",
        }
        self.write_command(
            "git",
            '''[[ "$*" == "ls-remote --heads origin refs/heads/$AUTOMATION_BRANCH" ]]
if [[ -f "$FIXTURE_ROOT/published" && -f "$FIXTURE_ROOT/final-sha" ]]; then
    cat "$FIXTURE_ROOT/final-sha"
else
    cat "$FIXTURE_ROOT/remote-sha"
fi
''',
        )
        self.write_command(
            "gh",
            '''[[ "$1" == pr ]]
operation="$2"
shift 2
case "$operation" in
    list) cat "$FIXTURE_ROOT/existing.json" ;;
    edit|create)
        printf '%s\\n' "$operation" >> "$FIXTURE_ROOT/writes"
        while [[ "$1" != --body-file ]]; do shift; done
        cp "$2" "$FIXTURE_ROOT/body"
        touch "$FIXTURE_ROOT/published"
        if [[ "$operation" == create ]]; then
            echo 'https://github.example.test/fixture/catalog/pull/42'
        fi
        ;;
    view)
        [[ "$1" == 42 ]]
        jq --rawfile body "$FIXTURE_ROOT/body" '.body = $body' "$FIXTURE_ROOT/readback.json"
        ;;
    *) exit 99 ;;
esac
''',
        )
        (self.root / "remote-sha").write_text(self.sha)

    def write_command(self, name: str, source: str) -> None:
        path = self.root / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + source)
        path.chmod(0o755)

    def run_publication(
        self, existing: dict | None, readback: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        (self.root / "existing.json").write_text(
            json.dumps(existing) if existing is not None else ""
        )
        (self.root / "readback.json").write_text(json.dumps(readback or self.record))
        return subprocess.run(
            ["bash", "-c", self.shell],
            cwd=self.root,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=15,
        )

    def test_refresh_and_create_read_back_validated_head_and_new_body(self) -> None:
        for existing, operation in ((self.record, "edit"), (None, "create")):
            with self.subTest(operation=operation):
                result = self.run_publication(existing)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / "writes").read_text(), operation + "\n")
                body = (self.root / "body").read_text()
                self.assertIn(self.environment["PR_MARKER"], body)
                self.assertIn(
                    "https://github.example.test/fixture/catalog/actions/runs/123", body
                )
                self.assertNotIn("Previous validation", body)
                (self.root / "writes").unlink()

    def test_refuses_unowned_or_unvalidated_existing_pr_before_writing(self) -> None:
        changes = {
            "author": {"is_bot": False},
            "baseRefName": "other",
            "body": "human PR",
            "headRefName": "human-branch",
            "headRefOid": "b" * 40,
            "headRepository": {"nameWithOwner": "other/catalog"},
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                result = self.run_publication({**self.record, field: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Refusing to edit", result.stderr)
                self.assertFalse((self.root / "writes").exists())

    def test_refuses_changed_remote_before_publication(self) -> None:
        (self.root / "remote-sha").write_text("b" * 40)
        result = self.run_publication(self.record)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed before PR publication", result.stderr)
        self.assertFalse((self.root / "writes").exists())

    def test_refuses_mismatched_published_head(self) -> None:
        result = self.run_publication(
            self.record, {**self.record, "headRefOid": "b" * 40}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Published PR does not match", result.stderr)

    def test_refuses_changed_remote_after_publication(self) -> None:
        (self.root / "final-sha").write_text("b" * 40)
        result = self.run_publication(self.record)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed during PR publication", result.stderr)


if __name__ == "__main__":
    unittest.main()
