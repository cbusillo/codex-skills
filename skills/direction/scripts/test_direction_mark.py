#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The marker writer records turns and audits the session-start hook can read."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("direction_mark.py")
HOOK = Path(__file__).resolve().parents[3] / "hooks" / "direction_check_hook.py"
NOW = dt.datetime(2026, 9, 22, 12, second=30, microsecond=123456, tzinfo=dt.timezone.utc)


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_turn_records_repository_and_audit_preserves_daily_evidence() -> None:
    mark = load(SCRIPT, "direction_mark_under_test")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "direction-last-check.json"
        first = mark.mark_turn(path, "owner/start", NOW)
        assert first == {"turn": mark.utc_stamp(NOW), "turn_repo": "owner/direction", "audits": {}}, first
        later = mark.mark_audit(path, "owner/repo", NOW + dt.timedelta(days=1))
        stamp = mark.utc_stamp(NOW + dt.timedelta(days=1))
        assert later == {**first, "audits": {"owner/repo": stamp}, "audit_runs": {"owner/repo": {"ran": stamp}}}, later
        again = mark.mark_turn(path, "next", NOW + dt.timedelta(days=2))
        assert again["turn_repo"] == "next/direction"
        assert again["audits"] == {"owner/repo": "2026-09-23T12:00:30Z"}, "a turn must not touch audit stamps"
        other = mark.mark_audit(path, "owner/other", NOW + dt.timedelta(days=3))
        assert (other["turn"], other["turn_repo"]) == (again["turn"], again["turn_repo"])
        assert set(other["audits"]) == {"owner/repo", "owner/other"}, "audits are kept per repository"
        assert json.loads(path.read_text()) == other


def test_hook_reads_what_the_marker_writes() -> None:
    mark = load(SCRIPT, "direction_mark_under_test2")
    hook = load(HOOK, "direction_check_hook_under_test")
    assert hook.MARKER_NAME == mark.MARKER_NAME
    assert (hook.TURN_RECORD_TAG, hook.WEEKLY_RECORD_TAG) == (mark.TURN_RECORD_TAG, mark.WEEKLY_RECORD_TAG)
    for env in ({"HOME": "/h"}, {"CODEX_HOME": "/h/.codex", "HOME": "/h"}, {"DIRECTION_MARKER": "/s/m.json"}):
        assert hook.marker_path(env) == mark.marker_path(env), env
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / mark.MARKER_NAME
        mark.mark_audit(path, "owner/repo", NOW)
        read = hook.read_marker(path)
        assert hook.marker_account(read) is None, "a single-repository audit does not name the account"
        assert hook.reminder_states(read, NOW + dt.timedelta(hours=1), "owner") == {"turn": "due", "audit": "due"}
        assert "turn" not in mark.load(path)
        mark.mark_turn(path, "owner", NOW)
        read = hook.read_marker(path)
        assert read["turn_repo"] == "owner/direction" and hook.marker_account(read) == "owner"
        assert hook.reminder_states(read, NOW + dt.timedelta(hours=1), "owner") == {"turn": "current", "audit": "due"}
        mark.mark_weekly(path, "owner", NOW)
        read = hook.read_marker(path)
        assert hook.reminder(read, NOW + dt.timedelta(hours=1), path, "owner") == ""
        assert hook.reminder_states(read, NOW + dt.timedelta(days=8), "owner") == {"turn": "due", "audit": "due"}
        assert mark.load(path)["audits"] == {"owner/repo": mark.utc_stamp(NOW)}, "the weekly stamp leaves per-repository windows alone"


def test_malformed_marker_is_replaced_not_crashed() -> None:
    mark = load(SCRIPT, "direction_mark_under_test3")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.json"
        path.write_text("[1, 2]")
        assert mark.mark_turn(path, "owner/start", NOW) == {"turn": mark.utc_stamp(NOW), "turn_repo": "owner/direction", "audits": {}}


