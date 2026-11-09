#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import github_plan_lease as LEASE


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
        os.environ["HOME"] = self.enterContext(tempfile.TemporaryDirectory())
        os.environ.pop(LEASE.LEASE_MINUTES_VARIABLE, None)

    def age(self, session, minutes):
        stale = time.time() - minutes * 60
        os.utime(LEASE.lease_path(session), (stale, stale))

    def test_lease_expires_after_the_period_its_claimant_recorded_whatever_the_reader_sets(self):
        os.environ[LEASE.LEASE_MINUTES_VARIABLE] = "120"
        LEASE.renew("session", minutes=LEASE.lease_minutes())
        LEASE.renew("session")
        os.environ.pop(LEASE.LEASE_MINUTES_VARIABLE)
        self.age("session", 45)
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)
        self.age("session", 121)
        self.assertEqual(LEASE.state("session"), LEASE.EXPIRED)
        LEASE.renew("other")
        os.environ[LEASE.LEASE_MINUTES_VARIABLE] = "1"
        self.age("other", LEASE.DEFAULT_LEASE_MINUTES - 1)
        self.assertEqual(LEASE.state("other"), LEASE.LIVE)

    def test_renewal_revives_an_expired_lease_and_unseen_sessions_stay_unknown(self):
        LEASE.renew("session")
        self.age("session", LEASE.DEFAULT_LEASE_MINUTES + 1)
        LEASE.renew("session")
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)
        self.assertEqual(LEASE.state("never-seen"), LEASE.UNKNOWN)

    def test_a_session_is_live_by_its_lease_or_by_a_recent_first_sighting_when_this_host_has_no_lease(self):
        now = time.time()
        period = LEASE.DEFAULT_LEASE_MINUTES * 60
        self.assertTrue(LEASE.session_is_live("elsewhere", first_seen=now - period + 60))
        self.assertFalse(LEASE.session_is_live("elsewhere", first_seen=now - period - 60))
        LEASE.renew("session")
        self.assertTrue(LEASE.session_is_live("session", first_seen=now - 10 * period))
        self.age("session", LEASE.DEFAULT_LEASE_MINUTES + 1)
        self.assertFalse(LEASE.session_is_live("session", first_seen=now))

    def test_session_end_frees_its_claims_at_once_and_later_activity_revives_them(self):
        LEASE.renew_from_hook({"session_id": "session", "hook_event_name": "PostToolUse"})
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)
        LEASE.renew_from_hook({"session_id": "session", "hook_event_name": "SessionEnd"})
        self.assertEqual(LEASE.state("session"), LEASE.EXPIRED)
        self.assertFalse(LEASE.session_is_live("session", first_seen=time.time()))
        LEASE.renew_from_hook({"session_id": "session", "hook_event_name": "UserPromptSubmit"})
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)
        LEASE.renew_from_hook({"session_id": "never-claimed", "hook_event_name": "SessionEnd"})
        self.assertEqual(LEASE.state("never-claimed"), LEASE.UNKNOWN)

    def test_live_claim_repositories_come_from_the_host_list_and_default_to_none(self):
        self.assertFalse(LEASE.uses_live_claims("Owner/Repo"))
        listed = Path(os.environ["HOME"]) / ".code/skill-data/live-claim-repositories"
        listed.parent.mkdir(parents=True)
        listed.write_text("owner/repo\n\n")
        self.assertTrue(LEASE.uses_live_claims("Owner/Repo"))
        self.assertFalse(LEASE.uses_live_claims("owner/other"))

    def test_invalid_period_refuses_before_recording_a_lease(self):
        for value in ("0", "-5", "soon", "inf"):
            with self.subTest(value=value):
                os.environ[LEASE.LEASE_MINUTES_VARIABLE] = value
                with self.assertRaises(ValueError):
                    LEASE.lease_minutes()

    def test_a_redirected_lease_file_is_refused(self):
        LEASE.lease_directory().mkdir(parents=True)
        target = Path(os.environ["HOME"]) / "elsewhere"
        target.touch()
        LEASE.lease_path("session").symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            LEASE.renew("session")
        with self.assertRaises(ValueError):
            LEASE.state("session")


    def test_a_redirected_or_shared_lease_store_is_refused(self):
        shared = Path(os.environ["HOME"]) / "shared"
        shared.mkdir()
        LEASE.lease_directory().parent.mkdir(parents=True)
        LEASE.lease_directory().symlink_to(shared)
        with self.assertRaises(ValueError):
            LEASE.renew("session")
        LEASE.lease_directory().unlink()
        LEASE.renew("session")
        LEASE.lease_path("session").chmod(0o666)
        with self.assertRaises(ValueError):
            LEASE.state("session")
        LEASE.lease_path("session").chmod(0o600)
        LEASE.lease_directory().chmod(0o777)
        with self.assertRaises(ValueError):
            LEASE.state("session")


if __name__ == "__main__":
    unittest.main()
