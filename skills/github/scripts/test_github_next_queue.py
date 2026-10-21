# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavioral coverage for current source work versus delivery qualification."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import github_direction_next as direction
import github_next_queue as queue
import github_plan_claim as claim
import github_plan_wait as wait
import test_gh_plan_next as cli


def item(action: str = "Implement the missing export path.", state: str = "Active", number: int = 1) -> dict:
    raw = cli.issue(number)
    raw['body'] = f"## Current Status\nState: {state}\nNext action: {action}\nWaiting for: None.\n"
    raw['body'] += "\n" + wait.marker(wait.reviewed(evidence="Fixture request reviewed; no current holds"))
    raw['user'] = {'login': 'owner'}
    return {**direction.compact_list_issue('owner/repo', raw),
            'discussion': direction.discussion_snapshot(raw, [], complete=True)}


def evidence() -> dict:
    return {'complete': True, 'pulls': [], 'inventory': {'local_branches': [], 'remote_branches': [], 'worktrees': []},
            'coverage': {'sessions': 'partial'}}


def classify(row: dict, observed: dict | None = None) -> dict:
    return queue.classify(row, observed or evidence(), trusted={'owner', 'bot'})


def test_october_16_delivered_and_unstarted_status_shapes() -> None:
    fixtures = json.loads(Path(__file__).with_name('fixtures').joinpath('next-current-status.json').read_text())
    for number, record in fixtures.items():
        row = item(record['action'], record['state'], int(number))
        status = queue.current_status(row['discussion'], {'owner'})
        result = queue.remaining_work(status)
        assert result['state'] == record['expected_work_state'], (number, result)
        if record['expected_work_state'] == 'engineering':
            assert classify(row)['state'] == 'available'
        else:
            assert result['state'] != 'engineering', (number, result)
            assert classify(row)['state'] != 'available', (number, classify(row))
    for state in ['Active; prerequisite #3266 merged via PR #3310.', 'Active; integration remains unstarted.']:
        assert classify(item(fixtures['3267']['action'], state))['state'] == 'available'
    assert classify(item('Verify deployed export.', 'Active; first source slice merged and prerequisite #10 landed'))['state'] == 'verification'
    # Finished clauses cannot create or erase a genuine present source step.
    for action in ['Earlier repair is complete. Implement the remaining parser.',
                   'Implement the remaining parser; the prerequisite repair is merged.',
                   'An agent implements the CLI wiring once #3266 is merged.',
                   'Fix the importer so the backfill is complete.',
                   'Earlier repair is complete and an agent implements the remaining parser.',
                   'Since #3266 is already merged, an assigned agent integrates the generic-web driver.',
                   'Prerequisite #3266 is complete, so implement the remaining parser.',
                   'An assigned agent integrates the generic-web driver now that #3266 is merged.',
                   'Implement the remaining parser because the earlier repair is merged.',
                   'Diagnose the importer timeout; historical traces are attached.']:
        assert classify(item(action, 'Active; first slice merged'))['state'] == 'available'


def test_source_work_is_visible_without_caller_snapshot_but_requires_claim() -> None:
    result = classify(item())
    assert result['state'] == 'available'
    assert result['claim_required'] and not result['ownership_complete']
    assert result['ownership_coverage']['sessions'] == 'partial'
    assert not result['wait_review_required']
    observed = evidence()
    observed['complete'] = False
    assert classify(item(), observed)['state'] == 'review'
    row = item()
    row['discussion']['complete'] = False
    assert classify(row)['state'] == 'review'
    row['discussion']['complete'] = 'unknown'
    assert classify(row)['state'] == 'review'
    row = item()
    row['discussion']['author'] = None
    row['discussion']['comments'] = [{'author': None, 'body': 'Historical comment'}]
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
    source = {**source, 'branch': 'work/issue-1-source'}
    row['discussion']['comments'][0]['body'] = claim.marker(source)
    row['discussion']['comments'][-1]['created_at'] = '2026-10-01T00:01:00Z'
    observed['pulls'] = []
    observed['inventory']['remote_branches'] = [source['branch']]
    observed['inventory']['local_branches'] = [source['branch']]
    assert classify(row, observed)['state'] == 'available'
    row['discussion']['comments'].pop()
    assert classify(row, observed)['state'] == 'underway'


