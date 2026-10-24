#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Standing acceptance requires the service mapping and a matching scoped profile."""

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
ready_handoff = importlib.import_module("skills.github.scripts.ready_handoff")
acceptance = importlib.import_module("skills.launchplane.scripts.launchplane_acceptance")

REPO = "example/tool"
PRODUCT = "different-product-id"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"repository": REPO, "classification": "managed_runtime", "product": PRODUCT}
        self.mapping: dict[str, Any] = {"mapping": {"repositories": [self.entry]}}
        self.profile: dict[str, Any] = {"status": "ok", "trace_id": "test-trace", "profile": {
            "product": PRODUCT, "repository": REPO, "production_use": "live", "lifecycle_state": "active",
            "release_on_acceptance": "director_standing", "owner": {"github_login": "director"},
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

    def test_mapped_product_not_repository_basename_supplies_standing_acceptance(self):
        self.assertTrue(acceptance.read_director_standing(REPO))

    def requires_client(self):
        return ready_handoff.requires_client_acceptance(
            {"labels": [], "body": "## Client test notes\nTry the changed feature."}, [], repository=REPO
        )

    def test_nonruntime_product_standing_reaches_the_real_ready_reader(self):
        for classification in ("active_awareness", "support_dependency", "out_of_scope"):
            with self.subTest(classification=classification):
                self.entry["classification"] = classification
                self.assertFalse(self.requires_client())
                self.assertTrue(acceptance.read_client_release_repository(REPO))

    def test_ambiguous_mapping_withholds_standing(self):
        self.mapping["mapping"]["ambiguous_repositories"] = ["example/shared-client"]
        self.assertTrue(self.requires_client())
        self.assertTrue(acceptance.read_client_release_repository(REPO))
        self.mapping["mapping"]["ambiguous_repositories"] = "malformed"
        self.assertTrue(self.requires_client())
        self.assertTrue(acceptance.read_client_release_repository(REPO))

    def test_client_lane_pins_override_a_director_product_record(self):
        self.entry["classification"] = "active_awareness"
        client_product = "client-runtime"
        client_repository = "example/client"
        self.mapping["mapping"]["repositories"].append(
            {
                "repository": client_repository,
                "classification": "managed_runtime",
                "product": client_product,
                "driver_id": "odoo",
            }
        )
        client_profile = copy.deepcopy(self.profile)
        client_profile["profile"].update(
            product=client_product,
            repository=client_repository,
            driver_id="odoo",
            release_on_acceptance="held",
            owner={"github_login": "client"},
            lanes=[{"context": "prod", "instance": "prod"}],
        )
        environment = copy.deepcopy(self.environment)
        environment["environment"].update(product=client_product, repository=client_repository)
        sources = environment["environment"]["target"]["artifact_manifest"]["addon_sources"]
        sources[0]["repository"] = REPO

        def read(**kwargs):
            if kwargs["path"] == acceptance.operator.helper_command_path("product-profile-read").format(product=client_product):
                return copy.deepcopy(client_profile)
            if kwargs["path"] == acceptance.operator.helper_command_path("product-environment-read").format(
                product=client_product, environment="prod"
            ):
                return copy.deepcopy(environment)
            return self.read(**kwargs)

        self.transport.side_effect = read
        self.assertTrue(self.requires_client())
        self.assertTrue(acceptance.read_client_release_repository(REPO))
        client_profile["profile"]["owner"] = None
        self.assertTrue(self.requires_client())
        sources.clear()
        self.assertFalse(self.requires_client())
        client_profile["profile"]["owner"] = {"github_login": "client"}
        sources.append({"repository": "odoo-shared-addons"})
        self.assertFalse(self.requires_client())
        sources[0]["repository"] = "tool"
        self.assertTrue(self.requires_client())
        sources.clear()
        client_profile["profile"]["driver_id"] = "generic-web"
        self.assertTrue(self.requires_client())
        client_profile["profile"]["driver_id"] = "odoo"
        client_profile["profile"]["lanes"] = [
            {"context": f"lane-{index}", "instance": "prod"} for index in range(acceptance.operator.PRODUCT_PROFILE_MAX_LANES + 1)
        ]
        self.assertTrue(self.requires_client())
        client_profile["profile"]["lanes"] = [{"context": "prod", "instance": "prod"}]
        sources.append({"repository": 42})
        self.assertTrue(self.requires_client())
        environment.pop("environment")
        self.assertTrue(self.requires_client())

    def test_other_release_modes_do_not_exempt(self):
        for mode in acceptance.operator.PRODUCT_RELEASE_ON_ACCEPTANCE_MODES - {"director_standing"}:
            with self.subTest(mode=mode):
                self.profile["profile"]["release_on_acceptance"] = mode
                self.assertFalse(acceptance.read_director_standing(REPO))

    def test_absent_ambiguous_or_mismatched_evidence_never_exempts(self):
        original_mapping, original_profile = copy.deepcopy(self.mapping), copy.deepcopy(self.profile)
        mutations = (
            lambda: self.mapping.pop("mapping"),
            lambda: self.mapping["mapping"].update(repositories=[]),
            lambda: self.mapping["mapping"]["repositories"].append(copy.deepcopy(self.entry)),
            lambda: self.profile["profile"].update(repository="other/tool"),
            lambda: self.profile["profile"].update(product="other-product"),
            lambda: self.profile["profile"].update(owner=None),
            lambda: self.profile["profile"].update(lifecycle_state="retired"),
            lambda: self.profile["profile"].update(lifecycle_state="archived"),
            lambda: self.profile["profile"].update(lifecycle_state="unknown"),
            lambda: self.profile["profile"].pop("lifecycle_state"),
            lambda: self.profile["profile"].pop("release_on_acceptance"),
            lambda: self.profile["profile"].update(release_on_acceptance="new-unknown-mode"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.mapping, self.profile = copy.deepcopy(original_mapping), copy.deepcopy(original_profile)
                mutate()
                self.assertFalse(acceptance.read_director_standing(REPO))

    def test_unavailable_reads_preserve_acceptance_path(self):
        for error in (TimeoutError(), urllib.error.HTTPError("https://fixture.invalid", 403, "denied", Message(), None), ValueError("invalid_response")):
            with self.subTest(error=type(error).__name__):
                self.transport.side_effect = error
                self.assertFalse(acceptance.read_director_standing(REPO))

    def test_client_release_repository_includes_direct_products_and_shared_odoo_addons(self):
        for mode in acceptance.operator.PRODUCT_RELEASE_ON_ACCEPTANCE_MODES:
            with self.subTest(mode=mode):
                self.profile["profile"]["release_on_acceptance"] = mode
                self.assertTrue(acceptance.read_client_release_repository(REPO))
        self.entry["driver_id"] = "odoo"
        self.profile["profile"]["driver_id"] = "odoo"
        self.profile["profile"]["lanes"] = [{"context": "prod", "instance": "prod"}]
        self.assertTrue(acceptance.read_client_release_repository("cbusillo/odoo-shared-addons"))
        self.assertFalse(acceptance.read_client_release_repository("cbusillo/codex-skills"))
        self.profile["profile"]["owner"] = None
        self.assertFalse(acceptance.read_client_release_repository(REPO))
        self.assertFalse(acceptance.read_client_release_repository("cbusillo/odoo-shared-addons"))


if __name__ == "__main__":
    unittest.main()
