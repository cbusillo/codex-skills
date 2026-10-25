# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Invocation-local, bounded parallel observations for global next.

Every first observation still uses the maintained authenticated reader. Nothing
here supplies an offline body, retries a failed read, or changes its identity.
"""
from __future__ import annotations

import copy
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from typing import Any, Callable, TypeVar

import github_api

T = TypeVar("T")
R = TypeVar("R")


def observation_key(path: str) -> str:
    return re.sub(r"^/repos/([^/]+/[^/?]+)", lambda match: f"/repos/{match[1].casefold()}", path)


class ReadDeadline(Exception):
    pass


class NextReads:
    def __init__(self, *, workers: int, seconds: float, progress_seconds: float = 15):
        self.workers = workers
        self.seconds = seconds
        self.started = time.monotonic()
        self.deadline = time.time() + seconds if seconds else None
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="global-next")
        self.lock = threading.Lock()
        self.observations: dict[str, Future[Any]] = {}
        self.tasks: dict[Any, Future[Any]] = {}
        self.reused = 0
        self.requests = 0
        self.reused_failures = 0
        self.budget_stops: dict[str, int] = {}
        self.read_ahead_unevaluated = 0
        self.read_ahead_failures: dict[str, int] = {}
        self.budget_errors: set[BaseException] = set()
        self.budget_context: list[dict[str, Any]] = []
        self.effective_seconds: float | None = seconds or None
        self.phases: list[dict[str, Any]] = []
        self.phase_name = "startup"
        self.stop = threading.Event()
        self.retry_runtime = github_api.RetryRuntime(sleep=self._sleep, cancelled=self.stop.is_set)
        self.progress_seconds = progress_seconds
        self.previous_deadline: str | None = None
        self.phase_started = self.started
        self.phase_requests = self.phase_reused = 0

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.time() >= self.deadline

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.time()) if self.deadline is not None else float("inf")

    @property
    def final_evidence_reserve(self) -> float:
        return min(30, self.effective_seconds / 5) if self.effective_seconds is not None else 0

    def __enter__(self) -> NextReads:
        self.previous_deadline = os.environ.get("GITHUB_RETRY_DEADLINE_AT")
        if self.previous_deadline:
            inherited = float(self.previous_deadline)
            self.deadline = min(self.deadline, inherited) if self.deadline else inherited
        if self.deadline is not None:
            self.effective_seconds = max(0.0, self.deadline - time.time())
            os.environ["GITHUB_RETRY_DEADLINE_AT"] = str(self.deadline)
        self.reporter = threading.Thread(target=self._progress, daemon=True)
        self.reporter.start()
        self.progress()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop.set()
        try:
            self.pool.shutdown(wait=True, cancel_futures=True)
        finally:
            self.reporter.join()
            if self.previous_deadline is None:
                os.environ.pop("GITHUB_RETRY_DEADLINE_AT", None)
            else:
                os.environ["GITHUB_RETRY_DEADLINE_AT"] = self.previous_deadline

    def _sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def _read(self, read: Callable[[], T]) -> T:
        if self.stop.is_set():
            raise CancelledError()
        return read()

    def progress(self) -> None:
        # No repository names, issue text, URLs, credentials or session data.
        print(f"global next: {self.phase_name}; {time.monotonic() - self.started:.1f}s; "
              f"{self.requests} reads, {self.reused} reused", file=sys.stderr, flush=True)

    def start_phase(self, name: str) -> None:
        self.phases.append(self._current_phase())
        self.phase_name, self.phase_started = name, time.monotonic()
        self.phase_requests, self.phase_reused = self.requests, self.reused
        self.progress()

    def _current_phase(self) -> dict[str, Any]:
        return {"phase": self.phase_name, "seconds": round(time.monotonic() - self.phase_started, 3),
                "reads": self.requests - self.phase_requests, "reused": self.reused - self.phase_reused}

    def _progress(self) -> None:
        while not self.stop.wait(self.progress_seconds):
            self.progress()

    def observation(self, path: str) -> Future[Any] | None:
        with self.lock:
            return self.observations.get(observation_key(path))

    def request(self, read: Callable[[], T]) -> T:
        if self.expired:
            raise ReadDeadline("global next time budget exhausted; evidence unread")
        with self.lock:
            self.requests += 1
        return self._read(read)

    def observe(self, path: str, read: Callable[[], T]) -> T:
        path = observation_key(path)
        with self.lock:
            future = self.observations.get(path)
            first = future is None
            if first:
                future = Future()
                self.observations[path] = future
            else:
                self.reused += 1
        if first:
            try:
                future.set_result(self.request(read))
            except BaseException as exc:
                future.set_exception(exc)
        # Consumers may annotate their own copy, never another observation.
        try:
            return copy.deepcopy(future.result())
        except BaseException:
            if not first:
                with self.lock:
                    self.reused_failures += 1
            raise

    def budget_stop(self, cause: str, *, error: BaseException, retry_at: Any = None) -> None:
        with self.lock:
            if error in self.budget_errors:
                return
            self.budget_errors.add(error)
            self.budget_stops[cause] = self.budget_stops.get(cause, 0) + 1
            self.budget_context.append({"cause": cause,
                "retry_at": retry_at if type(retry_at) in {int, float} else None})

    def submit(self, key: Any, read: Callable[[], T]) -> Future[T]:
        with self.lock:
            if key not in self.tasks:
                self.tasks[key] = self.pool.submit(self._read, read)
            return self.tasks[key]

    def map(self, read: Callable[[T], R], values: list[T]) -> list[R]:
        # A slow source occupies one slot, never an entire fixed batch. Keep
        # at most workers pending, and restore source order in the result.
        results: list[Any] = [None] * len(values)
        pending: dict[Future[R], int] = {}
        offset = 0
        while offset < len(values) or pending:
            while offset < len(values) and len(pending) < self.workers:
                pending[self.pool.submit(self._read, lambda value=values[offset]: read(value))] = offset
                offset += 1
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                results[pending.pop(future)] = future.result()
        return results

    def drain(self, *, unused_failure: Callable[[BaseException], None] | None = None) -> None:
        # In particular, an authentication failure in an unused read-ahead
        # task must never be hidden behind another worker's retry wait.
        pending = {future for future in self.tasks.values() if not future.cancel()}
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                try:
                    future.result()
                except BaseException as exc:
                    if unused_failure is None:
                        raise
                    unused_failure(exc)
        self.pool.shutdown(wait=True, cancel_futures=True)

    def receipt(self) -> dict[str, Any]:
        return {"elapsed_seconds": round(time.monotonic() - self.started, 3),
                "time_budget_seconds": self.seconds, "deadline_exhausted": self.expired,
                "workers": self.workers, "reads": self.requests, "reused_reads": self.reused,
                "reused_failures": self.reused_failures, "budget_stops": self.budget_stops,
                "budget_context": self.budget_context,
                "effective_time_budget_seconds": self.effective_seconds,
                "read_ahead_unevaluated": self.read_ahead_unevaluated,
                "read_ahead_failures": self.read_ahead_failures,
                "phases": [*self.phases, self._current_phase()]}
