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
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from github_next_reads import NextReads, ReadDeadline


def cancellation_fixture(mode: str) -> None:
    """Exercise the real CLI envelope and reader/retry path, without GitHub."""
    from test_gh_plan_next import load_module
    module = load_module()
    module.api_json = module.real_api_json
    api = module.github_api_core
    entered = threading.Event()
    attempted = []
    stopped_at = []
    pools = []
    runtime_type = api.RetryRuntime
    actor = "fixture-reader"

    def runtime(**kwargs):
        original_sleep = kwargs.get("sleep", time.sleep)
        def sleep(seconds):
            entered.set()
            original_sleep(seconds)
        return runtime_type(**{**kwargs, "sleep": sleep})

    def transport(method, path, body, **kwargs):
        attempted.append(path)
        assert kwargs["actor"] == kwargs["expected_actor"] == actor
        limit = api.RateLimitInfo(retry_after=60, resource="core")
        return api.ApiResult(ok=False, status=503, body={}, actor=actor,
            expected_actor=actor, rate_limit=limit,
            failure=api.FailureDetail(cause="server_error", message="fixture retry",
                retryable=True, fallback_eligible=False, disposition="retry"))

    fatal = module.PlanError("fixture credential failure", failure=api.FailureDetail(
        cause="invalid_credentials", message="fixture credential failure",
        retryable=False, fallback_eligible=False, disposition="stop"))

    def read():
        if mode == "preflight":
            return module.ensure_graphql_budget(recoverable=True)
        return module.api_json("GET", "/repos/fixture/source/issues/1")

    original_json = module.gh_json
    def gh_json(args, **kwargs):
        if mode == "preflight":
            attempted.append("quota preflight")
            return actor, {"resources": {"graphql": {"remaining": 0, "reset": int(time.time()) + 60}}}
        return original_json(args, **kwargs)

    def fail():
        assert entered.wait(3), "reader did not reach retry wait"
        stopped_at.append(time.monotonic())
        raise fatal

    def direction_next(_args, _repo):
        reads = module.NEXT_READS
        pools.append(reads.pool)
        if mode == "fatal":
            reads.map(lambda index: read() if index == 0 else fail(), [0, 1])
        else:
            reads.submit("backoff", read)
            assert entered.wait(3), "reader did not reach retry wait"
            if mode == "drain":
                assert reads.submit("fatal", fail).exception(timeout=3) is fatal
                reads.drain(unused_failure=lambda exc: module.next_source_error(exc))
            else:
                # Two workers are occupied before a third task is queued.
                held = threading.Event()
                def other():
                    held.set()
                    return read()
                reads.submit("other", other)
                assert held.wait(3)
                reads.submit("queued", lambda: attempted.append("unexpected queued read"))
                stopped_at.append(time.monotonic())
                os.kill(os.getpid(), signal.SIGINT)

    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "GITHUB_RETRY_STATE_DIR": directory, "GITHUB_RETRY_MAX_WAIT_SECONDS": "120",
            "GITHUB_RETRY_BASE_BACKOFF_SECONDS": "60", "GITHUB_RETRY_JITTER_SECONDS": "0",
            "GITHUB_RETRY_WAIT_SLICE_SECONDS": "60", "GITHUB_RETRY_DEADLINE_AT": str(time.time() + 180),
            "GH_PLAN_SKIP_BOT": "0", "GH_PLAN_ALLOW_ACTIVE_FIRST": "0"}), patch.multiple(
                module, EXPECTED_ACTOR=actor, direction_next=direction_next, gh_json=gh_json), patch.object(
                api, "request_identity", return_value=(actor, actor, [])), patch.object(
                api, "call_gh", transport), patch.object(api, "RetryRuntime", runtime), patch.object(
                module.github_identity, "active_auth_fallback_allowed", return_value=False), patch.object(
                module.github_identity, "own_user_opted_in", return_value=False), patch.object(
                sys, "argv", ["gh-plan.py", "--repo", "fixture/direction", "next", "--read-workers", "2"]):
        prior_deadline = os.environ["GITHUB_RETRY_DEADLINE_AT"]
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code
        else:
            raise AssertionError("fixture did not produce a terminal failure")
        assert module.NEXT_READS is None
        assert os.environ["GITHUB_RETRY_DEADLINE_AT"] == prior_deadline
        assert all(not thread.is_alive() for pool in pools for thread in pool._threads)
        assert "unexpected queued read" not in attempted
        assert len(attempted) <= 2, "cancellation issued another transport attempt"
        latency = time.monotonic() - stopped_at[0]
        print(json.dumps({"mode": mode, "latency": latency, "attempts": len(attempted),
                          "threads_clean": True}), file=sys.stderr)
        raise SystemExit(code)


