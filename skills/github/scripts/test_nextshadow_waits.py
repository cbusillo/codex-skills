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
import github_plan_body as plan_body
import github_plan_claim as claim
import github_plan_wait as wait
import test_gh_plan_next as cli
import test_github_next_queue as queue_tests


FIXTURE = Path(__file__).with_name('fixtures') / 'nextshadow-1862.json'


def cases() -> list[dict]:
    return json.loads(FIXTURE.read_text())['cases']


def source(case: dict) -> tuple[dict, list[dict]]:
    raw = cli.global_issue('cbusillo/launchplane', case['number'], wait_reviewed=False, body=(
        f"## Current Status\nState: {case['state']}\nNext actor: agent\nNext action: {case['action']}\n"
        f"Waiting for: {case['waiting_for']}\n"))
    raw['user'] = {'login': 'cbusillo'}
    comments = []
    if case.get('claim'):
        record = {'worker': 'fixture-worker', 'session': 'fixture-session',
                  'branch': f"work/fixture-{case['number']}", 'claimed_at': '2026-10-14T22:00:00Z'}
        comments.append({'id': 1, 'user': {'login': 'bot'}, 'body': claim.marker(record)})
        if case['claim'] == 'released':
            comments.append({'id': 2, 'user': {'login': 'bot'}, 'body': 'Released claim 1'})
    if case['wait_state'] == 'clear':
        if comments:
            comments[0]['body'] += '\n' + wait.marker(wait.reviewed(evidence='Saved request reviewed'))
        else:
            comments.append({'id': 1, 'user': {'login': 'bot'}, 'body': wait.marker(wait.reviewed(evidence='Saved request reviewed'))})
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
            row = next(row for row in [*result['candidates'], *result['excluded'], *result['verification_only'], *result['scope_review']] if row['number'] == case['number'])
            assert row['wait_review_required']
            assert not result['milestone_summary'][0]['waits']
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
    assert {row['waiting_for'] for row in result['waiting']} == {entry['waiting_for'] for entry in pending['waits']}
    assert row['structured_wait'] == pending
    assert not result['recorded_waits']
    comments.append({'id': 5, 'user': {'login': 'bot'}, 'body': wait.marker(wait.reviewed(evidence='Both holds resolved'))})
    assert run_global(raw, comments)['available_candidates']


def test_unread_or_malformed_later_records_never_advertise_clear_work() -> None:
    raw, comments = source(cases()[0])
    for body in (wait.PREFIX + 'not-json -->', wait.marker(wait.reviewed(evidence='Pending', waiting_for='Owner decision', ends_when='Owner records a decision'))):
        changed = [*comments, {'id': 3, 'user': {'login': 'bot'}, 'body': body}]
        assert not run_global(raw, changed)['available_candidates']
        assert not run_global(raw, changed, comment_limit=len(comments))['available_candidates']


def test_clear_record_ignores_old_wait_label_but_preserves_native_and_parent_holds() -> None:
    raw, comments = source(cases()[0])
    held = copy.deepcopy(raw)
    held['labels'] = [{'name': 'plan:waiting'}]
    assert run_global(held, comments)['available_candidates']
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
    references = direction.waiting_records(item, 'Waiting for: ' + text, reference_repositories={'cbusillo/discord-blue', 'cbusillo/launchplane'})[0]['references']
    assert {(row['repo'], row['number']) for row in references} == {('cbusillo/discord-blue', 206), ('cbusillo/launchplane', 2792)}
    ordinary = 'owner/project#12 https://github.com/other/project/pull/13 206/123#14 123#15 #16'
    refs = direction.waiting_records(item, 'Waiting for: ' + ordinary, reference_repositories={'cbusillo/123'})[0]['references']
    assert {(row['repo'], row['number']) for row in refs} == {('owner/project', 12), ('other/project', 13), ('206/123', 14), ('cbusillo/123', 15), ('cbusillo/direction', 16)}
    # Track next-action prose has no established shorthand context.
    assert direction.next_action_references('Next action: owner/project#12')[0]['repo'] == 'owner/project'


