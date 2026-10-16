#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Standing acceptance requires the service mapping and a matching scoped profile."""

import copy
import unittest
import urllib.error
from email.message import Message
from typing import Any
from unittest.mock import patch

import launchplane_acceptance as acceptance

REPO = "example/tool"
PRODUCT = "different-product-id"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"repository": REPO, "classification": "managed_runtime", "product": PRODUCT}
        self.mapping = {"mapping": {"repositories": [self.entry]}}
        self.profile: dict[str, Any] = {"status": "ok", "trace_id": "test-trace", "profile": {
            "product": PRODUCT, "repository": REPO, "production_use": "live",
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
