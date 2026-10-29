# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

from unittest.mock import Mock, patch

import github_client
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



def test_pr_artifacts_cannot_hide_the_recorded_actor_or_missing_owner() -> None:
    for actor, expected in [("", "needs_status"), ("agent", "available"),
                            ("agent:integrator", "underway"), ("person:owner", "waiting")]:
        row = item(actor=actor)
        row["record_type"] = "pull_request"
        observed = evidence()
        pull = {"number": row["number"], "state": "open", "title": row["title"], "body": row["discussion"]["body"],
                "html_url": row["url"], "head": {"sha": "a" * 40, "ref": "work/old-source", "repo": {"full_name": row["repo"]}},
                "base": {"repo": {"full_name": row["repo"]}}}
        observed["pulls"] = [pull]
        observed["inventory"]["local_branches"] = [pull["head"]["ref"]]
        observed["inventory"]["worktrees"] = [{"branch": pull["head"]["ref"], "path": "/workspace/old-source"}]
        result = classify(row, observed)
        assert result["state"] == expected
        assert result["next_actor"] == (actor or None)
        assert result["claim_required"] and not result["ownership_complete"]
        source = {"worker": "worker", "session": "native-session", "branch": "work/old-source", "claimed_at": "2026-10-01T00:00:00Z"}
        row["discussion"]["comments"] = [{"id": 1, "author": "bot", "created_at": source["claimed_at"],
                                          "body": claim.marker(source)}]
        assert classify(row, observed)["state"] == ("waiting" if actor == "person:owner" else "underway")


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
    row['direction_category'] = None
    result = direction.rank_portfolio_work(graph, [row], milestone_titles=[])
    assert not result['available_candidates']


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
    result = module.project_local_queue(rows, [])
    assert [x['number'] for x in result['available_candidates']] == [1, 4]
    assert [x['number'] for x in result['waiting']] == [2]
    assert result['verification_only'] == result['scope_review'] == []
    assert [x['number'] for x in result['underway']] == [3]
    assert result['candidate_count'] == result['available_candidate_count'] == 2
    assert ctx['claim_required'] and not ctx['all_active_sessions_searched']
    assert len(observed_paths) == 2  # one ownership inventory per repo
    counts = queue.coverage(rows, rows[:2], result)
    assert counts['active_inventory_count'] == 4 and counts['active_unevaluated_count'] == 2
    ambiguous = item('Evaluate remaining export scope.', actor='')
    ambiguous['queue'] = classify(ambiguous)
    assert module.project_local_queue([ambiguous], [])['needs_status']
    assert not module.project_local_queue([ambiguous], [])['available_candidates']


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
        if path.endswith('/comments'):
            return 'bot', []
        assert path == '/repos/someone/product/issues'
        if kwargs['query'].get('labels'):
            return 'bot', []
        return 'bot', raw[:kwargs.get('limit')]
    raw[-1]["updated_at"] = "2026-08-22T00:00:00Z"
    args = cli.next_args()
    with patch.multiple(module, collect_paged_rest_items=read, load_direction=lambda *_: cli.DIRECTION), patch.multiple(module.github_identity, github_app_config=lambda: {}):
        complete, context = module.real_discover_direction_work('someone/direction', args)
        args.repository_issue_limit = 1
        limited, partial = module.real_discover_direction_work('someone/direction', args)
    assert len(complete) == len(raw) and context['complete']
    assert len(limited) < len(complete) and not partial['complete']
    assert limited[0]['number'] == raw[-1]['number']
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
    graph = {"candidates": [], "excluded": [], "dependency_context": {"complete": True}}
    result = direction.rank_portfolio_work(graph, [row], milestone_titles=[])
    assert result["needs_status"] and not result["available_candidates"]
    assert result["verification_only"] == result["scope_review"] == []
    summary = direction.milestone_summary(["First"], completed=[], candidates=[], graph_waits=[],
        track_next_actions={}, needs_status=[row])
    assert summary[0]["state"] == "needs_status"
    assert summary[0]["needs_status"]


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
    assert not module.project_local_queue([row], [])["available_candidates"]
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
        local = module.project_local_queue([row], [])
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


