# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral coverage for current source work versus delivery qualification."""
from __future__ import annotations

from unittest.mock import Mock, patch

import github_direction_next as direction
import github_next_queue as queue
import github_plan_claim as claim
import github_plan_wait as wait
import test_gh_plan_next as cli


def item(action: str = "Implement the missing export path.", state: str = "Active", number: int = 1) -> dict:
    raw = cli.issue(number)
    raw['body'] = f"## Current Status\nState: {state}\nNext action: {action}\nWaiting for: None.\n"
    raw['user'] = {'login': 'owner'}
    return {**direction.compact_list_issue('owner/repo', raw),
            'discussion': direction.discussion_snapshot(raw, [], complete=True)}


def evidence() -> dict:
    return {'complete': True, 'pulls': [], 'inventory': {'local_branches': [], 'remote_branches': [], 'worktrees': [], 'sessions': []},
            'coverage': {'sessions': 'partial'}}


def classify(row: dict, observed: dict | None = None) -> dict:
    return queue.classify(row, observed or evidence(), trusted={'owner', 'bot'})


def test_source_work_is_visible_without_caller_snapshot_but_requires_claim() -> None:
    result = classify(item())
    assert result['state'] == 'available'
    assert result['claim_required'] and not result['ownership_complete']
    assert result['ownership_coverage']['sessions'] == 'partial'
    assert result['wait_review_required']
    observed = evidence()
    observed['complete'] = False
    assert classify(item(), observed)['state'] == 'review'
    row = item()
    row['discussion']['complete'] = False
    assert classify(row)['state'] == 'review'
    row['discussion']['complete'] = 'unknown'
    assert classify(row)['state'] == 'review'


def test_claim_and_linked_pr_cannot_be_advertised_as_unclaimed_work() -> None:
    row = item()
    source = {'worker': 'worker', 'session': 'native-session', 'branch': 'work/source', 'claimed_at': '2026-10-01T00:00:00Z'}
    row['discussion']['comments'] = [{'id': 1, 'author': 'bot', 'created_at': source['claimed_at'],
                                    'body': claim.marker(source)}]
    assert classify(row)['state'] == 'underway'
    row['discussion']['comments'].append({'id': 2, 'author': 'foreign', 'body': 'Released claim 1'})
    assert classify(row)['state'] == 'underway'
    row['discussion']['comments'].append({'id': 3, 'author': 'bot', 'body': 'Released claim 1'})
    assert classify(row)['state'] == 'available'
    observed = evidence()
    observed['pulls'] = [{'number': 80, 'state': 'open', 'title': 'Export implementation', 'body': 'Closes #1', 'head': {'ref': 'work/source'}}]
    assert classify(row, observed)['state'] == 'underway'
    # An unrelated PR mentioning the issue is not implementation ownership.
    observed['pulls'][0]['body'] = 'Background context: https://github.com/owner/repo/issues/1'
    assert classify(row, observed)['state'] == 'underway'  # recorded source branch
    observed['pulls'][0]['head']['ref'] = 'work/unrelated'
    assert classify(row, observed)['state'] == 'available'


def test_current_qualification_does_not_hide_a_mixed_engineering_remainder() -> None:
    for state, action in [
        ('Active; source engineering is complete and landed', 'Supervisor carries accepted releases, then verifies process arguments.'),
        ('Active; source repair merged; physical confirmation outstanding', 'Delivery agent supplies the signed companion build; owner compares device freshness.'),
        ('Active; qualification remains the prerequisite', 'Observe qualification evidence. Once qualified, remove the old schedule.'),
        ('Active; completed reader/writer slices recorded', 'Read the next natural TLS cycle and complete diagnostic evidence.'),
        ('Active; source repair landed; device acceptance remains', 'Delivery agent provides the matching signed build, records installed version/build, then owner performs device test notes.'),
        ('Active; implementation merged; installed qualification remains', 'Release engineering continues genuine Build verification, then qualifies the supported rollout.'),
        ('Active; native qualification remains', 'A separately selected worker qualifies native child-reaping behavior. Correct only a reproduced defect.'),
    ]:
        result = classify(item(action, state))
        assert result['state'] == 'verification', result
        assert result['next_action'] == action
    mixed = item('Implement the remaining classifier migration.', 'Active; the first slice is merged; source work remains')
    assert classify(mixed)['state'] == 'available'
    assert classify(item('An executing agent repairs the controller response projection.', 'Active; first source PR landed'))['state'] == 'available'
    assert classify(item('Implement a feature that verifies TLS certificates.'))['state'] == 'available'
    assert classify(item('Verify the next deployed candidate update preserves the request.'))['state'] == 'review'
    assert classify(item('Supervisor verifies deployed web drill support.'))['state'] == 'review'
    assert classify(item('Another source owner delivers the prerequisite repair with a PR, then Supervisor verifies runtime.', 'Active; original source landed but the updater fails'))['state'] == 'review'
    assert classify(item('An agent records scheduled-path recovery evidence after source landing.', 'Active; Source landing confirmed'))['state'] == 'verification'
    assert classify(item('Supervisor selects remaining work through canonical #7.', 'Active; original slice already delivered'))['state'] == 'scope_review'
    # Historical delivery elsewhere in the body is not current-scope delivery.
    row = item('Implement export; then verify it.')
    row['discussion']['body'] += '\n## History\nA prerequisite merged last week.'
    assert classify(row)['state'] == 'available'


