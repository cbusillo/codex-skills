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
import subprocess
import sys
from pathlib import Path
from unittest import TestCase, main


SCRIPT_PATH = Path(__file__).with_name("resolve_person.py")
SPEC = importlib.util.spec_from_file_location("resolve_person", SCRIPT_PATH)
assert SPEC is not None
resolve_person = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(resolve_person)


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
