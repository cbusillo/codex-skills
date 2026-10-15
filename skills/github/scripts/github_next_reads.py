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
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


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
        self.phases: list[dict[str, Any]] = []
        self.phase_name = "startup"
        self.stop = threading.Event()
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

    def __enter__(self) -> NextReads:
        self.previous_deadline = os.environ.get("GITHUB_RETRY_DEADLINE_AT")
        if self.previous_deadline:
            inherited = float(self.previous_deadline)
            self.deadline = min(self.deadline, inherited) if self.deadline else inherited
        if self.deadline is not None:
            os.environ["GITHUB_RETRY_DEADLINE_AT"] = str(self.deadline)
        self.reporter = threading.Thread(target=self._progress, daemon=True)
        self.reporter.start()
        self.progress()
        return self

    def __exit__(self, *_: Any) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.stop.set()
        self.reporter.join()
        if self.previous_deadline is None:
            os.environ.pop("GITHUB_RETRY_DEADLINE_AT", None)
        else:
            os.environ["GITHUB_RETRY_DEADLINE_AT"] = self.previous_deadline

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

    def observe(self, path: str, read: Callable[[], T]) -> T:
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
                if self.expired:
                    raise ReadDeadline("global next time budget exhausted; evidence unread")
                with self.lock:
                    self.requests += 1
                future.set_result(read())
            except BaseException as exc:
                future.set_exception(exc)
        # Consumers may annotate their own copy, never another observation.
        return copy.deepcopy(future.result())

    def submit(self, key: Any, read: Callable[[], T]) -> Future[T]:
        with self.lock:
            if key not in self.tasks:
                self.tasks[key] = self.pool.submit(read)
            return self.tasks[key]

    def map(self, read: Callable[[T], R], values: list[T]) -> list[R]:
        # executor.map eagerly queues the entire portfolio. Keep at most one
        # worker window pending, preserving source order and bounded memory.
        results = []
        for offset in range(0, len(values), self.workers):
            futures = [self.pool.submit(read, value) for value in values[offset:offset + self.workers]]
            results.extend(future.result() for future in futures)
        return results

    def drain(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        # In particular, an authentication failure in an unused read-ahead
        # task must never be hidden by its consumer's scan bound.
        for future in self.tasks.values():
            if not future.cancelled():
                future.result()

    def receipt(self) -> dict[str, Any]:
        return {"elapsed_seconds": round(time.monotonic() - self.started, 3),
                "time_budget_seconds": self.seconds, "deadline_exhausted": self.expired,
                "workers": self.workers, "reads": self.requests, "reused_reads": self.reused,
                "phases": [*self.phases, self._current_phase()]}