def test_current_qualification_does_not_hide_a_mixed_engineering_remainder() -> None:
    for state, action in [
        ('Active; source engineering is complete and landed', 'Supervisor carries accepted releases, then verifies process arguments.'),
        ('Active; source repair merged; physical confirmation outstanding', 'Delivery agent supplies the signed companion build; owner compares device freshness.'),
        ('Active; qualification remains the prerequisite', 'Observe qualification evidence. Once qualified, remove the old schedule.'),
        ('Active; completed reader/writer slices recorded', 'Read the next natural TLS cycle and complete diagnostic evidence.'),
        ('Active; source repair landed; device acceptance remains', 'Delivery agent provides the matching signed build, records installed version/build, then owner performs device test notes.'),
        ('Active; implementation merged; installed qualification remains', 'Release engineering continues genuine Build verification, then qualifies the supported rollout.'),
        ('Active; native qualification remains', 'A separately selected worker qualifies native child-reaping behavior. Correct only a reproduced defect.'),
        ('Active; source merged', 'Verify the latest build.'),
        ('Active; source merged', 'Observe the next build.'),
        ('Active; source merged', 'Test this update.'),
    ]:
        result = classify(item(action, state))
        assert result['state'] == 'verification', result
        assert result['next_action'] == action
    mixed = item('Implement the remaining classifier migration.', 'Active; the first slice is merged; source work remains')
    assert classify(mixed)['state'] == 'available'
    assert classify(item('An executing agent repairs the controller response projection.', 'Active; first source PR landed'))['state'] == 'available'
    assert classify(item('Implement a feature that verifies TLS certificates.'))['state'] == 'available'
    assert classify(item('Verify export and fix the reproduced fault.', 'Active; earlier source merged'))['state'] == 'available'
    assert classify(item('Verify the next deployed candidate update preserves the request.'))['state'] == 'review'
    assert classify(item('Supervisor verifies deployed web drill support.'))['state'] == 'review'
    assert classify(item('Another source owner delivers the prerequisite repair with a PR, then Supervisor verifies runtime.', 'Active; original source landed but the updater fails'))['state'] == 'review'
    assert classify(item('An agent records scheduled-path recovery evidence after source landing.', 'Active; Source landing confirmed'))['state'] == 'verification'
    assert classify(item('Supervisor selects remaining work through canonical #7.', 'Active; original slice already delivered'))['state'] == 'scope_review'
    assert classify(item('#7 moves shared build config into the site repos.', 'Active; original source is done'))['state'] == 'scope_review'
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
    row['discussion']['comments'] = [{'author': 'owner', 'created_at': '2026-10-12T00:00:00Z', 'body': '## Current Status\nState: Active; implementation landed\nNext action: Verify the deployed image.\nWaiting for: None.'}]
    assert classify(row)['state'] == 'verification'
    row['discussion']['updated_at'] = '2026-10-13T00:00:00Z'
    assert classify(row)['state'] == 'available'
    row['discussion']['updated_at'] = '2026-08-21T00:00:00Z'
    body = row['discussion']['body']
    row['discussion']['body'] = 'Original request without managed status.'
    row['discussion']['comments'].insert(0, {'author': 'owner', 'body': body})
    assert classify(row)['state'] == 'verification'
    row['discussion']['body'] = body
    row['discussion']['comments'].pop(0)
    row['discussion']['comments'][0]['author'] = 'foreign'
    assert classify(row)['state'] == 'available'
    row = item()
    row['discussion']['body'] += '\n## Scope\nOriginal request retained above.\n## Current Status\nState: Active; source landed\nNext action: Verify deployed export.\nWaiting for: None.'
    assert classify(row)['state'] == 'verification'
    row = item()
    row['discussion']['body'] = row['discussion']['body'].replace('Waiting for: None.', 'Waiting for: Owner must approve the migration.')
    assert classify(row)['state'] == 'available'  # Structured clear is authoritative over stale prose.


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
    reviewed_row = rows[0]
    for state in ['waiting', 'ineligible', 'underway']:
        review = {'state': state, 'reason': 'Caller checked current ownership/wait', 'evidence': ['Current thread'],
                  'discussion_digest': reviewed_row['discussion']['digest']}
        context = {'issues': {'owner/repo#1': review}}
        assert not module.project_local_queue([reviewed_row], [], context)['available_candidates']
        review['discussion_digest'] = 'obsolete discussion'
        assert module.project_local_queue([reviewed_row], [], context)['available_candidates']
    counts = queue.coverage(rows, rows[:2], result)
    assert counts['active_inventory_count'] == 4 and counts['active_unevaluated_count'] == 2
    ambiguous = item('Evaluate remaining export scope.')
    ambiguous['queue'] = classify(ambiguous)
    review = {'state': 'available', 'reason': 'Current source scope reviewed', 'evidence': ['Current thread'],
              'ownership_complete': True, 'discussion_digest': ambiguous['discussion']['digest']}
    context = {'issues': {'owner/repo#1': review}}
    assert module.project_local_queue([ambiguous], [], context)['available_candidates']
    ambiguous['queue']['observed_ownership_complete'] = False
    assert not module.project_local_queue([ambiguous], [], context)['available_candidates']


