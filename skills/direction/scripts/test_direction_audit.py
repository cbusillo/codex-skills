#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Tests for the read-only direction audit. Each case plants a real drift."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("direction_audit.py")
NOW = dt.datetime(2026, 9, 21, 12, tzinfo=dt.timezone.utc)

DIRECTION = """# Direction

## Purpose
Ship it.

## Stop Boundaries
- spending money

## Journey
One change lands end to end.

## Retired
- the old thing

## Milestones
- `Thin fork decision` proves the engine choice; ends if the spikes fail.
- `Dogfood week` proves daily use; ends if the owner stops using it.
"""


def load() -> Any:
    spec = importlib.util.spec_from_file_location("direction_audit_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.planning_config = lambda _: {"labels": {"waiting": "plan:waiting", "blocked": "plan:blocked",
                                                   "stale": "plan:stale", "done": "plan:done"}}
    return module


def milestone(number: int, title: str, *, state: str = "open", creator: str = "owner") -> dict[str, Any]:
    return {"number": number, "title": title, "state": state, "creator": {"login": creator}}


def issue(number: int, title: str, *, body: str = "", labels: tuple[str, ...] = (), created: str = "2026-09-10T00:00:00Z") -> dict[str, Any]:
    return {"number": number, "title": title, "body": body, "labels": [{"name": name} for name in labels], "created_at": created}


def run(module: Any, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "direction_text": DIRECTION,
        "milestones": [milestone(1, "Thin fork decision"), milestone(2, "Dogfood week")],
        "issues": [],
        "owner": "owner",
        "automation": "bot",
        "now": NOW,
    }
    params.update(overrides)
    return module.audit(**params)


def kinds(result: dict[str, Any]) -> list[str]:
    return [item["kind"] for item in result["findings"]]


def test_clean_state_has_no_findings() -> None:
    module = load()
    result = run(module)
    assert result["ok"] is True, result
    assert result["listed_milestones"] == ["Thin fork decision", "Dogfood week"]


def test_owner_as_automation_is_a_limit_and_preserves_real_findings() -> None:
    module = load()
    clean = run(module, automation="OWNER")
    assert clean["ok"] is True
    assert clean["findings"] == []
    assert clean["counts"] == {}
    assert [item["kind"] for item in clean["limits"]] == ["owner_acts_as_automation"]
    assert run(module)["limits"] == [], "distinct automation retains the existing audit behavior"
    fallback = run(module, automation="owner", expected_automation="app[bot]")
    assert fallback["ok"] is False
    assert kinds(fallback) == ["coverage_incomplete"]
    assert fallback["limits"] == []
    assert run(module, automation="owner", expected_automation="OWNER")["ok"] is True
    assert "coverage_incomplete" in kinds(run(module, automation=None))
    dirty = run(module, automation="owner", truncated=["issues"], rulesets=[])
    assert dirty["ok"] is False
    assert set(kinds(dirty)) == {"coverage_incomplete", "ruleset_missing"}
    assert dirty["limits"] == clean["limits"]


def test_owner_only_cli_audit_uses_explicit_reader_and_returns_known_limit() -> None:
    import base64

    module = load()
    calls: list[list[str]] = []

    def read(request: list[str], *, gh: str) -> Any:
        assert gh == reader
        calls.append(request)
        endpoint = request[1]
        if endpoint.endswith("/contents/DIRECTION.md"):
            return {"content": base64.b64encode(DIRECTION.encode()).decode()}
        if endpoint == "user":
            return {"login": login}
        if "/milestones?" in endpoint:
            return [milestone(1, "Thin fork decision"), milestone(2, "Dogfood week")]
        if "/rulesets?" in endpoint:
            return [{"name": name, "target": "branch", "enforcement": "active"}
                    for name in module.github_rulesets.required_ruleset_names()]
        if "/issues?" in endpoint or "/pulls?" in endpoint:
            return []
        raise AssertionError(endpoint)

    for reader, login in (("gh", "owner"), ("gh", "other"), (str(module.WRAPPER), "owner"),
                          ("skills/github/scripts/gh-with-env-token", "owner")):
        calls.clear()
        output = StringIO()
        with (patch.dict(vars(module), {
                  "gh_json": read,
                  "previous_audit_stamp": lambda *_: None,
                  "record_audit": lambda *_: None,
              }),
              patch.dict(vars(module.github_identity), {
                  "configured_bot_logins": lambda: (),
                  "automation_login": lambda: None,
              }),
              redirect_stdout(output)):
            code = module.main(["--repo", "owner/repo", "--gh", reader])
        result = json.loads(output.getvalue())
        explicit = reader == "gh" and login == "owner"
        assert code == (0 if explicit else 3)
        assert result["ok"] is explicit
        assert result["read_only"] is True
        if explicit:
            assert result["findings"] == []
            assert [item["kind"] for item in result["limits"]] == ["owner_acts_as_automation"]
        else:
            assert kinds(result) == ["coverage_incomplete"]
            assert not result["ok"]
        assert calls and all(request[0] == "api" and request[-2:] == ["--method", "GET"] for request in calls)


def test_default_reader_error_exposes_read_only_owner_remedy() -> None:
    module = load()
    output = StringIO()

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise module.AuditError("fixture reader refusal")

    with patch.dict(vars(module), {"gh_json": fail}), redirect_stdout(output):
        assert module.main(["--repo", "owner/repo"]) == 1
    result = json.loads(output.getvalue())
    assert result["ok"] is False
    assert result["owner_reader_hint"]


def test_audit_questions_are_ordinary_open_issues_not_pull_requests() -> None:
    module = load()
    result = run(module, issues=[
        issue(10, "Compare costs", labels=("audit",)),
        issue(11, "Choose a name", labels=("plan", "Audit", "direction")),
        issue(12, "Unrelated"),
        {**issue(13, "A pull request", labels=("audit",)), "pull_request": {}},
    ])
    questions = [item for item in result["findings"] if item["kind"] == "audit_question"]
    assert questions == [
        {"kind": "audit_question", "number": 10, "title": "Compare costs"},
        {"kind": "audit_question", "number": 11, "title": "Choose a name"},
    ], result
    assert result["counts"] == {"audit_question": 2, "escalation_open": 1}
    assert result["ok"] is False


def test_closed_audit_work_uses_closure_time_and_the_previous_audit() -> None:
    module = load()
    previous = NOW - dt.timedelta(days=2)
    base = {**issue(20, "Completed routing fix", labels=("audit",)), "state": "closed"}
    recent = {**base, "closed_at": "2026-09-20T12:00:00Z"}
    old_edited = {**base, "number": 21, "closed_at": "2026-09-16T00:00:00Z", "updated_at": "2026-09-21T00:00:00Z"}
    boundary = {**base, "number": 22, "closed_at": previous.isoformat()}
    during_scan = {**base, "number": 23, "closed_at": (NOW + dt.timedelta(seconds=1)).isoformat()}
    result = run(module, audit_since=previous, issues=[
        recent, old_edited, boundary, during_scan,
        {**recent, "number": 24, "labels": []},
        {**recent, "number": 25, "pull_request": {}},
        {**base, "number": 26, "closed_at": None},
    ])
    assert result["findings"] == [
        {"kind": "audit_judge", "number": 20, "title": base["title"], "closed_at": recent["closed_at"]},
        {"kind": "audit_judge", "number": 22, "title": base["title"], "closed_at": boundary["closed_at"]},
    ], result
    assert result["counts"] == {"audit_judge": 2}
    assert kinds(run(module, issues=[old_edited])) == ["audit_judge"], "first audit uses a seven-day window"
    weeks_old = {**base, "closed_at": "2026-09-01T00:00:00Z"}
    assert kinds(run(module, issues=[weeks_old])) == []
    assert kinds(run(module, audit_since=NOW - dt.timedelta(days=30), issues=[weeks_old])) == ["audit_judge"]


def test_parse_reads_only_backticked_titles_under_milestones() -> None:
    module = load()
    parsed = module.parse_direction(DIRECTION + "\n## Notes\n- `Not a milestone` here\n")
    assert parsed["milestones"] == ["Thin fork decision", "Dogfood week"]
    assert parsed["missing_headings"] == []


def test_agent_milestone_additions_since_the_last_audit_are_listed_without_failing() -> None:
    module = load()
    assigned = {"title": "Thin fork decision"}
    base = {**issue(10, "Choose the engine"), "milestone": assigned}
    since = NOW - dt.timedelta(days=2)

    def added(by: str, at: dt.datetime = NOW - dt.timedelta(days=1), **extra: Any) -> dict[str, Any]:
        return {**base, **extra, "_milestone_added": {"by": by, "at": at.isoformat()}}

    result = run(module, audit_since=since, repo="o/r", issues=[added("bot"), added("", number=11), added("bot", state="closed", number=12)])
    assert result["ok"] is True, "additions are information, never findings"
    assert result["findings"] == []
    assert [(item["number"], item["added_by"]) for item in result["milestone_additions"]] == [(10, "bot"), (11, None), (12, "bot")]
    assert result["milestone_additions"][0] == {
        "repo": "o/r", "number": 10, "title": "Choose the engine", "milestone": "Thin fork decision",
        "added_by": "bot", "added_at": (NOW - dt.timedelta(days=1)).isoformat(),
    }
    assert run(module, audit_since=since, issues=[added("owner")])["milestone_additions"] == [], "the Director's additions are decisions"
    assert run(module, audit_since=since, issues=[added("bot", NOW - dt.timedelta(days=3))])["milestone_additions"] == [], "before the prior audit"
    unlisted = added("bot", milestone={"title": "Not in the file"})
    assert run(module, audit_since=since, issues=[unlisted])["milestone_additions"] == []
    assert run(module, audit_since=since, issues=[base])["milestone_additions"] == [], "unread events list nothing"
    same_login = run(module, automation="owner", audit_since=since, issues=[added("owner")])
    assert same_login["milestone_additions"] == []
    assert [item["kind"] for item in same_login["limits"]] == ["owner_acts_as_automation"]


