#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "packaging==26.3",
# ]
# ///
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import MappingProxyType
from collections.abc import Callable, Mapping
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

import pep723_proposal as proposal
from pep723_metadata import normalize_package_name
import update_pep723_dependencies as updater


SCRIPT = Path(__file__).with_name("pep723_proposal.py")
FIXTURE_SCRIPT_MARKER = "# /// script"
WORKFLOW_REF = "fixture/catalog/.github/workflows/update-pep723-dependencies.yml@refs/heads/main"
NEW_VERSIONS = MappingProxyType({"packaging": "26.3", "pyyaml": "6.0.3"})


def script_source(dependencies: list[str], body: str = 'print("ok")\n', *, extra: str = "") -> str:
    listed = "".join(f'#     "{value}",\n' for value in dependencies)
    return (
        "#!/usr/bin/env -S uv run --script\n"
        f"{FIXTURE_SCRIPT_MARKER}\n"
        '# requires-python = ">=3.12"\n'
        f"# dependencies = [\n{listed}# ]\n"
        f"{extra}"
        "# ///\n"
        f"{body}"
    )


def git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.test",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.test",
    )
    return environment


class ProposalBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "repository"
        self.root.mkdir()
        self.marker = self.temporary / "candidate-code-ran"
        self.write(".github/github.json", '{"defaultBranch": "main"}\n')
        self.write(
            "tools/runner.py",
            script_source(
                ["packaging==26.2", "PyYAML==6.0.2"],
                f"open({str(self.marker)!r}, 'w').close()\n",
            ),
            executable=True,
        )
        self.write(
            "tools/reader.py",
            script_source(["PyYAML==6.0.2"], extra='# [tool.fixture]\n# owner = "team"\n'),
        )
        self.write("tools/plain.py", "print('no metadata')\n")
        self.write("README.md", "fixture\n")
        self.base_sha = self.commit("base")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            env=git_environment(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(self, relative: str, content: str, *, executable: bool = False) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)
        return path

    def commit(self, message: str) -> str:
        if not (self.root / ".git").exists():
            self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def run_updater(self, versions: Mapping[str, str] = NEW_VERSIONS) -> tuple[Path, ...]:
        changed, _ = updater.update_repository(
            self.root,
            dry_run=False,
            resolver=lambda requirements: {name: versions[name] for name in requirements},
        )
        return changed

    def package(self) -> tuple[Path, str]:
        directory = self.temporary / f"candidate-{len(list(self.temporary.iterdir()))}"
        digest = proposal.package_candidate(self.context, directory, self.root)
        self.assertIsNotNone(digest)
        assert digest is not None
        return directory, digest

    def trusted_checkout(self) -> None:
        self.git("checkout", "-q", "--", ".")
        self.git("clean", "-qfdx")

    def updated_candidate(self) -> tuple[dict[str, Any], dict[str, str]]:
        self.run_updater()
        expected = {
            "tools/runner.py": (self.root / "tools/runner.py").read_text(encoding="utf-8"),
            "tools/reader.py": (self.root / "tools/reader.py").read_text(encoding="utf-8"),
        }
        directory, _ = self.package()
        self.trusted_checkout()
        candidate = json.loads((directory / proposal.CANDIDATE_FILE).read_text(encoding="utf-8"))
        return candidate, expected

    def write_candidate(self, candidate: Any, *, raw: str | None = None) -> tuple[Path, str]:
        directory = self.temporary / f"artifact-{len(list(self.temporary.iterdir()))}"
        directory.mkdir()
        data = (raw if raw is not None else json.dumps(candidate)).encode(errors="surrogatepass")
        (directory / proposal.CANDIDATE_FILE).write_bytes(data)
        return directory, hashlib.sha256(data).hexdigest()

    def prepare(self, candidate: Any) -> proposal.PreparedRequest:
        directory, digest = self.write_candidate(candidate)
        return proposal.prepare_request(self.context, directory, digest, self.root)

    def assert_refused(self, action: Callable[[], object], fragment: str) -> None:
        with self.assertRaises(proposal.ProposalRefused) as caught:
            action()
        self.assertIn(fragment, str(caught.exception))

    def enter_repository(self) -> None:
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)

    def submit_environment(self, **overrides: str) -> dict[str, str]:
        environment = {
            "PATH": os.environ["PATH"],
            "GITHUB_REPOSITORY": "fixture/catalog",
            "GITHUB_SHA": self.base_sha,
            "GITHUB_RUN_ID": "41",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": WORKFLOW_REF,
        }
        environment.update(overrides)
        return environment

    def test_updater_candidate_becomes_the_exact_request_without_dependencies(self) -> None:
        self.run_updater()
        updated = {
            path: (self.root / path).read_text(encoding="utf-8")
            for path in ("tools/runner.py", "tools/reader.py")
        }
        output = self.temporary / "package-output"
        directory = self.temporary / "candidate"
        environment = self.submit_environment(GITHUB_OUTPUT=str(output))
        self.enter_repository()
        self.assertEqual(proposal.main(["package", str(directory)], environment), 0)
        digest = output.read_text(encoding="utf-8").split("digest=")[1].strip()
        self.assertIn("changed=true", output.read_text(encoding="utf-8"))
        self.trusted_checkout()

        request_file = self.temporary / "request.json"
        without_site_packages = "-S"
        result = subprocess.run(
            [
                sys.executable,
                "-E",
                without_site_packages,
                str(SCRIPT),
                "prepare",
                str(directory),
                str(request_file),
            ],
            cwd=self.root,
            env=self.submit_environment(EXPECTED_DIGEST=digest, CANDIDATE_RUN_ATTEMPT="2"),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        request = json.loads(request_file.read_text(encoding="utf-8"))
        self.assertEqual(request["repository"], "fixture/catalog")
        self.assertEqual(request["base_sha"], self.base_sha)
        self.assertEqual({item["path"]: item["content"] for item in request["files"]}, updated)
        self.assertTrue(all(set(item) == {"path", "content"} for item in request["files"]))
        self.assertIn("6.0.2 → 6.0.3", request["body"])
        self.assertIn("26.2 → 26.3", request["body"])
        self.assertFalse(self.marker.exists())

    def test_rerunning_submit_resends_the_identical_request(self) -> None:
        candidate, _ = self.updated_candidate()
        directory, digest = self.write_candidate(candidate)
        requests = []
        for submit_attempt in ("2", "3"):
            request_file = self.temporary / f"request-{submit_attempt}.json"
            environment = self.submit_environment(
                GITHUB_RUN_ATTEMPT=submit_attempt,
                CANDIDATE_RUN_ATTEMPT="2",
                EXPECTED_DIGEST=digest,
            )
            self.enter_repository()
            self.assertEqual(
                proposal.main(["prepare", str(directory), str(request_file)], environment), 0
            )
            requests.append(request_file.read_bytes())
        self.assertEqual(requests[0], requests[1])
        for calculated_in in ("", "0", "4"):
            with self.subTest(calculated_in=calculated_in):
                self.assert_refused(
                    partial(
                        proposal.main,
                        ["prepare", str(directory), str(self.temporary / "never.json")],
                        self.submit_environment(
                            GITHUB_RUN_ATTEMPT="3",
                            CANDIDATE_RUN_ATTEMPT=calculated_in,
                            EXPECTED_DIGEST=digest,
                        ),
                    ),
                    "no attempt of this run",
                )

    def test_no_dependency_change_packages_nothing(self) -> None:
        self.run_updater({"packaging": "26.2", "pyyaml": "6.0.2"})
        output = self.temporary / "package-output"
        directory = self.temporary / "candidate"
        environment = self.submit_environment(GITHUB_OUTPUT=str(output))
        self.enter_repository()
        self.assertEqual(proposal.main(["package", str(directory)], environment), 0)
        self.assertIn("changed=false", output.read_text(encoding="utf-8"))
        self.assertFalse(directory.exists())

    def test_edits_beyond_dependency_pins_are_refused(self) -> None:
        candidate, expected = self.updated_candidate()
        runner = expected["tools/runner.py"]
        reader = expected["tools/reader.py"]
        edits = {
            "code": ("tools/runner.py", runner + "import os\n", "outside the rendered"),
            "shebang": (
                "tools/runner.py",
                runner.replace("uv run --script", "uv run --script --with evil", 1),
                "outside the rendered",
            ),
            "metadata": (
                "tools/reader.py",
                reader.replace('owner = "team"', 'owner = "other"'),
                "outside the rendered",
            ),
            "requires-python": (
                "tools/reader.py",
                reader.replace(">=3.12", ">=3.11"),
                "outside the rendered",
            ),
            "added package": (
                "tools/reader.py",
                reader.replace('"PyYAML==6.0.3",', '"PyYAML==6.0.3",\n#     "requests==2.0",'),
                "adds or removes",
            ),
            "removed package": (
                "tools/runner.py",
                runner.replace('#     "packaging==26.3",\n', ""),
                "adds or removes",
            ),
            "renamed package": ("tools/reader.py", reader.replace("PyYAML", "pyyaml"), "renames"),
            "url requirement": (
                "tools/reader.py",
                reader.replace("PyYAML==6.0.3", "PyYAML @ https://example.test/p.whl"),
                "exact stable pin",
            ),
            "non-canonical version": (
                "tools/reader.py",
                reader.replace("6.0.3", "6.0.03"),
                "exact stable pin",
            ),
            "prerelease": (
                "tools/reader.py",
                reader.replace("6.0.3", "7.0rc1"),
                "exact stable pin",
            ),
            "local version": (
                "tools/reader.py",
                reader.replace("6.0.3", "6.0.3+evil"),
                "exact stable pin",
            ),
            "lower bound": (
                "tools/reader.py",
                reader.replace("==6.0.3", ">=6.0.3"),
                "exact stable pin",
            ),
            "inconsistent": ("tools/reader.py", reader.replace("6.0.3", "6.0.4"), "pinned to both"),
            "unchanged": ("tools/plain.py", "print('no metadata')\n", "unchanged"),
            "no metadata": ("tools/plain.py", "print('changed')\n", "no PEP 723 dependencies"),
        }
        for name, (path, content, fragment) in edits.items():
            with self.subTest(name):
                tampered = {**candidate, "files": {**candidate["files"], path: content}}
                self.assert_refused(partial(self.prepare, tampered), fragment)
        with self.subTest("omitted file"):
            omitted = {**candidate, "files": {"tools/runner.py": runner}}
            self.assert_refused(partial(self.prepare, omitted), "pinned to both")
        with self.subTest("reordered dependencies"):
            reordered = runner.replace(
                '#     "packaging==26.3",\n#     "PyYAML==6.0.3",\n',
                '#     "PyYAML==6.0.3",\n#     "packaging==26.3",\n',
            )
            self.assertNotEqual(reordered, runner)
            tampered = {**candidate, "files": {**candidate["files"], "tools/runner.py": reordered}}
            self.assert_refused(partial(self.prepare, tampered), "updater's order")

    def test_paths_outside_tracked_regular_python_files_are_refused(self) -> None:
        (self.root / "tools/link.py").symlink_to("plain.py")
        self.write("docs/notes.py", "x = 1\n")
        self.base_sha = self.commit("symlink and file")
        self.git("rm", "-q", "docs/notes.py")
        self.commit("remove notes")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")
        candidate, expected = self.updated_candidate()
        content = expected["tools/reader.py"]
        for path in (
            "../outside.py",
            "/etc/passwd.py",
            "tools/../tools/reader.py",
            "tools/new.py",
            "tools/link.py",
            "docs/notes.py",
            ".github/github.json",
            ".github/workflows/update-pep723-dependencies.yml",
        ):
            with self.subTest(path):
                tampered = {**candidate, "files": {**candidate["files"], path: content}}
                self.assert_refused(partial(self.prepare, tampered), "not an existing tracked")

    def test_artifact_must_be_this_runs_exact_data(self) -> None:
        candidate, _ = self.updated_candidate()
        for field, value in (
            ("repository", "other/catalog"),
            ("base_sha", "f" * 40),
            ("run_id", "40"),
            ("run_attempt", "1"),
            ("schema_version", 2),
        ):
            with self.subTest(field):
                self.assert_refused(
                    partial(self.prepare, {**candidate, field: value}), "exact run and base"
                )
        self.assert_refused(partial(self.prepare, {**candidate, "mode": "100755"}), "exact run")
        self.assert_refused(partial(self.prepare, [candidate]), "JSON object")
        self.assert_refused(
            partial(self.prepare, {**candidate, "files": {}}),
            f"between 1 and {proposal.ROUTE_FILE_LIMIT}",
        )
        surrogate = {**candidate, "files": {**candidate["files"], "tools/reader.py": "\udc80"}}
        self.assert_refused(partial(self.prepare, surrogate), "not valid UTF-8")

        directory, digest = self.write_candidate(candidate)
        self.assert_refused(
            partial(proposal.prepare_request, self.context, directory, "0" * 64, self.root),
            "not the candidate",
        )
        (directory / "extra.py").write_text("print('extra')\n", encoding="utf-8")
        self.assert_refused(
            partial(proposal.prepare_request, self.context, directory, digest, self.root),
            "only candidate.json",
        )
        linked, linked_digest = self.write_candidate(candidate)
        target = self.temporary / "elsewhere.json"
        (linked / proposal.CANDIDATE_FILE).rename(target)
        (linked / proposal.CANDIDATE_FILE).symlink_to(target)
        self.assert_refused(
            partial(proposal.prepare_request, self.context, linked, linked_digest, self.root),
            "regular file",
        )
        text = json.dumps(candidate)
        for raw, fragment in (
            ('{"run_id": "41", ' + text[1:], "repeats a JSON key"),
            (text.replace('"schema_version": 1', '"schema_version": NaN'), "non-JSON constant"),
        ):
            with self.subTest(fragment):
                directory, digest = self.write_candidate(None, raw=raw)
                self.assert_refused(
                    partial(proposal.prepare_request, self.context, directory, digest, self.root),
                    fragment,
                )

    def test_submission_requires_a_clean_checkout_of_the_run_commit(self) -> None:
        candidate, _ = self.updated_candidate()
        self.write("tools/reader.py", "print('tampered')\n")
        self.assert_refused(partial(self.prepare, candidate), "not clean")
        self.trusted_checkout()
        self.write("tools/later.py", "x = 2\n")
        self.commit("later")
        self.assert_refused(partial(self.prepare, candidate), "does not match the run commit")

    def test_package_refuses_workspace_changes_outside_pin_edits(self) -> None:
        cases: dict[str, Callable[[], object]] = {
            "untracked": partial(self.write, "tools/new.py", "x = 1\n"),
            "non-python": partial(self.write, "README.md", "changed\n"),
            "deleted": lambda: (self.root / "tools/plain.py").unlink(),
            "mode": lambda: (self.root / "tools/reader.py").chmod(0o755),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.trusted_checkout()
                self.run_updater()
                change()
                self.assert_refused(
                    partial(
                        proposal.package_candidate,
                        self.context,
                        self.temporary / f"out-{name}",
                        self.root,
                    ),
                    "tracked",
                )

    def test_submit_refuses_runs_outside_the_default_branch_updater(self) -> None:
        for overrides in (
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "push"},
            {"GITHUB_REF": "refs/heads/feature"},
            {"GITHUB_WORKFLOW_REF": WORKFLOW_REF.replace("update-pep723", "other")},
        ):
            with self.subTest(overrides):
                self.assert_refused(
                    partial(
                        proposal.require_default_branch_workflow,
                        self.submit_environment(**overrides),
                        self.base_sha,
                        self.root,
                    ),
                    "default branch",
                )
        proposal.require_default_branch_workflow(
            self.submit_environment(GITHUB_EVENT_NAME="workflow_dispatch"),
            self.base_sha,
            self.root,
        )

    def assert_refused_when_packaged_and_when_hand_built(self, fragment: str) -> None:
        changed = self.run_updater()
        self.assert_refused(
            partial(
                proposal.package_candidate, self.context, self.temporary / "oversize", self.root
            ),
            fragment,
        )
        files = {
            path.relative_to(self.root).as_posix(): path.read_text(encoding="utf-8")
            for path in changed
        }
        self.trusted_checkout()
        candidate = {
            "schema_version": 1,
            **self.context.as_candidate_fields(),
            "files": files,
        }
        self.assert_refused(partial(self.prepare, candidate), fragment)

    def test_large_scripts_fit_until_the_route_limits(self) -> None:
        filler = "".join(f"value_{index:06d} = {index}\n" for index in range(18_000))
        self.write("tools/large.py", script_source(["PyYAML==6.0.2"], filler))
        self.base_sha = self.commit("large script")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")
        self.assertGreater(len(filler), 350_000)
        candidate, _ = self.updated_candidate()
        prepared = self.prepare(candidate)
        self.assertEqual(prepared.changed_files, 3)
        self.assertLessEqual(len(prepared.payload), proposal.ROUTE_BODY_LIMIT)

        self.git("rm", "-q", "tools/large.py")
        json_doubles_these_lines = '"\\\\"\n' * 54_000
        for index in range(2):
            self.write(
                f"tools/quoted_{index}.py",
                script_source(["PyYAML==6.0.2"], json_doubles_these_lines),
            )
        self.base_sha = self.commit("escaped scripts")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")
        self.assert_refused_when_packaged_and_when_hand_built("The request is")

        for index in range(3):
            self.write(f"tools/quoted_{index}.py", script_source(["PyYAML==6.0.2"], "x" * 360_000))
        self.base_sha = self.commit("wide scripts")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")
        self.assert_refused_when_packaged_and_when_hand_built("old-plus-new bytes")

    def test_file_count_is_accepted_up_to_the_route_limit(self) -> None:
        fixture_scripts_changed = 2
        for index in range(proposal.ROUTE_FILE_LIMIT - fixture_scripts_changed):
            self.write(f"tools/many_{index:02d}.py", script_source(["PyYAML==6.0.2"]))
        self.base_sha = self.commit("scripts up to the limit")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")
        candidate, _ = self.updated_candidate()
        self.assertEqual(self.prepare(candidate).changed_files, proposal.ROUTE_FILE_LIMIT)

        self.write("tools/one_more.py", script_source(["PyYAML==6.0.2"]))
        self.base_sha = self.commit("one script over the limit")
        self.context = proposal.RunContext("fixture/catalog", self.base_sha, "41", "2")
        self.run_updater()
        self.assert_refused(
            partial(proposal.package_candidate, self.context, self.temporary / "out", self.root),
            f"between 1 and {proposal.ROUTE_FILE_LIMIT}",
        )


class SummaryTests(unittest.TestCase):
    repository = "fixture/catalog"

    def response(self, **overrides: Any) -> dict[str, Any]:
        response = {
            "status": "ok",
            "disposition": "created",
            "repository": self.repository,
            "branch": "launchplane/updater/" + "a" * 64 + "/" + "b" * 64,
            "head_sha": "c" * 40,
            "pull_request_number": 7,
            "pull_request_url": f"https://github.com/{self.repository}/pull/7",
        }
        response.update(overrides)
        return response

    def summarize(
        self, response: Any, status: str = "201", outcome: str = "success", **outputs: str
    ) -> tuple[bool, str]:
        source = response if isinstance(response, dict) else {}
        observed = {
            field: str(source.get(field, ""))
            for field in ("disposition", "pull_request_url", "head_sha")
        }
        observed.update(outputs)
        return proposal.summarize_response(
            response, self.repository, status, outcome, observed, "d" * 64
        )

    def test_confirmed_dispositions_link_the_returned_proposal(self) -> None:
        for disposition in ("created", "replayed", "pending"):
            with self.subTest(disposition):
                confirmed, report = self.summarize(self.response(disposition=disposition))
                self.assertTrue(confirmed)
                self.assertIn(f"https://github.com/{self.repository}/pull/7", report)
                self.assertIn("c" * 40, report)
                self.assertIn("d" * 64, report)
        _, pending = self.summarize(self.response(disposition="pending"))
        self.assertIn("not applied", pending)

    def test_malformed_or_inconsistent_responses_stay_unconfirmed(self) -> None:
        cases = {
            "status": self.response(status="error"),
            "repository": self.response(repository="other/catalog"),
            "disposition": self.response(disposition="updated"),
            "head": self.response(head_sha="short"),
            "boolean number": self.response(pull_request_number=True),
            "url": self.response(pull_request_url="https://example.test/pull/7"),
            "branch": self.response(branch="automation/pep723-dependencies"),
            "missing": None,
        }
        for name, response in cases.items():
            with self.subTest(name):
                confirmed, report = self.summarize(response)
                self.assertFalse(confirmed)
                self.assertIn("d" * 64, report)
        confirmed, _ = self.summarize(self.response(), disposition="replayed")
        self.assertFalse(confirmed)

    def test_failures_keep_the_request_identity_and_name_the_next_step(self) -> None:
        error = {"error": {"code": "authorization_denied"}, "trace_id": "trace-1"}
        for status, outcome, fragment in (
            ("403", "failure", "denied"),
            ("409", "failure", "conflict"),
            ("503", "failure", "unconfirmed"),
            ("", "failure", "unconfirmed"),
            ("201", "failure", "unconfirmed"),
        ):
            with self.subTest(status=status, outcome=outcome):
                confirmed, report = self.summarize(error, status, outcome)
                self.assertFalse(confirmed)
                self.assertIn(fragment, report)
                self.assertIn("d" * 64, report)
        _, report = self.summarize(error, "403", "failure")
        self.assertIn("trace-1", report)


class StandardLibrarySemanticsTests(unittest.TestCase):
    def test_exact_pin_matches_packaging_canonical_stable_versions(self) -> None:
        versions = [
            "0",
            "1",
            "1.0",
            "1.0.0",
            "26.3",
            "2.25",
            "1!2.0",
            "0!1.0",
            "1.0.post1",
            "1.0.post0",
            "1.0post1",
            "1.0-1",
            "01.0",
            "1.00",
            "1.0.0.0.0",
            "1.0a1",
            "1.0rc1",
            "1.0.dev1",
            "1.0.post1.dev2",
            "1.0+local",
            "v1.0",
            "1.0.0-alpha",
            "1_0",
            "",
            "1.",
        ]
        for version in versions:
            with self.subTest(version):
                try:
                    parsed = Version(version)
                    expected = (
                        str(parsed) == version
                        and parsed.local is None
                        and not parsed.is_prerelease
                        and not parsed.is_devrelease
                    )
                except InvalidVersion:
                    expected = False
                matched = proposal.EXACT_PIN.fullmatch(f"PyYAML=={version}") is not None
                self.assertEqual(matched, expected)

    def test_name_normalization_matches_packaging(self) -> None:
        for name in ("PyYAML", "tree-sitter", "tree_sitter", "Zope.Interface", "a-_.b", "A__B"):
            with self.subTest(name):
                self.assertEqual(normalize_package_name(name), canonicalize_name(name))


if __name__ == "__main__":
    unittest.main()
