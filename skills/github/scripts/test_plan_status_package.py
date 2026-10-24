#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Build and import the committed planning contract as its native consumer does."""
from __future__ import annotations

import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


class InstalledContractTests(unittest.TestCase):
    def test_committed_wheel_contains_and_enforces_the_shared_contract(self):
        root = Path(__file__).resolve().parents[3]
        # Archive tracked immutable source, so ignored/untracked host files and
        # the editable import path cannot make a broken distribution pass.
        archive = subprocess.run([
            "git", "archive", "HEAD", "skills/github/scripts/pyproject.toml",
            "skills/github/scripts/github_plan_status",
        ], cwd=root, capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory(prefix=".plan-status-package-", dir=root) as directory:
            temporary = Path(directory)
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(temporary, filter="data")
            distribution = temporary / "dist"
            build = subprocess.run([
                "uv", "build", "--python", sys.executable,
                str(temporary / "skills/github/scripts"), "--out-dir", str(distribution),
            ], capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stderr)
            wheels = list(distribution.glob("*.whl"))
            self.assertEqual(len(wheels), 1, build.stdout)
            smoke = subprocess.run([
                "uv", "run", "--no-project", "--python", sys.executable,
                "--with", str(wheels[0]), "python", "-I", "-c", """
from importlib.resources import files
from github_plan_status import DEFAULT_STATUS_LABELS, derive, label_changes, waits
from github_plan_status.body import wrap_contributor_plan
from github_plan_status.workflow_runs import current_workflow_runs, workflow_execution_identity
from github_plan_status.client_test_notes import ENGINEERING_RELEASE_TESTS, body_client_test_notes, client_test_notes
from github_plan_status.references import linked_issues
pull = {'title': 'o/r#3: first slice', 'body': 'Refs #5 and o/x#6. See #9.'}
assert linked_issues('o/r', pull) == [('o/r', 3), ('o/r', 5), ('o/x', 6)]
assert current_workflow_runs([], 'head')['current'] == []
assert workflow_execution_identity({}) is None
notes = ENGINEERING_RELEASE_TESTS + '\\nCheck the imported parts at release.'
assert not client_test_notes(notes).review_required
assert client_test_notes(notes, review_override=True).review_required
assert client_test_notes('Approve the screen before merge.').review_required
body = '## Client test notes\\n' + notes + '\\n## Owner test notes\\nApprove the wording before merge.'
assert body_client_test_notes(body).review_required
body = '## Client test notes\\nNothing for the Client to test: tooling.\\n## Owner test notes\\nApprove wording.'
assert body_client_test_notes(body).review_required
assert not body_client_test_notes('## Client test notes\\n' + notes).review_required
held = waits.reviewed(evidence='Client requested acceptance',
                      waiting_for='Client acceptance', ends_when='Client accepts')
issue = {'state': 'open', 'user': {'id': 71, 'login': 'owner'},
         'labels': [DEFAULT_STATUS_LABELS['waiting']]}
issue['body'] = wrap_contributor_plan({'body': 'Original request'}, waits.marker(held))
record = waits.current(issue, [], lambda source: waits.author_is_trusted(source, 'owner', author_ids=(71,)))
derived = derive(issue, {'blocked_by': []}, record)
assert derived['status'] == 'waiting' and derived['waits'] == held['waits']
review = derive(issue, {'blocked_by': []}, waits.current({}, [], set()))
changes = label_changes(issue['labels'], DEFAULT_STATUS_LABELS, review)
assert not changes['add'] and not changes['remove'] and changes['result'] == issue['labels']
assert files('github_plan_status').joinpath('py.typed').is_file()
""",
            ], cwd=temporary, capture_output=True, text=True, timeout=120)
            self.assertEqual(smoke.returncode, 0, smoke.stderr)


if __name__ == "__main__":
    unittest.main()
