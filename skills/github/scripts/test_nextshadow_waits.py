# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline replay of NEXTSHADOW-D1's selection and reference disagreements."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import github_direction_next as direction
import github_next_queue as queue
import github_plan_claim as claim
import github_plan_wait as wait
import test_gh_plan_next as cli
import test_github_next_queue as queue_tests


FIXTURE = Path(__file__).with_name('fixtures') / 'nextshadow-1862.json'


def cases() -> list[dict]:
    return json.loads(FIXTURE.read_text())['cases']


def source(case: dict) -> tuple[dict, list[dict]]:
    raw = cli.global_issue('cbusillo/launchplane', case['number'], body=(
        f"## Current Status\nState: {case['state']}\nNext action: {case['action']}\n"
        f"Waiting for: {case['waiting_for']}\n"))
    raw['user'] = {'login': 'cbusillo'}
    comments = []
    if case.get('claim'):
        record = {'worker': 'fixture-worker', 'session': 'fixture-session',
                  'branch': f"work/fixture-{case['number']}", 'claimed_at': '2026-10-14T22:00:00Z'}
        comments.append({'id': 1, 'user': {'login': 'bot'}, 'body': claim.marker(record) + '\n' + wait.marker(wait.reviewed(evidence='Saved request reviewed'))})
        if case['claim'] == 'released':
            comments.append({'id': 2, 'user': {'login': 'bot'}, 'body': 'Released claim 1'})
    return raw, comments


def run_global(raw: dict, comments: list[dict], *, comment_limit: int = 100) -> dict:
    root = cli.track('cbusillo/direction', 1, 'First')
    edges = {(root['repo'], root['number']): cli.relationships(sub_issues=[raw])}
    with cli.global_fixture([root], [raw], edges, repo=root['repo'], comments={(raw['repo'], raw['number']): comments}) as (module, result, _reads):
        original = module.collect_paged_rest_items
        def read(path: str, **kwargs):
            if path.endswith('/pulls') or path.endswith('/branches'):
                return 'bot', []
            return original(path, **kwargs)
        with patch.multiple(module, annotate_next_queue=module.real_annotate_next_queue, collect_paged_rest_items=read), patch.multiple(module.github_identity, configured_bot_logins=lambda: ('bot',)), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
            args = cli.next_args(repo=root['repo'])
            args.comment_limit = comment_limit
            module.cmd_next(args)
    return result


def test_live_cases_preserve_available_review_and_underway_states() -> None:
    for case in cases():
        raw, comments = source(case)
        result = run_global(raw, comments)
        if case['wait_state'] == 'review':
            assert not result['available_candidates'], case
            row = next(row for row in result['excluded'] if row['number'] == case['number'])
            assert row['exclusion'] == 'wait_review_required', row
            assert row['wait_review_required']
            summary = result['milestone_summary'][0]
            assert summary['wait_reviews'] and not summary['waits'], summary
        elif case['claim'] == 'active':
            assert not result['available_candidates'], case
            assert [row['number'] for row in result['underway']] == [case['number']]
            summary = result['milestone_summary'][0]
            assert summary['underway'] and not summary['waits'], summary
        else:
            assert [row['number'] for row in result['available_candidates']] == [case['number']], result['candidates']
            assert not result['waiting'] and not result['recorded_waits']
            assert result['milestone_summary'][0]['work']
        # Local queue and global graph use the same structured authority.
        item = {**direction.compact_list_issue(raw['repo'], raw), 'discussion': direction.discussion_snapshot(raw, comments, complete=True)}
        projected = queue.classify(item, queue_tests.evidence(), trusted={'cbusillo', 'bot'})
        expected = 'review' if case['wait_state'] == 'review' else 'underway' if case['claim'] == 'active' else 'available'
        assert projected['state'] == expected, projected


def test_current_authorized_wait_wins_and_real_event_waits_remain() -> None:
    raw, comments = source(cases()[0])
    pending = wait.reviewed(evidence='Current request retains both holds',
        waiting_for=['Director device acceptance', 'Scheduled provider completion'],
        ends_when=['Director records the device result', 'Provider completion event is recorded'])
    comments.append({'id': 3, 'user': {'login': 'cbusillo'}, 'body': wait.marker(pending)})
    comments.append({'id': 4, 'user': {'login': 'outsider'}, 'body': wait.marker(wait.reviewed(evidence='Foreign clear'))})
    result = run_global(raw, comments)
    assert not result['available_candidates']
    row = next(row for row in result['excluded'] if row['number'] == raw['number'])
    assert row['exclusion'] == 'waiting'
    assert {row['waiting_for'].split(';')[0] for row in result['recorded_waits']} == {entry['waiting_for'] for entry in pending['waits']}
    comments.append({'id': 5, 'user': {'login': 'bot'}, 'body': wait.marker(wait.reviewed(evidence='Both holds resolved'))})
    assert run_global(raw, comments)['available_candidates']