def test_only_a_turn_can_be_marked_by_hand() -> None:
    mark = load(SCRIPT, "direction_mark_under_test4")
    try:
        mark.main(["audit"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("audit must not be markable from the command line")


def test_turn_cli_requires_coverage_and_reports_written_repository() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "marker.json"
        env = {**os.environ, "DIRECTION_MARKER": str(path)}
        for args in (["turn"], ["turn", "--repo", "missing-owner"], ["turn", "--owner", "owner/repo/extra"],
                     ["turn", "--owner", "owner", "--repo", "owner/start"]):
            result = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)
            assert result.returncode == 2, args
            assert not path.exists()
        mark = load(SCRIPT, "direction_mark_cli")
        github = FakeGitHub(Path(tmp) / "github")
        receipts = []
        for args in (["--owner", "owner"], ["--repo", "owner/start"]):
            printed = io.StringIO()
            with patch.dict(os.environ, {"DIRECTION_MARKER": str(path)}), patch.object(mark, "GITHUB_SCRIPTS", github.scripts), redirect_stdout(printed):
                assert mark.main(["turn", *args]) == 0
            written = json.loads(path.read_text())
            output = json.loads(printed.getvalue())
            assert output["turn_repo"] == written["turn_repo"] == "owner/direction", "the --repo alias records OWNER/direction too"
            assert output["turn"] == written["turn"]
            assert output["shared"]["repo"] == "owner/direction"
            assert written["audits"] == {}
            receipts.append((output["marker"], output["turn_repo"], output["shared"]["repo"]))
        assert receipts[0] == receipts[1]
        assert "covering owner/direction" in github.issues()[0]["body"]
        github.fail = True
        printed = io.StringIO()
        with patch.dict(os.environ, {"DIRECTION_MARKER": str(path)}), patch.object(mark, "GITHUB_SCRIPTS", github.scripts), redirect_stdout(printed):
            assert mark.main(["turn", "--owner", "owner"]) == 2, "a failed shared write must not look like success"
        output = json.loads(printed.getvalue())
        assert output["ok"] is False and output["shared"]["ok"] is False
        assert json.loads(path.read_text())["turn"] == output["turn"], "the local turn is still recorded"


def test_turn_receipt_is_the_same_from_any_starting_folder() -> None:
    mark = load(SCRIPT, "direction_mark_folders")
    import types

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return NOW.astimezone(tz)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        github = FakeGitHub(root / "github")
        folders = []
        for name, origin, adopted in (("adopted", "owner/adopted", True), ("product", "owner/product", False),
                                      ("fork", "someone/fork", False)):
            folder = root / name
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
            subprocess.run(["git", "-C", str(folder), "remote", "add", "origin", f"git@github.com:{origin}.git"], check=True)
            if adopted:
                (folder / "DIRECTION.md").write_text("# Direction\n")
            folders.append(folder)
        (root / "plain").mkdir()
        folders.append(root / "plain")
        with patch.object(mark, "GITHUB_SCRIPTS", github.scripts):
            assert mark.share_turn("2026-09-01T00:00:00Z", "owner")["ok"], "an earlier turn already created the record"
        receipts = set()
        start = Path.cwd()
        for folder in folders:
            path = root / "marker.json"
            path.unlink(missing_ok=True)
            printed = io.StringIO()
            os.chdir(folder)
            try:
                with (patch.dict(os.environ, {"DIRECTION_MARKER": str(path)}), patch.object(mark, "GITHUB_SCRIPTS", github.scripts),
                      patch.object(mark, "dt", types.SimpleNamespace(datetime=Clock, timezone=dt.timezone)), redirect_stdout(printed)):
                    assert mark.main(["turn", "--owner", "owner"]) == 0
            finally:
                os.chdir(start)
            receipts.add(printed.getvalue())
        assert len(receipts) == 1, receipts


def test_turn_cli_reports_local_write_failure_without_sharing() -> None:
    mark = load(SCRIPT, "direction_mark_local_failure")
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp) / "not-a-directory"
        parent.write_text("keep")
        path = parent / "marker.json"
        output, errors = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, {"DIRECTION_MARKER": str(path)}),
              patch.object(mark, "share_turn") as shared,
              redirect_stdout(output), redirect_stderr(errors)):
            assert mark.main(["turn", "--repo", "owner/start"]) == 1
        result = json.loads(output.getvalue())
        assert result["ok"] is False and result["local_recorded"] is False
        assert result["marker"] == str(path) and result["error"]
        assert str(path) in errors.getvalue() and result["error"] in errors.getvalue()
        shared.assert_not_called()
        assert parent.read_text() == "keep"