def test_current_waits_and_authorized_status_override_prevent_false_availability() -> None:
    row = item()
    row['discussion']['comments'] = [{'id': 1, 'author': 'owner', 'body': wait.marker(wait.reviewed(evidence='Owner test required', waiting_for='Owner acceptance', ends_when='Owner records acceptance'))}]
    assert classify(row)['state'] == 'waiting'
    row['discussion']['comments'].append({'id': 2, 'author': 'foreign', 'body': wait.marker(wait.reviewed(evidence='Clear'))})
    assert classify(row)['state'] == 'waiting'
    row = item()
    row['discussion']['comments'] = [{'author': 'owner', 'body': '## Current Status\nState: Active; implementation landed\nNext action: Verify the deployed image.\nWaiting for: None.'}]
    assert classify(row)['state'] == 'verification'
    row['discussion']['comments'][0]['author'] = 'foreign'
    assert classify(row)['state'] == 'available'
    row = item()
    row['discussion']['body'] = row['discussion']['body'].replace('Waiting for: None.', 'Waiting for: Owner must approve the migration.')
    assert classify(row)['state'] == 'waiting'


def test_existing_artifacts_and_partial_reads_stay_out_of_start_queue() -> None:
    for field, value in [('local_branches', 'work/issue-1'), ('remote_branches', 'work/issue-1'), ('worktrees', {'branch': 'work/issue-1', 'path': '/workspace/issue-1'})]:
        observed = evidence()
        observed['inventory'][field] = [value]
        assert classify(item(), observed)['state'] == 'underway'
    row = item()
    row['discussion']['body'] += '\n<!-- github-plan:wait broken -->'
    assert classify(row)['state'] == 'review'


def test_direction_list_is_authoritative_and_holds_override_automatic_review() -> None:
    patterns = ['repo', 'family*']
    assert queue.own_project('owner/repo', patterns, 'owner')
    assert queue.own_project('owner/family-module', patterns, 'owner')
    assert not queue.own_project('foreign/repo', patterns, 'owner')
    assert not queue.own_project('owner/tools', patterns, 'owner')
    row = item()
    row.update(queue=classify(row), direction_category='own_project', plan_status='active')
    row['discussion']['ancestry_complete'] = True
    graph = {'candidates': [], 'excluded': [], 'dependency_context': {'complete': True}}
    result = direction.rank_portfolio_work(graph, [row], milestone_titles=[])
    assert len(result['available_candidates']) == 1
    assert not result['tooling_capacity_context']['admitted']
    context = {'repository_holds': {'owner/repo': {'reason': 'Director hold', 'evidence': ['Current instruction']}}}
    result = direction.rank_portfolio_work(graph, [row], milestone_titles=[], selection_context=context)
    assert not result['available_candidates']
    assert result['excluded'][0]['exclusion'] == 'repository_held'
    row['direction_category'] = None
    result = direction.rank_portfolio_work(graph, [row], milestone_titles=[])
    assert not result['available_candidates']  # no inferred tooling admission


