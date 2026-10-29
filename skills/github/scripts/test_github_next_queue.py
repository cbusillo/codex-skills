# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

from unittest.mock import Mock, patch

import github_direction_next as direction
import github_next_queue as queue
import github_plan_claim as claim
import github_plan_wait as wait
import test_gh_plan_next as cli


def item(action: str = "Implement the missing export path.", state: str = "Active", number: int = 1, actor: str = "agent") -> dict:
    raw = cli.issue(number)
    raw['body'] = f"## Current Status\nState: {state}\nNext actor: {actor}\nNext action: {action}\nWaiting for: None.\n"
    raw['body'] += "\n" + wait.marker(wait.reviewed(evidence="Fixture request reviewed; no current holds"))
    raw['user'] = {'login': 'owner'}
    return {**direction.compact_list_issue('owner/repo', raw),
            'discussion': direction.discussion_snapshot(raw, [], complete=True)}


def evidence() -> dict:
    return {'complete': True, 'pulls': [], 'inventory': {'local_branches': [], 'remote_branches': [], 'worktrees': []},
            'coverage': {'sessions': 'partial'}}


def classify(row: dict, observed: dict | None = None) -> dict:
    return queue.classify(row, observed or evidence(), trusted={'owner', 'bot'})


def test_empty_status_fields_do_not_consume_following_fields() -> None:
    row = item()
    status = 'Waiting for: Owner observation\nLast verified: \nNext action: owner/repo#7\nBlocked by: \nEvidence: retained logs'
    records = direction.waiting_records(row, status)
    assert not records[0]['last_verified']
    assert direction.next_action_references('Next action: \nEvidence: owner/repo#7') == []
    checked = direction.milestone_wait_evidence(row, 'Blocked by: \nNext action: owner/repo#7', [])
    assert not checked['requested']


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


def test_current_waits_and_authorized_status_override_prevent_false_availability() -> None:
    row = item()
    row['discussion']['comments'] = [{'id': 1, 'author': 'owner', 'body': wait.marker(wait.reviewed(evidence='Owner test required', waiting_for='Owner acceptance', ends_when='Owner records acceptance'))}]
    assert classify(row)['state'] == 'waiting'
    row['discussion']['comments'].append({'id': 2, 'author': 'foreign', 'body': wait.marker(wait.reviewed(evidence='Clear'))})
    assert classify(row)['state'] == 'waiting'
    row = item()
    row['discussion']['comments'] = [{'author': 'owner', 'created_at': '2026-10-12T00:00:00Z', 'body': '## Current Status\nState: Active; implementation landed\nNext actor: person:owner\nNext action: Verify the deployed image.\nWaiting for: None.'}]
    assert classify(row)['state'] == 'waiting'
    row['discussion']['updated_at'] = '2026-10-13T00:00:00Z'
    row['discussion']['body_recorded_at'] = '2026-08-21T00:00:00Z'
    assert classify(row)['state'] == 'waiting'
    row['discussion']['updated_at'] = '2026-08-21T00:00:00Z'
    body = row['discussion']['body']
    row['discussion']['body'] = 'Original request without managed status.'
    row['discussion']['comments'].insert(0, {'author': 'owner', 'body': body})
    assert classify(row)['state'] == 'waiting'
    row['discussion']['body'] = body
    row['discussion']['comments'].pop(0)
    row['discussion']['comments'][0]['author'] = 'foreign'
    assert classify(row)['state'] == 'available'
    row = item()
    row['discussion']['body'] += '\n## Scope\nOriginal request retained above.\n## Current Status\nState: Active; source landed\nNext actor: event:deployment\nNext action: Verify deployed export.\nWaiting for: None.'
    assert classify(row)['state'] == 'waiting'
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
    rows[0]['discussion']['body'] += '\nEvidence: https://github.com/owner/repo/pull/80'
    rows[1] = item('Verify the deployed result.', 'Active; source merged', 2, actor='event:deployment')
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
    assert [x['number'] for x in result['waiting']] == [2]
    assert result['verification_only'] == result['scope_review'] == []
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
    ambiguous = item('Evaluate remaining export scope.', actor='')
    ambiguous['queue'] = classify(ambiguous)
    review = {'state': 'available', 'reason': 'Current source scope reviewed', 'evidence': ['Current thread'],
              'ownership_complete': True, 'discussion_digest': ambiguous['discussion']['digest']}
    context = {'issues': {'owner/repo#1': review}}
    assert module.project_local_queue([ambiguous], [], context)['needs_status']
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


