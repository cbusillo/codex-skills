# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bounded, content-silent filesystem probes for repo_cleanup.py (POSIX)."""

from __future__ import annotations

import errno
import hashlib
import hmac
import multiprocessing
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


class ProbeError(Exception):
    """Only a fixed reason code crosses a probe boundary, never exception text."""


def signature(info: os.stat_result) -> dict[str, int]:
    return dict(zip(("device", "inode", "mode", "size", "mtime_ns", "ctime_ns", "links", "uid", "gid"),
                    (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                     info.st_mtime_ns, info.st_ctime_ns, info.st_nlink, info.st_uid, info.st_gid)))


def tag(key: bytes, value: bytes) -> str:
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def _worker(sender: Any, operation: Callable, args: tuple) -> None:
    os.setsid()
    try:
        sender.send({"result": operation(*args)})
    except ProbeError as exc:
        sender.send({"error": str(exc)})
    except OSError as exc:
        sender.send({"error": "permission_denied" if exc.errno in (errno.EACCES, errno.EPERM)
                     else "filesystem_unavailable", "errno": exc.errno})
    except Exception:
        sender.send({"error": "probe_failed"})
    finally:
        sender.close()


def bounded(operation: Callable, args: tuple, timeout: float) -> dict:
    """A hung filesystem or child command cannot turn a requested root into a hang."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return {"error": "unsupported_platform"}
    if timeout <= 0:
        return {"error": "timed_out"}
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(sender, operation, args))
    process.start()
    sender.close()
    try:
        if receiver.poll(timeout):
            try:
                return receiver.recv()
            except EOFError:
                return {"error": "probe_interrupted"}
        return {"error": "timed_out"}
    finally:
        # The worker owns this process group, including Git/lsof descendants.
        cleanup_deadline = time.monotonic() + 1
        while True:
            # macOS can return EPERM for a zombie-only group until its leader is reaped.
            process.join(timeout=0)
            try:
                os.killpg(process.pid, signal.SIGKILL)
                break
            except ProcessLookupError:
                break
            except PermissionError:
                if time.monotonic() >= cleanup_deadline:
                    raise ProbeError("probe_cleanup_unconfirmed") from None
                time.sleep(0.01)
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        receiver.close()


def command(argv: list[str], *, timeout: float = 8, limit: int = 8_000_000,
            env: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Bound both output and time; stderr can contain credential-bearing URLs."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as process:
        output = bytearray()
        consumed = 0
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProbeError("command_timed_out")
                    for entry, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(entry.fd, 65536)
                        if not chunk:
                            selector.unregister(entry.fileobj)
                            continue
                        consumed += len(chunk)
                        if consumed > limit:
                            raise ProbeError("command_output_limit")
                        if entry.data:
                            output.extend(chunk)
                return process.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
            finally:
                if process.poll() is None:
                    process.kill()


PRIVATE_PARTS = frozenset({".local", ".ssh", ".aws", ".gnupg", "secrets", "credentials", "notes",
                           "private", "recovery", "backups", ".trash", ".trashes", "trash", "host-caches"})
PRIVATE_SUFFIXES = (".env", ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".kdbx",
                    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".sqlite-wal",
                    ".sqlite-shm", ".sql", ".dump", ".bak")


def classify(relative: str, kind: str, tracked: bool) -> str:
    parts = [part.lower() for part in Path(relative).parts]
    name = parts[-1] if parts else ""
    template = tracked and (name.endswith((".example", ".sample", ".template"))
                            or ".example." in name or ".sample." in name)
    if not template and (any(part in PRIVATE_PARTS for part in parts)
                         or name in (".env", ".envrc", ".npmrc", ".netrc", ".pypirc")
                         or name.startswith((".env.", "env."))
                         or name.endswith(PRIVATE_SUFFIXES)
                         or any(word in name for word in ("credential", "secret", "recovery", "private", "id_rsa", "id_ed25519"))):
        return "protected_private"
    if kind != "file" and kind != "directory":
        return "boundary"
    if tracked:
        return "tracked_source"
    if kind == "file" and name.endswith((".pyc", ".pyo")) and "__pycache__" in parts:
        return "generated_hint"
    return "unclassified"


def _walk(root: str, key: bytes | None, tracked: set[str], ignored: set[str],
          options: dict) -> dict:
    records: dict[str, dict] = {}
    exclusions: list[dict] = []
    total_bytes = 0
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_stat = os.fstat(root_fd)

    def visit(directory_fd: int, relative: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > options["max_depth"]:
            raise ProbeError("depth_limit")
        before = signature(os.fstat(directory_fd))
        with os.scandir(directory_fd) as iterator:
            for item in iterator:
                if len(records) >= options["max_entries"]:
                    raise ProbeError("entry_limit")
                name = item.name
                path = f"{relative}/{name}" if relative else name
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                kind = ("directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode)
                        else "symlink" if stat.S_ISLNK(info.st_mode) else "special")
                record = {"stat": signature(info), "kind": kind,
                          "git": "tracked" if path in tracked else "ignored" if path in ignored else "untracked",
                          "category": classify(path, kind, path in tracked)}
                records[path] = record
                if any(path == excluded or path.startswith(excluded + "/") for excluded in options["exclude"]):
                    exclusions.append({"path": path, "reason": "explicit_scope_exclusion"})
                    continue
                if kind == "directory":
                    if name == ".git":
                        exclusions.append({"path": path, "reason": "protected_git_metadata"})
                        continue
                    if info.st_dev != root_stat.st_dev:
                        exclusions.append({"path": path, "reason": "mount_boundary"})
                        continue
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                    try:
                        if signature(os.fstat(child_fd)) != signature(info):
                            raise ProbeError("path_changed")
                        # Both nested worktrees and bare repos are boundaries, never traversed.
                        child_names = set()
                        with os.scandir(child_fd) as children:
                            for child in children:
                                child_names.add(child.name)
                                if len(child_names) > options["max_entries"]:
                                    raise ProbeError("entry_limit")
                        if ".git" in child_names or {"HEAD", "objects", "refs"} <= child_names:
                            record["category"] = "nested_repository"
                            exclusions.append({"path": path, "reason": "nested_repository"})
                            continue
                        visit(child_fd, path, depth + 1)
                    finally:
                        os.close(child_fd)
                elif kind == "file" and key is not None:
                    total_bytes += info.st_size
                    if total_bytes > options["max_bytes"]:
                        raise ProbeError("byte_limit")
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    try:
                        if signature(os.fstat(fd)) != signature(info):
                            raise ProbeError("path_changed")
                        digest = hmac.new(key, digestmod=hashlib.sha256)
                        read_bytes = 0
                        while chunk := os.read(fd, 65536):
                            read_bytes += len(chunk)
                            if read_bytes > info.st_size:
                                raise ProbeError("file_changed")
                            digest.update(chunk)
                        if read_bytes != info.st_size or signature(os.fstat(fd)) != signature(info):
                            raise ProbeError("file_changed")
                        record["_content_tag"] = digest.hexdigest()
                    finally:
                        os.close(fd)
                elif kind == "symlink" and key is not None:
                    record["_content_tag"] = tag(key, os.fsencode(os.readlink(name, dir_fd=directory_fd)))
        if signature(os.fstat(directory_fd)) != before:
            raise ProbeError("directory_changed")

    try:
        visit(root_fd, "", 0)
        if signature(os.stat(root, follow_symlinks=False)) != signature(root_stat):
            raise ProbeError("root_changed")
        return {"identity": signature(root_stat), "entries": records, "exclusions": exclusions}
    finally:
        os.close(root_fd)


def scan_root(requested: str, key: bytes | None, tracked: set[str], ignored: set[str], options: dict) -> dict:
    root = os.path.realpath(requested, strict=True)
    first = _walk(root, key, tracked, ignored, options)
    second = _walk(root, key, tracked, ignored, options)
    if first != second or os.path.realpath(requested, strict=True) != root:
        raise ProbeError("scan_changed")
    unique = {(entry["stat"]["device"], entry["stat"]["inode"]): entry["stat"]["size"]
              for entry in first["entries"].values() if entry["kind"] == "file"}
    return {"resolved": root, "root_alias": requested != root, **first,
            "unique_file_bytes": sum(unique.values()), "content_verified": key is not None
            and all(item["kind"] == "directory" or "_content_tag" in item for item in first["entries"].values())}


def use_probe(root: str) -> dict:
    try:
        code, output = command(["lsof", "-nP", "-t", "+D", root], timeout=2, limit=65536)
        pids = sorted({int(line) for line in output.splitlines() if line.isdigit()})
        return {"state": "observed" if pids else "unknown", "pids": pids[:64],
                "method": "lsof_recursive_best_effort", "exit_code": code,
                "absence_proves_inactive": False}
    except (OSError, ProbeError):
        return {"state": "unknown", "method": "lsof_unavailable_or_incomplete",
                "absence_proves_inactive": False}
