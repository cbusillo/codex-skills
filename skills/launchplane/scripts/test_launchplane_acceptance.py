#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import copy
import importlib
from pathlib import Path
import sys
import unittest
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
acceptance = importlib.import_module("skills.launchplane.scripts.launchplane_acceptance")

REPO = "example/tool"
PRODUCT = "different-product-id"


def state(repository=REPO):
    return acceptance.read_client_release(repository).state


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"repository": REPO, "classification": "managed_runtime", "product": PRODUCT,
                      "source": "product_profile"}
        self.mapping: dict[str, Any] = {"mapping": {"repositories": [self.entry]}}
        self.profile: dict[str, Any] = {"status": "ok", "trace_id": "test-trace", "client_is_director": True, "profile": {
            "product": PRODUCT, "repository": REPO, "production_use": "live", "lifecycle_state": "active",
            "release_on_acceptance": "director_standing", "owner": {"github_login": "director"},
            "updated_at": "2026-01-01T00:00:00Z",
        }}
        settings_patch = patch.object(acceptance.operator, "resolve_settings", return_value={
            "service_url": "https://launchplane.example", "token": "fixture",
        })
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        transport_patch = patch.object(acceptance.operator, "request_launchplane_read", side_effect=self.read)
        self.transport = transport_patch.start()
        self.addCleanup(transport_patch.stop)

    def read(self, **kwargs):
        if kwargs["path"] == acceptance.operator.helper_command_path("repo-product-mapping-read"):
            return copy.deepcopy(self.mapping)
        if kwargs["path"] == acceptance.operator.helper_command_path("product-profile-read").format(product=PRODUCT):
            return copy.deepcopy(self.profile)
        raise AssertionError(kwargs["path"])

    def test_launchplanes_director_answer_applies_in_any_release_mode(self):
        for mode in acceptance.operator.PRODUCT_RELEASE_ON_ACCEPTANCE_MODES:
            with self.subTest(mode=mode):
                self.profile["profile"]["release_on_acceptance"] = mode
                self.assertEqual(state(), acceptance.DIRECTOR_STANDING)

    def test_without_launchplanes_answer_only_the_standing_mode_counts(self):
        self.profile.pop("client_is_director")
        self.assertEqual(state(), acceptance.DIRECTOR_STANDING)
        for mode in acceptance.operator.PRODUCT_RELEASE_ON_ACCEPTANCE_MODES - {"director_standing"}:
            for answer in (None, False, "true"):
                with self.subTest(mode=mode, answer=answer):
                    self.profile["profile"]["release_on_acceptance"] = mode
                    self.profile["client_is_director"] = answer
                    self.assertEqual(state(), acceptance.CLIENT)

    def test_no_client_needs_a_settled_mapping(self):
        self.assertEqual(state("example/unmapped"), acceptance.NO_CLIENT)
        self.profile["profile"]["owner"] = None
        self.assertEqual(state(), acceptance.NO_CLIENT)
        self.entry.pop("product")
        self.entry["source"] = "every_code_work_request"
        self.assertEqual(state(), acceptance.NO_CLIENT)

    def test_unsettled_evidence_reads_unknown(self):
        mutations = {
            "no mapping": lambda: self.mapping.pop("mapping"),
            "malformed entry": lambda: self.mapping["mapping"]["repositories"].append("example/tool"),
            "duplicate entry": lambda: self.mapping["mapping"]["repositories"].append(copy.deepcopy(self.entry)),
            "ambiguous": lambda: self.mapping["mapping"].update(ambiguous_repositories=["example/shared"]),
            "malformed ambiguous": lambda: self.mapping["mapping"].update(ambiguous_repositories="malformed"),
            "other repository": lambda: self.profile["profile"].update(repository="other/tool"),
            "other product": lambda: self.profile["profile"].update(product="other-product"),
            "retired": lambda: self.profile["profile"].update(lifecycle_state="retired"),
            "no lifecycle": lambda: self.profile["profile"].pop("lifecycle_state"),
            "unknown release mode": lambda: self.profile["profile"].update(release_on_acceptance="new-mode"),
            "product entry without product": lambda: self.entry.pop("product"),
            "profile omits its Client": lambda: self.profile["profile"].pop("owner"),
            "profile omits its login": lambda: self.profile["profile"].update(client={}),
            "unrelated entry without product": lambda: self.mapping["mapping"]["repositories"].append(
                {"repository": "example/web", "classification": "managed_runtime", "driver_id": "generic-web",
                 "source": "product_profile"}),
        }
        for name, mutate in mutations.items():
            with self.subTest(evidence=name):
                self.setUp()
                mutate()
                release = acceptance.read_client_release(REPO)
                self.assertEqual(release.state, acceptance.UNKNOWN)
                self.assertTrue(release.reason)

    def test_unavailable_reads_are_unknown(self):
        for error in (TimeoutError(), urllib.error.HTTPError("https://fixture.invalid", 403, "denied", Message(), None), ValueError("invalid_response")):
            with self.subTest(error=type(error).__name__):
                self.transport.side_effect = error
                self.assertEqual(state(), acceptance.UNKNOWN)

    def test_missing_operator_token_is_unknown(self):
        with patch.object(acceptance.operator, "resolve_settings", return_value={
            "service_url": "https://launchplane.example", "token": "",
        }):
            self.assertEqual(state(), acceptance.UNKNOWN)
        self.transport.assert_not_called()

    def test_production_commit_comes_from_the_promotion_status_destination(self):
        path = acceptance.operator.helper_command_path("product-promotion-status-read").format(product=PRODUCT, environment="prod")

        def read(**kwargs):
            if kwargs["path"] == path:
                return {"promotion_status": {"source": {"source_git_ref": "testing"}, "destination": {"source_git_ref": "deployed"}}}
            return self.read(**kwargs)

        self.transport.side_effect = read
        self.assertEqual(acceptance.read_production_commit(REPO).commit, "deployed")

    def test_unmapped_missing_or_failed_production_reads_give_a_reason_and_no_commit(self):
        path = acceptance.operator.helper_command_path("product-promotion-status-read").format(product=PRODUCT, environment="prod")
        self.mapping["mapping"]["repositories"] = []
        self.assertTrue(acceptance.read_production_commit(REPO).reason)
        self.setUp()
        for response in ({"promotion_status": {"destination": {}}}, {"promotion_status": None}, TimeoutError()):
            with self.subTest(response=type(response).__name__):
                def read(response=response, **kwargs):
                    if kwargs["path"] == path:
                        if isinstance(response, Exception):
                            raise response
                        return response
                    return self.read(**kwargs)

                self.transport.side_effect = read
                result = acceptance.read_production_commit(REPO)
                self.assertEqual(result.commit, "")
                self.assertTrue(result.reason)


if __name__ == "__main__":
    unittest.main()