def test_cli_cancellation_and_fatal_reads_interrupt_backoff_and_join_workers() -> None:
    for mode in ("interrupt", "preflight", "fatal", "drain"):
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--cancellation-fixture", mode],
                              capture_output=True, text=True, timeout=20)
        interrupted = mode in {"interrupt", "preflight"}
        assert proc.returncode == (130 if interrupted else 1), (proc.stdout, proc.stderr)
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False, payload
        assert payload["error_code"] == ("cancelled" if interrupted else "invalid_credentials"), payload
        assert payload["fallback_eligible"] is False, payload
        metrics = json.loads(proc.stderr.splitlines()[-1])
        assert metrics["latency"] < 5, metrics
        assert payload["elapsed_wait"] < 5, payload
        print(f"cancellation fixture: {metrics}")


def test_cancelled_global_graphql_read_never_launches_transport() -> None:
    from test_gh_plan_next import load_module
    module = load_module()
    with NextReads(workers=1, seconds=60) as reads, patch.object(
            module.github_api_core, "request_identity", return_value=("fixture-reader", "fixture-reader", [])), patch.object(
            module.subprocess, "run", side_effect=AssertionError("transport launched after cancellation")):
        module.NEXT_READS = reads
        reads.stop.set()
        try:
            module.run_raw(["api", "graphql", "--input", "-"],
                           input_text=json.dumps({"query": "query { viewer { login } }"}),
                           is_write=False, operation="github.plan.next", bucket="graphql")
        except module.PlanError as exc:
            assert exc.failure.cause == "cancelled"
            assert exc.api_result["attempts"] == 0
        else:
            raise AssertionError("cancelled GraphQL read succeeded")
        finally:
            module.NEXT_READS = None


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


def test_slow_first_source_does_not_idle_other_workers() -> None:
    third_started = threading.Event()
    def read(index):
        if index == 0:
            assert third_started.wait(3), "one stalled source prevented independent work"
        if index == 2:
            third_started.set()
        return index
    with NextReads(workers=2, seconds=0) as reads:
        assert reads.map(read, [0, 1, 2]) == [0, 1, 2]


def test_short_inherited_budget_keeps_some_time_for_discovery() -> None:
    clock = 100.0
    with patch.dict(os.environ, {"GITHUB_RETRY_DEADLINE_AT": str(clock + 25)}), patch(
            "github_next_reads.time.time", return_value=clock):
        with NextReads(workers=1, seconds=180) as reads:
            assert 0 < reads.final_evidence_reserve < reads.remaining_seconds
            assert reads.receipt()["effective_time_budget_seconds"] < reads.seconds


def test_local_ownership_commands_share_one_total_deadline() -> None:
    import github_plan_claim as claim
    from types import SimpleNamespace
    clock = {"now": 100.0}
    observed = []
    def command(argv, **kwargs):
        observed.append(kwargs["timeout"])
        clock["now"] += 1
        return SimpleNamespace(returncode=0, stdout="git@github.com:someone/project.git\n" if "get-url" in argv else "")
    with patch.object(claim.time, "time", lambda: clock["now"]), patch.object(claim.subprocess, "run", command):
        deadline = clock["now"] + 1.5
        try:
            claim.local_inventory("someone/project", 1, deadline_at=deadline)
        except claim.subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("local inventory started commands after its total deadline")
    assert len(observed) == 2
    assert 0 < observed[-1] < observed[0]


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cancellation-fixture":
        cancellation_fixture(sys.argv[2])
    else:
        main()