def test_milestone_addition_uses_the_latest_event_across_renames() -> None:
    module = load()
    old = {"event": "milestoned", "milestone": {"title": "Old title"}, "actor": {"login": "bot"}, "created_at": "2026-09-01T00:00:00Z"}
    owner = {"event": "milestoned", "milestone": {"title": "Thin fork decision"}, "actor": {"login": "owner"}, "created_at": "2026-09-02T00:00:00Z"}
    assert module.milestone_addition([old]) == {"by": "bot", "at": "2026-09-01T00:00:00Z"}
    assert module.milestone_addition([owner, old])["by"] == "owner"
    assert module.milestone_addition([old, owner])["by"] == "owner"
    assert module.milestone_addition([{"event": "labeled"}]) is None


def test_event_reads_are_bounded_and_skip_pull_requests() -> None:
    module = load()
    first = {**issue(10, "One"), "milestone": {"title": "Thin fork decision"}}
    second = {**issue(11, "Two"), "milestone": {"title": "Thin fork decision"}}
    pull = {**issue(12, "PR"), "milestone": {"title": "Thin fork decision"}, "pull_request": {}}
    untouched = {**issue(13, "Untouched", created="2026-08-01T00:00:00Z"), "milestone": {"title": "Thin fork decision"}}
    calls: list[list[str]] = []
    def fetch(args: list[str]) -> list[dict[str, Any]]:
        calls.append(args)
        return [{"event": "milestoned", "actor": {"login": "bot"}, "created_at": "2026-09-10T00:00:00Z"}]
    cut = module.enrich_admission_actors(
        [first, second, pull, untouched], {"Thin fork decision": "- `Thin fork decision` proves the engine choice; ends if the spikes fail."},
        "o/r", fetch=fetch, since=NOW - dt.timedelta(days=30), max_issues=1,
    )
    assert cut is True
    assert first["_milestone_added"] == {"by": "bot", "at": "2026-09-10T00:00:00Z"}
    assert second["_admission_unknown"] is True
    assert "_milestone_added" not in pull
    assert len(calls) == 1
    module.enrich_admission_actors([untouched], {"Thin fork decision": ""}, "o/r", fetch=fetch, since=NOW - dt.timedelta(days=7))
    assert "_milestone_added" not in untouched and len(calls) == 1, "an issue not updated since the audit window needs no event read"


def test_issue_fetch_reads_recent_milestone_additions_including_closed_issues() -> None:
    module = load()
    open_issue = {**issue(10, "Open"), "milestone": {"title": "Thin fork decision"}, "state": "open", "updated_at": "2026-09-01T00:00:00Z"}
    closed_issue = {**issue(11, "Closed"), "milestone": {"title": "Thin fork decision"}, "state": "closed", "updated_at": "2026-09-20T00:00:00Z"}
    calls: list[str] = []
    def fetch(args: list[str]) -> list[dict[str, Any]]:
        path = args[1]
        calls.append(path)
        if "state=open" in path:
            return [open_issue]
        if "labels=audit" in path:
            return []
        if "state=closed" in path:
            return [closed_issue]
        if "/issues/11/events" in path:
            return [{"event": "milestoned", "actor": {"login": "bot"}, "created_at": "2026-09-20T00:00:00Z"}]
        raise AssertionError(path)
    found, truncated = module.fetch_audit_issues(
        "o/r", [milestone(1, "Thin fork decision")],
        {"Thin fork decision": "- `Thin fork decision` proves the engine choice; ends if the spikes fail."},
        NOW - dt.timedelta(days=7), fetch=fetch,
    )
    assert truncated == []
    assert {item["number"] for item in found} == {10, 11}
    assert closed_issue["_milestone_added"]["by"] == "bot"
    assert not any("/issues/10/events" in path for path in calls), "an issue untouched since the window needs no event read"
    assert any("state=open" in path for path in calls)
    assert any("state=closed&since=" in path for path in calls)


def test_closed_audit_fetch_is_independent_of_milestones_and_deduplicates() -> None:
    module = load()
    closed = {**issue(20, "Finished", labels=("audit",)), "state": "closed", "closed_at": NOW.isoformat()}
    calls: list[str] = []
    def fetch(args: list[str]) -> list[dict[str, Any]]:
        path = args[1]
        calls.append(path)
        if "state=open" in path:
            return [issue(10, "Question", labels=("audit",))]
        if "labels=audit" in path:
            return [closed] * 100 if path.endswith("&page=1") else [closed]
        if "milestone=1" in path:
            return [closed]
        raise AssertionError(path)
    for milestones in ([], [milestone(1, "Thin fork decision")]):
        found, truncated = module.fetch_audit_issues(
            "o/r", milestones, module.parse_direction(DIRECTION)["milestone_lines"],
            NOW - dt.timedelta(days=7), fetch=fetch, audit_since=NOW - dt.timedelta(days=2),
        )
        assert truncated == []
        assert [item["number"] for item in found] == [10, 20]
    assert any("labels=audit&since=2026-09-19T12:00:00Z&per_page=100&page=2" in path for path in calls)
    assert any("milestone=1&state=closed&since=2026-09-14T12:00:00Z" in path for path in calls)


def test_main_preserves_closed_audit_cutoff_and_stamps_scan_start() -> None:
    module = load()
    previous = "2026-09-19T12:00:00Z"
    closed = {**issue(20, "Finished", labels=("audit",)), "state": "closed", "closed_at": "2026-09-20T12:00:00Z"}
    class Clock(dt.datetime):
        ticks = 0

        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> Self:
            value = NOW + dt.timedelta(minutes=cls.ticks)
            cls.ticks += 1
            return cls.fromtimestamp(value.timestamp(), tz)
    for cap in (None, "closed_audit", "milestone_events"):
        Clock.ticks = 0
        calls: list[str] = []
        def fetch(args: list[str], *, gh: str) -> list[dict[str, Any]]:
            assert gh == "fixture-gh"
            assert args[-2:] == ["--method", "GET"]
            path = args[1]
            calls.append(path)
            if "labels=audit" in path:
                return [closed] * 100 if cap == "closed_audit" else [closed]
            if "state=open" in path and cap == "milestone_events":
                return [
                    {**issue(number, "Owner-admitted work"), "milestone": {"title": "Thin fork decision"},
                     "user": {"login": "o"}, "updated_at": "2026-09-20T00:00:00Z"}
                    for number in range(100, 151)
                ]
            if path.startswith("repos/o/r/issues/") and "/events?" in path:
                return [{"event": "milestoned", "actor": {"login": "o"}}]
            if "/milestones?" in path:
                return [milestone(1, "Thin fork decision"), milestone(2, "Dogfood week")]
            return []
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.json"
            original = {"turn": previous, "turn_repo": "o/daily", "audits": {"o/r": previous, "o/other": "2026-09-01T00:00:00Z"}}
            marker.write_text(json.dumps(original))
            output = StringIO()
            with (patch.dict("os.environ", {"DIRECTION_MARKER": str(marker)}),
                  patch.dict(vars(module), {
                      "merged_direction": lambda *_args, **_kwargs: DIRECTION,
                      "gh_json": fetch,
                      "dt": types.SimpleNamespace(datetime=Clock, timezone=dt.timezone, timedelta=dt.timedelta),
                  }),
                  patch.dict(vars(module.github_identity), {"configured_bot_logins": lambda: ("bot",)}),
                  redirect_stdout(output)):
                assert module.main(["--repo", "o/r", "--automation", "bot", "--gh", "fixture-gh"]) == 3
            result = json.loads(output.getvalue())
            assert result["audit_since"] == previous
            assert result["counts"]["audit_judge"] == 1
            saved = json.loads(marker.read_text())
            assert saved["turn"] == original["turn"]
            assert saved["turn_repo"] == original["turn_repo"]
            if cap is not None:
                listing = "recent_closed_audit_issues" if cap == "closed_audit" else "milestone_issue_events"
                assert listing in result["findings"][0]["listings"]
                assert result["ok"] is False
                assert result["marked"] is None, "an unread milestone addition must stay in the next window"
                assert saved == original
            else:
                assert result["marked"] == str(marker)
                assert saved["audits"]["o/r"] == "2026-09-21T12:00:00Z"
                assert saved["audits"]["o/other"] == original["audits"]["o/other"]
            assert any(f"labels=audit&since={previous}" in path for path in calls)


def test_unadopted_audit_leaves_marker_untouched() -> None:
    module = load()
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        original = '{"turn":"earlier","audits":{"o/adopted":"earlier"},"other":true}\n'
        for exists in (True, False):
            if exists:
                marker.write_text(original)
            else:
                marker.unlink()
            output = StringIO()
            with (patch.dict("os.environ", {"DIRECTION_MARKER": str(marker)}),
                  patch.dict(vars(module), {
                      "merged_direction": lambda *_args, **_kwargs: None,
                      "gh_json": lambda *_args, **_kwargs: [],
                  }), redirect_stdout(output)):
                assert module.main(["--repo", "o/unadopted", "--automation", "bot", "--gh", "fixture-gh"]) == 3
            result = json.loads(output.getvalue())
            assert any(item["kind"] == "direction_missing" for item in result["findings"])
            assert result["marked"] is None
            assert marker.exists() == exists
            if exists:
                assert marker.read_text() == original


