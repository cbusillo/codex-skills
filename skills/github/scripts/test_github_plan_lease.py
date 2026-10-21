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

    def test_lease_expires_only_after_its_configured_quiet_period(self):
        LEASE.renew("session")
        self.age("session", LEASE.DEFAULT_LEASE_MINUTES - 1)
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)
        self.age("session", LEASE.DEFAULT_LEASE_MINUTES + 1)
        self.assertEqual(LEASE.state("session"), LEASE.EXPIRED)
        os.environ[LEASE.LEASE_MINUTES_VARIABLE] = str(LEASE.DEFAULT_LEASE_MINUTES * 2)
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)

    def test_renewal_revives_an_expired_lease_and_unseen_sessions_stay_unknown(self):
        LEASE.renew("session")
        self.age("session", LEASE.DEFAULT_LEASE_MINUTES + 1)
        LEASE.renew("session")
        self.assertEqual(LEASE.state("session"), LEASE.LIVE)
        self.assertEqual(LEASE.state("never-seen"), LEASE.UNKNOWN)

    def test_invalid_period_refuses_instead_of_expiring_live_claims(self):
        LEASE.renew("session")
        for value in ("0", "-5", "soon", "inf"):
            with self.subTest(value=value):
                os.environ[LEASE.LEASE_MINUTES_VARIABLE] = value
                with self.assertRaises(ValueError):
                    LEASE.state("session")

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
