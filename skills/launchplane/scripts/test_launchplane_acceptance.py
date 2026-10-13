#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Standing acceptance requires the service mapping and a matching scoped profile."""

import copy
import unittest
import urllib.error
from unittest.mock import patch

import launchplane_acceptance as acceptance

REPO = "example/tool"
PRODUCT = "different-product-id"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"repository": REPO, "classification": "managed_runtime", "product": PRODUCT}
        self.mapping = {"mapping": {"repositories": [self.entry]}}
        self.profile = {"status": "ok", "trace_id": "test-trace", "profile": {
            "product": PRODUCT, "repository": REPO, "production_use": "live",
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
        self.assertEqual(kwargs["path"], acceptance.operator.helper_command_path("product-profile-read").format(product=PRODUCT))
        return copy.deepcopy(self.profile)

    def test_mapped_product_not_repository_basename_supplies_standing_acceptance(self):
        self.assertTrue(acceptance.read_director_standing(REPO))
        self.assertEqual(len(self.transport.call_args_list), 2)

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
        for error in (TimeoutError(), urllib.error.HTTPError("https://fixture.invalid", 403, "denied", None, None), ValueError("invalid_response")):
            with self.subTest(error=type(error).__name__):
                self.transport.side_effect = error
                self.assertFalse(acceptance.read_director_standing(REPO))


if __name__ == "__main__":
    unittest.main()