def test_turn_cli_reports_marker_path_resolution_failure_without_sharing() -> None:
    mark = load(SCRIPT, "direction_mark_path_failure")
    marker = "~unresolvable-user/marker.json"
    output, errors = io.StringIO(), io.StringIO()
    with (patch.dict(os.environ, {"DIRECTION_MARKER": marker}),
          patch.object(Path, "expanduser", side_effect=RuntimeError("Could not determine home directory.")),
          patch.object(mark, "share_turn") as shared,
          redirect_stdout(output), redirect_stderr(errors)):
        assert mark.main(["turn", "--repo", "owner/start"]) == 1
    result = json.loads(output.getvalue())
    assert result["ok"] is False and result["local_recorded"] is False
    assert result["marker"] == marker and result["error"] in errors.getvalue()
    shared.assert_not_called()


class FakeGitHub:
    """gh-with-env-token and gh-issue stand-ins backed by one issue list, as OWNER/direction would be."""

    def __init__(self, root: Path) -> None:
        self.scripts = root / "scripts"
        self.scripts.mkdir(parents=True)
        self.state = root / "issues.json"
        self.state.write_text("[]")
        self.failure = root / "fail"
        shebang = f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\nstate = Path({str(self.state)!r})\n"
        guard = f"if Path({str(self.failure)!r}).exists():\n    sys.exit('gh: HTTP 403 Resource not accessible by integration')\n"
        # Serve one page of the issue list, as GitHub does for page=N&per_page=M.
        (self.scripts / "gh-with-env-token").write_text(shebang + guard + (
            "import re\n"
            "query = dict(re.findall(r'[?&](\\w+)=([^&]+)', sys.argv[2]))\n"
            "size, page = int(query.get('per_page', 30)), int(query.get('page', 1))\n"
            "print(json.dumps(json.loads(state.read_text())[(page - 1) * size:page * size]))\n"
        ))
        (self.scripts / "gh-issue").write_text(shebang + guard + (
            "issues = json.loads(state.read_text())\n"
            "body = sys.stdin.read()\n"
            "if sys.argv[1] == 'create':\n"
            "    issues.append({'number': len(issues) + 1, 'body': body + '\\n<!-- provenance -->\\n', 'author_association': 'NONE', 'user': {'type': 'Bot'}})\n"
            "else:\n"
            "    next(i for i in issues if i['number'] == int(sys.argv[2]))['body'] = body\n"
            "state.write_text(json.dumps(issues))\n"
        ))
        for script in self.scripts.iterdir():
            script.chmod(0o755)

    @property
    def fail(self) -> bool:
        return self.failure.exists()

    @fail.setter
    def fail(self, value: bool) -> None:
        self.failure.write_text("") if value else self.failure.unlink(missing_ok=True)

    def issues(self) -> list[dict[str, Any]]:
        return json.loads(self.state.read_text())

    def seed(self, count: int) -> None:
        """Open issues the Director filed that are not records."""
        issues = self.issues()
        start = len(issues)
        issues += [{"number": start + n + 1, "body": "Other work", "author_association": "OWNER", "user": {"type": "User"}}
                   for n in range(count)]
        self.state.write_text(json.dumps(issues))

    def add_record(self, body: str, **issue: object) -> int:
        issues = self.issues()
        number = len(issues) + 1
        issues.append({"number": number, "body": body, "author_association": "NONE", "user": {"type": "Bot"}, **issue})
        self.state.write_text(json.dumps(issues))
        return number


def test_two_machines_one_turn() -> None:
    """A turn marked on one machine clears the daily reminder on another the same day."""
    mark = load(SCRIPT, "direction_mark_two_machines")
    hook = load(HOOK, "direction_check_hook_two_machines")
    with tempfile.TemporaryDirectory() as tmp:
        github = FakeGitHub(Path(tmp) / "github")
        first, second = Path(tmp) / "first.json", Path(tmp) / "second.json"
        mark.mark_turn(second, "owner", NOW - dt.timedelta(days=3))
        mark.mark_weekly(second, "owner", NOW)
        later = NOW - dt.timedelta(days=1)
        for when in (later, NOW):
            stamp = mark.mark_turn(first, "owner", when)["turn"]
            with patch.object(mark, "GITHUB_SCRIPTS", github.scripts):
                assert mark.share_turn(str(stamp), "owner")["ok"]
        assert len(github.issues()) == 1, "later turns update the one record instead of adding issues"
        soon = NOW + dt.timedelta(hours=1)
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"):
            seen = hook.with_shared_records(hook.read_marker(second), soon, "owner")
        assert seen["turn"] == NOW.replace(microsecond=0)
        assert hook.reminder(seen, soon, second, "owner") == ""
        assert hook.reminder_states(hook.read_marker(second), soon, "owner")["turn"] == "due", "the second machine's own marker is unchanged"
        github.fail = True
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"):
            blind = hook.with_shared_records(hook.read_marker(second), soon, "owner")
        assert hook.reminder_states(blind, soon, "owner") == {"turn": "unconfirmed", "audit": "current"}
        assert "HTTP 403" in hook.reminder(blind, soon, second, "owner")