def test_bounded_global_coverage_counts_active_discoveries_not_yet_read() -> None:
    discoveries = [cli.global_issue('someone/product', number) for number in (1, 2)]
    with cli.global_fixture([], [], {}, discovered=discoveries) as (module, result, _reads):
        module.cmd_next(cli.next_args(scan_limit=1))
    counts = result['selection_coverage']
    assert counts['active_inventory_count'] == len(discoveries)
    assert counts['active_evaluated_count'] == 1
    assert counts['active_unevaluated_count'] == 1
    assert result['discovery_context']['unevaluated_count'] == 1


def test_default_global_cli_discovers_source_without_caller_review_and_splits_delivery() -> None:
    root = cli.global_issue('someone/direction', 1, title='Track: First', milestone=cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z'))
    source = cli.global_issue('someone/product', 2, body='## Current Status\nState: Active\nNext action: Implement export.\nWaiting for: None.')
    owned = cli.global_issue('someone/product', 3, body=source['body'])
    delivered = cli.global_issue('someone/product', 4, body='## Current Status\nState: Active; source merged\nNext action: Verify deployed export.\nWaiting for: None.')
    for raw in [root, source, owned, delivered]:
        raw['user'] = {'login': 'someone'}
        raw['body'] = (raw.get('body') or '') + '\n' + wait.marker(wait.reviewed(evidence='Fixture request reviewed'))
    holder = {'worker': 'worker', 'session': 'native-worker', 'branch': 'work/export', 'claimed_at': '2026-10-01T00:00:00Z'}
    comments = {(owned['repo'], owned['number']): [{'id': 1, 'user': {'login': 'bot'}, 'body': claim.marker(holder)}]}
    edges = {(root['repo'], root['number']): cli.relationships(sub_issues=[source, owned, delivered])}
    with cli.global_fixture([root], [source, owned, delivered], edges, comments=comments) as (module, result, _reads):
        original_read = module.collect_paged_rest_items
        def read(path: str, **kwargs):
            if path.endswith('/pulls') or path.endswith('/branches'):
                return 'bot', []
            return original_read(path, **kwargs)
        with patch.multiple(module, annotate_next_queue=module.real_annotate_next_queue, collect_paged_rest_items=read), patch.multiple(module.github_identity, configured_bot_logins=lambda: ('bot',)), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
            module.cmd_next(cli.next_args())
    assert [row['number'] for row in result['available_candidates']] == [source['number']]
    assert [row['number'] for row in result['underway']] == [owned['number']]
    assert [row['number'] for row in result['verification_only']] == [delivered['number']]
    assert result['queue_context']['claim_required']


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



def test_clear_discovered_milestone_work_is_available_without_a_caller_snapshot() -> None:
    row = item()
    row.update(queue=classify(row), plan_status='active', milestone=cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z'))
    row['discussion']['ancestry_complete'] = True
    graph = {'candidates': [], 'excluded': [], 'dependency_context': {'complete': True}}
    def rank():
        return direction.rank_portfolio_work(graph, [row], milestone_titles=['First'],
                                             repository_waypoints={'owner/repo': ['First']})
    result = rank()
    assert [entry['number'] for entry in result['available_candidates']] == [row['number']]
    assert result['available_candidates'][0]['category'] == 'milestone'
    assert result['available_candidates'][0]['claim_required']
    current_review = {'state': 'available', 'category': 'milestone', 'ownership_complete': True,
                      'discussion_digest': row['discussion']['digest'], 'reason': 'Current evidence', 'evidence': ['Fixture review']}
    reviewed = direction.rank_portfolio_work(graph, [row], milestone_titles=['First'],
               repository_waypoints={'owner/repo': ['First']}, selection_context={'issues': {'owner/repo#1': current_review}})
    assert reviewed['available_candidates']
    row['discussion']['ancestry_complete'] = False
    assert not rank()['available_candidates']
    row['discussion']['ancestry_complete'] = True
    row['queue']['state'] = 'review'
    row['queue']['reason'] = 'ownership_reads_incomplete'
    assert not rank()['available_candidates']
    row['queue'] = classify(item())
    row['milestone']['state'] = 'closed'
    assert not rank()['available_candidates']


def test_delivered_recovery_and_release_records_are_not_new_source_assignments() -> None:
    # Sanitized reproductions of the two duplicate-launch current actions.
    recovery = item("Coordinate #7's isolated testing/gated release prerequisite and an exact bounded recovery plan. Reconcile ID coverage after authorized recovery; do not repeat the landed source repair.",
                    'Active; source repair landed in PR #80; the data gap is not repaired')
    release = item("Supervisor reconciles downstream image consumption and accepted-release gates, then delivers and safely verifies process arguments. No duplicate implementation is needed.",
                   'Active release follow-through; source engineering is complete and landed')
    for row in [recovery, release]:
        row['discussion']['body'] = wait.without_records(row['discussion']['body'])
        result = classify(row)
        assert result['state'] == 'verification'
        assert result['wait_review_required']  # No wait is silently cleared.
    for action in ['An executing agent selects the next worthwhile duplicate-code cluster and opens its completing PR.',
                   'An executing agent selects the next independent test-audit slice and opens its completing PR.']:
        result = classify(item(action, 'Active; real remaining work, with delivered source preserved'))
        assert result['state'] == 'available', result


def test_verified_merged_source_is_reviewed_but_a_new_source_remainder_can_proceed() -> None:
    module = cli.load_module()
    row = item('Implement the export path.')
    row['discussion']['body'] += '\nEvidence: https://github.com/owner/repo/pull/80'
    pull = {'number': 80, 'body': 'Refs #1', 'merged_at': '2026-09-01T00:00:00Z', 'base': {'ref': 'main'}}
    def read(_method, path, payload=None, **_kwargs):
        if path == '/graphql':
            assert payload['variables']['number'] == row['number']
            return 'bot', {'data': {'repository': {'issue': {'createdAt': '2026-08-01T00:00:00Z', 'lastEditedAt': None}}}}
        if '/pulls/' in path:
            return 'bot', pull
        return 'bot', {'default_branch': 'main'}
    with patch.object(module, 'api_json', read):
        row['source_delivery'] = module.next_source_delivery(row, {'owner', 'bot'})
    assert row['source_delivery']['complete']
    assert row['source_delivery']['merged_prs'][0]['url'].endswith('/pull/80')
    assert not row['source_delivery']['completion_proven']
    assert classify(row)['state'] == 'scope_review'
    # A fresh recorded source remainder is not erased by an earlier merged slice.
    row['discussion']['updated_at'] = '2026-09-02T00:00:00Z'
    row['discussion']['comments'] = [{'author': 'owner', 'body': 'Claim release and closeout', 'created_at': row['discussion']['updated_at']}]
    assert classify(row)['state'] == 'scope_review'  # Activity is not a status edit.
    row['source_delivery']['body_recorded_at'] = '2026-09-02T00:00:00Z'
    assert classify(row)['state'] == 'scope_review'  # An unrelated body edit does not identify a new remainder.
    row['discussion']['body'] = row['discussion']['body'].replace('Implement the export path.', 'Implement the remaining export parser.')
    assert classify(row)['state'] == 'scope_review'  # Body-only remainder still needs a current review.
    row['discussion']['comments'].append({'author': 'owner', 'created_at': '2026-09-02T00:01:00Z', 'body': '## Current Status\nState: Active\nNext action: Implement the remaining export parser.\nWaiting for: None.\nEvidence: https://github.com/owner/repo/pull/80'})
    assert classify(row)['state'] == 'available'
    row['source_delivery']['complete'] = False
    assert classify(row)['state'] == 'review'
    row['source_delivery']['body_recorded_at'] = '2026-08-01T00:00:00Z'
    row['source_delivery']['complete'] = True
    row['discussion']['comments'] = [{'author': 'owner', 'body': '## Current Status\nState: Active; earlier slice merged\nNext action: Implement the remaining export handler.', 'created_at': '2026-09-03T00:00:00Z', 'updated_at': '2026-09-03T00:00:00Z'}]
    assert classify(row)['state'] == 'available'


def test_merge_evidence_requires_implementation_association_and_default_destination() -> None:
    module = cli.load_module()
    row = item('Review the remaining scope.')
    row['discussion']['body'] += '\nSource: PR #80 and https://github.com/owner/repo/pull/80'
    assert queue.delivery_references(row, {'owner'}) == [('owner/repo', 80)]
    pull = {'number': 80, 'body': 'Refs #1', 'merged_at': '2026-09-01T00:00:00Z', 'base': {'ref': 'main'}}
    def read(_method, path, **_kwargs):
        return 'bot', pull if '/pulls/' in path else {'default_branch': 'main'}
    with patch.object(module, 'api_json', read):
        row['source_delivery'] = module.next_source_delivery(row, {'owner'})
        assert classify(row)['state'] == 'scope_review'
        for key, value in [('body', 'Background context #1'), ('base', {'ref': 'release'}), ('merged_at', None)]:
            changed = {**pull, key: value}
            with patch.object(module, 'api_json', lambda _method, path, **_kwargs: ('bot', changed if '/pulls/' in path else {'default_branch': 'main'})):
                assert not module.next_source_delivery(row, {'owner'})['merged_prs']
    with patch.object(module, 'api_json', Mock(side_effect=module.PlanError('Unavailable source PR'))):
        result = module.next_source_delivery(row, {'owner'})
    assert not result['complete'] and result['unavailable']
    row['discussion']['comments'] = [{'author': 'stranger', 'body': '## Current Status\nSource: PR #99'}]
    assert ('owner/repo', 99) not in queue.delivery_references(row, {'owner'})



def test_source_slice_is_not_selected_before_its_qualification_or_for_an_owner() -> None:
    result = classify(item('Observe qualification evidence; once qualified, an executing agent selects the next independent source slice.',
                           'Active; qualification remains'))
    assert result['state'] == 'verification'
    assert classify(item('Supervisor selects the next source owner.'))['state'] == 'review'
    assert classify(item('An executing agent selects the next refactor slice.'))['state'] == 'available'


def test_current_caller_review_can_establish_a_merged_records_real_remainder() -> None:
    row = item('Evaluate remaining export scope.')
    row['source_delivery'] = {'complete': True, 'merged_prs': [{'url': 'https://github.com/owner/repo/pull/80', 'merged_at': '2026-09-01T00:00:00Z'}]}
    row['queue'] = classify(row)
    assert row['queue']['state'] == 'scope_review'
    row['discussion']['ancestry_complete'] = True
    row['direction_category'] = 'own_project'
    review = {'state': 'available', 'category': 'own_project', 'ownership_complete': True,
              'discussion_digest': row['discussion']['digest'], 'reason': 'Remaining handler reviewed', 'evidence': ['Fixture review']}
    context = {'issues': {'owner/repo#1': review}}
    graph = {'candidates': [], 'excluded': [], 'dependency_context': {'complete': True}}
    assert direction.rank_portfolio_work(graph, [row], milestone_titles=[], selection_context=context)['available_candidates']
    module = cli.load_module()
    assert module.project_local_queue([row], [], context)['available_candidates']
    review['discussion_digest'] = 'stale'
    assert not direction.rank_portfolio_work(graph, [row], milestone_titles=[], selection_context=context)['available_candidates']
    assert not module.project_local_queue([row], [], context)['available_candidates']

    followthrough = item('An executing agent writes the remaining deployment guide and opens its PR.', 'Active; first slice merged')
    followthrough['source_delivery'] = {'complete': True, 'merged_prs': []}
    followthrough['queue'] = classify(followthrough)
    assert followthrough['queue']['state'] == 'scope_review'
    review['discussion_digest'] = followthrough['discussion']['digest']
    assert module.project_local_queue([followthrough], [], context)['available_candidates']


def test_post_merge_status_comment_stays_current_after_unrelated_issue_activity() -> None:
    row = item('Implement the export handler.')
    row['source_delivery'] = {'complete': True, 'body_recorded_at': '2026-08-01T00:00:00Z',
        'merged_prs': [{'merged_at': '2026-09-01T00:00:00Z', 'url': 'https://github.com/owner/repo/pull/80'}]}
    row['discussion']['updated_at'] = '2026-09-03T00:00:00Z'  # A later comment or label event.
    row['discussion']['comments'] = [{'author': 'owner', 'created_at': '2026-09-02T00:00:00Z',
        'body': '## Current Status\nState: Active; source merged\nNext action: Verify deployed export.'}]
    assert classify(row)['state'] == 'verification'
    row['discussion']['comments'][0]['body'] = '## Current Status\nState: Active; source merged\nNext action: Implement the remaining parser.'
    assert classify(row)['state'] == 'available'



def test_delivery_bounds_deadlines_and_incomplete_review_never_prove_availability() -> None:
    module = cli.load_module()
    row = item('Evaluate the remaining scope.')
    row['discussion']['body'] += '\nSources: ' + ' '.join(f'https://github.com/owner/repo/pull/{n}' for n in range(80, 80 + module.NEXT_DELIVERY_REFERENCE_LIMIT + 1))
    with patch.object(module, 'api_json', lambda *_args, **_kwargs: ('bot', {'merged_at': None})):
        row['source_delivery'] = module.next_source_delivery(row, {'owner'})
    assert not row['source_delivery']['complete'] and row['source_delivery']['omitted_reference_count']
    row['queue'] = classify(row)
    assert row['queue']['state'] == 'review'
    review = {'state': 'available', 'ownership_complete': True, 'discussion_digest': row['discussion']['digest']}
    assert not module.project_local_queue([row], [], {'issues': {'owner/repo#1': review}})['available_candidates']
    review.update(reason='All current source PRs and remaining scope reviewed', evidence=['Current source review'])
    assert module.project_local_queue([row], [], {'issues': {'owner/repo#1': review}})['available_candidates']
    assert not row['source_delivery']['complete']  # The automated read gap remains visible.
    with patch.object(module, 'api_json', Mock(side_effect=AssertionError('Expired reads must not run'))) as api:
        with patch.object(module, 'NEXT_READS', Mock(expired=True)):
            bounded = module.next_source_delivery(row, {'owner'})
    assert not bounded['complete'] and bounded['unread_reference_count']
    api.assert_not_called()


def test_delivery_annotation_uses_the_real_adapter_and_preserves_unknown_graphql() -> None:
    module = cli.load_module()
    row = item('Implement the remaining handler.')
    row['discussion']['body'] += '\nSource: PR #80'
    row['discussion']['updated_at'] = '2026-09-03T00:00:00Z'
    row['discussion']['comments'] = [{'author': 'owner', 'created_at': '2026-09-02T00:00:00Z', 'body': '## Current Status\nState: Source merged\nNext action: Verify deployed handler.'}]
    pull = {'number': 80, 'body': 'Refs #1', 'merged_at': '2026-09-01T00:00:00Z', 'base': {'ref': 'main'}}
    def read(_method, path, payload=None, **_kwargs):
        if path == '/graphql':
            return 'bot', []  # A malformed successful response is unknown, never a crash.
        return 'bot', pull if '/pulls/' in path else {'default_branch': 'main'}
    with patch.multiple(module, api_json=read, collect_paged_rest_items=lambda *_a, **_kw: ('bot', [])), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
        context = module.real_annotate_next_queue([row], cli.next_args(repo='owner/repo'))
    assert context['claim_required']
    assert row['queue']['state'] == 'review'
    assert not row['source_delivery']['complete'] and row['source_delivery']['body_edit_error']



def test_real_reader_finds_merged_pr_in_status_comment_after_later_activity() -> None:
    module = cli.load_module()
    row = item('Implement the export parser.')
    row['discussion']['updated_at'] = '2026-09-03T00:00:00Z'
    row['discussion']['comments'] = [{'author': 'owner', 'created_at': '2026-09-02T00:00:00Z',
        'body': '## Current Status\nState: Active; parser merged in PR #80\nNext action: Verify deployed export.'}]
    def read(_method, path, payload=None, **_kwargs):
        if path == '/graphql':
            return 'bot', {'data': {'repository': {'issue': {'createdAt': '2026-08-01T00:00:00Z', 'lastEditedAt': None}}}}
        if '/pulls/' in path:
            return 'bot', {'number': 80, 'body': 'Refs #1', 'merged_at': '2026-09-01T00:00:00Z', 'base': {'ref': 'main'}}
        return 'bot', {'default_branch': 'main'}
    with patch.multiple(module, api_json=read, collect_paged_rest_items=lambda *_a, **_kw: ('bot', [])), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
        module.real_annotate_next_queue([row], cli.next_args(repo='owner/repo'))
    assert row['source_delivery']['merged_prs']
    assert row['queue']['state'] == 'verification'



def test_historical_status_prs_do_not_block_a_current_source_remainder() -> None:
    module = cli.load_module()
    row = item('Implement the remaining export handler.')
    row['discussion']['comments'] = [{'author': 'owner', 'created_at': f'2025-01-{n:02d}T00:00:00Z',
        'body': f'## Current Status\nState: Active; source merged in PR #{n}\nNext action: Verify the previous slice.'} for n in range(1, 11)]
    def read(_method, _path, payload=None, **_kwargs):
        assert payload is not None  # No historical PR or inaccessible old repo is queried.
        return 'bot', {'data': {'repository': {'issue': {'createdAt': '2026-08-01T00:00:00Z', 'lastEditedAt': '2026-08-21T00:00:00Z'}}}}
    with patch.multiple(module, api_json=read, collect_paged_rest_items=lambda *_a, **_kw: ('bot', [])), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
        module.real_annotate_next_queue([row], cli.next_args(repo='owner/repo'))
    assert row['source_delivery']['complete']
    assert not row['source_delivery']['merged_prs']
    assert row['queue']['state'] == 'available'


def test_delivered_status_comment_does_not_need_a_verifiable_pr_to_stop_duplicates() -> None:
    module = cli.load_module()
    for state in ['Active; source merged', 'Active; source merged in PR #80']:
        row = item('Implement the original export.')
        row['discussion']['updated_at'] = '2026-09-03T00:00:00Z'
        row['discussion']['comments'] = [{'author': 'owner', 'created_at': '2026-09-02T00:00:00Z',
            'body': f'## Current Status\nState: {state}\nNext action: Verify deployed export.'}]
        def read(_method, path, payload=None, **_kwargs):
            if payload:
                return 'bot', {'data': {'repository': {'issue': {'createdAt': '2026-08-01T00:00:00Z', 'lastEditedAt': None}}}}
            return 'bot', ({'number': 80, 'body': 'Part of #1', 'merged_at': '2026-09-01T00:00:00Z', 'base': {'ref': 'main'}} if '/pulls/' in path else {'default_branch': 'main'})
        with patch.multiple(module, api_json=read, collect_paged_rest_items=lambda *_a, **_kw: ('bot', [])), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
            module.real_annotate_next_queue([row], cli.next_args(repo='owner/repo'))
        assert row['queue']['state'] == 'verification'
        assert not row['source_delivery']['merged_prs']


def test_inherited_overall_milestone_orders_work_without_copying_membership() -> None:
    early = item(number=1)
    later = item(number=2)
    parent = {'repo': 'owner/repo', 'number': 9, 'url': 'https://github.com/owner/repo/issues/9',
              'milestone': cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z')}
    for row in [early, later]:
        row.update(queue=classify(row), plan_status='active')
        row['discussion']['ancestry_complete'] = True
    early['discussion']['parents'] = [parent]
    later['milestone'] = cli.milestone_data(3, 'Third', created_at='2026-01-01T00:00:00Z')
    later['created_at'] = '2025-01-01T00:00:00Z'
    graph = {'candidates': [], 'excluded': [], 'dependency_context': {'complete': True}}
    result = direction.rank_portfolio_work(graph, [later, early], milestone_titles=['First', 'Second', 'Third'],
                                          repository_waypoints={'owner/repo': ['First', 'Second', 'Third']})
    assert [row['number'] for row in result['available_candidates']] == [early['number'], later['number']]
    assert result['available_candidates'][0].get('milestone') is None



def test_scope_review_wait_remains_in_its_matched_milestone_summary() -> None:
    row = item('An agent writes the remaining deployment guide.', 'Active; first slice merged')
    row.update(overall_milestone_context={'state': 'matched', 'titles': ['First']}, wait_review_required=True)
    summary = direction.milestone_summary(['First'], completed=[], candidates=[], graph_waits=[],
        track_next_actions={}, scope_review=[row])
    assert summary[0]['state'] == 'wait_review_required'
    assert summary[0]['scope_review'] and summary[0]['wait_reviews']


def main() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith('test_'):
            test()
    print('next queue tests passed')


if __name__ == '__main__':
    main()