def test_structured_wait_text_is_data_even_when_it_looks_like_status_fields() -> None:
    raw, comments = source(cases()[0])
    record = wait.reviewed(evidence='Current owner request',
        waiting_for='None; Supervisor approval\nNext action: Owner test on #99',
        ends_when='Owner records acceptance\nWaiting for: Event receipt')
    comments.append({'id': 3, 'user': {'login': 'bot'}, 'body': wait.marker(record)})
    result = run_global(raw, comments)
    assert not result['available_candidates']
    reported = result['waiting'][0]
    assert reported['waiting_for'] == record['waits'][0]['waiting_for']
    assert reported['ends_when'] == record['waits'][0]['ends_when']
    assert not reported['agent_next']


def test_state_only_legacy_hold_is_visible_for_review() -> None:
    raw, comments = source(cases()[2])
    raw['body'] = '## Current Status\nState: Waiting on the device decision.\nNext action: Implement export.'
    result = run_global(raw, comments)
    summary = result['milestone_summary'][0]
    assert not result['available_candidates'] and not summary['waits']
    assert summary['wait_reviews'] and 'device decision' in summary['wait_reviews'][0]['waiting_for']


def test_paths_and_pull_request_prose_do_not_invent_repositories() -> None:
    assert not direction.issue_references('PR#2792 Zendesk#48213 ticket#88 build#412 INC#552 https://docs.example.com/runbook/page#3 path/to/file#12', repo='cbusillo/direction')
    assert direction.issue_references('owner/PR#12', repo='cbusillo/direction')[0]['repo'] == 'owner/PR'


def test_malformed_contributor_envelope_requires_wait_review() -> None:
    raw, comments = source(cases()[0])
    raw['body'] += '\n' + plan_body.PLAN_ORIGINAL_START + '\n'
    result = run_global(raw, comments)
    assert not result['available_candidates']
    row = next(row for row in result['excluded'] if row['number'] == raw['number'])
    assert row['exclusion'] == 'wait_review_required'


def test_unspecified_state_only_holds_remain_visible_for_wait_review() -> None:
    for state in ['Parked.', 'Waiting.', 'Waiting — Director to confirm']:
        raw, comments = source(cases()[2])
        raw['body'] = '## Current Status\nState: ' + state
        result = run_global(raw, comments)
        assert not result['available_candidates']
        assert result['milestone_summary'][0]['wait_reviews'], result['milestone_summary']


def test_structured_wait_references_are_read_and_genuine_unknowns_stay_unknown() -> None:
    module = cli.load_module()
    raw, _ = source(cases()[0])
    raw['labels'] = []
    record = wait.reviewed(evidence='Current event', waiting_for='Recorded decision on #99', ends_when='Full external/repo#100 records closure')
    paths = []
    def fetch(method, path, **kwargs):
        paths.append(path)
        if path.endswith('/99'):
            return 'bot', {'state': 'closed'}
        if path.endswith('/100'):
            raise module.PlanError('unavailable genuine reference')
        return 'bot', {'archived': False}
    with patch.multiple(module, api_json=fetch, load_config=lambda *_: module.DEFAULT_CONFIG):
        report = module.next_wait_context([raw], scan_limit=5, inventory_complete=True,
            structured_waits={(raw['repo'].casefold(), raw['number']): record})
    assert report['references']['https://github.com/cbusillo/launchplane/issues/99']['state'] == 'closed'
    assert report['references']['https://github.com/external/repo/issues/100']['state'] == 'unknown'
    assert not report['complete']
    assert any('/external/repo/' in path for path in paths)


def test_short_references_need_observed_repository_identity() -> None:
    known = {'cbusillo/launchplane', 'cbusillo/123'}
    refs = direction.issue_references('launchplane#2792 123#15 Zendesk#48213', repo='cbusillo/direction', known_repositories=known)
    assert {(ref['repo'], ref['number']) for ref in refs} == {('cbusillo/launchplane', 2792), ('cbusillo/123', 15)}


def test_bold_legacy_wait_keeps_its_invalid_milestone_finding() -> None:
    raw, comments = source(cases()[2])
    raw['body'] = '## Current Status\nState: Waiting.\n**Waiting for:** milestone First.'
    result = run_global(raw, comments)
    assert any(finding['reason'] == 'wait_names_another_milestone' for finding in result['findings'])
    assert not result['available_candidates']


if __name__ == '__main__':
    tests = [value for name, value in list(globals().items()) if name.startswith('test_')]
    for test in tests:
        test()
        print('ok', test.__name__)