def test_two_machines_one_weekly_audit() -> None:
    """A weekly audit recorded on one machine clears the audit reminder on another."""
    mark = load(SCRIPT, "direction_mark_two_machines_weekly")
    hook = load(HOOK, "direction_check_hook_two_machines_weekly")
    with tempfile.TemporaryDirectory() as tmp:
        github = FakeGitHub(Path(tmp) / "github")
        first, second = Path(tmp) / "first.json", Path(tmp) / "second.json"
        mark.mark_turn(second, "owner", NOW)
        mark.mark_weekly(second, "owner", NOW - dt.timedelta(days=9))
        stamp = mark.mark_weekly(first, "owner", NOW)["weekly_audit"]["ran"]
        with patch.object(mark, "GITHUB_SCRIPTS", github.scripts):
            assert mark.share_weekly(str(stamp), "owner")["ok"]
        soon = NOW + dt.timedelta(hours=1)
        assert hook.reminder_states(hook.read_marker(second), soon, "owner")["audit"] == "due"
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"):
            seen = hook.with_shared_records(hook.read_marker(second), soon, "owner")
        assert hook.reminder_states(seen, soon, "owner") == {"turn": "current", "audit": "current"}
        assert hook.reminder(seen, soon, second, "owner") == ""
        github.fail = True
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"):
            blind = hook.with_shared_records(hook.read_marker(second), soon, "owner")
        assert hook.reminder_states(blind, soon, "owner") == {"turn": "current", "audit": "unconfirmed"}
        assert len(hook.reminder(blind, soon, second, "owner").splitlines()) == 1


def test_turn_and_weekly_records_leave_each_other_untouched() -> None:
    mark = load(SCRIPT, "direction_mark_separate_records")
    with tempfile.TemporaryDirectory() as tmp:
        github = FakeGitHub(Path(tmp) / "github")
        with patch.object(mark, "GITHUB_SCRIPTS", github.scripts):
            assert mark.share_turn("2026-09-20T00:00:00Z", "owner")["ok"]
            turn_body = github.issues()[0]["body"]
            assert mark.share_weekly("2026-09-21T00:00:00Z", "owner")["ok"]
            issues = github.issues()
            assert len(issues) == 2 and issues[0]["body"] == turn_body, "a weekly write leaves the turn record alone"
            weekly_body = issues[1]["body"]
            assert mark.share_turn("2026-09-22T00:00:00Z", "owner")["ok"]
            issues = github.issues()
            assert len(issues) == 2 and issues[1]["body"] == weekly_body, "a turn write leaves the weekly record alone"
            assert "2026-09-22T00:00:00Z" in issues[0]["body"]
            assert mark.share_weekly("2026-09-23T00:00:00Z", "owner")["ok"]
            issues = github.issues()
            assert len(issues) == 2 and "2026-09-22T00:00:00Z" in issues[0]["body"] and "2026-09-23T00:00:00Z" in issues[1]["body"]


def test_a_record_beyond_the_first_page_is_found_and_updated() -> None:
    mark = load(SCRIPT, "direction_mark_paged_records")
    hook = load(HOOK, "direction_check_hook_paged_records")
    with tempfile.TemporaryDirectory() as tmp:
        github = FakeGitHub(Path(tmp) / "github")
        github.seed(150)
        record = github.add_record(mark.weekly_record_body("2026-09-01T00:00:00Z"))
        github.seed(10)
        with patch.object(mark, "GITHUB_SCRIPTS", github.scripts):
            assert mark.share_weekly(mark.utc_stamp(NOW), "owner") == {"ok": True, "repo": "owner/direction", "issue": record}
        issues = github.issues()
        assert len(issues) == 161, "the record is updated, not duplicated"
        assert mark.utc_stamp(NOW) in issues[record - 1]["body"]
        local = hook.read_marker(Path(tmp) / "fresh-machine.json")
        soon = NOW + dt.timedelta(hours=1)
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"):
            seen = hook.with_shared_records(local, soon, "owner")
        assert hook.reminder_states(seen, soon, "owner")["audit"] == "current", "the reader finds it beyond the first page"