def test_local_adapter_reads_current_ownership_and_splits_counts_without_writing() -> None:
    module = cli.load_module()
    rows = [item(number=n) for n in range(1, 5)]
    rows[1] = item('Verify the deployed result.', 'Active; source merged', 2)
    observed_paths = []
    def read(path: str, **_kwargs):
        observed_paths.append(path)
        if path.endswith('/pulls'):
            return 'bot', [{'number': 99, 'state': 'open', 'title': 'Implementation', 'body': 'Refs #3', 'head': {'ref': 'work/started'}}]
        if path.endswith('/branches'):
            return 'bot', []
        raise AssertionError(path)
    with patch.multiple(module, collect_paged_rest_items=read), patch.multiple(module.github_identity, configured_bot_logins=lambda: ('bot',)), patch.multiple(module.github_plan_claim, local_inventory=lambda *_a, **_kw: evidence()['inventory']):
        ctx = module.real_annotate_next_queue(rows, cli.next_args(repo='owner/repo'))
    result = module.project_local_queue(rows, [], {})
    assert [x['number'] for x in result['available_candidates']] == [1, 4]
    assert [x['number'] for x in result['verification_only']] == [2]
    assert [x['number'] for x in result['underway']] == [3]
    assert result['candidate_count'] == result['available_candidate_count'] == 2
    assert ctx['claim_required'] and not ctx['all_active_sessions_searched']
    assert len(observed_paths) == 2  # one ownership inventory per repo
    hold = {'repository_holds': {'owner/repo': {'reason': 'Hold', 'evidence': ['Director instruction']}}}
    assert not module.project_local_queue(rows, [], hold)['candidates']
    counts = queue.coverage(rows, rows[:2], result)
    assert counts['active_inventory_count'] == 4 and counts['active_unevaluated_count'] == 2


def test_bounded_global_coverage_counts_active_discoveries_not_yet_read() -> None:
    discoveries = [cli.global_issue('someone/product', number) for number in (1, 2)]
    with cli.global_fixture([], [], {}, discovered=discoveries) as (module, result, _reads):
        module.cmd_next(cli.next_args(scan_limit=1))
    counts = result['selection_coverage']
    assert counts['active_inventory_count'] == len(discoveries)
    assert counts['active_evaluated_count'] == 1
    assert counts['active_unevaluated_count'] == 1
    assert result['discovery_context']['unevaluated_count'] == 1


def test_repository_inventory_bound_can_be_widened_without_losing_held_work() -> None:
    module = cli.load_module()
    raw = [cli.global_issue('someone/product', n, labels=['plan', 'plan:waiting'] if n < 3 else ['plan', 'plan:active']) for n in (1, 2, 3)]
    def read(path: str, **kwargs):
        if path == '/installation/repositories':
            return 'bot', [{'full_name': 'someone/product', 'size': 1, 'open_issues_count': len(raw)}]
        assert path == '/repos/someone/product/issues'
        if kwargs['query'].get('labels'):
            return 'bot', []
        return 'bot', raw[:kwargs['limit']]
    args = cli.next_args()
    with patch.multiple(module, collect_paged_rest_items=read, load_direction=lambda *_: cli.DIRECTION), patch.multiple(module.github_identity, github_app_config=lambda: {}):
        complete, context = module.real_discover_direction_work('someone/direction', args, selection_context={})
        args.repository_issue_limit = 1
        limited, partial = module.real_discover_direction_work('someone/direction', args, selection_context={})
    assert len(complete) == len(raw) and context['complete']
    assert len(limited) < len(complete) and not partial['complete']
    assert any('plan:active' in direction.normalize_labels(row['labels']) for row in complete)


def test_optional_local_inventory_failure_keeps_remote_queue_evidence() -> None:
    module = cli.load_module()
    row = item()
    def read(_path: str, **_kwargs):
        return 'bot', []
    with patch.multiple(module, collect_paged_rest_items=read), patch.multiple(module.pathlib.Path, is_dir=lambda *_: True), patch.multiple(module.github_plan_claim, local_inventory=Mock(side_effect=ValueError('Local Git inventory unavailable'))):
        context = module.real_annotate_next_queue([row], cli.next_args(repo='owner/repo'))
    assert row['queue']['state'] == 'available'
    assert context['repositories']['owner/repo']['local_error']
    assert row['queue']['claim_required'] and not row['queue']['ownership_complete']


def test_status_formatting_and_prospective_delivery_do_not_hide_source_work() -> None:
    for status in [
        '- State: Active\n- Next action: Implement the export path.',
        '* State: Active\n* Next action: Implement the export path.',
        '**State**: Active\n**Next action**: Implement the export path.',
        '**State:** Active\n**Next action:** Implement the export path.',
    ]:
        assert queue.remaining_work(status)['state'] == 'engineering'
    for state in ['Active; not merged yet', 'Active; needs to be merged', 'Active; awaiting delivery', 'Active; will be delivered']:
        status = f'State: {state}\nNext action: Verify the deployed image.'
        assert queue.remaining_work(status)['state'] == 'review'


def main() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith('test_'):
            test()
    print('next queue tests passed')


if __name__ == '__main__':
    main()
