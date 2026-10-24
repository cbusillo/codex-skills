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
        self.profile: dict[str, Any] = {"status": "ok", "trace_id": "test-trace", "profile": {
            "product": PRODUCT, "repository": REPO, "production_use": "live", "lifecycle_state": "active",
            "release_on_acceptance": "director_standing", "owner": {"github_login": "director"}, "lanes": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }}
        self.environment: dict[str, Any] = {
            "status": "ok",
            "trace_id": "test-trace",
            "environment": {
                "product": PRODUCT,
                "environment": "prod",
                "context": "prod",
                "repository": REPO,
                "driver_id": "odoo",
                "target": {
                    "provider": "dokploy",
                    "target_type": "compose",
                    "provider_target_type": "compose",
                    "target_id_recorded": True,
                    "artifact_manifest": {
                        "artifact_id": "artifact-1",
                        "source_commit": "a" * 40,
                        "image": {
                            "repository": "ghcr.io/example/tool",
                            "digest": "sha256:" + "b" * 64,
                        },
                        "addon_sources": [
                            {"repository": "cbusillo/odoo-shared-addons", "commit": "c" * 40}
                        ],
                    },
                },
            },
        }
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
        for instance in ("prod", "testing"):
            expected = acceptance.operator.helper_command_path("product-environment-read").format(
                product=PRODUCT, environment=instance
            )
            if kwargs["path"] == expected:
                payload = copy.deepcopy(self.environment)
                payload["environment"]["environment"] = instance
                payload["environment"]["context"] = instance
                return payload
        raise AssertionError(kwargs["path"])

    def pin_into_client_lane(self):
        client_product, client_repository = "client-runtime", "example/client"
        self.mapping["mapping"]["repositories"].append(
            {"repository": client_repository, "classification": "managed_runtime",
             "product": client_product, "driver_id": "odoo", "source": "product_profile"}
        )
        client_profile = copy.deepcopy(self.profile)
        client_profile["profile"].update(
            product=client_product, repository=client_repository, driver_id="odoo",
            release_on_acceptance="held", owner={"github_login": "client"},
            lanes=[{"context": "prod", "instance": "prod"}],
        )
        environment = copy.deepcopy(self.environment)
        environment["environment"].update(product=client_product, repository=client_repository)
        sources = environment["environment"]["target"]["artifact_manifest"]["addon_sources"]
        sources[0]["repository"] = REPO
        self.client_entry = self.mapping["mapping"]["repositories"][-1]

        def read(**kwargs):
            if kwargs["path"] == acceptance.operator.helper_command_path("product-profile-read").format(product=client_product):
                return copy.deepcopy(client_profile)
            if kwargs["path"] == acceptance.operator.helper_command_path("product-environment-read").format(
                product=client_product, environment="prod"
            ):
                return copy.deepcopy(environment)
            return self.read(**kwargs)

        self.transport.side_effect = read
        return client_profile["profile"], environment, sources

    def test_direct_product_reads_client_unless_it_releases_on_director_standing(self):
        self.assertEqual(state(), acceptance.DIRECTOR_STANDING)
        for mode in acceptance.operator.PRODUCT_RELEASE_ON_ACCEPTANCE_MODES - {"director_standing"}:
            with self.subTest(mode=mode):
                self.profile["profile"]["release_on_acceptance"] = mode
                self.assertEqual(state(), acceptance.CLIENT)

    def test_no_client_needs_a_settled_mapping(self):
        self.assertEqual(state("example/unmapped"), acceptance.NO_CLIENT)
        self.profile["profile"]["owner"] = None
        self.assertEqual(state(), acceptance.NO_CLIENT)
        self.entry.pop("product")
        self.entry["source"] = "every_code_work_request"
        self.assertEqual(state(), acceptance.NO_CLIENT)

    def test_client_lane_pin_overrides_a_director_product(self):
        client_profile, _environment, sources = self.pin_into_client_lane()
        self.assertEqual(state(), acceptance.CLIENT)
        client_profile["owner"] = None
        self.assertEqual(state(), acceptance.DIRECTOR_STANDING)
        client_profile["owner"] = {"github_login": "client"}
        sources[0]["repository"] = "tool"
        self.assertEqual(state(), acceptance.CLIENT)
        sources[0]["repository"] = "other-addons"
        self.assertEqual(state(), acceptance.DIRECTOR_STANDING)
        sources[0]["repository"] = REPO
        self.client_entry.pop("driver_id")
        self.assertEqual(state(), acceptance.CLIENT)

    def test_unsettled_evidence_reads_unknown(self):
        truncated = [{"context": f"lane-{index}", "instance": "prod"}
                     for index in range(acceptance.operator.PRODUCT_PROFILE_MAX_LANES + 1)]
        mutations = {
            "no mapping": lambda pin: self.mapping.pop("mapping"),
            "malformed entry": lambda pin: self.mapping["mapping"]["repositories"].append("example/tool"),
            "duplicate entry": lambda pin: self.mapping["mapping"]["repositories"].append(copy.deepcopy(self.entry)),
            "ambiguous": lambda pin: self.mapping["mapping"].update(ambiguous_repositories=["example/shared"]),
            "malformed ambiguous": lambda pin: self.mapping["mapping"].update(ambiguous_repositories="malformed"),
            "other repository": lambda pin: self.profile["profile"].update(repository="other/tool"),
            "other product": lambda pin: self.profile["profile"].update(product="other-product"),
            "retired": lambda pin: self.profile["profile"].update(lifecycle_state="retired"),
            "no lifecycle": lambda pin: self.profile["profile"].pop("lifecycle_state"),
            "unknown release mode": lambda pin: self.profile["profile"].update(release_on_acceptance="new-mode"),
            "Client lane not Odoo": lambda pin: pin[0].update(driver_id="generic-web"),
            "Client lanes truncated": lambda pin: pin[0].update(lanes=truncated),
            "unreadable source": lambda pin: pin[2].append({"repository": 42}),
            "no lane record": lambda pin: pin[1].pop("environment"),
            "product entry without product": lambda pin: self.entry.pop("product"),
            "profile omits its Client": lambda pin: self.profile["profile"].pop("owner"),
            "profile omits its lanes": lambda pin: self.profile["profile"].pop("lanes"),
        }
        for name, mutate in mutations.items():
            with self.subTest(evidence=name):
                self.setUp()
                pinned = self.pin_into_client_lane()
                pinned[2][0]["repository"] = "example/elsewhere"
                mutate(pinned)
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


if __name__ == "__main__":
    unittest.main()