def test_incomplete_record_discovery_is_unconfirmed_and_creates_nothing() -> None:
    mark = load(SCRIPT, "direction_mark_bounded_discovery")
    hook = load(HOOK, "direction_check_hook_bounded_discovery")
    with tempfile.TemporaryDirectory() as tmp:
        github = FakeGitHub(Path(tmp) / "github")
        github.seed(2 * 100)
        path = Path(tmp) / "marker.json"
        printed = io.StringIO()
        with (patch.object(mark, "GITHUB_SCRIPTS", github.scripts), patch.object(mark, "RECORD_PAGES", 2),
              patch.dict(os.environ, {"DIRECTION_MARKER": str(path)}), redirect_stdout(printed), redirect_stderr(io.StringIO())):
            assert mark.share_weekly(mark.utc_stamp(NOW), "owner")["ok"] is False
            assert mark.main(["turn", "--owner", "owner"]) == 2, "an unfinished search is not shared"
        assert len(github.issues()) == 200, "no record is created when one may exist beyond the pages read"
        assert json.loads(printed.getvalue())["shared"]["ok"] is False
        soon = NOW + dt.timedelta(hours=1)
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"), patch.object(hook, "RECORD_PAGES", 2):
            seen = hook.with_shared_records(hook.read_marker(Path(tmp) / "other.json"), soon, "owner")
        assert hook.reminder_states(seen, soon, "owner") == {"turn": "unconfirmed", "audit": "unconfirmed"}


def test_several_records_reader_takes_the_newest_and_writer_updates_the_oldest_trusted() -> None:
    mark = load(SCRIPT, "direction_mark_duplicate_records")
    hook = load(HOOK, "direction_check_hook_duplicate_records")
    with tempfile.TemporaryDirectory() as tmp:
        github = FakeGitHub(Path(tmp) / "github")
        forged = github.add_record(mark.weekly_record_body("2026-09-01T00:00:00Z"), author_association="NONE", user={"type": "User"})
        oldest = github.add_record(mark.weekly_record_body("2026-09-10T00:00:00Z"))
        newest = github.add_record(mark.weekly_record_body(mark.utc_stamp(NOW)), author_association="MEMBER", user={"type": "User"})
        with patch.object(hook, "GH_READER", github.scripts / "gh-with-env-token"):
            shared, error = hook.read_shared_records("owner")
        assert error is None and shared["weekly"] == NOW.replace(microsecond=0), "the newest stamp across the records"
        before = github.issues()
        with patch.object(mark, "GITHUB_SCRIPTS", github.scripts):
            assert mark.share_weekly("2026-09-23T00:00:00Z", "owner")["issue"] == oldest
        after = github.issues()
        assert "2026-09-23T00:00:00Z" in after[oldest - 1]["body"], "the oldest trusted record is the one written"
        assert after[forged - 1] == before[forged - 1] and after[newest - 1] == before[newest - 1]
        assert len(after) == 3


def test_overlapping_audits_and_turn_preserve_updates() -> None:
    mark = load(SCRIPT, "direction_mark_concurrency")
    for kind in ("turn", "audit"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marker.json"
            mark.mark_turn(path, "owner/start", NOW)
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
                    "mark.mark_turn(Path(sys.argv[2]),'o/daily',now) if sys.argv[3]=='turn' "
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
                assert current["turn"] == ("2026-10-04T00:00:00Z" if kind == "turn" else mark.utc_stamp(NOW)), current
                assert current["turn_repo"] == ("o/direction" if kind == "turn" else "owner/direction")
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
                mark.mark_turn(path, "owner/start", NOW)
            except OSError:
                pass
            else:
                raise AssertionError("replacement failure was hidden")
        assert path.read_bytes() == original
        assert list(Path(tmp).glob("*.pending-*")) == []
        assert mark.mark_turn(path, "owner/start", NOW)["other"] is True


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
            "mark.mark_turn(Path(sys.argv[2]),'o/daily',dt.datetime.now(dt.timezone.utc))",
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
            assert mark.mark_turn(path, "owner/start", NOW)["other"] is True
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
