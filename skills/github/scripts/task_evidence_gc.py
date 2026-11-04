#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import time
import uuid
from pathlib import Path

from dev_worktree import artifact_roots

RETENTION_SECONDS = 30 * 86400


def raise_walk_error(error: OSError) -> None:
    raise error


def newest_mtime(name: str, parent_fd: int) -> float:
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    newest = info.st_mtime
    if not stat.S_ISDIR(info.st_mode):
        return newest
    for _, directories, files, directory_fd in os.fwalk(name, dir_fd=parent_fd, onerror=raise_walk_error):
        for entry in directories + files:
            newest = max(newest, os.stat(entry, dir_fd=directory_fd, follow_symlinks=False).st_mtime)
    return newest


def remove(name: str, parent_fd: int, cutoff: float) -> bool:
    quarantine = f".expiring-{uuid.uuid4().hex}"
    os.rename(name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    try:
        if newest_mtime(quarantine, parent_fd) >= cutoff:
            return False
        info = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            for _, _, _, directory_fd in os.fwalk(quarantine, dir_fd=parent_fd):
                os.fchmod(directory_fd, os.fstat(directory_fd).st_mode | stat.S_IWUSR)
            shutil.rmtree(quarantine, dir_fd=parent_fd)
        else:
            os.unlink(quarantine, dir_fd=parent_fd)
        return True
    finally:
        if quarantine in os.listdir(parent_fd):
            if name in os.listdir(parent_fd):
                print(f"kept concurrent evidence at {quarantine}; original name reused: {name}", file=sys.stderr)
            else:
                os.rename(quarantine, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)


def collect(parent_fd: int, path: Path, cutoff: float, deadline: float, apply: bool, *, repositories: bool = False) -> None:
    for name in sorted(os.listdir(parent_fd)):
        if time.monotonic() >= deadline:
            print("evidence sweep budget exhausted; remaining units kept", file=sys.stderr)
            return
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if repositories and stat.S_ISDIR(info.st_mode):
                repo_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    collect(repo_fd, path / name, cutoff, deadline, apply)
                finally:
                    os.close(repo_fd)
                if apply:
                    try:
                        os.rmdir(name, dir_fd=parent_fd)
                    except OSError:
                        continue
            elif newest_mtime(name, parent_fd) < cutoff and (not apply or remove(name, parent_fd, cutoff)):
                print(f"{'removed' if apply else 'would remove'} {path / name}", file=sys.stderr)
        except OSError as exc:
            print(f"kept, could not measure/remove {path / name}: {exc}", file=sys.stderr)


def open_directory(path: Path) -> int:
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def sweep(*, apply: bool = False, budget_seconds: float = float("inf")) -> int:
    root, _ = artifact_roots()
    if apply and not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("symlink-resistant deletion is unavailable; evidence kept")
    if not root.exists():
        return 0
    deadline = time.monotonic() + budget_seconds
    cutoff = time.time() - RETENTION_SECONDS
    root_fd = open_directory(root)
    try:
        collect(root_fd, root, cutoff, deadline, apply, repositories=True)
    finally:
        os.close(root_fd)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expire task evidence after 30 quiet days.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        return sweep(apply=args.apply)
    except (OSError, ValueError) as exc:
        print(f"evidence sweep refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