def test_milestone_availability_flag_requires_complete_current_evidence() -> None:
    row = item()
    row.update(queue=classify(row), plan_status='active', via=[{'repo': 'owner/direction', 'number': 1}],
               milestone=cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z'))
    row['discussion']['ancestry_complete'] = True
    graph = {'candidates': [row], 'excluded': [], 'dependency_context': {'complete': True}}
    flag = 'all_listed_milestones_have_no_available_item'

    def rank(complete: bool = True) -> dict:
        return direction.rank_portfolio_work(graph, [], milestone_titles=['First'], coverage_complete=complete)

    assert rank()[flag] is False
    assert rank(False)[flag] is False
    for state, expected in [('underway', True), ('waiting', True), ('needs_status', None), ('review', None)]:
        row['queue']['state'] = state
        row['queue']['reason'] = 'recorded_next_actor'
        result = rank()
        assert result[flag] is expected, (state, result)
        assert not result['available_candidates']
        assert rank(False)[flag] is None
    row['queue']['state'] = 'underway'
    row['wait_evidence_complete'] = False
    assert rank()[flag] is None
    row.pop('wait_evidence_complete')
    row['discussion']['complete'] = False
    assert rank()[flag] is None
    row['discussion']['complete'] = True
    row['discussion']['ancestry_complete'] = False
    assert rank()[flag] is None
    row['discussion']['ancestry_complete'] = True
    graph['dependency_context']['complete'] = False
    assert rank()[flag] is None
    graph['dependency_context']['complete'] = True
    graph['candidates'] = []
    graph['excluded'] = [row]
    raw = cli.global_issue('owner/repo', row['number'], labels=[cli.load_module().DEFAULT_CONFIG['labels']['blocked']],
                           milestone=row['milestone'])
    _, excluded = direction.evaluate_next_plan(raw, config=cli.load_module().DEFAULT_CONFIG,
                                               focus=None, relationships=cli.relationships())
    excluded.update(discussion=row['discussion'], via=row['via'])
    graph['excluded'] = [excluded]
    assert rank()[flag] is None
    graph['excluded'] = []
    assert rank()[flag] is True


def test_global_flag_precedes_family_and_display_filters() -> None:
    for family, other in [('codex', 'claude'), ('claude', 'codex')]:
        root = cli.track('someone/direction', 1, 'First')
        leaf = cli.global_issue('someone/business', 2, labels=[f'agent:{other}'],
                                body='## Current Status\nNext actor: agent\nNext action: Implement the next item.')
        edges = {(root['repo'], root['number']): cli.relationships(sub_issues=[leaf])}
        with cli.global_fixture([root], [leaf], edges) as (module, result, _reads):
            args = cli.next_args()
            args.agent = family
            args.limit = 1
            def annotate(rows: list[dict], _args: object) -> dict:
                for row in rows:
                    row['queue'] = {'state': 'available'}
                return {'repositories': {}}
            module.annotate_next_queue = annotate
            module.cmd_next(args)
        assert not result['available_candidates']
        assert result['all_listed_milestones_have_no_available_item'] is False


def test_global_flag_settles_unrelated_exclusions_but_not_unread_milestone_waits() -> None:
    root = cli.track('someone/direction', 1, 'First')
    leaf = cli.global_issue('someone/business', 2, waiting_for='Client tests the candidate')
    blocked = cli.global_issue('someone/tools', 3)
    blocker = cli.global_issue('someone/tools', 4)
    parked = cli.global_issue('someone/tools', 5, waiting_for='Director tests hardware')
    edges = {(root['repo'], root['number']): cli.relationships(sub_issues=[leaf]),
             (blocked['repo'], blocked['number']): cli.relationships(blocked_by=[blocker]),
             (blocker['repo'], blocker['number']): cli.relationships(blocking=[blocked])}
    flag = 'all_listed_milestones_have_no_available_item'
    with cli.global_fixture([root], [leaf, blocked, blocker, parked], edges,
                            discovered=[blocked, blocker, parked]) as (module, result, _reads):
        module.load_direction = lambda *_: '# Direction\n## Milestones\n- `First`\n'
        module.cmd_next(cli.next_args())
        assert result[flag] is True, result
        original = module.next_wait_context

        def unread(*args: object, **kwargs: object) -> dict:
            report = original(*args, **kwargs)
            report['unavailable'].append({'repo': leaf['repo'], 'number': leaf['number']})
            return report

        module.next_wait_context = unread
        module.cmd_next(cli.next_args())
        assert result[flag] is None


def test_milestone_flag_follows_open_dependencies_without_assuming_their_status() -> None:
    module = cli.load_module()
    flag = 'all_listed_milestones_have_no_available_item'
    for relation in ['blocked_by', 'sub_issues']:
        for state in ['available', 'own_project', 'underway', 'needs_status', 'waiting', 'missing', 'cycle', 'transitive', 'closed_seed', 'done_seed', 'shared']:
            raws = [cli.global_issue('owner/repo', number,
                body='## Current Status\nNext actor: agent\nNext action: Complete the item.') for number in [1, 2, 3]]
            raws[0]['milestone'] = cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z')
            if state == 'closed_seed':
                raws[0]['state'] = 'closed'
            if state == 'done_seed':
                raws[0]['labels'] = [{'name': 'plan:done'}]
            if state == 'shared':
                raws[1]['milestone'] = cli.milestone_data(2, 'Second', created_at='2026-01-01T00:00:00Z')
            rows = []
            for index, raw in enumerate(raws):
                raw['user'] = {'login': 'bot'}
                links = cli.relationships(**{relation: [raws[1]]}) if index == 0 else cli.relationships()
                if index == 1 and relation == 'blocked_by':
                    links = cli.relationships(blocking=[raws[0]])
                if index == 1 and state in ['cycle', 'transitive']:
                    links = cli.relationships(**{relation: [raws[0] if state == 'cycle' else raws[2]]})
                actor = 'agent:worker' if state == 'underway' else '' if state == 'needs_status' else 'agent'
                if index == 1:
                    raw['body'] = f'## Current Status\nNext actor: {actor}\nNext action: Complete the item.'
                record = wait.reviewed(evidence='Fixture request reviewed')
                if index == 1 and state == 'waiting':
                    record = wait.reviewed(evidence='Director requested acceptance',
                                           waiting_for='Director tests the candidate', ends_when='Director records the result')
                row = direction.evaluate_direction_node(raw, config=module.DEFAULT_CONFIG, focus=None,
                    relationships=links, structured_wait=record)['item']
                row['discussion'] = direction.discussion_snapshot(raw, [
                    {'user': {'login': 'bot'}, 'body': wait.marker(record)}], complete=True)
                row['discussion']['ancestry_complete'] = True
                row['queue'] = classify(row)
                row['wait_evidence_complete'] = True
                if index == 1 and state == 'own_project':
                    row['direction_category'] = 'own_project'
                rows.append(row)
            if state == 'missing':
                rows = rows[:1]
            graph = {'candidates': [], 'excluded': [], 'dependency_context': {'complete': True}}
            if state in ['closed_seed', 'done_seed']:
                rows[0]['via'] = [{'repo': 'owner/direction', 'number': 9}]
                rows[1]['discussion']['parents'] = [rows[0]]
                graph['excluded'] = [rows[0]]
                graph['tracking_milestones'] = [{key: rows[0][key] for key in ['repo', 'number', 'milestone']}]
                rows = rows[1:]
            result = direction.rank_portfolio_work(graph, rows, milestone_titles=['First', 'Second'],
                repository_waypoints={'owner/repo': ['First', 'Second']}, coverage_complete=True)
            if state in ['closed_seed', 'done_seed']:
                assert result[flag] is True, (relation, state, result)
                assert all(row.get('category') != 'milestone' for row in result['candidates'])
            elif state == 'shared':
                blocker = next(row for row in result['available_candidates'] if row['number'] == raws[1]['number'])
                assert blocker['overall_milestone_context']['titles'] == [raw['milestone']['title'] for raw in raws[:2]]
                assert blocker['overall_milestone_context']['basis'] == 'exact_listed_title_match'
                assert blocker['milestone'] == rows[1]['milestone']
            elif state == 'own_project':
                assert result[flag] is False, (relation, state, result)
            elif state in ['underway', 'waiting']:
                assert result[flag] is True, (relation, state, result)
            elif state in ['available', 'transitive']:
                assert result[flag] is False, (relation, state, result)
                blocker = next(row for row in result['candidates'] if row['number'] == raws[1 if state == 'available' else 2]['number'])
                assert blocker['overall_milestone_context']['titles'] == [raws[0]['milestone']['title']]
                assert blocker['category'] == 'milestone'
                assert blocker['milestone'] is None
            else:
                assert result[flag] is None, (relation, state, result)


def test_discovered_prerequisites_rank_with_listed_or_client_milestone_through_the_cli() -> None:
    for seed in ['listed', 'client']:
        for relation in ['blocked_by', 'sub_issues']:
            rows = [cli.global_issue('someone/product', number,
                body='## Current Status\nNext actor: agent\nNext action: Complete the item.') for number in [1, 2, 3, 4]]
            for raw in rows:
                raw['user'] = {'login': 'bot'}
            rows[0]['milestone'] = cli.milestone_data(1, 'First', created_at='2026-01-01T00:00:00Z')
            rows[3]['milestone'] = cli.milestone_data(2, 'Second', created_at='2026-01-01T00:00:00Z')
            if seed == 'client':
                rows[0].update(milestone=None, user={'login': 'client'})
            edges = {(rows[index]['repo'], rows[index]['number']): cli.relationships(**{relation: [rows[index + 1]]})
                     for index in [0, 1]}
            roots = [cli.track('someone/direction', 90 + index, title) for index, title in enumerate(['First', 'Second'])]
            comments: dict[tuple[str, int], list[dict]] = {}
            with cli.global_fixture(roots, rows, edges, discovered=list(reversed(rows)), clear_waits=rows,
                                    comments=comments) as (module, result, _reads):
                module.load_direction = lambda *_: '# Direction\n## Milestones\n- `First`\n- `Second`\n'
                module.discover_direction_work = lambda *_args, **_kwargs: (list(reversed(rows)), {
                    'complete': True, 'repositories': [{'repo': rows[0]['repo'], 'direction': module.load_direction()}]})

                def annotate(items: list[dict], _args: object) -> dict:
                    for row in items:
                        row['queue'] = queue.classify(row, evidence(), trusted={'someone', 'client', 'bot'})
                    return {'repositories': {}}

                module.annotate_next_queue = annotate
                with patch.object(github_client, 'recorded_client', return_value={
                    'status': 'recorded', 'source': 'repo_people_overlay', 'login': 'client'}):
                    module.cmd_next(cli.next_args())
                available = result['available_candidates']
                assert [row['number'] for row in available] == [rows[2]['number'], rows[3]['number']], (seed, relation, result)
                assert available[0]['category'] == 'milestone'
                assert available[0]['overall_milestone_context']['titles'] == ['First']
                assert available[0]['milestone'] is None
                assert result['all_listed_milestones_have_no_available_item'] is False
                held = rows[1 if relation == 'sub_issues' else 2]
                comments[(held['repo'], held['number'])].append({'user': {'login': 'someone'},
                    'body': wait.marker(wait.reviewed(evidence='Director holds the whole plan before any preparation',
                        waiting_for='Director tests the candidate', ends_when='Director records the result'))})
                with patch.object(github_client, 'recorded_client', return_value={
                    'status': 'recorded', 'source': 'repo_people_overlay', 'login': 'client'}):
                    module.cmd_next(cli.next_args())
                assert [row['number'] for row in result['available_candidates']] == [rows[3]['number']]
                assert any(row['number'] == held['number'] and row['overall_milestone_context']['titles'] == ['First']
                           for row in result['waiting'])
                if relation == 'sub_issues':
                    assert any(row['number'] == rows[2]['number'] and row['exclusion'] == 'parent_waiting'
                               for row in result['excluded'])


if __name__ == '__main__':
    main()