def test_prune_preserves_adopted_unknown_and_other_marker_state() -> None:
    module = load()
    original = {"turn": "earlier", "audits": {"o/adopted": "a", "o/missing": "b", "o/private": "c"}, "other": True}
    def fetch(args: list[str]) -> dict[str, Any]:
        endpoint = args[1]
        if endpoint == "repos/o/private":
            raise module.AuditError("HTTP 404")
        if endpoint.endswith("/contents/DIRECTION.md"):
            if "/missing/" in endpoint:
                raise module.AuditError("HTTP 404")
            return {"type": "file", "content": "direction"}
        return {"full_name": endpoint.removeprefix("repos/")}
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        marker.write_text(json.dumps(original))
        before = marker.read_bytes()
        preview = module.prune_unadopted(marker, fetch=fetch)
        assert preview["removed"] == ["o/missing"]
        assert preview["retained"] == ["o/adopted"]
        assert list(preview["unknown"]) == ["o/private"]
        assert not preview["applied"] and preview["backup"] is None
        assert marker.read_bytes() == before
        applied = module.prune_unadopted(marker, fetch=fetch, apply=True)
        assert Path(applied["backup"]).read_bytes() == before
        assert Path(applied["backup"]).stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_text()) == {**original, "audits": {"o/adopted": "a", "o/private": "c"}}
        again = marker.read_bytes()
        repeated = module.prune_unadopted(marker, fetch=fetch, apply=True)
        assert repeated["backup"] is None and not repeated["applied"]
        assert marker.read_bytes() == again


def test_prune_rejects_malformed_input_and_concurrent_marker_edits() -> None:
    module = load()
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        marker.write_text('{"audits":[]}')
        try:
            module.prune_unadopted(marker, fetch=lambda _: {}, apply=True)
        except module.AuditError:
            pass
        else:
            raise AssertionError("malformed marker was accepted")
        marker.write_text('{"audits":{"o/missing":"a"}}')
        changed = '{"turn":"new","audits":{"o/missing":"a"}}'
        def fetch(args: list[str]) -> dict[str, Any]:
            if args[1].endswith("/contents/DIRECTION.md"):
                marker.write_text(changed)
                raise module.AuditError("HTTP 404")
            return {"full_name": "o/missing"}
        try:
            module.prune_unadopted(marker, fetch=fetch, apply=True)
        except module.AuditError:
            pass
        else:
            raise AssertionError("concurrent change was overwritten")
        assert marker.read_text() == changed


