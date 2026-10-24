#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""gh-pr.py ready and Supervisor auto-enqueue give the same Client verdict from Launchplane's mapping."""

import argparse
import copy
import importlib
import sys
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from test_ready_handoff import PR, REPO, SHA, Reader, helper
from ready_handoff import handoff_body

acceptance = importlib.import_module("skills.launchplane.scripts.launchplane_acceptance")
watch = importlib.import_module("skills.supervisor.scripts.auto_enqueue")
lp = acceptance.operator

CLIENT_NOTES = "## Client test notes\nTry the new export button on the parts list."
NOTHING_TO_TEST = "## Client test notes\nNothing for the Client to test: internal tooling."
TARGET = {"repo": REPO, "base": "main", "label": "enqueue", "policy": {}, "scheduler_drives_train": True}


class FakeLaunchplane:
    def __init__(self):
        self.repositories: list[Any] = []
        self.ambiguous: Any = []
        self.profiles: dict[str, dict[str, Any]] = {}
        self.sources: dict[tuple[str, str], list[str]] = {}
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

    def pin(self, product, sources, lanes=("prod", "testing")):
        for lane in lanes:
            self.sources[(product, lane)] = list(sources)

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
                                   "target_id_recorded": True, "artifact_manifest": {
                                       "artifact_id": "artifact-1", "source_commit": "c" * 40,
                                       "image": {"repository": "ghcr.io/example/runtime", "digest": "sha256:" + "d" * 64},
                                       "addon_sources": [{"repository": source, "commit": "e" * 40}
                                                         for source in self.sources.get((product, lane), [])],
                                   }},
                    }}
        raise AssertionError(path)


class ClientRuleTests(unittest.TestCase):
    def setUp(self):
        self.launchplane = FakeLaunchplane()
        self.token = "fixture"
        for target, name, kwargs in (
            (lp, "resolve_settings", {"side_effect": lambda _args: {"service_url": "https://launchplane.example", "token": self.token}}),
            (lp, "request_launchplane_read", {"side_effect": self.launchplane.read}),
            (acceptance, "launchplane_configuration", {"return_value": "configured", "create": True}),
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
        self.live = object.__new__(watch.Live)
        self.live.reader = object()
        self.live.get = lambda _path: {"checks": [{"context": "unit", "app_id": None}]}
        ready_comment = {"id": 1, "user": {"login": "engineer"}, "body": handoff_body(SHA),
                         "created_at": "2026-01-01T00:01:00Z"}
        self.live.pages = lambda path: [] if path.endswith("/reviews") else [ready_comment]
        self.live.acceptance = lambda *_args: False
        checks = patch.object(watch, "pull_request_checks", side_effect=lambda *_a, **_k: {
            "summary": {"countsComplete": True, "unavailableComponents": [], "failingCount": 0,
                        "pendingCount": 0, "unfinishedWorkflowRunCount": 0},
            "checkRuns": copy.deepcopy(self.reader.checks), "statuses": [],
        })
        checks.start()
        self.addCleanup(checks.stop)

    def verdict(self, body, labels=()):
        """Return (ready refusal or "", whether auto-enqueue qualifies) and assert they agree."""
        self.reader.pr = {**copy.deepcopy(PR), "body": body, "labels": [{"name": name} for name in labels]}
        self.reader.pr_reads = 0
        try:
            helper.cmd_ready(self.args)
            refusal = ""
        except helper.PrHelperError as error:
            refusal = str(error)
        pull = {**copy.deepcopy(self.reader.pr), "mergeable": True}
        qualified = self.live.qualified(TARGET, pull, set())
        self.assertEqual(not refusal, qualified, refusal or "auto-enqueue refused a PR that ready accepted")
        return refusal

    def test_unmapped_repository_with_client_notes_gets_plain_ready(self):
        self.launchplane.product("example/client", "client-runtime")
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

    def test_repository_pinned_into_a_client_odoo_lane_waits(self):
        self.launchplane.product(REPO, "tool", release="director_standing", client="director")
        self.launchplane.product("example/tenant", "tenant", driver="odoo", lanes=("prod", "testing"))
        self.launchplane.pin("tenant", [REPO], lanes=("testing",))
        self.assertTrue(self.verdict(CLIENT_NOTES))
        self.launchplane.pin("tenant", ["example/other-addons"], lanes=("testing",))
        self.assertEqual(self.verdict(CLIENT_NOTES), "")

    def test_source_recorded_without_owner_counts_only_for_the_same_name(self):
        self.launchplane.product("example/tenant", "tenant", driver="odoo", lanes=("prod",))
        self.launchplane.pin("tenant", ["other-addons"], lanes=("prod",))
        self.assertEqual(self.verdict(CLIENT_NOTES), "")
        self.launchplane.pin("tenant", ["tool"], lanes=("prod",))
        self.assertTrue(self.verdict(CLIENT_NOTES))

    def test_director_standing_product_skips_acceptance_unless_held(self):
        self.launchplane.product(REPO, "tool", client="director", release="director_standing")
        self.assertEqual(self.verdict(CLIENT_NOTES), "")
        self.assertTrue(self.verdict(CLIENT_NOTES, labels=("owner-review",)))
        self.reader.checks.append({"id": 2, "name": "launchplane/owner-review", "status": "in_progress", "conclusion": None})
        self.assertTrue(self.verdict(CLIENT_NOTES))
        # The train still rejects missing notes on a product with a Client.
        self.reader.checks.pop()
        self.assertIn("Client test notes are missing", self.verdict("No notes section."))

    def test_unsettled_reads_hold_on_the_client_path(self):
        truncated = [f"lane-{index}" for index in range(lp.PRODUCT_PROFILE_MAX_LANES + 1)]

        def failed():
            self.launchplane.failure = TimeoutError()

        def ambiguous():
            self.launchplane.ambiguous = ["example/shared"]

        def lanes_truncated():
            self.launchplane.product("example/tenant", "tenant", driver="odoo", lanes=("prod",))
            self.launchplane.profiles["tenant"]["lanes"] = [{"context": lane, "instance": "prod"} for lane in truncated]

        def malformed():
            self.launchplane.repositories.append("example/tool")

        def unreadable_profile():
            self.launchplane.product(REPO, "tool")
            self.launchplane.profiles["tool"]["repository"] = "example/elsewhere"

        def no_token():
            self.token = ""

        for unsettle in (failed, ambiguous, lanes_truncated, malformed, unreadable_profile, no_token):
            with self.subTest(read=unsettle.__name__):
                self.launchplane.__init__()
                self.token = "fixture"
                unsettle()
                for body in ("No notes section.", CLIENT_NOTES):
                    self.assertIn("Launchplane could not confirm", self.verdict(body))
                self.assertEqual(self.verdict(NOTHING_TO_TEST), "")


if __name__ == "__main__":
    unittest.main()
