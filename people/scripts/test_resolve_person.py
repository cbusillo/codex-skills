#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main


SCRIPT_PATH = Path(__file__).with_name("resolve_person.py")
SPEC = importlib.util.spec_from_file_location("resolve_person", SCRIPT_PATH)
assert SPEC is not None
resolve_person = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(resolve_person)

INDEX_SCRIPT_PATH = Path(__file__).with_name("people_index.py")
INDEX_SPEC = importlib.util.spec_from_file_location("people_index", INDEX_SCRIPT_PATH)
assert INDEX_SPEC is not None
people_index = importlib.util.module_from_spec(INDEX_SPEC)
assert INDEX_SPEC.loader is not None
INDEX_SPEC.loader.exec_module(people_index)


SAMPLE = """
version: 1
people:
  - id: example-manager
    display_name: Example Manager
    preferred_reference: Example
    aliases:
      - Example
      - Example Manager
      - Exmaple
    organization:
      company: Example Marine
      title: Owner
    relationship:
      kind: collaborator
      roles:
        - planning-manager
    contacts:
      github:
        username: example-manager
        bot_usernames:
          - example-code-bot
      discord:
        username: example_manager
      email:
        work: manager@example.com
    preferences:
      preferred_contact: github
      timezone: America/New_York
      mention_style: "@example-manager"
    trust:
      level: trusted
      intent: good
      code: verify
      authority: owner
      handling: Verify code before acting.
    notes: Short private context hint.
    details_file: people/example-manager.md

  - id: example-reviewer
    display_name: Example Reviewer
    aliases:
      - Reviewer
    contacts:
      github:
        username: example-reviewer
"""