def test_unread_or_malformed_later_records_never_advertise_clear_work() -> None:
    raw, comments = source(cases()[0])
    for body in ('<!-- github-plan:wait not-json -->', wait.marker(wait.reviewed(evidence='Pending', waiting_for='Owner decision', ends_when='Owner records a decision'))):
        changed = [*comments, {'id': 3, 'user': {'login': 'bot'}, 'body': body}]
        assert not run_global(raw, changed)['available_candidates']
        assert not run_global(raw, changed, comment_limit=len(comments))['available_candidates']


def test_clear_record_does_not_override_native_wait_or_blocker_or_parent_hold() -> None:
    raw, comments = source(cases()[0])
    held = copy.deepcopy(raw)
    held['labels'] = [{'name': 'plan:waiting'}]
    assert not run_global(held, comments)['available_candidates']
    module = cli.load_module()
    clear = wait.current(raw, comments, {'cbusillo', 'bot'})
    node = direction.evaluate_direction_node(raw, config=module.DEFAULT_CONFIG, focus=None,
        relationships=cli.relationships(blocked_by=[cli.related(99)]), structured_wait=clear)
    assert node['item']['exclusion'] == 'blocked_by_open_dependency'
    parent = direction.evaluate_direction_node(raw, config=module.DEFAULT_CONFIG, focus=None,
        relationships=cli.relationships(), structured_wait=wait.reviewed(evidence='Whole plan hold', waiting_for='Owner acceptance', ends_when='Owner accepts'))['item']
    parent['number'] = 99
    parent['url'] += '-parent'
    child = direction.include_parent_context(node['item'], [parent], complete=True, tracking_roots=[])
    assert child['exclusion'] == 'parent_waiting'


def test_adjacent_reference_boundaries_preserve_supported_context_and_numeric_names() -> None:
    item = {'repo': 'cbusillo/direction', 'number': 52, 'url': 'https://github.com/cbusillo/direction/issues/52'}
    text = 'discord-blue#206/launchplane#2792'
    references = direction.waiting_records(item, 'Waiting for: ' + text)[0]['references']
    assert {(row['repo'], row['number']) for row in references} == {('cbusillo/discord-blue', 206), ('cbusillo/launchplane', 2792)}
    ordinary = 'owner/project#12 https://github.com/other/project/pull/13 206/123#14 123#15 #16'
    refs = direction.waiting_records(item, 'Waiting for: ' + ordinary)[0]['references']
    assert {(row['repo'], row['number']) for row in refs} == {('owner/project', 12), ('other/project', 13), ('206/123', 14), ('cbusillo/123', 15), ('cbusillo/direction', 16)}
    # Track next-action prose has no established shorthand context.
    assert direction.next_action_references('Next action: ' + text) == []
    assert direction.next_action_references('Next action: owner/project#12')[0]['repo'] == 'owner/project'


def test_structured_wait_text_is_data_even_when_it_looks_like_status_fields() -> None:
    raw, comments = source(cases()[0])
    record = wait.reviewed(evidence='Current owner request',
        waiting_for='None; Supervisor approval\nNext action: Owner test on #99',
        ends_when='Owner records acceptance\nWaiting for: Event receipt')
    comments.append({'id': 3, 'user': {'login': 'bot'}, 'body': wait.marker(record)})
    result = run_global(raw, comments)
    assert not result['available_candidates']
    reported = result['recorded_waits'][0]
    assert reported['waiting_for'] == record['waits'][0]['waiting_for']
    assert reported['ends_when'] == record['waits'][0]['ends_when']
    assert not reported['agent_next']


def test_milestone_qualification_is_visible_even_without_source_work() -> None:
    row = {'repo': 'owner/repo', 'number': 1, 'url': 'https://github.com/owner/repo/issues/1', 'milestone': {'title': 'First'}}
    summary = direction.milestone_summary(['First'], completed=[], candidates=[], graph_waits=[], track_next_actions={}, verification_only=[row])[0]
    assert summary['state'] == 'work_listed' and summary['verification_only']
    assert not summary['waits']


if __name__ == '__main__':
    tests = [value for name, value in list(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
        print('ok', test.__name__)