def test_prune_preserves_writer_started_after_final_read() -> None:
    module = load()
    for kind in ("turn", "audit"):
        with tempfile.TemporaryDirectory() as tmp:
            marker = (Path(tmp) / "marker.json").resolve()
            original = b'{"turn":"earlier", "audits":{"o/missing":"a"}, "other":true}\n'
            marker.write_bytes(original)
            ready = Path(tmp) / "ready"
            writer: subprocess.Popen[str] | None = None
            reads = 0

            def read(path: Path) -> bytes:
                nonlocal reads, writer
                with path.open("rb") as stream:
                    value = stream.read()
                if path == marker:
                    reads += 1
                    if reads == 2:
                        writer = subprocess.Popen([
                            sys.executable, "-c",
                            "import datetime as dt, sys; from pathlib import Path; "
                            "sys.path.insert(0, sys.argv[1]); import direction_mark as mark; "
                            "Path(sys.argv[3]).write_text('ready'); "
                            "now=dt.datetime(2026,10,4,tzinfo=dt.timezone.utc); "
                            "mark.mark_turn(Path(sys.argv[2]),'o/daily',now) if sys.argv[4]=='turn' "
                            "else mark.mark_audit(Path(sys.argv[2]),'o/new',now)",
                            str(SCRIPT.parent), str(marker), str(ready), kind,
                        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        import time

                        deadline = time.monotonic() + 5
                        while not ready.exists() and writer.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.01)
                        assert ready.exists(), "concurrent writer never started"
                        try:
                            writer.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass  # A serialized writer finishes after cleanup releases its lock.
                return value

            def fetch(args: list[str]) -> dict[str, Any]:
                if args[1].endswith("/contents/DIRECTION.md"):
                    raise module.AuditError("HTTP 404")
                return {"full_name": "o/missing"}

            try:
                with patch.object(Path, "read_bytes", read):
                    result = module.prune_unadopted(marker, fetch=fetch, apply=True)
                assert writer is not None
                stdout, stderr = writer.communicate(timeout=5)
                assert writer.returncode == 0, (stdout, stderr)
                after = json.loads(marker.read_text())
                assert after["turn"] == ("2026-10-04T00:00:00Z" if kind == "turn" else "earlier"), after
                assert after.get("turn_repo") == ("o/daily" if kind == "turn" else None)
                assert after["audits"] == ({"o/new": "2026-10-04T00:00:00Z"} if kind == "audit" else {})
                assert after["other"] is True
                assert Path(result["backup"]).read_bytes() == original
                assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
            finally:
                if writer is not None and writer.poll() is None:
                    writer.kill()
                    writer.communicate()


def test_interrupted_prune_preserves_marker_and_exact_backup() -> None:
    module = load()
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        original = b'{"turn":"earlier", "audits":{"o/missing":"a"}}\n'
        marker.write_bytes(original)

        def fetch(_args: list[str]) -> Any:
            raise module.AuditError("HTTP 404")

        with patch.object(os, "replace", side_effect=OSError("interrupted replace")):
            try:
                module.prune_unadopted(marker, fetch=fetch, apply=True, remove_missing_repos=("o/missing",))
            except OSError:
                pass
            else:
                raise AssertionError("replacement failure was hidden")
        assert marker.read_bytes() == original
        backups = list(Path(tmp).glob("*.backup-*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == original
        assert backups[0].stat().st_mode & 0o777 == 0o600
        assert list(Path(tmp).glob("*.pending-*")) == []


def test_prune_does_not_confuse_repository_digits_with_http_status() -> None:
    module = load()
    def fetch(args: list[str]) -> dict[str, Any]:
        if args[1].endswith("/contents/DIRECTION.md"):
            raise module.AuditError(f"wrapper/go404 {args[1]} failed: HTTP 502")
        return {"full_name": "o/app404"}
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        marker.write_text('{"audits":{"o/app404":"a"}}')
        original = marker.read_bytes()
        result = module.prune_unadopted(marker, fetch=fetch, apply=True)
        assert result["removed"] == [] and "o/app404" in result["unknown"]
        assert marker.read_bytes() == original
        try:
            module.merged_direction("o/app404", fetch=fetch)
        except module.AuditError:
            pass
        else:
            raise AssertionError("repository digits were mistaken for a missing direction file")


def test_prune_owner_approved_missing_repos_are_exact_and_recoverable() -> None:
    module = load()
    original = {"turn": "earlier", "audits": {"o/approved": "a", "o/unapproved": "b", "o/adopted": "c"}}
    calls: list[str] = []

    def fetch(args: list[str]) -> dict[str, Any]:
        calls.append(args[1])
        if args[1] in ("repos/o/approved", "repos/o/unapproved"):
            raise module.AuditError("HTTP 404")
        if args[1].endswith("/contents/DIRECTION.md"):
            return {"type": "file", "content": "direction"}
        return {"full_name": "o/adopted"}

    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        marker.write_text(json.dumps(original))
        before = marker.read_bytes()
        names = ("o/approved", "o/adopted")
        preview = module.prune_unadopted(marker, fetch=fetch, remove_missing_repos=names)
        assert preview["removed"] == ["o/approved"]
        assert preview["retained"] == ["o/adopted"]
        assert list(preview["unknown"]) == ["o/unapproved"]
        assert marker.read_bytes() == before and preview["backup"] is None
        applied = module.prune_unadopted(marker, fetch=fetch, apply=True, remove_missing_repos=names)
        backup = Path(applied["backup"])
        assert backup.read_bytes() == before and backup.stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_text()) == {**original, "audits": {"o/unapproved": "b", "o/adopted": "c"}}
        assert "repos/o/approved/contents/DIRECTION.md" not in calls
        after = marker.read_bytes()
        repeated = module.prune_unadopted(marker, fetch=fetch, apply=True, remove_missing_repos=names)
        assert repeated["backup"] is None and not repeated["applied"]
        assert marker.read_bytes() == after


def test_prune_approved_name_does_not_override_other_read_failures() -> None:
    module = load()
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        before = b'{"audits":{"o/app404":"a"}}'
        marker.write_bytes(before)
        for error in ("HTTP 403", "HTTP 429", "HTTP 502", "unreadable o/app404"):
            def fetch(_args: list[str], _error: str = error) -> Any:
                raise module.AuditError(_error)

            result = module.prune_unadopted(marker, fetch=fetch, apply=True, remove_missing_repos=("o/app404",))
            assert result["removed"] == [] and "o/app404" in result["unknown"]
            assert result["backup"] is None and marker.read_bytes() == before


def test_prune_approved_removal_preserves_concurrent_edits() -> None:
    module = load()
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        marker.write_text('{"audits":{"o/missing":"a"}}')
        changed = '{"turn":"new","audits":{"o/missing":"a"}}'

        def fetch(_args: list[str]) -> Any:
            marker.write_text(changed)
            raise module.AuditError("HTTP 404")

        try:
            module.prune_unadopted(marker, fetch=fetch, apply=True, remove_missing_repos=("o/missing",))
        except module.AuditError:
            pass
        else:
            raise AssertionError("approved removal overwrote a concurrent edit")
        assert marker.read_text() == changed
        assert list(Path(tmp).glob("*.backup-*")) == []


def test_prune_cli_passes_approved_names_and_requires_prune_mode() -> None:
    module = load()
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        marker.write_text('{"audits":{"o/first":"a","o/second":"b"}}')
        before = marker.read_bytes()

        def fetch(_args: list[str], **_kwargs: Any) -> Any:
            raise module.AuditError("HTTP 404")

        mark = types.SimpleNamespace(marker_path=lambda: marker)
        output = StringIO()
        with (patch.dict(sys.modules, {"direction_mark": mark}),
              patch.dict(vars(module), {"gh_json": fetch}), redirect_stdout(output)):
            assert module.main(["--prune-unadopted", "--apply-prune", "--remove-missing-repo", "o/first",
                                "--remove-missing-repo", "o/second"]) == 0
        result = json.loads(output.getvalue())
        assert result["removed"] == ["o/first", "o/second"] and result["applied"]
        assert Path(result["backup"]).read_bytes() == before
        assert json.loads(marker.read_text())["audits"] == {}
        for args in (["--remove-missing-repo", "o/first"],
                     ["--prune-unadopted", "--remove-missing-repo", "o/*"]):
            try:
                module.main(args)
            except SystemExit as exc:
                assert exc.code != 0
            else:
                raise AssertionError("invalid removal invocation was accepted")


def test_open_audit_questions_beyond_the_general_issue_cap_are_still_reported() -> None:
    module = load()
    question = issue(1, "Old audit question", labels=("audit",))
    def fetch(args: list[str]) -> list[dict[str, Any]]:
        path = args[1]
        if "state=open&labels=audit" in path:
            return [question]
        if "state=open" in path:
            return [issue(10, "Unrelated")] * 100
        if "state=closed&labels=audit" in path:
            return []
        raise AssertionError(path)
    issues, truncated = module.fetch_audit_issues("o/r", [], {}, NOW, fetch=fetch)
    result = run(module, issues=issues, truncated=truncated)
    assert result["counts"] == {"coverage_incomplete": 1, "audit_question": 1}
    assert result["findings"][1] == {"kind": "audit_question", "number": 1, "title": question["title"]}
    assert truncated == ["issues"], "the rest of the repository remains incompletely audited"


def test_missing_file_and_missing_heading() -> None:
    module = load()
    assert kinds(run(module, direction_text=None)) == ["direction_missing"]
    broken = DIRECTION.replace("## Retired", "## History")
    result = run(module, direction_text=broken)
    assert kinds(result) == ["direction_shape"]
    assert result["findings"][0]["headings"] == ["Retired"]


def test_unlisted_pending_and_foreign_creator() -> None:
    module = load()
    result = run(
        module,
        milestones=[
            milestone(1, "Thin fork decision"),
            milestone(3, "Agent invented this", creator="some-agent"),
            milestone(4, "Closed and gone", state="closed"),
            milestone(2, "Dogfood week", state="closed"),
        ],
    )
    found = {(item["kind"], item.get("title")) for item in result["findings"]}
    assert ("milestone_unlisted", "Agent invented this") in found
    assert ("milestone_creator", "Agent invented this") in found
    assert ("milestone_closed_listed", "Dogfood week") in found, "a shipped milestone still listed asks for line removal"
    assert ("milestone_pending", "Dogfood week") not in found, "a closed milestone must never be reported as not yet created"
    assert ("milestone_unlisted", "Closed and gone") not in found, "closed unlisted milestones are not drift"
    assert ("milestone_pending", "Thin fork decision") not in found


def test_configured_bot_logins_are_trusted_creators() -> None:
    module = load()
    milestones = [
        milestone(1, "Thin fork decision", creator="app[bot]"),
        milestone(2, "Dogfood week", creator="Legacy-Bot"),
        milestone(3, "Old phase", state="closed", creator="stranger"),
    ]
    result = run(module, milestones=milestones, automation="app[bot]", bot_logins=("legacy-bot",))
    creators = [item["creator"] for item in result["findings"] if item["kind"] == "milestone_creator"]
    assert creators == [], "closed milestones do not need creator trust; both open creators are trusted"
    result = run(module, milestones=milestones, automation="other", bot_logins=("legacy-bot",))
    creators = [item["creator"] for item in result["findings"] if item["kind"] == "milestone_creator"]
    assert creators == ["app[bot]"], "the automation override replaces the acting identity only for open milestones"


def test_milestone_creator_finding_applies_only_to_open_milestones() -> None:
    module = load()
    result = run(
        module,
        milestones=[
            milestone(1, "Open unknown creator", creator="stranger"),
            milestone(2, "Closed unknown creator", state="closed", creator="stranger"),
        ],
    )
    creator_titles = [item["title"] for item in result["findings"] if item["kind"] == "milestone_creator"]
    assert creator_titles == ["Open unknown creator"]


def test_listed_but_never_created_is_pending() -> None:
    module = load()
    result = run(module, milestones=[milestone(1, "Thin fork decision")])
    assert kinds(result) == ["milestone_pending"]
    assert result["findings"][0]["title"] == "Dogfood week"


def test_escalation_age_and_gate_phrases() -> None:
    module = load()
    result = run(
        module,
        issues=[
            issue(10, "Reviewer proposes retiring the policy engine", labels=("direction",), created="2026-09-14T12:00:00Z"),
            issue(11, "Plan", body="Opus/Gemini final planning findings are resolved before close."),
            issue(16, "Plan", body="- [ ] all findings resolved"),
            issue(12, "Plan", body="Finish line: Opus, Gemini Pro, and Sol independently approve the same final evidence bundle."),
            issue(13, "Plan", body="Both reviewers approve the plan before merge."),
            issue(14, "Plan", body="Reviewers may propose; findings are hypotheses and can be declined."),
            {"number": 15, "title": "PR with gate text", "body": "both reviewers approve", "labels": [], "pull_request": {}},
        ],
    )
    by_number = {item.get("number"): item for item in result["findings"]}
    assert by_number[10]["kind"] == "escalation_open" and by_number[10]["age_days"] == 7
    assert by_number[11]["phrases"] == ["all findings resolved"]
    assert by_number[16]["phrases"] == ["all findings resolved"], "the bare checklist literal must be caught"
    assert by_number[12]["phrases"] == ["named reviewer sign-off"]
    assert by_number[13]["phrases"] == ["both reviewers approve"]
    assert 14 not in by_number, "declinable findings are not a gate"
    assert 15 not in by_number, "pull requests are skipped"


def test_milestone_descriptions_are_checked_for_gate_phrases() -> None:
    module = load()
    result = run(module, milestones=[
        {**milestone(1, "Thin fork decision"), "description": "Closes when both reviewers approve the evidence bundle."},
        {**milestone(2, "Dogfood week"), "description": "Closes after seven days of installed use."},
    ])
    assert kinds(result) == ["gate_phrase"]
    assert result["findings"][0]["milestone"] == 1


def test_direction_pull_requests_and_truncation_are_reported() -> None:
    module = load()
    result = run(
        module,
        direction_pulls=[{"number": 40, "title": "Retire the policy engine", "created_at": "2026-09-19T12:00:00Z"}],
        truncated=["issues"],
    )
    assert kinds(result) == ["coverage_incomplete", "escalation_open"]
    assert result["ok"] is False
    assert result["findings"][1]["pull_request"] is True and result["findings"][1]["age_days"] == 2


def test_standard_rulesets_are_required_for_adopted_repositories() -> None:
    module = load()
    standard = [
        {"name": name, "target": "branch", "enforcement": "active"}
        for name in module.github_rulesets.required_ruleset_names()
    ]
    assert run(module, rulesets=standard)["ok"] is True

    result = run(module, rulesets=[standard[0]])
    assert kinds(result) == ["ruleset_missing"]
    assert result["findings"][0]["name"] == standard[1]["name"]

    disabled = [{**item, "enforcement": "disabled"} for item in standard]
    result = run(module, rulesets=disabled)
    assert kinds(result) == ["ruleset_missing", "ruleset_missing"]


def test_rulesets_are_not_required_before_direction_is_adopted() -> None:
    module = load()
    result = run(module, direction_text=None, rulesets=[])
    assert kinds(result) == ["direction_missing"]


def test_ruleset_plan_limit_is_a_finding_and_other_403s_still_fail() -> None:
    module = load()
    assert kinds(run(module, rulesets=None, rulesets_unavailable=True)) == ["ruleset_unavailable"]
    assert kinds(run(module, direction_text=None, rulesets=None, rulesets_unavailable=True)) == ["direction_missing"]

    plan_limit = "reader failed: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)"
    permission = "reader failed: Resource not accessible by integration (HTTP 403)"
    with tempfile.TemporaryDirectory() as tmp:
        for direction_text, error, code in (
            (DIRECTION, plan_limit, 3),
            (None, plan_limit, 3),
            (DIRECTION, permission, 1),
        ):
            def gh_json(args: list[str], _error: str = error, **_kwargs: Any) -> Any:
                if "/rulesets" in args[1]:
                    raise module.AuditError(_error)
                return []

            output = StringIO()
            with (patch.dict("os.environ", {"DIRECTION_MARKER": str(Path(tmp) / "marker.json")}),
                  patch.dict(vars(module), {
                      "merged_direction": lambda *_args, _text=direction_text, **_kwargs: _text,
                      "gh_json": gh_json,
                  }), redirect_stdout(output)):
                assert module.main(["--repo", "o/private", "--automation", "bot", "--gh", "fixture-gh"]) == code
            result = json.loads(output.getvalue())
            if code == 1:
                assert result["ok"] is False and "Resource not accessible" in result["error"], result
            else:
                found = [item["kind"] for item in result["findings"]]
                assert ("ruleset_unavailable" in found) is (direction_text is not None), found
                assert "ruleset_missing" not in found, found


def test_direction_pull_request_discovery_uses_label_or_changed_file() -> None:
    module = load()
    files = {
        1: [{"filename": "DIRECTION.md"}],
        2: [{"filename": "README.md"}],
    }

    def fetch(args: list[str]) -> Any:
        number = int(args[1].split("/pulls/")[1].split("/")[0])
        return files[number]

    pulls = [
        {"number": 1, "labels": []},
        {"number": 2, "labels": []},
        {"number": 3, "labels": [{"name": "direction"}]},
    ]
    matched = module.direction_pull_requests("o/r", pulls, fetch=fetch)
    assert [pull["number"] for pull in matched] == [1, 3]


def test_merged_direction_reads_the_default_branch_and_treats_404_as_not_adopted() -> None:
    import base64

    module = load()
    encoded = {"content": base64.b64encode(DIRECTION.encode()).decode(), "encoding": "base64"}
    assert module.merged_direction("o/r", fetch=lambda args: encoded) == DIRECTION

    def missing(_args: list[str]) -> Any:
        raise module.AuditError("contents read failed: HTTP 404: Not Found")

    assert module.merged_direction("o/r", fetch=missing) is None

    def broken(_args: list[str]) -> Any:
        raise module.AuditError("contents read failed: HTTP 502")

    try:
        module.merged_direction("o/r", fetch=broken)
    except module.AuditError:
        pass
    else:
        raise AssertionError("an unreadable file must not be reported as not adopted")


def test_fetch_paginated_follows_full_pages_and_flags_the_cap() -> None:
    module = load()
    pages = {1: [{"n": i} for i in range(100)], 2: [{"n": 100}]}
    calls: list[str] = []

    def fetch(args: list[str]) -> Any:
        calls.append(args[1])
        page = int(args[1].rsplit("page=", 1)[1])
        return pages.get(page, [])

    items, cut = module.fetch_paginated("repos/o/r/milestones?state=all", fetch=fetch)
    assert (len(items), cut) == (101, False)
    assert calls == ["repos/o/r/milestones?state=all&per_page=100&page=1", "repos/o/r/milestones?state=all&per_page=100&page=2"]

    full = lambda args: [{"n": 0}] * 100  # noqa: E731
    items, cut = module.fetch_paginated("repos/o/r/issues?state=open", fetch=full)
    assert (len(items), cut) == (100 * module.MAX_PAGES, True), "a listing that never shortens must report the cap"


def test_waiting_unmilestoned_cross_repository_blocker_is_a_named_finding() -> None:
    module = load()
    gate = issue(2554, "Gate", labels=("plan", "plan:waiting"))
    gate["html_url"] = "https://github.com/owner/repo/issues/2554"
    calls: list[str] = []
    targets = [
        {"number": 141, "state": "open", "html_url": "https://github.com/owner/other/issues/141"},
        {"number": 142, "state": "closed", "html_url": "https://github.com/owner/other/issues/142"},
        {"number": 10, "state": "open", "url": "https://api.github.com/repos/OWNER/REPO/issues/10"},
    ]

    def fetch(args: list[str]) -> Any:
        calls.append(args[1])
        return targets

    assert not module.enrich_waiting_inbound_blockers([gate], "owner/old-name", fetch=fetch)
    result = run(module, issues=[gate])
    pair = next(item for item in result["findings"] if item["kind"] == "waiting_blocks_other_repository")
    assert pair["number"] == 2554
    assert pair["blocking"] == [{"repo": "owner/other", "number": 141, "url": "https://github.com/owner/other/issues/141"}]
    assert len(calls) == 1
    assert "milestone" not in gate
    for changed in ({"state": "closed"}, {"labels": [{"name": "plan:active"}]}):
        assert "waiting_blocks_other_repository" not in kinds(run(module, issues=[{**gate, **changed}]))


def test_waiting_inbound_fetch_coverage_includes_caps_errors_and_ambiguous_targets() -> None:
    module = load()
    gates = [issue(number, "Gate", labels=("plan:waiting",)) for number in (1, 2)]
    assert module.enrich_waiting_inbound_blockers(gates, "o/r", fetch=lambda _: [], max_issues=1)
    assert module.enrich_waiting_inbound_blockers(gates, "o/r", fetch=lambda _: [{"state": "open", "number": 3}])
    assert module.enrich_waiting_inbound_blockers(gates, "o/r", fetch=lambda _: [{"state": "open", "html_url": "https://github.com/o/other/issues/3"}] * 100)

    def denied(_args: list[str]) -> Any:
        raise module.AuditError("permission denied")

    def fetch(args: list[str]) -> Any:
        if "/dependencies/blocking" in args[1]:
            return denied(args)
        if "state=open" in args[1]:
            return gates
        return []

    found, incomplete = module.fetch_audit_issues("o/r", [], {}, NOW, fetch=fetch)
    assert "waiting_inbound_blockers" in incomplete
    assert "coverage_incomplete" in kinds(run(module, issues=found, truncated=incomplete))
    zero = {**gates[0], "issue_dependencies_summary": {"blocking": 0}}
    assert not module.enrich_waiting_inbound_blockers([zero], "o/r", fetch=denied)

SINCE = NOW - dt.timedelta(days=7)
RANKS = """[repositories]
"o/live" = "milestone"
"o/tools" = "tooling"
"o/fun" = "own"
"""


def stamp(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def merged_pull(number: int, merged: dt.datetime | None, *, title: str = "Change", body: str = "") -> dict[str, Any]:
    return {"number": number, "title": title, "body": body, "merged_at": stamp(merged) if merged else None,
            "updated_at": stamp(merged or SINCE)}


def summary(module: Any, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "rank_map": module.parse_rank_map(RANKS), "rank_map_source": "o/direction:ranks.toml",
        "pulls": {}, "milestones": {}, "events": {}, "since": SINCE, "until": NOW,
    }
    params.update(overrides)
    return module.capacity_summary(**params)


def test_capacity_counts_merges_inside_the_window_by_rank() -> None:
    module = load()
    second = dt.timedelta(seconds=1)
    result = summary(module, pulls={
        "o/live": [merged_pull(1, SINCE), merged_pull(2, SINCE - second), merged_pull(3, None)],
        "o/fun": [merged_pull(4, NOW), merged_pull(5, NOW + second), merged_pull(4, NOW)],
        "o/tools": [merged_pull(6, NOW - second)],
    })
    assert result["merged_by_rank"] == {"milestone": 1, "tooling": 1, "own": 1, "unranked": 0}
    assert result["merged_total"] == 3
    assert result["by_repository"]["o/live"] == {"rank": "milestone", "merged": 1}
    assert result["unranked"] == []


def test_capacity_reports_unranked_repositories_instead_of_guessing() -> None:
    module = load()
    pulls = {"o/fun": [merged_pull(1, NOW)], "o/stranger": [merged_pull(2, NOW), merged_pull(3, NOW)], "o/quiet": [merged_pull(4, SINCE - dt.timedelta(days=1))]}
    result = summary(module, pulls=pulls)
    assert result["unranked"] == [{"repo": "o/stranger", "merged": 2}]
    assert result["merged_by_rank"]["unranked"] == 2
    found = run(module, capacity=result)
    assert [item for item in found["findings"] if item["kind"] == "repository_unranked"] == [
        {"kind": "repository_unranked", "title": "o/stranger", "merged": 2},
    ]
    assert found["capacity"] is result
    missing = summary(module, rank_map=None, rank_map_source=None, pulls=pulls)
    assert missing["merged_by_rank"]["unranked"] == 3
    assert {"rank_map_missing", "repository_unranked"} <= set(kinds(run(module, capacity=missing)))


def test_own_share_floor_is_unknown_while_unranked_merges_could_lift_it() -> None:
    module = load()
    floor = round(module.OWN_SHARE_FLOOR * 100)

    def share(own: int, unranked: int) -> dict[str, Any]:
        pulls = {
            "o/fun": [merged_pull(n, NOW) for n in range(own)],
            "o/stranger": [merged_pull(n, NOW) for n in range(unranked)],
            "o/live": [merged_pull(n, NOW) for n in range(100 - own - unranked)],
        }
        return summary(module, pulls=pulls)

    assert share(floor, 0)["own_share_floor"] == "met"
    below = share(floor - 1, 0)
    assert below["own_share_floor"] == "below"
    assert "own_share_below_floor" in kinds(run(module, capacity=below))
    assert share(floor - 1, 1)["own_share_floor"] == "unknown"
    assert summary(module)["own_share_floor"] == "no_merges"
    assert summary(module, pulls={"o/fun": [merged_pull(1, NOW)]}, incomplete=True)["own_share_floor"] == "unknown"
    assert "own_share_below_floor" not in kinds(run(module, capacity=share(floor, 0)))


def test_capacity_lists_reverts_reopened_issues_and_closed_milestones_in_window() -> None:
    module = load()
    early = SINCE - dt.timedelta(hours=1)
    pulls = {"o/live": [
        merged_pull(1, NOW, title='Revert "Add the cache"', body="Reverts o/live#9"),
        merged_pull(2, NOW, title="Undo the pin", body="This reverts commit 0123abc."),
        merged_pull(3, NOW, title="Revertible migrations"),
        merged_pull(4, early, title="Revert old"),
    ]}
    events = {"o/live": [
        {"event": "reopened", "created_at": stamp(NOW), "issue": {"number": 7, "title": "Bug"}},
        {"event": "reopened", "created_at": stamp(SINCE), "issue": {"number": 7, "title": "Bug"}},
        {"event": "reopened", "created_at": stamp(NOW), "issue": {"number": 8, "pull_request": {}}},
        {"event": "reopened", "created_at": stamp(early), "issue": {"number": 9}},
        {"event": "closed", "created_at": stamp(NOW), "issue": {"number": 10}},
    ]}
    milestones = {"o/live": [
        {"number": 1, "title": "Shipped", "state": "closed", "closed_at": stamp(NOW)},
        {"number": 2, "title": "Old", "state": "closed", "closed_at": stamp(early)},
    ]}
    result = summary(module, pulls=pulls, events=events, milestones=milestones)
    assert [item["number"] for item in result["revert_pulls"]] == [1, 2]
    assert result["issues_reopened"] == [{"repo": "o/live", "number": 7, "title": "Bug", "reopened_at": stamp(NOW)}]
    assert [item["number"] for item in result["milestones_closed"]] == [1]


def test_rank_map_rejects_unknown_ranks_and_names() -> None:
    module = load()
    for text in ('[repositories]\n"o/r" = "hobby"\n', '[repositories]\nr = "own"\n', 'x = 1\n', '[repositories\n'):
        try:
            module.parse_rank_map(text)
        except module.AuditError:
            continue
        raise AssertionError(text)
    assert module.parse_rank_map('[repositories]\n"O/Fun" = "own"\n') == {"o/fun": "own"}


def test_capacity_reads_each_repository_and_stops_at_the_window() -> None:
    import base64

    module = load()
    old = stamp(SINCE - dt.timedelta(days=1))
    listing = [
        {"full_name": "o/live", "owner": {"login": "o"}, "pushed_at": stamp(NOW)},
        {"full_name": "o/quiet", "owner": {"login": "o"}, "pushed_at": old},
        {"full_name": "o/attic", "owner": {"login": "o"}, "pushed_at": old, "updated_at": old, "archived": True},
        {"full_name": "o/shut", "owner": {"login": "o"}, "pushed_at": old, "updated_at": stamp(NOW), "archived": True},
        {"full_name": "x/other", "owner": {"login": "x"}, "pushed_at": stamp(NOW)},
    ]
    calls: list[str] = []

    def fetch(args: list[str]) -> Any:
        path = args[1]
        calls.append(path)
        if path.startswith("repos/o/direction/contents/"):
            return {"content": base64.b64encode(RANKS.encode()).decode()}
        if path.startswith("installation/repositories"):
            raise module.AuditError("installation/repositories failed: HTTP 403")
        if path.startswith("user/repos"):
            return listing
        if path == "repos/o/tools":
            return {"full_name": "o/tools", "owner": {"login": "o"}, "pushed_at": stamp(NOW)}
        if path == "repos/o/fun":
            raise module.AuditError("HTTP 404")
        if path.startswith("repos/o/live/pulls"):
            # A full page that reaches past the window ends the read.
            return [merged_pull(n, NOW) for n in range(99)] + [{**merged_pull(99, None), "updated_at": old}]
        return []

    result, truncated = module.fetch_capacity("o/direction", SINCE, NOW, fetch=fetch)
    assert result["merged_by_rank"]["milestone"] == 99
    assert truncated == ["capacity_repository:o/fun"]
    assert not any("page=2" in path for path in calls if "/pulls" in path)
    assert not any(path.startswith(("repos/o/quiet/pulls", "repos/o/attic/", "repos/x/")) for path in calls)
    assert any(path.startswith("repos/o/quiet/issues/events") for path in calls)
    assert any(path.startswith("repos/o/shut/milestones") for path in calls)
    assert result["own_share_floor"] == "unknown"
    assert any(path.startswith("repos/o/tools/pulls") for path in calls)

    def nothing_lists(args: list[str]) -> Any:
        if "/contents/" in args[1]:
            raise module.AuditError("HTTP 404")
        raise module.AuditError(f"{args[1]} failed: HTTP 403")

    try:
        module.fetch_capacity("o/direction", SINCE, NOW, fetch=nothing_lists)
    except module.AuditError as exc:
        assert "installation/repositories" in str(exc) and "user/repos" in str(exc)
    else:
        raise AssertionError("both listings failed")

def test_only_the_direction_repository_audit_counts_capacity() -> None:
    module = load()
    windows: list[tuple[str, dt.datetime]] = []

    def capacity(repo: str, since: dt.datetime, until: dt.datetime, **_kwargs: Any) -> tuple[dict[str, Any], list[str]]:
        windows.append((repo, since))
        return summary(module, since=since, until=until), ["capacity_pulls:o/live"]

    for audited in ("o/direction", "o/r"):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "marker.json"
            marker.write_text(json.dumps({"audits": {audited: stamp(SINCE)}}))
            output = StringIO()
            with (patch.dict("os.environ", {"DIRECTION_MARKER": str(marker)}),
                  patch.dict(vars(module), {
                      "merged_direction": lambda *_args, **_kwargs: DIRECTION,
                      "gh_json": lambda *_args, **_kwargs: [],
                      "fetch_capacity": capacity,
                  }),
                  redirect_stdout(output)):
                module.main(["--repo", audited, "--automation", "bot", "--gh", "fixture-gh"])
            result = json.loads(output.getvalue())
            assert ("capacity" in result) is (audited == "o/direction")
            # The direction audit's capacity read was incomplete, so its window stays open.
            assert (result["marked"] is None) is (audited == "o/direction")
            assert (json.loads(marker.read_text())["audits"][audited] == stamp(SINCE)) is (audited == "o/direction")
    assert windows == [("o/direction", SINCE)]


def test_stale_wait_report_checks_realistic_parked_records_without_writes() -> None:
    module = load()
    rows = [
        issue(41, "Agent handoff parked as a wait", labels=("plan", "plan:waiting"),
              body="## Current Status\nState: Waiting.\nWaiting for: Supervisor routing.\n"),
        issue(42, "Source landing prerequisite", labels=("plan", "plan:blocked"),
              body="## Current Status\nWaiting for: PR owner/tools#71 to merge.\n"),
        issue(43, "Consumer proof after blocker", labels=("plan", "plan:blocked"),
              body="## Current Status\nBlocked by: Native blocker owner/product#13.\nWaiting for: Next agent.\n"),
        issue(44, "Test after the tracked migration completes", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: Completion of owner/product#14.\n"),
        issue(45, "CM website acceptance", labels=("plan", "plan:waiting"),
              body="## Problem\nWaiting for: Next agent.\n## Current Status\n"
                   "State: Waiting.\nWaiting for: Justin to accept the first release; source PR "
                   "https://github.com/owner/tools/pull/71 has merged.\n"
                   "Next action: Run the gated release after Justin accepts.\n"),
        issue(46, "A real capacity reset event", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: Provider capacity reset tomorrow at noon.\n"),
        issue(47, "Merged code still needs a device test", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: Chris to test PR owner/tools#71 on the device.\n"),
        issue(48, "Active engineering", labels=("plan:active",),
              body="## Current Status\nWaiting for: None.\n"),
        {**issue(49, "Closed work", labels=("plan:waiting",),
                  body="## Current Status\nWaiting for: Capacity.\n"), "state": "closed"},
        {**issue(50, "A pull request is not a plan", labels=("plan:waiting",)), "pull_request": {}},
    ]
    calls: list[str] = []

    def fetch(args: list[str]) -> Any:
        assert args[0] == "api" and args[-2:] == ["--method", "GET"], args
        path = args[1]
        calls.append(path)
        if "/dependencies/blocked_by" in path:
            return ([{"number": 13, "state": "closed", "closed_at": stamp(NOW),
                      "html_url": "https://github.com/owner/product/issues/13"}]
                    if "/issues/43/" in path else [])
        if path == "repos/owner/tools/issues/71":
            return {"state": "closed", "pull_request": {}}
        if path == "repos/owner/tools/pulls/71":
            return {"state": "closed", "merged_at": stamp(NOW)}
        if path in {"repos/owner/product/issues/13", "repos/owner/product/issues/14"}:
            return {"state": "closed", "state_reason": "completed", "closed_at": stamp(NOW)}
        raise AssertionError(path)

    before = json.dumps(rows, sort_keys=True)
    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch)
    assert json.dumps(rows, sort_keys=True) == before
    assert report["read_only"] and report["complete"]
    assert report["checked"] == 7
    by_number = {item["number"]: item for item in report["items"]}
    assert set(by_number) == {41, 42, 43, 44}, report
    assert by_number[42]["evidence"][0]["url"] == "https://github.com/owner/tools/pull/71"
    assert {item["kind"] for item in by_number[43]["evidence"]} == {"no_external_wait", "closed_native_blocker", "completed_wait_issue"}
    assert by_number[44]["evidence"][0]["kind"] == "completed_wait_issue"
    assert all(item["review_required"] for item in report["items"])
    assert calls.count("repos/owner/tools/pulls/71") == 1


def test_stale_wait_report_preserves_open_closed_unmerged_and_unknown_conditions() -> None:
    module = load()
    rows = [issue(n, reason, labels=("plan:waiting",),
                  body=f"## Current Status\nWaiting for: {reason}\n") for n, reason in enumerate((
        "PR #71 to merge.",
        "Landing of [tools#72](https://github.com/owner/tools/pull/72).",
        "Completion of owner/product#14.",
        "Chris's decision after tomorrow's meeting.",
        "Observation of 24 hours of successful worker passes.",
        "Supervisor to ask Chris for approval.",
        "Capacity; Chris must select a paid plan.",
        "Capacity.\nChris must approve the paid plan first.",
        "None; waiting for Chris to finish testing.",
        "PR #71 requires Chris to confirm the production backup after merge.",
        "PR #71 awaits publication to the Marketplace.",
    ), 1)]

    def fetch(args: list[str]) -> Any:
        path = args[1]
        if "/dependencies/blocked_by" in path:
            return []
        if "/issues/71" in path:
            return {"state": "open", "pull_request": {}}
        if "/issues/72" in path:
            return {"state": "closed", "pull_request": {}}
        if "/pulls/" in path:
            return {"merged_at": None}
        if "/issues/14" in path:
            return {"state": "closed", "state_reason": "not_planned"}
        raise AssertionError(path)

    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch)
    assert report["complete"] and report["items"] == [], report


def test_stale_wait_report_exposes_unread_coverage_and_caches_wait_targets() -> None:
    module = load()
    rows = [issue(n, "Landing", labels=("plan:waiting",),
                  body="## Current Status\nWaiting for: PR #71 to merge.\n") for n in (1, 2)]
    calls: list[str] = []

    def fetch(args: list[str]) -> Any:
        path = args[1]
        calls.append(path)
        if "/issues/1/dependencies/" in path:
            return [{"number": 90, "state": "open"}] * 100
        raise module.AuditError("HTTP 403: permission denied")

    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch, inventory_complete=False)
    assert not report["complete"] and not report["inventory_complete"]
    assert report["items"] == []
    assert {item["reason"] for item in report["unavailable"]} == {"page_limit", "unavailable"}
    assert calls.count("repos/owner/catalog/issues/71") == 1
    assert len([item for item in report["unavailable"] if item["source"] == "wait_reference"]) == 2


def test_audit_cli_includes_stale_wait_report_without_changing_exit_status() -> None:
    module = load()
    row = issue(41, "Parked on an agent", labels=("plan:waiting",),
                body="## Current Status\nWaiting for: Next agent.\n")
    with (tempfile.TemporaryDirectory() as tmp,
          patch.dict("os.environ", {"DIRECTION_MARKER": str(Path(tmp) / "marker.json")}),
          patch.dict(vars(module), {
              "merged_direction": lambda *_args, **_kwargs: DIRECTION,
              "fetch_audit_issues": lambda *_args, **_kwargs: ([row], []),
              "gh_json": lambda args, **_kwargs: (
                  [milestone(1, "Thin fork decision"), milestone(2, "Dogfood week")]
                  if "/milestones" in args[1] else []),
              "fetch_rulesets": lambda *_args, **_kwargs: (None, False),
          })):
        # Ruleset absence is already an audit finding; stale waits must not add
        # a new exit-status gate. Compare the same audit without parked records.
        output = StringIO()
        with redirect_stdout(output):
            code = module.main(["--repo", "owner/catalog", "--automation", "bot", "--gh", "fixture-gh"])
        result = json.loads(output.getvalue())
        assert result["stale_wait_report"]["items"][0]["number"] == 41
        with patch.dict(vars(module), {"fetch_audit_issues": lambda *_args, **_kwargs: ([], [])}):
            clean_output = StringIO()
            with redirect_stdout(clean_output):
                clean_code = module.main(["--repo", "owner/catalog", "--automation", "bot", "--gh", "fixture-gh"])
        assert code == clean_code
        assert result["findings"] == json.loads(clean_output.getvalue())["findings"]


def test_stale_wait_report_requires_all_holds_and_all_references_to_be_met() -> None:
    module = load()
    rows = [
        issue(1, "Open native blocker", labels=("plan:blocked",),
              body="## Current Status\nBlocked by: owner/product#90\nWaiting for: None.\n"),
        issue(2, "Historical closed blocker plus an open blocker", labels=("plan:blocked",)),
        issue(3, "Acceptance after source prerequisite", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: Justin to accept the first release.\nParked until: None.\n"),
        issue(4, "A stack that has only partly merged", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: PR #71 and #72 to merge.\n"),
        issue(5, "Device acceptance after merge", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: Merge of #71, then Chris's device sign-off.\n"),
        issue(6, "Acceptance with an old closed native blocker", labels=("plan:waiting",),
              body="## Current Status\nWaiting for: Justin to accept the first release.\n"),
    ]
    def fetch(args: list[str]) -> Any:
        path = args[1]
        if "/dependencies/blocked_by" in path:
            closed = {"number": 13, "state": "closed"}
            opened = {"number": 90, "state": "open"}
            if "/issues/1/" in path:
                return [opened]
            if "/issues/2/" in path:
                return [closed, opened]
            if "/issues/6/" in path:
                return [closed]
            return []
        if path == "repos/owner/catalog/issues/71":
            return {"state": "closed", "pull_request": {}}
        if path == "repos/owner/catalog/issues/72":
            return {"state": "open", "pull_request": {}}
        if path == "repos/owner/catalog/pulls/71":
            return {"merged_at": stamp(NOW)}
        if path == "repos/owner/catalog/pulls/72":
            return {"merged_at": None}
        if path == "repos/owner/product/issues/90":
            return {"state": "open"}
        raise AssertionError(path)
    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch)
    assert report["complete"] and report["items"] == [], report


def test_stale_wait_report_covers_passive_completion_and_text_only_blockers() -> None:
    module = load()
    rows = [issue(n, "Prerequisite", labels=("plan:blocked",), body="## Current Status\n" + condition)
            for n, condition in enumerate((
                "Blocked by: Supervisor routing.\n",
                "Waiting for: PR #71 to be merged.\n",
                "Waiting for: Issue #14 to be closed.\n",
                "Blocked by: #14\nWaiting for: None.\n",
            ), 1)]
    def fetch(args: list[str]) -> Any:
        path = args[1]
        if "/dependencies/blocked_by" in path:
            return []
        if "/issues/71" in path:
            return {"state": "closed", "pull_request": {}}
        if "/pulls/71" in path:
            return {"merged_at": stamp(NOW)}
        if "/issues/14" in path:
            return {"state": "closed", "state_reason": "completed"}
        raise AssertionError(path)
    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch)
    assert report["complete"] and {item["number"] for item in report["items"]} == {1, 2, 3, 4}, report


def test_stale_wait_report_bounds_reads_and_skips_native_zero_summary() -> None:
    module = load()
    rows = [{**issue(n, "Agent routing", labels=("plan:waiting",),
                    body="## Current Status\nWaiting for: Next agent.\n"),
             "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0}} for n in (1, 2)]
    def unexpected(_args: list[str]) -> Any:
        raise AssertionError("a known empty native relationship needs no API call")
    report = module.stale_wait_report(rows, "owner/catalog", fetch=unexpected, max_issues=1)
    assert report["checked"] == 1 and not report["complete"]
    assert report["unavailable"] == [{"number": 2, "source": "issue", "reason": "issue_limit"}]
    assert [item["number"] for item in report["items"]] == [1]


def test_stale_wait_report_only_cli_never_reads_or_advances_audit_marker() -> None:
    module = load()
    row = {**issue(41, "Agent routing", labels=("plan:waiting",),
                   body="## Current Status\nWaiting for: Next agent.\n"),
           "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0}}
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "marker.json"
        original = json.dumps({"audits": {"owner/catalog": stamp(SINCE)}, "turns": {"owner/catalog": stamp(NOW)}})
        marker.write_text(original)
        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("report-only mode must not consume direction-audit records")
        def fetch(args: list[str], **_kwargs: Any) -> Any:
            assert args[0] == "api" and args[-2:] == ["--method", "GET"]
            assert args[1].startswith("repos/owner/catalog/issues?state=open&")
            return [row]
        with (patch.dict("os.environ", {"DIRECTION_MARKER": str(marker)}),
              patch.dict(vars(module), {"gh_json": fetch, "previous_audit_stamp": forbidden,
                                       "record_audit": forbidden, "merged_direction": forbidden})):
            output = StringIO()
            with redirect_stdout(output):
                assert module.main(["--repo", "owner/catalog", "--stale-waits-only", "--gh", "fixture-gh"]) == 0
            report = json.loads(output.getvalue())["stale_wait_report"]
            assert report["complete"] and report["items"][0]["number"] == 41
        assert marker.read_text() == original


def test_stale_wait_report_reads_closed_native_history_despite_zero_open_count() -> None:
    module = load()
    row = {**issue(41, "Prerequisite finished", labels=("plan:blocked",),
                   body="## Current Status\nWaiting for: None.\n"),
           "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 1}}
    calls: list[str] = []
    def fetch(args: list[str]) -> Any:
        calls.append(args[1])
        return [{"number": 13, "state": "closed", "state_reason": "completed",
                 "html_url": "https://github.com/owner/product/issues/13"}]
    report = module.stale_wait_report([row], "owner/catalog", fetch=fetch)
    assert report["complete"] and len(calls) == 1
    evidence = report["items"][0]["evidence"]
    assert any(item["kind"] == "closed_native_blocker" and item["state_reason"] == "completed" for item in evidence)


def test_stale_wait_report_preserves_unparsed_waits_and_continued_notes() -> None:
    module = load()
    bodies = (
        "## Current Status\nState: Waiting for Justin to accept the release.\n",
        "## Current Status\nWaiting on: Justin to accept the release.\n",
        "## Current status\nWaiting for: Justin to accept the release.\n",
        "## Objective\nJustin must accept the release.\n",
        "## Current Status\nWaiting for: Capacity.\n\nChris must pick the paid plan first.\n",
        "## Current Status\nWaiting for: Supervisor routing.\nNote: only after Justin approves.\n",
        "## Current Status\nWaiting for: Supervisor routing.\nhttps://github.com/owner/product/issues/90 is still awaiting Justin.\n",
        "## Current Status\nState: Waiting for Justin to accept.\nWaiting for: None.\n",
    )
    rows = [{**issue(n, "Genuine acceptance wait", labels=("plan:waiting",), body=body),
             "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 1}}
            for n, body in enumerate(bodies, 1)]
    report = module.stale_wait_report(rows, "owner/catalog", fetch=lambda _: [
        {"number": 13, "state": "closed", "state_reason": "completed"},
    ])
    assert report["complete"] and report["items"] == [], report


def test_stale_wait_report_does_not_treat_abandoned_or_duplicate_work_as_completed() -> None:
    module = load()
    for reason in ("not_planned", "duplicate"):
        row = issue(41, "Abandoned prerequisite", labels=("plan:blocked",),
                    body="## Current Status\nBlocked by: #13\nWaiting for: None.\n")
        report = module.stale_wait_report([row], "owner/catalog", fetch=lambda _: (
            [{"number": 13, "state": "closed", "state_reason": reason}]
            if "/dependencies/" in _[1] else {"state": "closed", "state_reason": reason}
        ))
        assert report["complete"] and report["items"] == [], report


def test_stale_wait_report_preserves_contradictory_status_and_split_prerequisites() -> None:
    module = load()
    bodies = (
        "## Current Status\nJustin has not accepted yet.\nWaiting for: Supervisor routing.\n",
        "## Current Status\nWaiting for: Supervisor routing.\nNext action: After Justin approves, release.\n",
        "## Current Status\nState: Blocked on Justin's approval.\nWaiting for: Next agent.\n",
        "## Current Status\nState: Pending Justin.\nWaiting for: Next agent.\n",
        "## Current Status\nBlocked by: None. Justin must approve first.\nWaiting for: Next agent.\n",
        "## Current Status\nWaiting for: Next agent.\nLast verified: Monday; Justin has not accepted the release yet.\n",
        "## Current Status\nWaiting for: Supervisor routing.\nNext action: Once Chris confirms the backup, release.\n",
        "## Current Status\nWaiting for: Completion of #14.\n",
    )
    rows = [issue(n, "Unproven wait", labels=("plan:waiting",), body=body)
            for n, body in enumerate(bodies, 1)]
    def fetch(args: list[str]) -> Any:
        if "/dependencies/" in args[1]:
            return []
        return {"state": "closed", "state_reason": "completed",
                "body": "## Current Status\nState: Split. Live testing moved to #20.\n"}
    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch)
    assert report["complete"] and not report["items"], report
    parent = {"state": "closed", "sub_issues_summary": {"total": 2, "completed": 1}}
    assert not module.closed_wait_prerequisite(parent)
    assert not module.closed_wait_prerequisite({"state": "closed", "body": "## Current Status\n- State: Split. Live test moved to #20.\n"})
    guarded = issue(99, "Unfinished parent", labels=("plan:blocked",),
                    body="## Current Status\nWaiting for: None.\n")
    assert not module.stale_wait_report([guarded], "owner/catalog", fetch=lambda _: [parent])["items"]


def test_stale_wait_report_landing_requires_default_branch_but_merge_means_merge() -> None:
    module = load()
    rows = [issue(n, "Stacked child", labels=("plan:waiting",),
                  body=f"## Current Status\nWaiting for: PR #71 to {action}.\n")
            for n, action in ((1, "land"), (2, "merge"))]
    def fetch(args: list[str]) -> Any:
        path = args[1]
        if "/dependencies/" in path:
            return []
        if "/issues/71" in path:
            return {"state": "closed", "pull_request": {}}
        if "/pulls/71" in path:
            return {"merged_at": stamp(NOW), "base": {"ref": "work/root"}}
        if path == "repos/owner/catalog":
            return {"default_branch": "main"}
        raise AssertionError(path)
    report = module.stale_wait_report(rows, "owner/catalog", fetch=fetch)
    assert report["complete"] and [item["number"] for item in report["items"]] == [2], report
    def default_landing(args: list[str]) -> Any:
        value = fetch(args)
        return {**value, "base": {"ref": "main"}} if "/pulls/71" in args[1] else value
    landed = module.stale_wait_report(rows, "owner/catalog", fetch=default_landing)
    assert landed["complete"] and [item["number"] for item in landed["items"]] == [1, 2], landed


def test_stale_wait_report_reports_explicit_absence_of_all_waits() -> None:
    module = load()
    row = {**issue(41, "Parked on nobody", labels=("plan:waiting",),
                   body="## Current Status\nWaiting for: None.\nBlocked by: None.\n"),
           "issue_dependencies_summary": {"total_blocked_by": 0}}
    report = module.stale_wait_report([row], "owner/catalog", fetch=lambda _: [])
    assert report["complete"] and report["items"][0]["number"] == 41, report


def test_invalid_milestone_wait_is_reported_but_named_event_or_native_blocker_is_valid() -> None:
    module = load()
    base = {**issue(120, "Inventory", labels=("plan:waiting",)),
            "milestone": {"title": "Dogfood week"}}
    for reason in ("starts after milestone Thin fork decision", "", "nothing"):
        waiting = {**base, "body": "## Current Status\nState: Waiting.\nWaiting for: " + reason}
        assert "milestone_wait_invalid" in kinds(run(module, issues=[waiting]))
    event = {**base, "body": "## Current Status\nWaiting for: beta release on October 1."}
    assert "milestone_wait_invalid" not in kinds(run(module, issues=[event]))


def test_overall_audit_follows_track_to_invalid_cross_repository_wait() -> None:
    module = load()
    root = {**issue(1, "Track: Dogfood week"), "state": "open",
            "milestone": {"title": "Dogfood week", "state": "open"},
            "html_url": "https://github.com/owner/direction/issues/1"}
    child = {**issue(120, "Inventory", labels=("plan:waiting",),
                    body="## Current Status\nWaiting for: milestone Thin fork decision."),
             "state": "open", "html_url": "https://github.com/owner/business/issues/120"}
    def fetch(args: list[str]) -> Any:
        path = args[1].split("?")[0]
        if path == "repos/owner/direction/issues/1/sub_issues":
            return [child]
        if path == "repos/owner/business/issues/120":
            return child
        return []
    pull = {**issue(2, "Direction proposal"), "pull_request": {"url": "https://github.com/owner/direction/pull/2"}}
    other = {**issue(3, "Track: Thin fork decision"), "state": "open",
             "milestone": {"title": "Thin fork decision", "state": "open"},
             "html_url": "https://github.com/owner/direction/issues/3"}
    assert not module.enrich_milestone_waits([pull, root, other], "owner/direction", ["Dogfood week", "Thin fork decision"], fetch=fetch)
    result = run(module, issues=[pull, root, other])
    finding = next(item for item in result["findings"] if item["kind"] == "milestone_wait_invalid")
    assert (finding["repo"], finding["number"]) == ("owner/business", 120)
    assert sum(item["kind"] == "milestone_wait_invalid" for item in result["findings"]) == 1
    assert module.enrich_milestone_waits([root], "owner/direction", ["Dogfood week"],
                                       fetch=lambda _: (_ for _ in ()).throw(module.AuditError("unavailable")))


def test_local_wait_findings_need_no_dependency_reads() -> None:
    module = load()
    base = {**issue(120, "Inventory", labels=("plan:waiting",),
                    body="## Current Status\nWaiting for: milestone Thin fork decision."),
            "milestone": {"title": "Dogfood week"}}
    zero = {**base, "issue_dependencies_summary": {"blocked_by": 0}}
    def denied(_args: list[str]) -> Any:
        raise module.AuditError("unavailable")
    assert not module.enrich_milestone_waits([zero], "owner/product", ["Thin fork decision", "Dogfood week"], fetch=denied)
    assert "milestone_wait_invalid" in kinds(run(module, issues=[zero]))
    unknown = {**base}
    assert not module.enrich_milestone_waits([unknown], "owner/product", ["Thin fork decision", "Dogfood week"], fetch=denied)
    assert "milestone_wait_invalid" in kinds(run(module, issues=[unknown]))
    module.planning_config = lambda _: {"labels": {"waiting": "paused"}}
    custom = {**base, "body": "## Current Status\nWaiting for: nothing.", "labels": [{"name": "paused"}]}
    assert not module.enrich_milestone_waits([custom], "owner/product", ["Dogfood week"], fetch=denied)
    assert "milestone_wait_invalid" in kinds(run(module, issues=[custom]))


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