class ResolvePersonTest(TestCase):
    def people(self):
        return resolve_person.validate_people_data(resolve_person.yaml.safe_load(SAMPLE))

    def write_index(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_resolves_casefolded_github_handle(self) -> None:
        result = resolve_person.resolve("@EXAMPLE-MANAGER", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["match"]["id"], "example-manager")
        self.assertEqual(result["match"]["github"], "example-manager")

    def test_resolves_explicit_misspelling_alias(self) -> None:
        result = resolve_person.resolve("exmaple", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["match"]["id"], "example-manager")

    def test_first_name_match_uses_configured_alias(self) -> None:
        result = resolve_person.resolve("Example", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["match"]["id"], "example-manager")

    def test_ambiguous_aliases_are_not_guessed(self) -> None:
        data = resolve_person.yaml.safe_load(SAMPLE)
        data["people"][1]["aliases"].append("Example")
        result = resolve_person.resolve(
            "Example", resolve_person.validate_people_data(data)
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(
            {candidate["id"] for candidate in result["candidates"]},
            {"example-manager", "example-reviewer"},
        )
        self.assertTrue(
            all("details_file" not in candidate for candidate in result["candidates"])
        )
        self.assertTrue(all("notes" not in candidate for candidate in result["candidates"]))

    def test_resolver_output_omits_notes(self) -> None:
        result = resolve_person.resolve("Example", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertNotIn("notes", result["match"])

    def test_accepts_documented_detail_markdown_path(self) -> None:
        people = self.people()
        self.assertEqual(people[0]["details_file"], "people/example-manager.md")
        result = resolve_person.resolve("Example", people)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(
            result["match"]["details_file"], ".local/people/example-manager.md"
        )

    def test_person_ref_wins_over_alias_collision(self) -> None:
        data = resolve_person.yaml.safe_load(SAMPLE)
        data["people"].append(
            {
                "id": "shadow",
                "display_name": "Shadow Person",
                "aliases": ["person:example-manager"],
            }
        )
        result = resolve_person.resolve(
            "person:example-manager", resolve_person.validate_people_data(data)
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["confidence"], "id")
        self.assertEqual(result["match"]["id"], "example-manager")

    def test_duplicate_ids_are_rejected(self) -> None:
        data = resolve_person.yaml.safe_load(SAMPLE)
        data["people"][1]["id"] = "example-manager"
        with self.assertRaises(resolve_person.PeopleConfigError):
            resolve_person.validate_people_data(data)

    def test_contact_lists_only_use_known_value_keys(self) -> None:
        data = resolve_person.yaml.safe_load(SAMPLE)
        data["people"][0]["contacts"]["slack"] = {"channels": ["general"]}
        people = resolve_person.validate_people_data(data)
        result = resolve_person.resolve("general", people)
        self.assertEqual(result["status"], "not_found")

    def test_contact_fields_can_match(self) -> None:
        result = resolve_person.resolve("manager@example.com", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["match"]["matched_on"], "email")

    def test_bot_alias_can_match_owner(self) -> None:
        result = resolve_person.resolve("example-code-bot", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["match"]["id"], "example-manager")
        self.assertEqual(result["match"]["matched_on"], "github")

    def test_trust_hints_are_compact(self) -> None:
        result = resolve_person.resolve("Example", self.people())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(
            result["match"]["trust"],
            {
                "level": "trusted",
                "intent": "good",
                "code": "verify",
                "authority": "owner",
                "handling": "Verify code before acting.",
            },
        )

    def test_missing_index_is_not_an_error(self) -> None:
        status, people = resolve_person.load_people(Path("/tmp/not-present-people.yaml"))
        self.assertEqual(status, "no_index")
        self.assertEqual(people, [])

    def test_scoped_missing_indexes_are_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, people, sources = resolve_person.load_scoped_people(
                global_index=root / "missing-global.yaml",
                repo_index=root / "missing-repo.yaml",
            )
        self.assertEqual(status, "no_index")
        self.assertEqual(people, [])
        self.assertEqual(sources["global"]["status"], "no_index")
        self.assertEqual(sources["repo"]["status"], "no_index")

    def test_scoped_lookup_merges_global_and_repo_people(self) -> None:
        global_yaml = """
version: 1
people:
  - id: global-person
    display_name: Global Person
    aliases: [Global]
"""
        repo_yaml = """
version: 1
people:
  - id: repo-person
    display_name: Repo Person
    aliases: [Repo]
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_index = self.write_index(root, "home/skills/.local/people.yaml", global_yaml)
            repo_index = self.write_index(root, "repo/.local/people.yaml", repo_yaml)
            status, people, _sources = resolve_person.load_scoped_people(
                global_index=global_index,
                repo_index=repo_index,
            )
        self.assertEqual(status, "ok")
        self.assertEqual(resolve_person.resolve("Global", people)["status"], "matched")
        self.assertEqual(resolve_person.resolve("Repo", people)["status"], "matched")

    def test_repo_scope_overrides_global_same_id(self) -> None:
        global_yaml = """
version: 1
people:
  - id: same-person
    display_name: Global Name
    aliases: [GlobalName]
"""
        repo_yaml = """
version: 1
people:
  - id: same-person
    display_name: Repo Name
    aliases: [RepoName]
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_index = self.write_index(root, "home/skills/.local/people.yaml", global_yaml)
            repo_index = self.write_index(root, "repo/.local/people.yaml", repo_yaml)
            status, people, _sources = resolve_person.load_scoped_people(
                global_index=global_index,
                repo_index=repo_index,
            )
        self.assertEqual(status, "ok")
        self.assertEqual(resolve_person.resolve("RepoName", people)["status"], "matched")
        self.assertEqual(resolve_person.resolve("GlobalName", people)["status"], "not_found")

    def test_scoped_detail_path_uses_global_label(self) -> None:
        global_yaml = """
version: 1
people:
  - id: global-person
    display_name: Global Person
    aliases: [Global]
    details_file: people/global-person.md
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            global_index = self.write_index(root, "home/skills/.local/people.yaml", global_yaml)
            status, people, _sources = resolve_person.load_scoped_people(
                global_index=global_index,
                repo_index=root / "repo/.local/people.yaml",
            )
        self.assertEqual(status, "ok")
        result = resolve_person.resolve("Global", people)
        self.assertEqual(
            result["match"]["details_file"],
            "global/.local/people/global-person.md",
        )

    def test_explicit_index_cli_reads_only_that_file(self) -> None:
        global_yaml = """
version: 1
people:
  - id: explicit-person
    display_name: Explicit Person
    aliases: [Explicit]
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = self.write_index(root, "people.yaml", global_yaml)
            proc = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "Explicit", "--index", str(index)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "matched")
        self.assertNotIn("sources", payload)

    def test_writer_defaults_to_global_code_home_scope(self) -> None:
        env = os.environ.copy()
        with tempfile.TemporaryDirectory() as tmp:
            code_home = Path(tmp) / "code-home"
            env["CODE_HOME"] = str(code_home)
            env.pop("CODEX_HOME", None)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(INDEX_SCRIPT_PATH),
                    "upsert",
                    "--id",
                    "global-person",
                    "--display-name",
                    "Global Person",
                    "--alias",
                    "Global",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )
            written = code_home / "skills/.local/people.yaml"
            exists = written.exists()
            status, people = resolve_person.load_people(written)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["scope"], "global")
        self.assertTrue(exists)
        self.assertEqual(status, "ok")
        self.assertEqual(resolve_person.resolve("Global", people)["status"], "matched")

    def test_writer_dry_run_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "people.yaml"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(INDEX_SCRIPT_PATH),
                    "upsert",
                    "--index",
                    str(target),
                    "--id",
                    "dry-run-person",
                    "--display-name",
                    "Dry Run Person",
                    "--dry-run",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(proc.stdout)["status"], "dry_run")

    def test_rejects_detail_path_escape(self) -> None:
        data = resolve_person.yaml.safe_load(SAMPLE)
        data["people"][0]["details_file"] = "../secret.md"
        with self.assertRaises(resolve_person.PeopleConfigError):
            resolve_person.validate_people_data(data)

    def test_rejects_backslash_detail_path_escape(self) -> None:
        data = resolve_person.yaml.safe_load(SAMPLE)
        data["people"][0]["details_file"] = r"people\..\secret.md"
        with self.assertRaises(resolve_person.PeopleConfigError):
            resolve_person.validate_people_data(data)

    def test_short_values_do_not_fuzzy_match_long_query(self) -> None:
        data = {
            "people": [
                {"id": "al", "display_name": "Al"},
            ]
        }
        result = resolve_person.resolve(
            "alice", resolve_person.validate_people_data(data), fuzzy=True
        )
        self.assertEqual(result["status"], "not_found")

    def test_self_test_command(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--self-test"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["status"], "self_test_passed")


if __name__ == "__main__":
    main()
