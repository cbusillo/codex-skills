#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The marker writer records turns and audits the session-start hook can read."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("direction_mark.py")
HOOK = Path(__file__).resolve().parents[3] / "hooks" / "direction_check_hook.py"
NOW = dt.datetime(2026, 9, 22, 12, 0, 30, 123456, dt.timezone.utc)


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_turn_updates_turn_only_and_audit_updates_both() -> None:
    mark = load(SCRIPT, "direction_mark_under_test")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "direction-last-check.json"
        first = mark.mark_turn(path, NOW)
        assert first == {"turn": "2026-09-22T12:00:30Z", "audits": {}}, first
        later = mark.mark_audit(path, "owner/repo", NOW + dt.timedelta(days=1))
        assert later == {"turn": "2026-09-23T12:00:30Z", "audits": {"owner/repo": "2026-09-23T12:00:30Z"}}, later
        again = mark.mark_turn(path, NOW + dt.timedelta(days=2))
        assert again["audits"] == {"owner/repo": "2026-09-23T12:00:30Z"}, "a turn must not touch audit stamps"
        other = mark.mark_audit(path, "owner/other", NOW + dt.timedelta(days=3))
        assert set(other["audits"]) == {"owner/repo", "owner/other"}, "audits are kept per repository"
        assert json.loads(path.read_text()) == other


def test_hook_reads_what_the_marker_writes() -> None:
    mark = load(SCRIPT, "direction_mark_under_test2")
    hook = load(HOOK, "direction_check_hook_under_test")
    assert hook.MARKER_NAME == mark.MARKER_NAME
    for env in ({"HOME": "/h"}, {"CODEX_HOME": "/h/.codex", "HOME": "/h"}, {"DIRECTION_MARKER": "/s/m.json"}):
        assert hook.marker_path(env) == mark.marker_path(env), env
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / mark.MARKER_NAME
        mark.mark_audit(path, "owner/repo", NOW)
        read = hook.read_marker(path)
        assert hook.reminder(read, NOW + dt.timedelta(hours=1), "owner/repo", path) == ""
        assert "last weekly audit of owner/repo was 8 days ago" in hook.reminder(read, NOW + dt.timedelta(days=8), "owner/repo", path)
        assert "owner/other has a DIRECTION.md but no recorded weekly audit" in hook.reminder(read, NOW + dt.timedelta(hours=1), "owner/other", path)


def test_malformed_marker_is_replaced_not_crashed() -> None:
    mark = load(SCRIPT, "direction_mark_under_test3")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.json"
        path.write_text("[1, 2]")
        assert mark.mark_turn(path, NOW) == {"turn": "2026-09-22T12:00:30Z", "audits": {}}


def test_only_a_turn_can_be_marked_by_hand() -> None:
    mark = load(SCRIPT, "direction_mark_under_test4")
    try:
        mark.main(["audit"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("audit must not be markable from the command line")


def test_overlapping_audits_and_turn_preserve_updates() -> None:
    mark = load(SCRIPT, "direction_mark_concurrency")
    for kind in ("turn", "audit"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marker.json"
            mark.mark_turn(path, NOW)
            writer: subprocess.Popen[str] | None = None
            read = mark.load

            def overlap(target: Path) -> dict[str, object]:
                nonlocal writer
                snapshot = read(target)
                writer = subprocess.Popen([
                    sys.executable, "-c",
                    "import datetime as dt, sys; from pathlib import Path; "
                    "sys.path.insert(0,sys.argv[1]); import direction_mark as mark; "
                    "now=dt.datetime(2026,10,4,tzinfo=dt.timezone.utc); "
                    "mark.mark_turn(Path(sys.argv[2]),now) if sys.argv[3]=='turn' "
                    "else mark.mark_audit(Path(sys.argv[2]),'o/second',now)",
                    str(SCRIPT.parent), str(path), kind,
                ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    writer.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                return snapshot

            try:
                with patch.dict(vars(mark), {"load": overlap}):
                    mark.mark_audit(path, "o/first", NOW)
                assert writer is not None
                stdout, stderr = writer.communicate(timeout=5)
                assert writer.returncode == 0, (stdout, stderr)
                current = json.loads(path.read_text())
                assert current["turn"] == "2026-10-04T00:00:00Z", current
                assert current["audits"]["o/first"] == mark.utc_stamp(NOW)
                assert set(current["audits"]) == ({"o/first", "o/second"} if kind == "audit" else {"o/first"})
            finally:
                if writer is not None and writer.poll() is None:
                    writer.kill()
                    writer.communicate()


def test_failed_replace_preserves_marker_and_releases_lock() -> None:
    mark = load(SCRIPT, "direction_mark_interrupted")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "marker.json"
        original = b'{"audits":{}, "other":true}\n'
        path.write_bytes(original)
        with patch.object(os, "replace", side_effect=OSError("interrupted replace")):
            try:
                mark.mark_turn(path, NOW)
            except OSError:
                pass
            else:
                raise AssertionError("replacement failure was hidden")
        assert path.read_bytes() == original
        assert list(Path(tmp).glob("*.pending-*")) == []
        assert mark.mark_turn(path, NOW)["other"] is True


def test_killed_writer_leaves_complete_marker_and_unlocked_sidecar() -> None:
    mark = load(SCRIPT, "direction_mark_killed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "marker.json"
        ready = Path(tmp) / "ready"
        original = b'{"audits":{}, "other":true}\n'
        path.write_bytes(original)
        writer = subprocess.Popen([
            sys.executable, "-c",
            "import datetime as dt, sys, time; from pathlib import Path; "
            "sys.path.insert(0,sys.argv[1]); import direction_mark as mark; "
            "mark.os.replace=lambda *_: (Path(sys.argv[3]).write_text('ready'),time.sleep(30)); "
            "mark.mark_turn(Path(sys.argv[2]),dt.datetime.now(dt.timezone.utc))",
            str(SCRIPT.parent), str(path), str(ready),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and writer.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "writer did not reach replacement"
            writer.kill()
            writer.communicate(timeout=5)
            assert path.read_bytes() == original
            assert mark.mark_turn(path, NOW)["other"] is True
        finally:
            if writer.poll() is None:
                writer.kill()
                writer.communicate()


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
