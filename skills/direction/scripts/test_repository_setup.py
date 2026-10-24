#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import direction_audit as audit
import repository_setup as setup

STANDARD = [{"name": name, "target": "branch", "enforcement": "active"}
            for name in audit.github_rulesets.required_ruleset_names()]


def project_fixture_toml(directory: Path) -> str:
    return f'[projects.{json.dumps(str(directory))}]\ntrust_level = "trusted"\n'


class FakeOwner:
    def __init__(self, root: Path) -> None:
        self.developer = root / "Developer"
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir()
        self.selection = "all"
        self.repos = {"o/ready": {}, "o/old": {"archived": True}}
        self.enrolled = {"o/ready", "o/old"}
        self.files = {"o/ready": {"": ["renovate.json"]}}
        self.rulesets: dict[str, Any] = {"o/ready": STANDARD}
        self.failing: set[str] = set()
        self.owner_login = False
        self.codex_projects: set[str] = set()
        for name in ("ready", "old"):
            self.add_checkout(name, in_codex=True)

    def add_checkout(self, name: str, *, in_codex: bool) -> None:
        (self.developer / name / ".git").mkdir(parents=True)
        (self.developer / name / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        if in_codex:
            self.codex_projects.add(name)
        (self.codex_home / "config.toml").write_text("".join(
            project_fixture_toml(self.developer / item) for item in sorted(self.codex_projects)))

    def fetch(self, args: list[str]) -> Any:
        path = args[1]
        repo = "/".join(path.split("/")[1:3])
        if path.startswith("installation/repositories") and self.owner_login:
            raise audit.AuditError("gh: This endpoint requires an installation token (HTTP 403)")
        if path.startswith("user/repos"):
            path = "installation/repositories"
        if path.startswith("installation/repositories"):
            listing = [{"full_name": name, "owner": {"login": "o"}, "archived": False, **meta}
                       for name, meta in self.repos.items()]
            return listing if self.owner_login else {"repositories": listing, "repository_selection": self.selection}
        if repo in self.failing:
            raise audit.AuditError("gh: HTTP 502 Bad Gateway")
        if "/rulesets?" in path:
            if self.rulesets.get(repo) == "plan":
                raise audit.AuditError("gh: Upgrade to GitHub Pro or make this repository public (HTTP 403)")
            return self.rulesets.get(repo, [])
        if "/contents/" in path:
            folder = path.split("/contents/", 1)[1]
            names = self.files.get(repo, {}).get(folder)
            if names is None:
                raise audit.ReferenceNotFound("gh: Not Found (HTTP 404)")
            return [{"name": name} for name in names]
        raise AssertionError(f"unexpected read {path}")

    def collect(self) -> dict[str, Any]:
        return setup.collect(
            "o", fetch=self.fetch, developer=self.developer,
            codex_homes=[{"name": "codex-a", "env": {"CODEX_HOME": str(self.codex_home)}}],
            policy_reader=lambda repo: {"status": "enrolled" if repo in self.enrolled else "not_enrolled", "targets": []},
        )


class RepositorySetupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.account = FakeOwner(Path(directory.name))

    def gaps(self) -> set[tuple[str | None, str, str]]:
        return {(row["repo"], row["setup"], row["fixer"]) for row in self.account.collect()["gaps"]}

    def add_new_repository(self, **meta: Any) -> None:
        self.account.repos["o/new"] = meta
        self.account.add_checkout("new", in_codex=True)
        self.account.enrolled.add("o/new")
        self.account.files["o/new"] = {"": ["README.md", ".github"], ".github": ["renovate.json5"]}
        self.account.rulesets["o/new"] = STANDARD

    def test_fully_set_up_account_reports_one_short_line(self) -> None:
        self.add_new_repository()
        result = self.account.collect()
        self.assertEqual((result["gaps"], result["coverage_gaps"]), ([], []))
        self.assertEqual(setup.markdown(result), "Repository setup: nothing missing across 2 repositories (complete).\n")

    def test_new_repository_without_any_setup_reports_every_gap_with_its_fixer(self) -> None:
        self.account.repos["o/new"] = {}
        self.account.files["o/new"] = {"": ["README.md"]}
        self.assertEqual(self.gaps(), {
            ("o/new", "merge_train", "agent"), ("o/new", "renovate", "agent"),
            ("o/new", "rulesets", "agent"), ("o/new", "checkout", "agent"),
        })

    def test_checkout_without_codex_trust_is_left_to_the_director(self) -> None:
        self.add_new_repository()
        self.account.codex_projects.discard("new")
        self.account.add_checkout("other", in_codex=False)
        self.assertEqual(self.gaps(), {("o/new", "codex_trust", "Director")})

    def test_one_missing_ruleset_of_the_pair_is_named(self) -> None:
        self.add_new_repository()
        self.account.rulesets["o/new"] = STANDARD[:1]
        [row] = self.account.collect()["gaps"]
        self.assertEqual((row["setup"], row["detail"]), ("rulesets", STANDARD[1]["name"]))

    def test_private_repository_without_rulesets_on_its_plan_is_left_to_the_director(self) -> None:
        self.add_new_repository()
        self.account.rulesets["o/new"] = "plan"
        self.assertEqual(self.gaps(), {("o/new", "rulesets_unavailable", "Director")})

    def test_app_installed_on_selected_repositories_is_left_to_the_director(self) -> None:
        self.account.selection = "selected"
        result = self.account.collect()
        self.assertIn((None, "app", "Director"), self.gaps())
        self.assertFalse(result["complete"])

    def test_owner_login_cannot_see_the_app_so_its_installation_is_unread(self) -> None:
        self.add_new_repository()
        self.account.owner_login = True
        result = self.account.collect()
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["coverage_gaps"], [{"source": "app", "reason": "unavailable"}])

    def test_unreadable_repository_root_is_unread_not_missing_renovate(self) -> None:
        self.add_new_repository()
        del self.account.files["o/new"]
        result = self.account.collect()
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["coverage_gaps"], [{"repo": "o/new", "source": "renovate", "reason": "unavailable"}])

    def test_unreadable_repository_is_incomplete_not_set_up(self) -> None:
        self.add_new_repository()
        self.account.failing.add("o/new")
        result = self.account.collect()
        self.assertFalse(result["complete"])
        self.assertEqual({row["source"] for row in result["coverage_gaps"]}, {"renovate", "rulesets"})
        self.assertIn("INCOMPLETE", setup.markdown(result))

    def test_unknown_enrollment_is_incomplete_not_a_gap(self) -> None:
        result = setup.collect("o", fetch=self.account.fetch, developer=self.account.developer, codex_homes=[],
                               policy_reader=lambda _repo: None)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["coverage_gaps"], [{"repo": "o/ready", "source": "merge_train", "reason": "unavailable"}])

    def test_report_marks_forks_and_names_each_fix_once(self) -> None:
        self.account.repos["o/new"] = {"fork": True}
        self.account.repos["o/other"] = {}
        self.account.files.update({"o/new": {"": []}, "o/other": {"": []}})
        text = setup.markdown(self.account.collect())
        self.assertIn("- o/new (fork): merge train, Renovate, ruleset pair, no checkout\n", text)
        self.assertEqual(text.count(setup.SETUPS["renovate"].fix), 1)


if __name__ == "__main__":
    unittest.main()