def test_default_global_cli_routes_recorded_actors_without_caller_review() -> None:
    root = cli.global_issue('someone/direction', 1, title='Track: First', milestone=cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z'))
    source = cli.global_issue('someone/product', 2, body='## Current Status\nState: Active\nNext actor: agent\nNext action: Implement export.\nWaiting for: None.')
    owned = cli.global_issue('someone/product', 3, body=source['body'])
    delivered = cli.global_issue('someone/product', 4, body='## Current Status\nState: Active; source merged\nNext actor: event:deployment\nNext action: Verify deployed export.\nWaiting for: None.')
    missing = cli.global_issue('someone/product', 5, body='## Current Status\nNext action: Implement export.')
    for raw in [root, source, owned, delivered, missing]:
        raw['user'] = {'login': 'someone'}
        raw['body'] = (raw.get('body') or '') + '\n' + wait.marker(wait.reviewed(evidence='Fixture request reviewed'))
    holder = {'worker': 'worker', 'session': 'native-worker', 'branch': 'work/export', 'claimed_at': '2026-10-01T00:00:00Z'}
    comments = {(owned['repo'], owned['number']): [{'id': 1, 'user': {'login': 'bot'}, 'body': claim.marker(holder)}]}
    edges = {(root['repo'], root['number']): cli.relationships(sub_issues=[source, owned, delivered, missing])}
    with cli.global_fixture([root], [source, owned, delivered, missing], edges, comments=comments) as (module, result, _reads):
        original_read = module.collect_paged_rest_items
        def read(path: str, **kwargs):
            if path.endswith('/pulls') or path.endswith('/branches'):
                return 'bot', []
            return original_read(path, **kwargs)
        with patch.multiple(module, annotate_next_queue=module.real_annotate_next_queue, collect_paged_rest_items=read), patch.multiple(module.github_identity, configured_bot_logins=lambda: ('bot',)), patch.multiple(module.pathlib.Path, is_dir=lambda *_: False):
            module.cmd_next(cli.next_args())
    assert [row['number'] for row in result['available_candidates']] == [source['number']]
    assert [row['number'] for row in result['underway']] == [owned['number']]
    assert [row['number'] for row in result['waiting']] == [delivered['number']]
    assert result['verification_only'] == result['scope_review'] == []
    assert [row['number'] for row in result['needs_status']] == [missing['number']]
    assert result['needs_status_count'] == len(result['needs_status'])
    assert result['selection_coverage']['active_outside_evaluated_graph_count'] == 0
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


def test_next_actor_routes_work_without_prose_inference() -> None:
    for action in ["Implement export.", "Verify the deployed build.", "Supervisor fixes GUI selection.", "#7 owns the source."]:
        assert classify(item(action, "Source delivery is complete"))["state"] == "available"
        assert classify(item(action, actor="agent:integrator"))["state"] == "underway"
        for actor in ["person:owner", "event:Apple approves the build"]:
            assert classify(item(action, actor=actor))["state"] == "waiting"
    for actor in ["", "someone", "agent:", "person:", "event:"]:
        assert classify(item(actor=actor))["state"] == "needs_status"
    row = item()
    row["discussion"]["body"] = row["discussion"]["body"].replace("Next actor: agent\n", "")
    assert classify(row)["state"] == "needs_status"
    row = item()
    row["discussion"]["body"] += "\nNext actor: person:owner\n"
    assert classify(row)["state"] == "needs_status"


def test_latest_claim_for_named_session_decides_released_actor_status() -> None:
    row = item(actor="agent:native-session")
    source = {"worker": "worker", "session": "native-session", "branch": "work/source", "claimed_at": "2026-10-01T00:00:00Z"}
    row["discussion"]["comments"] = [
        {"id": 1, "author": "bot", "created_at": source["claimed_at"], "body": claim.marker(source)},
        {"id": 2, "author": "foreign", "created_at": "2026-10-01T00:01:00Z", "body": "Released claim 1"},
    ]
    assert classify(row)["state"] == "underway"
    row["discussion"]["comments"][-1]["author"] = "bot"
    assert classify(row)["state"] == "needs_status"
    other = {**source, "session": "other-session", "claimed_at": "2026-10-02T00:00:00Z"}
    row["discussion"]["comments"].extend([
        {"id": 3, "author": "bot", "created_at": other["claimed_at"], "body": claim.marker(other)},
        {"id": 4, "author": "bot", "created_at": "2026-10-02T00:01:00Z", "body": "Released claim 3"},
    ])
    assert classify(row)["state"] == "needs_status"
    source["claimed_at"] = "2026-10-03T00:00:00Z"
    row["discussion"]["comments"].append(
        {"id": 5, "author": "bot", "created_at": source["claimed_at"], "body": claim.marker(source)})
    assert classify(row)["state"] == "underway"
    row["discussion"]["comments"].append(
        {"id": 6, "author": "bot", "created_at": "2026-10-03T00:01:00Z", "body": "Released claim 5"})
    assert classify(row)["state"] == "needs_status"


def test_needs_status_survives_caller_reviews_and_milestone_projection() -> None:
    row = item(actor="")
    row.update(queue=classify(row), direction_category="own_project", plan_status="active",
               overall_milestone_context={"state": "matched", "titles": ["First"]})
    row["discussion"]["ancestry_complete"] = True
    context = {"issues": {"owner/repo#1": {"state": "available", "reason": "Reviewed", "evidence": ["Thread"],
                "ownership_complete": True, "discussion_digest": row["discussion"]["digest"], "category": "own_project"}}}
    graph = {"candidates": [], "excluded": [], "dependency_context": {"complete": True}}
    result = direction.rank_portfolio_work(graph, [row], milestone_titles=[], selection_context=context)
    assert result["needs_status"] and not result["available_candidates"]
    assert result["verification_only"] == result["scope_review"] == []
    summary = direction.milestone_summary(["First"], completed=[], candidates=[], graph_waits=[],
        track_next_actions={}, needs_status=[row])
    assert summary[0]["state"] == "needs_status"
    assert summary[0]["needs_status"]
    row["milestone"] = cli.milestone_data(1, "First", created_at="2026-01-01T00:00:00Z")
    context["issues"]["owner/repo#1"]["state"] = "underway"
    capacity = direction.tooling_capacity_context(graph, [row], milestone_titles=["First"], context=context,
        repository_waypoints={"owner/repo": ["First"]}, coverage_complete=True)
    assert not capacity["admitted"]
    assert capacity["required"] == "current_next_actor"


def test_adapter_uses_body_edit_time_for_the_current_actor() -> None:
    module = cli.load_module()
    row = item()
    row["discussion"]["updated_at"] = "2026-10-29T02:00:00Z"
    row["discussion"]["comments"] = [{"id": 7, "author": "owner", "created_at": "2026-10-29T01:00:00Z",
        "body": "## Current Status\nNext actor: person:owner\nNext action: Observe the result."}]
    response = {"data": {"repository": {"issue": {"createdAt": "2026-10-01T00:00:00Z",
                                                "lastEditedAt": "2026-10-28T23:00:00Z"}}}}
    with patch.multiple(module, read_queue_ownership=lambda *_: evidence(), wait_authors=lambda *_: {"owner"},
                        api_json=Mock(return_value=("bot", response))):
        module.real_annotate_next_queue([row], cli.next_args(repo="owner/repo"))
    assert row["queue"]["state"] == "waiting"
    assert row["queue"]["next_actor"] == "person:owner"
    row["discussion"].pop("body_recorded_at")
    row.pop("queue")
    with patch.multiple(module, read_queue_ownership=lambda *_: evidence(), wait_authors=lambda *_: {"owner"},
                        api_json=Mock(return_value=("bot", {}))):
        module.real_annotate_next_queue([row], cli.next_args(repo="owner/repo"))
    assert row["queue"]["state"] == "review"
    assert not module.project_local_queue([row], [], {})["available_candidates"]
    source = {"worker": "worker", "session": "session", "branch": "work/source", "claimed_at": "2026-10-29T01:10:00Z"}
    for state, marker in [
        ("waiting", wait.marker(wait.reviewed(evidence="Owner test needed", waiting_for="Owner test", ends_when="Owner records result"))),
        ("underway", claim.marker(source)),
    ]:
        protected = item()
        protected["discussion"]["comments"] = [*row["discussion"]["comments"],
            {"id": 8, "author": "owner", "created_at": "2026-10-29T01:10:00Z", "body": marker}]
        with patch.multiple(module, read_queue_ownership=lambda *_: evidence(), wait_authors=lambda *_: {"owner"},
                            api_json=Mock(return_value=("bot", {}))):
            module.real_annotate_next_queue([protected], cli.next_args(repo="owner/repo"))
        assert protected["queue"]["state"] == state


def test_inherited_actor_waits_are_compact_and_visible_in_milestones() -> None:
    module = cli.load_module()
    for actor in ["person:owner", "event:deployment"]:
        row = item(actor=actor)
        row.update(queue=classify(row), plan_status="active")
        row["discussion"].update(ancestry_complete=True, parents=[{
            "repo": "owner/repo", "number": 9, "url": "https://github.com/owner/repo/issues/9",
            "milestone": cli.milestone_data(1, "First", created_at="2026-01-01T00:00:00Z")}])
        graph = {"candidates": [], "excluded": [], "dependency_context": {"complete": True}}
        result = direction.rank_portfolio_work(graph, [row], milestone_titles=["First"],
            repository_waypoints={"owner/repo": ["First"]})
        summary = direction.milestone_summary(["First"], completed=[], candidates=[], graph_waits=result["waiting"],
            track_next_actions={})
        assert summary[0]["state"] == "waiting"
        assert not result["excluded"]
        local = module.project_local_queue([row], [], {})
        assert not local["excluded"]
        assert local["waiting"][0]["reported_by"] == result["waiting"][0]["reported_by"] == row["url"]
        assert not result["waiting"][0]["non_external"]
        assert "discussion" not in result["waiting"][0]


def test_actor_buckets_preserve_legacy_wait_review_visibility() -> None:
    for actor in ["", "agent:integrator", "person:owner", "event:deployment"]:
        row = item(actor=actor)
        row["discussion"]["body"] = row["discussion"]["body"].split(wait.PREFIX)[0]
        row.update(queue=classify(row), plan_status="active")
        assert row["queue"]["wait_review_required"]
        row["discussion"]["ancestry_complete"] = True
        row["milestone"] = cli.milestone_data(1, "First", created_at="2026-01-01T00:00:00Z")
        graph = {"candidates": [], "excluded": [], "dependency_context": {"complete": True}}
        result = direction.rank_portfolio_work(graph, [row], milestone_titles=["First"],
            repository_waypoints={"owner/repo": ["First"]})
        summary = direction.milestone_summary(["First"], completed=[], candidates=[], graph_waits=[*result["unowned"], *result["waiting"]],
            underway=result["underway"], needs_status=result["needs_status"], track_next_actions={})
        assert summary[0]["wait_reviews"]


def main() -> None:
    for name, test in sorted(globals().items()):
        if name.startswith('test_'):
            test()
    print('next queue tests passed')


if __name__ == '__main__':
    main()
