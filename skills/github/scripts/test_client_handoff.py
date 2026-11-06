#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import copy
import importlib
import sys
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from test_ready_handoff import PR, REPO, Reader, helper

acceptance = importlib.import_module("skills.launchplane.scripts.launchplane_acceptance")
lp = acceptance.operator

CLIENT_NOTES = "## Client test notes\nTry the new export button on the parts list."
NOTHING_TO_TEST = "## Client test notes\nNothing for the Client to test: internal tooling."


class FakeLaunchplane:
    def __init__(self):
        self.repositories: list[Any] = []
        self.ambiguous: Any = []
        self.profiles: dict[str, dict[str, Any]] = {}
        self.failure: Optional[Exception] = None

    def product(self, repository, product, *, client="client", release="held", driver="generic-web", lanes=()):
        self.repositories.append({"repository": repository, "classification": "managed_runtime",
                                  "product": product, "driver_id": driver, "source": "product_profile"})
        self.profiles[product] = {
            "product": product, "repository": repository, "production_use": "live",
            "lifecycle_state": "active", "release_on_acceptance": release, "driver_id": driver,
            "owner": {"github_login": client} if client else None,
            "lanes": [{"context": f"{product}-{lane}", "instance": lane} for lane in lanes],
            "updated_at": "2026-01-01T00:00:00Z",
        }

    def read(self, **kwargs):
        if self.failure:
            raise self.failure
        path = kwargs["path"]
        if path == lp.helper_command_path("repo-product-mapping-read"):
            mapping = {"generated_at": "now", "repositories": copy.deepcopy(self.repositories)}
            if self.ambiguous:
                mapping["ambiguous_repositories"] = self.ambiguous
            return {"mapping": mapping}
        for product, profile in self.profiles.items():
            if path == lp.helper_command_path("product-profile-read").format(product=product):
                return {"status": "ok", "trace_id": "fixture", "profile": copy.deepcopy(profile)}
            for lane in ("prod", "testing"):
                if path == lp.helper_command_path("product-environment-read").format(product=product, environment=lane):
                    return {"status": "ok", "trace_id": "fixture", "environment": {
                        "product": product, "environment": lane, "context": lane,
                        "repository": profile["repository"], "driver_id": profile["driver_id"],
                        "target": {"provider": "dokploy", "target_type": "compose", "provider_target_type": "compose",
                                   "target_id_recorded": True, "artifact_manifest": None},
                    }}
        raise AssertionError(path)


class ClientRuleTests(unittest.TestCase):
    def setUp(self):
        self.launchplane = FakeLaunchplane()
        self.token = "fixture"
        for target, name, kwargs in (
            (lp, "resolve_settings", {"side_effect": lambda _args: {"service_url": "https://launchplane.example", "token": self.token}}),
            (lp, "request_launchplane_read", {"side_effect": self.launchplane.read}),
        ):
            started = patch.object(target, name, **kwargs)
            started.start()
            self.addCleanup(started.stop)
        self.reader = Reader()
        self.args = argparse.Namespace(repo=REPO, pr="1", waiting_for_client=False, allow_no_checks=False)
        for target, name, value in (
            (helper.github_read_core, "GitHubReader", self.reader),
            (helper.github_comment_core, "comment", {"ok": True, "url": "fixture"}),
        ):
            started = patch.object(target, name, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        helper.CURRENT_OPERATION = "github.pr.ready"

    def verdict(self, body, labels=()):
        self.reader.pr = {**copy.deepcopy(PR), "body": body, "labels": [{"name": name} for name in labels]}
        self.reader.pr_reads = 0
        try:
            helper.cmd_ready(self.args)
            refusal = ""
        except helper.PrHelperError as error:
            refusal = str(error)
        return refusal

    def test_unmapped_repository_with_client_notes_gets_plain_ready(self):
        self.launchplane.product("example/client", "client-runtime", driver="odoo", lanes=("testing",))
        self.assertEqual(self.verdict(CLIENT_NOTES), "")
        self.assertEqual(self.verdict("No notes section."), "")

    def test_mapped_product_without_a_client_gets_plain_ready(self):
        self.launchplane.product(REPO, "tool", client="")
        self.assertEqual(self.verdict(CLIENT_NOTES), "")

    def test_repository_mapped_to_a_client_waits_for_acceptance(self):
        self.launchplane.product(REPO, "tool")
        self.assertIn("--waiting-for-client", self.verdict(CLIENT_NOTES + "\nClient change: a new export button."))
        self.assertIn("Client test notes are missing", self.verdict("No notes section."))
        self.assertEqual(self.verdict(NOTHING_TO_TEST), "")

    def test_director_standing_product_skips_acceptance_unless_held(self):
        self.launchplane.product(REPO, "tool", client="director", release="director_standing")
        self.assertEqual(self.verdict(CLIENT_NOTES), "")
        self.assertTrue(self.verdict(CLIENT_NOTES, labels=("owner-review",)))
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "in_progress", "conclusion": None})
        self.assertTrue(self.verdict(CLIENT_NOTES))
        self.reader.checks.pop()
        self.assertIn("Client test notes are missing", self.verdict("No notes section."))

    def test_unsettled_reads_hold_on_the_client_path(self):
        def failed():
            self.launchplane.failure = TimeoutError()

        def ambiguous():
            self.launchplane.ambiguous = ["example/shared"]

        def malformed():
            self.launchplane.repositories.append("example/tool")

        def unreadable_profile():
            self.launchplane.product(REPO, "tool")
            self.launchplane.profiles["tool"]["repository"] = "example/elsewhere"

        def no_token():
            self.token = ""

        for unsettle in (failed, ambiguous, malformed, unreadable_profile, no_token):
            with self.subTest(read=unsettle.__name__):
                self.launchplane.__init__()
                self.token = "fixture"
                unsettle()
                for body in ("No notes section.", CLIENT_NOTES):
                    self.assertIn("Launchplane could not confirm", self.verdict(body))
                self.assertEqual(self.verdict(NOTHING_TO_TEST), "")


if __name__ == "__main__":
    unittest.main()
