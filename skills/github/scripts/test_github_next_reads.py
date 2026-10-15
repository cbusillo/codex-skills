#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline concurrency, invocation freshness and bounded-read regressions."""
from __future__ import annotations

import contextlib
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from github_next_reads import NextReads, ReadDeadline


def test_duplicate_observations_share_one_request_and_independent_bodies() -> None:
    entered, release = threading.Event(), threading.Event()
    calls = []
    def read():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return {"nested": []}
    with NextReads(workers=2, seconds=0) as observations, ThreadPoolExecutor(2) as pool:
        first = pool.submit(observations.observe, "same", read)
        assert entered.wait(5)
        second = pool.submit(observations.observe, "same", read)
        release.set()
        one, two = first.result(), second.result()
        one["nested"].append("consumer annotation")
        assert not two["nested"]
        assert len(calls) == 1
        assert observations.receipt()["reused_reads"] == 1
    with NextReads(workers=1, seconds=0) as later:
        later.observe("same", lambda: calls.append(True))
    assert len(calls) == 2, "another invocation must contact its authenticated reader"


def test_worker_window_overlaps_reads_and_preserves_order() -> None:
    barrier = threading.Barrier(2)
    state = {"active": 0, "maximum": 0}
    lock = threading.Lock()
    def read(value):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        barrier.wait(5)
        with lock:
            state["active"] -= 1
        return value
    with NextReads(workers=2, seconds=0) as reads:
        assert reads.map(read, list(range(6))) == list(range(6))
    assert state["maximum"] == 2


def test_deadline_reuses_observed_evidence_but_never_invents_unread_success() -> None:
    clock = {"now": 100.0}
    prior = "1000"
    with patch.dict(os.environ, {"GITHUB_RETRY_DEADLINE_AT": prior}), patch("github_next_reads.time.time", lambda: clock["now"]):
        with NextReads(workers=1, seconds=10) as reads:
            assert float(os.environ["GITHUB_RETRY_DEADLINE_AT"]) < float(prior)
            assert reads.observe("observed", lambda: {"state": "open"})["state"] == "open"
            clock["now"] += 11
            assert reads.observe("observed", lambda: (_ for _ in ()).throw(AssertionError("duplicate HTTP read")))["state"] == "open"
            calls = []
            try:
                reads.observe("unread", lambda: calls.append(True))
            except ReadDeadline:
                pass
            else:
                raise AssertionError("deadline did not retain unread evidence")
            assert not calls
            assert reads.receipt()["deadline_exhausted"]
        assert os.environ["GITHUB_RETRY_DEADLINE_AT"] == prior


def test_progress_is_stderr_only_and_receipt_is_value_free() -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    private = "private repository body and credentials"
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        with NextReads(workers=1, seconds=0) as reads:
            reads.start_phase("graph")
            reads.observe(private, lambda: private)
            receipt = reads.receipt()
    assert not stdout.getvalue()
    assert stderr.getvalue()
    assert private not in stderr.getvalue() + json.dumps(receipt)
    assert any(row["phase"] == "graph" and row["reads"] for row in receipt["phases"])


def test_unused_read_ahead_failure_is_not_silently_discarded() -> None:
    class AuthFailure(Exception):
        pass
    def refused():
        raise AuthFailure("credential rejection")
    with NextReads(workers=1, seconds=0) as reads:
        task = reads.submit("unvisited", refused)
        # Wait for this intentionally unused task, then verify final accounting
        # still surfaces its failure rather than treating read-ahead as success.
        assert isinstance(task.exception(), AuthFailure)
        try:
            reads.drain()
        except AuthFailure:
            pass
        else:
            raise AssertionError("read-ahead credential failure was hidden")


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
