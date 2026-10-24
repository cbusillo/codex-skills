#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Hermetic evidence for Supervisor enqueue guards and mutation ordering."""

import argparse
import base64
import copy
import io as stdlib_io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import auto_enqueue as watch
from skills.github.scripts.ready_handoff import READY, WAITING
from skills.github.scripts import github_read, ready_handoff

REPO = "example/tool"
CLIENT = "example/client-product"
SHA = "a" * 40
TARGET = {
    "repo": REPO,
    "base": "trunk",
    "label": "enqueue-from-policy",
    "policy": {"record_id": "revision"},
    "scheduler_drives_train": True,
}


def pr(number=1):
    return {
        "number": number,
        "head": {"sha": SHA},
        "base": {"ref": "trunk"},
        "draft": False,
        "state": "open",
        "mergeable": True,
        "user": {"login": "engineer"},
        "labels": [],
        "body": "",
        "created_at": f"2026-01-01T00:00:{number:02d}Z",
        "changed_files": 1,
    }


def comment(body=None, number=1, login="engineer"):
    return {
        "id": number,
        "user": {"login": login},
        "created_at": f"2026-01-01T00:01:{number:02d}Z",
        "body": body
        if body is not None
        else f"{READY}\n\nHead: {SHA}. Verified.",
    }


def response(pulls, own=(), **result) -> dict[str, Any]:
    return {
        "status": "accepted",
        "result": {
            "mode": "dry-run",
            "controller_action": "idle",
            "dry_run_result": {
                "queue_order": list(own),
                "queue": [
                    {
                        "number": p["number"],
                        "head_sha": p["head"]["sha"],
                        "actor_role": "trusted_automation",
                        "mergeable": "mergeable",
                        "required_checks_status": "pass",
                        "eligible": p["number"] in own
                        or TARGET["label"] in {label["name"] for label in p["labels"]},
                        "branch_update_required": False,
                        "ineligible_reasons": []
                        if p["number"] in own
                        or TARGET["label"] in {label["name"] for label in p["labels"]}
                        else [f"missing {TARGET['label']} label"],
                    }
                    for p in pulls
                ],
            },
            **result,
        },
    }


def candidate_response(
    own, *, status="ready_for_checks", action="observe_candidate", records=None, queue=None
):
    result: dict[str, Any] = {
        "mode": "dry-run",
        "controller_action": action,
        "candidate": {
            "status": status,
            "base_sha": SHA,
            "candidate_ref": "launchplane/train/example/batch",
            "candidate_sha": "c" * 40,
            "entries": [
                {"pull_request_number": number, "head_sha": SHA} for number in own
            ],
        },
    }
    if queue is not None:
        result["dry_run_result"] = queue
    return watch.operator.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": REPO},
        provider_payload={
            "status": "accepted",
            "trace_id": "launchplane_req_candidate",
            "records": records
            if records is not None
            else {"merge_train_batch_candidate_record_id": "active"},
            "result": result,
        },
    )


def setUpModule():
    states = ready_handoff.acceptance_reader()
    unmapped = patch.object(ready_handoff, "client_release", return_value=states.ClientRelease(states.NO_CLIENT))
    unmapped.start()
    unittest.addModuleCleanup(unmapped.stop)


class Fake:
    def __init__(self, count=6):
        self.pulls = {number: pr(number) for number in range(1, count + 1)}
        self.writes = []
        self.qualify = True
        self.review_gaps = {}
        self.active = False
        self.calls = 0
        self.discovered = []
        self.before = lambda _self: None
        self.targets = [copy.deepcopy(TARGET)]
        self.after_label_action = "idle"
        self.ineligible = {}
        self.scheduler_owns_batch = False
        self.candidate_status = "ready_for_checks"

    def discover(self, repos):
        self.discovered = repos

    def open_pulls(self, _repo):
        return copy.deepcopy(list(self.pulls.values()))

    def policies(self, _owner):
        return copy.deepcopy(self.targets)

    def train(self, _target):
        self.calls += 1
        self.before(self)
        value = response(
            self.pulls.values(),
            self.writes,
            **(
                {
                    "controller_action": "observe_candidate",
                    "merge_train_batch_candidate_record_id": "active",
                }
                if self.active
                else {
                    "controller_action": self.after_label_action
                    if self.writes
                    else "idle"
                }
            ),
        )

        dry = value["result"]["dry_run_result"]
        for row in dry["queue"]:
            if row["number"] in self.ineligible:
                row["ineligible_reasons"] = self.ineligible[row["number"]]
        dry["queue_order"] = [
            row["number"] for row in dry["queue"] if not row["ineligible_reasons"]
        ]
        if self.scheduler_owns_batch and self.writes:
            # A normal active-candidate observation omits dry_run_result.
            value = candidate_response(self.writes, status=self.candidate_status)
        return value

    def get(self, path):
        return copy.deepcopy(self.pulls[int(path.rsplit("/", 1)[-1])])

    def get_many(self, paths):
        return [self.get(path) for path in paths]

    def pages(self, path, **_params):
        if path.endswith('/comments'):
            return [comment()]
        if path.endswith("/labels"):
            return self.pulls[int(path.split("/")[-2])]["labels"]
        if path.endswith("/files"):
            number = int(path.split("/")[-2])
            return [{"filename": "src/code.py" if number != 3 else "docs/guide.md"}]
        return copy.deepcopy(list(self.pulls.values()))

    def qualified(self, *_args):
        return self.qualify

    def review_gap(self, _target, pull, _trusted, *, file_inventory=None):
        return self.review_gaps.get(pull["number"], "")

    def queue(self, _target):
        dry = response(self.pulls.values(), self.writes)["result"]["dry_run_result"]
        for row in dry["queue"]:
            if row["number"] in self.ineligible:
                row["ineligible_reasons"] = self.ineligible[row["number"]]
        dry["queue_order"] = [row["number"] for row in dry["queue"] if not row["ineligible_reasons"]]
        return dry

    def label(self, _target, number):
        self.writes.append(number)
        self.pulls[number]["labels"].append({"name": TARGET["label"]})


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "holds.json"
        self.holds = {
            "paused_repositories": [],
            "coordinated_repositories": [],
            "client_releases": [],
        }
        self.save()
        self.args = argparse.Namespace(
            owner="example",
            repo=[],
            hold_file=self.path,
            handoff_login=[],
            max_batch_size=4,
            apply=True,
        )
        self.events = []

    def save(self):
        self.path.write_text(json.dumps(self.holds))

    def run_pass(self, fake):
        watch.run_pass(
            fake, self.args, lambda event, payload: self.events.append((event, payload))
        )

    def staging_fake(self):
        fake = Fake()
        fake.pulls[99] = pr(99)
        fake.pulls[99]["labels"] = [{"name": TARGET["label"]}]
        fake.observation = candidate_response([99])

        def train(_target):
            fake.calls += 1
            fake.before(fake)
            return copy.deepcopy(fake.observation)

        fake.train = train
        return fake

    def test_ready_handoffs_are_named_when_no_enqueue_is_possible(self):
        for condition in ('held', 'busy', 'ineligible', 'batch_limit', 'dry_run'):
            with self.subTest(condition=condition):
                self.events = []
                self.holds['paused_repositories'] = [REPO] if condition == 'held' else []
                self.save()
                self.args.apply = condition != 'dry_run'
                fake = Fake()
                if condition == 'busy':
                    fake.train = lambda _: {
                        'status': 'accepted', 'result': {
                            'mode': 'dry-run', 'controller_action': 'resume_reconciliation',
                            'dry_run_result': {'queue': [], 'queue_order': []},
                        },
                    }
                if condition == 'ineligible':
                    fake.ineligible = {n: ['current-head required checks are fail']
                                       for n in fake.pulls}
                self.run_pass(fake)
                named = {payload['pr'] for event, payload in self.events
                         if event in {'skipped', 'enqueued'}}
                self.assertEqual(named, set(fake.pulls))
                skipped = [p for event, p in self.events if event == 'skipped']
                self.assertTrue(all(p.get('reason') for p in skipped))
                if condition == 'ineligible':
                    self.assertTrue(all(p.get('ineligible_reasons') == fake.ineligible[p['pr']]
                                        for p in skipped))
                if condition in {'held', 'busy', 'ineligible', 'dry_run'}:
                    self.assertEqual(fake.writes, [])

    def test_skip_inventory_respects_current_trusted_handoffs(self):
        fake = Fake(count=3)
        self.holds['paused_repositories'] = [REPO]
        self.save()
        pages = fake.pages
        def comments(path, **params):
            if path.endswith('/comments'):
                number = int(path.split('/')[-2])
                if number == 1:
                    return [comment(login='untrusted')]
                if number == 2:
                    return [comment(), comment(body=WAITING, number=2)]
                return [comment(body=f"{READY}\n\nHead: {'b' * 40}.")]
            return pages(path, **params)
        fake.pages = comments
        self.run_pass(fake)
        self.assertFalse(any(event == 'skipped' for event, _ in self.events))
        self.assertEqual(fake.writes, [])

    def test_unavailable_skip_inventory_does_not_hold_lock_or_stop_other_trains(self):
        fake = Fake(count=1)
        fake.targets.append({**TARGET, 'repo': 'example/other'})
        self.holds['paused_repositories'] = [REPO]
        self.save()
        pages = fake.pages
        acquired = []
        def read(path, **params):
            if path.startswith(f'/repos/{REPO}/'):
                with watch.hold_lock(self.path):
                    acquired.append(True)
                raise github_read.GitHubReadShapeError('invalid observation')
            return pages(path, **params)
        fake.pages = read
        self.run_pass(fake)
        self.assertTrue(acquired)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == 'skip_inventory_unavailable' for event, _ in self.events))

    def test_skip_inventory_classifies_queue_members_for_every_outer_reason(self):
        for reason in ('repository_held', 'controller_busy', 'queue_read_unavailable',
                       'existing_queue', 'controller_ineligible'):
            with self.subTest(reason=reason):
                fake = Fake(count=4)
                fake.pulls[4]['labels'] = [{'name': TARGET['label']}]
                queue = fake.train(TARGET)['result']['dry_run_result']['queue'][:2]
                queue[0]['ineligible_reasons'] = []
                queue[1]['ineligible_reasons'] = [watch.DEPENDENCY_REVIEW]
                self.events = []
                self.args.handoff_login = ['engineer']
                watch.report_ready_skips(
                    fake, TARGET, self.args,
                    lambda event, payload: self.events.append((event, payload)),
                    reason, pulls=list(fake.pulls.values()), queue=queue,
                )
                named = {p['pr']: p for event, p in self.events if event == 'skipped'}
                self.assertEqual(set(named), {3})
                self.assertEqual(named[3]['controller_admission'], 'unknown')

    def test_deferred_inventory_never_delays_or_replaces_a_stop(self):
        for error in (watch.WatchError('enqueue write uncertain'), PermissionError(),
                      TimeoutError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch.object(watch, 'report_ready_skips') as report:
                    with self.assertRaises(type(error)) as raised:
                        with watch.deferred_skip_reports(Fake(), TARGET, self.args, Mock()) as schedule:
                            schedule('controller_busy')
                            raise error
                    self.assertIs(raised.exception, error)
                    report.assert_not_called()

    def test_bad_pr_inventory_does_not_hide_later_ready_handoffs(self):
        fake = Fake(count=2)
        pages = fake.pages
        def read(path, **params):
            if path.endswith('/1/comments'):
                raise github_read.GitHubReadShapeError('unavailable')
            return pages(path, **params)
        fake.pages = read
        watch.report_ready_skips(
            fake, TARGET, self.args,
            lambda event, payload: self.events.append((event, payload)), 'controller_busy',
        )
        self.assertEqual([p['pr'] for event, p in self.events if event == 'skipped'], [2])
        self.assertTrue(any(event == 'skip_inventory_unavailable' for event, _ in self.events))

    def test_inventory_stops_after_a_repository_identity_failure(self):
        fake = Fake(count=2)
        pages = fake.pages
        reads = []
        def read(path, **params):
            if path.endswith('/comments'):
                reads.append(path)
                raise github_read.GitHubReadError(
                    'identity mismatch', result=Mock(failure=Mock(retryable=False)), diagnostics={},
                )
            return pages(path, **params)
        fake.pages = read
        watch.report_ready_skips(fake, TARGET, self.args, Mock(), 'controller_busy')
        self.assertEqual(len(reads), 1)

    def test_staging_qualifies_next_batch_without_changing_published_candidate(self):
        fake = self.staging_fake()
        candidate = copy.deepcopy(fake.observation)
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertEqual(fake.observation, candidate)
        self.assertEqual(fake.pulls[99]["labels"], [{"name": TARGET["label"]}])
        self.assertFalse(any(event == "driver_handoff" for event, _ in self.events))
        self.assertTrue(any(event == "next_batch_queued" for event, _ in self.events))
        for _ in range(3):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_staging_preserves_existing_queue_and_unknown_label_author(self):
        for change in ("queued", "unknown_author", "missing"):
            with self.subTest(change=change):
                fake = self.staging_fake()
                fake.pulls[6]["labels"] = [{"name": TARGET["label"]}]
                if change == "unknown_author":
                    fake.ineligible[6] = [
                        f"{TARGET['label']} label ignored: could not read who applied it"
                    ]
                if change == "missing":
                    original = fake.queue

                    def queue(target):
                        dry = original(target)
                        dry["queue"] = [row for row in dry["queue"] if row["number"] != 6]
                        dry["queue_order"] = [number for number in dry["queue_order"] if number != 6]
                        return dry

                    fake.queue = queue
                self.run_pass(fake)
                self.assertEqual(fake.writes, [])

    def test_staging_stops_on_new_or_unknown_queue_labels_between_writes(self):
        for change in ("queued", "unknown_author", "missing"):
            with self.subTest(change=change):
                fake = self.staging_fake()

                def before(io):
                    if not io.writes:
                        return
                    io.pulls[6]["labels"] = [{"name": TARGET["label"]}]
                    if change == "unknown_author":
                        io.ineligible[6] = [
                            f"{TARGET['label']} label ignored: could not read who applied it"
                        ]
                    elif change == "missing":
                        io.ineligible[6] = ["targets another base"]

                fake.before = before
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1])

    def test_staging_rechecks_author_policy_head_and_readiness_before_each_label(self):
        for change in ("author", "head", "readiness", "mergeability"):
            with self.subTest(change=change):
                fake = self.staging_fake()

                def before(io):
                    if not io.writes:
                        return
                    if change == "author":
                        io.ineligible[2] = ["actor role is not allowed to enqueue"]
                    elif change == "head":
                        io.pulls[2]["head"]["sha"] = "d" * 40
                    elif change == "readiness":
                        io.qualify = False
                    else:
                        io.pulls[2]["mergeable"] = None

                fake.before = before
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1])

    def test_staging_stops_when_candidate_lands_changes_fails_or_release_is_held(self):
        for change in ("landed", "replacement", "failed", "release", "reconcile"):
            with self.subTest(change=change):
                fake = self.staging_fake()

                def before(io):
                    if not io.writes:
                        return
                    if change == "landed":
                        io.observation = response(io.pulls.values(), io.writes)
                    elif change == "replacement":
                        io.observation = candidate_response([98])
                    elif change == "failed":
                        io.observation = candidate_response([99], status="failed")
                    elif change == "release":
                        io.observation["result"]["controller_action"] = "release_hold"
                    else:
                        io.observation["result"]["controller_reconciliation_status"] = "required"

                fake.before = before
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1])

    def test_staging_preserves_blocked_prs_and_requires_complete_candidate_evidence(self):
        fake = self.staging_fake()
        fake.pulls[1]["labels"] = [{"name": "merge-blocked"}]
        fake.ineligible[1] = ["held by merge-blocked label"]
        self.run_pass(fake)
        self.assertNotIn(1, fake.writes)
        self.assertEqual(fake.pulls[1]["labels"], [{"name": "merge-blocked"}])
        for field in ("base_sha", "candidate_sha", "candidate_ref", "pull_request_numbers"):
            with self.subTest(field=field):
                fake = self.staging_fake()
                fake.observation["result"]["candidate"].pop(field)
                self.run_pass(fake)
                self.assertEqual(fake.writes, [])

    def test_staging_rechecks_policy_after_queue_read(self):
        fake = self.staging_fake()
        original = fake.queue
        reads = []

        def queue(target):
            reads.append(target)
            if len(reads) > 1:
                fake.targets[0]["policy"] = {"record_id": "new-policy"}
            return original(target)

        fake.queue = queue
        with self.assertRaises(watch.WatchError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_staging_requires_a_live_mutating_scheduler(self):
        for value in (False, None):
            with self.subTest(value=value):
                fake = self.staging_fake()
                fake.targets[0]["scheduler_drives_train"] = value
                self.run_pass(fake)
                self.assertEqual(fake.writes, [])

    def test_staging_unavailable_queue_or_another_enrolled_base_never_writes(self):
        fake = self.staging_fake()
        fake.queue = Mock(side_effect=watch.LaunchplaneReadError("queue unavailable"))
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        fake = self.staging_fake()
        fake.targets.append({**TARGET, "base": "release"})
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_staging_read_failure_before_writes_does_not_starve_other_repositories(self):
        fake = self.staging_fake()
        other = {**TARGET, "repo": "example/other"}
        fake.targets.append(other)
        visited = []
        original = fake.train

        def train(target):
            visited.append(target["repo"])
            if target == other:
                return response([pr()])
            return original(target)

        fake.train = train
        fake.queue = Mock(side_effect=watch.LaunchplaneReadError("queue unavailable"))
        self.run_pass(fake)
        self.assertIn(other["repo"], visited)

    def test_staging_read_failure_after_a_label_stops_without_replay(self):
        fake = self.staging_fake()
        original = fake.queue

        def queue(target):
            if fake.writes:
                raise watch.LaunchplaneReadError("queue unavailable")
            return original(target)

        fake.queue = queue
        with self.assertRaises(watch.LaunchplaneReadError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_capped_area_group_and_repeat_does_not_extend_queued_batch(self):
        fake = Fake()
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        handoff = next(
            payload for event, payload in self.events if event == "driver_handoff"
        )
        self.assertEqual(handoff["prs"], fake.writes)
        self.assertIn("launchplane-train-drive.py", handoff["argv"][2])

    def test_batch_read_reaches_candidate_sorting_and_final_rest_recheck(self):
        from skills.github.scripts import github_pr_batch, github_read

        fixture_path = (
            Path(__file__).parents[2] / "github/scripts/fixtures/pr_batch.json"
        )
        recorded = json.loads(fixture_path.read_text())
        node = copy.deepcopy(next(iter(recorded["data"]["repository"].values())))
        source = pr()
        node.update(
            number=1,
            headRefOid=SHA,
            headRefName="work/test",
            baseRefName="trunk",
            state="OPEN",
            isDraft=False,
            merged=False,
            mergedAt=None,
            mergeable="MERGEABLE",
            mergeStateStatus="CLEAN",
            createdAt=source["created_at"],
            changedFiles=1,
            author={"__typename": "User", "login": "engineer"},
            headRepository={"nameWithOwner": REPO},
            baseRepository={"nameWithOwner": REPO},
        )
        node["commits"]["nodes"][0]["commit"]["oid"] = SHA
        node["labels"] = {
            "totalCount": 0,
            "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
        document = {"data": {"repository": {"p1": node}, "rateLimit": {"cost": 1}}}

        class BatchedFake(Fake):
            def __init__(self, count=1):
                super().__init__(count=count)
                self.reader = None
                self.final_rest_recheck = False

            def batch(self, repo, numbers):
                self.reader = github_read.GitHubReader(
                    gh_cmd="fake-gh",
                    actor="fixture-reader",
                    expected_actor="fixture-reader",
                    operation="github.pr.view",
                )
                reply = watch.github_api.ApiResult(ok=True, status=200, body=document)
                with patch.object(self.reader, "graphql_json", return_value=reply):
                    github_pr_batch.batch_pull_requests(self.reader, repo, numbers)
                return self.reader.batch_diagnostic

            def get(self, path):
                if self.reader is not None and path in self.reader.batch_responses:
                    return self.reader.get_json(path, step="batch_metadata")
                self.final_rest_recheck = True
                return super().get(path)

        fake = BatchedFake()
        self.args.batch_read = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(fake.final_rest_recheck)
        note = next(payload for event, payload in self.events if event == "batch_read")
        self.assertTrue(note["used"])
        self.assertEqual(note["cost"], document["data"]["rateLimit"]["cost"])

    def test_real_qualification_consumes_batch_then_rechecks_rest(self):
        from skills.github.scripts import github_pr_batch, github_read
        root = Path(__file__).parents[2] / 'github/scripts/fixtures'
        document = json.loads((root / 'pr_batch.json').read_text())
        first = next(iter(document['data']['repository'].values()))
        # A current-head handoff is scenario input; the recordings' old ready
        # comments correctly fail after the head changes during train landing.
        ready_comment = {'databaseId':1,'body':f"{READY}\n\nHead: {first['headRefOid']}",
                         'createdAt':first['updatedAt'],'updatedAt':first['updatedAt'],
                         'url':'https://github.com/owner/repo/pull/1#issuecomment-1',
                         'author':{'__typename':'Bot','login':'fixture-bot'},
                         'authorAssociation':'CONTRIBUTOR'}
        first['comments']={'totalCount':1,'nodes':[ready_comment],
                           'pageInfo':{'hasNextPage':False,'endCursor':None}}
        checks = json.loads((root / 'pr_batch_rest_checks.json').read_text())
        comments = json.loads((root / 'pr_batch_rest_comments.json').read_text())
        runs = json.loads((root / 'pr_batch_workflow_runs.json').read_text())
        comments[f"/repos/owner/repo/issues/{first['number']}/comments"]=[{
            'id':ready_comment['databaseId'],'body':ready_comment['body'],
            'created_at':ready_comment['createdAt'],'updated_at':ready_comment['updatedAt'],
            'html_url':ready_comment['url'],'user':{'login':'fixture-bot[bot]','type':'Bot'}}]
        reader = github_read.GitHubReader(gh_cmd='fake-gh', actor='fixture-reader',
                                         expected_actor='fixture-reader', operation='github.pr.view')
        with patch.object(reader, 'graphql_json', return_value=watch.github_api.ApiResult(ok=True,status=200,body=document)):
            pulls = github_pr_batch.batch_pull_requests(reader,'owner/repo',[first['number']])
        source = pulls[first['number']]
        source.update(state='open',draft=False,mergeable=True,labels=[])
        suite = checks[f'/repos/owner/repo/commits/{first["headRefOid"]}/check-runs']['check_runs'][0]
        protection = {'checks':[{'context':suite['name'],'app_id':suite['app']['id']}]}
        transport_calls=[]
        def rest_request(_method,path,**_kwargs):
            transport_calls.append(path)
            endpoint=path.split('?')[0]
            if endpoint.endswith('/actions/runs'):body=runs
            elif endpoint.endswith('/status'):body={'state':'success'}
            elif endpoint.endswith('/required_status_checks'):body=protection
            elif endpoint in checks:body=checks[endpoint]
            elif endpoint in comments:body=comments[endpoint]
            elif endpoint.endswith('/reviews'):body=[]
            elif endpoint.endswith(f'/pulls/{first["number"]}'):body=source
            else:raise AssertionError(endpoint)
            return watch.github_api.ApiResult(ok=True,status=200,body=body)
        live=object.__new__(watch.Live)
        live.reader=reader
        target={**TARGET,'repo':'owner/repo','base':source['base']['ref']}
        with patch.object(reader,'_transport_request',side_effect=rest_request):
            pr_path=f'/repos/owner/repo/pulls/{first["number"]}'
            observed=live.get(pr_path)
            observed.update(state='open',draft=False,mergeable=True,labels=[])
            self.assertTrue(live.qualified(target,observed,set()))
            self.assertFalse(any(item.split('?')[0] in comments for item in transport_calls))
            fresh=live.get(pr_path)
            self.assertTrue(live.qualified(target,fresh,set()))
        self.assertTrue(any(item.split('?')[0] in comments for item in transport_calls))
        self.assertIn(pr_path,transport_calls)

    def test_custom_limit_and_dry_run(self):
        fake = Fake()
        self.args.max_batch_size = 2
        self.args.apply = False
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertEqual(
            next(data["prs"] for event, data in self.events if event == "batch"), [1, 2]
        )

    def test_dependency_enqueue_runs_live_trust_head_and_check_qualification(self):
        self.args.handoff_login = ["engineer"]
        for scenario in ("trusted", "author", "stale", "pending"):
            with self.subTest(scenario=scenario):
                fake = Fake(count=1)
                fake.pulls[1]["user"]["login"] = "renovate[bot]"
                fake.ineligible[1] = [watch.DEPENDENCY_REVIEW]
                live = object.__new__(watch.Live)
                live.reader = object()
                live.get = lambda _path: {"checks": [{"context": "CI", "app_id": 7}]}
                ready = comment(login="renovate[bot]" if scenario == "author" else "engineer")
                if scenario == "stale":
                    ready["body"] = f"{READY}\n\nHead: {'b' * 40}"
                live.pages = lambda path: [] if path.endswith("/reviews") else [ready]
                live.acceptance = lambda *_args: False
                fake.qualified = live.qualified
                checks = {
                    "summary": {"countsComplete": True, "unavailableComponents": [], "failingCount": 0, "pendingCount": int(scenario == "pending"), "unfinishedWorkflowRunCount": 0},
                    "checkRuns": [{"name": "CI", "status": "completed", "conclusion": "success", "appId": 7}],
                    "statuses": [],
                }
                with patch.object(watch, "pull_request_checks", return_value=checks):
                    self.run_pass(fake)
                self.assertEqual(fake.writes, [1] if scenario == "trusted" else [])

    def test_dependency_review_is_rechecked_before_label(self):
        self.args.handoff_login = ["engineer"]
        fake = Fake(count=1)
        fake.pulls[1]["user"]["login"] = "renovate[bot]"
        fake.ineligible[1] = [watch.DEPENDENCY_REVIEW]
        comments = [comment()]
        # The agent deletes its handoff between scan and write. The dependency
        # author's replacement comment cannot supply that missing agent review.
        def delete_agent_handoff(io):
            if io.calls > 1:
                comments[:] = [comment(login="renovate[bot]")]

        fake.before = delete_agent_handoff
        live = object.__new__(watch.Live)
        live.reader = object()
        live.get = lambda _path: {"checks": [{"context": "CI", "app_id": 7}]}
        live.pages = lambda path: [] if path.endswith("/reviews") else comments
        live.acceptance = lambda *_args: False
        fake.qualified = live.qualified
        checks = {
            "summary": {"countsComplete": True, "unavailableComponents": [], "failingCount": 0, "pendingCount": 0, "unfinishedWorkflowRunCount": 0},
            "checkRuns": [{"name": "CI", "status": "completed", "conclusion": "success", "appId": 7}],
            "statuses": [],
        }
        with patch.object(watch, "pull_request_checks", return_value=checks):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_dependency_review_never_bypasses_another_controller_hold(self):
        for hold in ("actor role is not allowed to enqueue", "held by merge-blocked label"):
            with self.subTest(hold=hold):
                fake = Fake(count=1)
                fake.ineligible[1] = [watch.DEPENDENCY_REVIEW, hold]
                self.run_pass(fake)
                self.assertEqual(fake.writes, [])

    def test_conflicting_pr_is_skipped_before_qualification(self):
        fake = Fake(count=1)
        fake.pulls[1]["mergeable"] = False
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "skipped" and data.get("reason") == "conflicting" for event, data in self.events))

    def test_unrecorded_review_of_record_is_skipped_with_its_reason(self):
        fake = Fake(count=1)
        fake.review_gaps[1] = "A review of record was requested with no later recorded outcome"
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        skipped = [data for event, data in self.events if event == "skipped"]
        self.assertEqual([data["reason"] for data in skipped], ["review_of_record_not_recorded"])
        self.assertEqual(skipped[0]["detail"], fake.review_gaps[1])

    def test_incomplete_inventory_reports_coverage_instead_of_review(self):
        fake = Fake(count=1)
        fake.pulls[1]["changed_files"] = 2
        fake.review_gaps[1] = "review-of-record evidence unavailable: incomplete inventory"
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        skipped = [data for event, data in self.events if event == "skipped"]
        self.assertEqual([data["reason"] for data in skipped], ["file_inventory_changed_or_incomplete"])

    def test_review_request_appearing_before_label_stops_batch(self):
        fake = Fake(count=1)
        fake.before = lambda io: io.review_gaps.update({1: "requested"}) if io.calls > 1 else None
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_conflict_appearing_before_label_stops_batch(self):
        fake = Fake(count=1)
        fake.before = lambda io: io.pulls[1].update(mergeable=False) if io.calls > 1 else None
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_unknown_mergeability_is_skipped(self):
        fake = Fake(count=1)
        fake.pulls[1]["mergeable"] = None
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "skipped" and data.get("reason") == "mergeability_unknown" for event, data in self.events))

    def test_lazy_mergeability_computes_before_selection_and_label_recheck(self):
        for batched in (False, True):
            for computed in (True, False, None):
                with self.subTest(batched=batched, computed=computed):
                    fake = Fake(count=1)
                    initial = {**pr(), "mergeable": None}
                    resolved = {**pr(), "mergeable": computed, "mergeable_state": "behind"}
                    reader = watch.GitHubReader(
                        gh_cmd="fake-gh", actor="fixture-reader",
                        expected_actor="fixture-reader", operation="github.pr.view",
                    )
                    live = object.__new__(watch.Live)
                    live.reader = reader
                    live.deadline = 100
                    live.mergeability_reads_remaining = 20
                    path = f"/repos/{REPO}/pulls/1"
                    if batched:
                        reader.batch_identity = reader.batch_context()
                        reader.batch_responses = {
                            path: initial, f"/repos/{REPO}/issues/1/comments": [],
                            f"/repos/{REPO}/pulls/2": pr(2),
                        }
                    replies = [] if batched else [initial]
                    replies.extend([initial, resolved])
                    requests = []

                    def transport(method, endpoint, **_kwargs):
                        self.assertEqual((method, endpoint), ("GET", path))
                        requests.append(endpoint)
                        body = replies.pop(0) if replies else fake.pulls[1]
                        fake.pulls[1].update(body)
                        return watch.github_api.ApiResult(ok=True, status=200, body=copy.deepcopy(body))

                    fake.get = live.get
                    fake.get_many = live.get_many
                    self.events.clear()
                    with (
                        patch.object(reader, "_transport_request", side_effect=transport),
                        patch.object(watch.time, "time", return_value=0),
                        patch.object(watch.time, "sleep") as sleep,
                    ):
                        self.run_pass(fake)
                    self.assertEqual(fake.writes, [1] if computed is True else [])
                    self.assertTrue(sleep.called)
                    self.assertTrue(all(call.args[0] > 0 for call in sleep.call_args_list))
                    self.assertNotIn(f"/repos/{REPO}/issues/1/comments", reader.batch_responses)
                    if batched:
                        self.assertNotIn(path, reader.batch_responses)
                        self.assertEqual(reader.batch_responses[f"/repos/{REPO}/pulls/2"], pr(2))
                    self.assertFalse(replies)
                    if computed is not True:
                        reason = "conflicting" if computed is False else "mergeability_unknown"
                        self.assertTrue(any(event == "skipped" and data.get("reason") == reason for event, data in self.events))

    def test_delayed_mergeability_backlog_progresses_across_passes(self):
        # A base landing invalidates every PR. Conditional observations keep
        # returning null; only individual fresh reads start/observe computation.
        for count in (14, 30):
            with self.subTest(count=count):
                fake = Fake(count=count)
                clock = [0.0]
                started = {}
                reads = []
                landed = set()
                delay = 5

                def transport(_method, path, **kwargs):
                    number = int(path.rsplit("/", 1)[-1])
                    value = copy.deepcopy(fake.pulls[number])
                    value.update(mergeable=None, mergeable_state="unknown")
                    if kwargs.get("conditional_cache") is False:
                        reads.append(number)
                        started.setdefault(number, clock[0])
                        if clock[0] - started[number] >= delay:
                            value.update(mergeable=True, mergeable_state="behind")
                    return watch.github_api.ApiResult(ok=True, status=200, body=value)

                def sleep(seconds):
                    clock[0] += seconds

                with (
                    patch.object(github_read.github_api_core, "request_identity", return_value=("fixture-reader", "fixture-reader", [])),
                    patch.dict("os.environ", {"GITHUB_READ_CACHE_DIR": str(Path(self.directory.name) / "reader-cache")}),
                    patch.object(watch.operator, "resolve_settings", return_value={"service_url": "https://example.test", "token": "fixture"}),
                    patch.object(watch.operator, "validate_service_url"),
                    patch.object(watch, "automation_only_gh_prefix_args", return_value=[]),
                    patch.object(watch.time, "time", side_effect=lambda: clock[0]),
                    patch.object(watch.time, "sleep", side_effect=sleep),
                    patch.object(watch.GitHubReader, "_transport_request", side_effect=transport),
                ):
                    for pass_number in range(count):
                        live = watch.Live(clock[0] + 1800)
                        self.assertEqual(live.reader.actor, "fixture-reader")
                        budget = live.mergeability_reads_remaining
                        fake.get = live.get
                        fake.get_many = lambda paths: live.get_many(paths)
                        before_reads = len(reads)
                        self.run_pass(fake)
                        # Every label has one mandatory fresh pre-write GET;
                        # all other unconditional reads draw on recovery quota.
                        self.assertLessEqual(len(reads) - before_reads, budget + len(fake.writes))
                        # When the budget has room to prime this backlog and
                        # revisit two PRs, a slow computation can form a batch.
                        if count == 14 and pass_number == 0 and budget >= count + 2:
                            self.assertGreater(len(fake.writes), 1)
                        landed.update(fake.writes)
                        if len(landed) == count:
                            break
                        # The next landing resets computation for remaining PRs.
                        if fake.writes:
                            for number in fake.writes:
                                del fake.pulls[number]
                            fake.writes.clear()
                            started.clear()
                        clock[0] += 300
                self.assertEqual(landed, set(range(1, count + 1)))

    def test_lazy_mergeability_budget_and_deadline_bound_extra_reads(self):
        live = object.__new__(watch.Live)
        live.reader = Mock(cache_enabled=True, batch_responses={})
        live.reader.get_json.return_value = {**pr(), "mergeable": None}
        live.reader.request.return_value = watch.github_api.ApiResult(
            ok=True, status=200, body={**pr(), "mergeable": None},
        )
        live.deadline = 100
        live.mergeability_reads_remaining = 3
        with (
            patch.object(watch.time, "time", return_value=0),
            patch.object(watch.time, "sleep") as sleep,
        ):
            values = live.get_many([f"/repos/{REPO}/pulls/{number}" for number in range(1, 5)])
            self.assertTrue(all(value["mergeable"] is None for value in values))
        self.assertEqual(live.reader.request.call_count, 3)
        self.assertEqual(sleep.call_count, 3)
        self.assertTrue(live.reader.cache_enabled)
        live.mergeability_reads_remaining = 3
        with patch.object(watch.time, "time", return_value=99), patch.object(watch.time, "sleep") as sleep:
            live.get_many([f"/repos/{REPO}/pulls/1"])
        sleep.assert_not_called()
        self.assertEqual(live.reader.request.call_count, 3)

    def test_mergeability_recovery_rechecks_deadline_after_sleep(self):
        live = object.__new__(watch.Live)
        live.reader = Mock(batch_responses={})
        live.reader.get_json.return_value = {**pr(), "mergeable": None}
        live.deadline = 100
        live.mergeability_reads_remaining = 20
        clock = [0]

        def oversleep(_seconds):
            clock[0] = 101

        with patch.object(watch.time, "time", side_effect=lambda: clock[0]), patch.object(watch.time, "sleep", side_effect=oversleep):
            live.get_many([f"/repos/{REPO}/pulls/1"])
        live.reader.request.assert_not_called()

    def test_final_pr_observation_is_fresh_after_recovery_budget_exhaustion(self):
        live = object.__new__(watch.Live)
        live.reader = Mock(batch_responses={})
        live.reader.get_json.return_value = {**pr(), "mergeable": None}
        live.reader.request.return_value = watch.github_api.ApiResult(ok=True, status=200, body=pr())
        live.deadline = 100
        live.mergeability_reads_remaining = 0
        self.assertTrue(live.get(f"/repos/{REPO}/pulls/1")["mergeable"])

    def test_lazy_mergeability_read_error_restores_cache(self):
        live = object.__new__(watch.Live)
        live.reader = Mock(cache_enabled=True, batch_responses={})
        live.reader.get_json.return_value = {**pr(), "mergeable": None}
        live.reader.request.side_effect = TimeoutError("fixture timeout")
        live.deadline = 100
        live.mergeability_reads_remaining = 3
        with patch.object(watch.time, "time", return_value=0), patch.object(watch.time, "sleep"):
            with self.assertRaises(TimeoutError):
                live.get(f"/repos/{REPO}/pulls/1")
        self.assertTrue(live.reader.cache_enabled)

    def test_active_candidate_keeps_all_arrivals_unlabeled(self):
        fake = Fake()
        fake.active = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_unavailable_controller_with_null_result_stays_busy(self):
        fake = Fake()
        fake.train = lambda _target: {"status": "unavailable", "result": None}
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_candidate_starts_between_labels_stops_remaining_group(self):
        fake = Fake()
        fake.before = lambda io: setattr(io, "active", bool(io.writes))
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_scheduler_candidate_from_own_labels_finishes_selected_batch(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertFalse(any(event == "batch_stopped" for event, _ in self.events))
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5, 3])

    def test_own_candidate_planning_building_and_passed_states_continue(self):
        for candidate_status in ("planned", "building", "passed"):
            with self.subTest(candidate_status=candidate_status):
                fake = Fake()
                fake.scheduler_owns_batch = True
                fake.candidate_status = candidate_status
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_own_scheduler_candidate_preserves_unchanged_held_labels(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        fake.pulls[6]["labels"] = [
            {"name": TARGET["label"]}, {"name": "merge-blocked"}
        ]
        fake.ineligible[6] = ["held by merge-blocked label"]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertEqual(
            fake.pulls[6]["labels"],
            [{"name": TARGET["label"]}, {"name": "merge-blocked"}],
        )

    def test_queue_free_observation_cannot_preserve_a_nonlabel_hold(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        fake.pulls[6]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[6] = ["actor role is not allowed to enqueue"]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_own_scheduler_candidate_stops_when_held_labels_change(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        fake.pulls[6]["labels"] = [
            {"name": TARGET["label"]}, {"name": "merge-blocked"}
        ]
        fake.ineligible[6] = ["held by merge-blocked label"]

        def before(io):
            if io.writes:
                io.pulls[6]["labels"] = [{"name": TARGET["label"]}]

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_own_candidate_does_not_hide_new_foreign_queue_labels(self):
        fake = Fake()
        fake.scheduler_owns_batch = True

        def before(io):
            if io.writes:
                io.pulls[6]["labels"].append({"name": TARGET["label"]})

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_own_candidate_reflow_after_next_label_finishes_batch(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        original_train = fake.train

        def train(target):
            value = original_train(target)
            if len(fake.writes) > 1:
                return candidate_response(
                    fake.writes,
                    action="plan_candidate",
                    queue=response(fake.pulls.values(), fake.writes)["result"][
                        "dry_run_result"
                    ],
                    records={
                        "superseded_merge_train_batch_candidate_record_id": "active"
                    },
                )
            return value

        fake.train = train
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_reflow_of_unobserved_candidate_stops_batch(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        original_train = fake.train

        def train(target):
            value = original_train(target)
            if fake.writes:
                value["result"]["controller_action"] = "plan_candidate"
                value["records"] = {
                    "superseded_merge_train_batch_candidate_record_id": "unobserved"
                }
            return value

        fake.train = train
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_own_candidate_retains_each_members_head_labels_and_readiness_guard(self):
        for changed in ("head", "hold", "readiness"):
            with self.subTest(changed=changed):
                fake = Fake()
                fake.scheduler_owns_batch = True

                def before(io):
                    if io.writes:
                        if changed == "head":
                            io.pulls[2]["head"]["sha"] = "b" * 40
                        elif changed == "hold":
                            io.pulls[2]["labels"].append({"name": "merge-blocked"})
                        else:
                            io.qualify = False

                fake.before = before
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1])

    def test_unrelated_or_incomplete_candidate_evidence_stops_batch(self):
        for mutation in (
            lambda value: value["candidate"].update(pull_request_numbers=[6]),
            lambda value: value["candidate"].update(pull_request_numbers=[]),
            lambda value: value["candidate"].pop("pull_request_numbers"),
            lambda value: value["candidate"].update(entries_count=2),
            lambda value: value["candidate"].update(status="failed"),
            lambda value: value.update(controller_action="plan_landing"),
            lambda value: value.update(controller_reconciliation_status="required"),
            lambda value: value.update(landing_plan={"record_id": "landing"}),
            lambda value: value.update(stack_collapse_plan={"record_id": "stack"}),
            lambda value: value.update(active_record_id="lease"),
            lambda value: value.update(error={"code": "failure"}),
            lambda value: value.update(
                dry_run_result={"queue": [], "queue_order": []}
            ),
        ):
            fake = Fake()
            fake.scheduler_owns_batch = True
            original_train = fake.train

            def train(target):
                value = original_train(target)
                if fake.writes:
                    mutation(value["result"])
                return value

            fake.train = train
            self.run_pass(fake)
            self.assertEqual(fake.writes, [1])

    def test_own_candidate_with_fresh_queue_still_checks_member_disposition(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        original_train = fake.train

        def train(target):
            value = original_train(target)
            if fake.writes:
                queue = response(fake.pulls.values(), fake.writes)["result"][
                    "dry_run_result"
                ]
                queue["queue"][1].update(
                    eligible=False,
                    ineligible_reasons=["actor role is not allowed to enqueue"],
                )
                return candidate_response(fake.writes, queue=queue)
            return value

        fake.train = train
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_unready_member_never_labeled(self):
        fake = Fake()
        fake.qualify = False
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_paused_coordinated_and_ready_or_open_release(self):
        for key, value in (
            ("paused_repositories", [REPO]),
            ("coordinated_repositories", [REPO]),
            ("client_releases", [{"repository": REPO, "status": "ready"}]),
            ("client_releases", [{"repository": REPO, "status": "open"}]),
        ):
            with self.subTest(key=key, value=value):
                self.holds[key] = value
                self.save()
                fake = Fake()
                self.run_pass(fake)
                self.assertEqual(fake.writes, [])
                self.holds[key] = []

    def test_client_release_pauses_only_its_product(self):
        self.holds["client_releases"] = [{"repository": CLIENT, "status": "open"}]
        self.save()
        self.assertTrue(watch.held(self.holds, CLIENT))
        fake = Fake()
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_missing_or_invalid_holds_refuse_before_effect(self):
        for value in ("{}", "{", '{"paused_repositories": []}'):
            self.path.write_text(value)
            fake = Fake()
            with self.assertRaises(ValueError):
                self.run_pass(fake)
            self.assertEqual(fake.writes, [])
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_pass(Fake())

    def test_hold_change_during_scan_refuses(self):
        fake = Fake()
        original = fake.qualified

        def qualifies(*args):
            self.holds["paused_repositories"] = [REPO]
            self.save()
            return original(*args)

        fake.qualified = qualifies
        with self.assertRaises(ValueError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_policy_change_during_scan_refuses(self):
        fake = Fake()

        def qualifies(*_args):
            fake.targets[0]["label"] = "changed-label"
            return True

        fake.qualified = qualifies
        with self.assertRaises(ValueError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_head_changes_before_write_refuse(self):
        fake = Fake()

        def before(io):
            if io.calls == 2:
                io.pulls[1]["head"]["sha"] = "b" * 40

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_changed_file_inventory_skips_that_pr_and_continues(self):
        fake = Fake()
        fake.pulls[1]["changed_files"] = 2
        self.run_pass(fake)
        self.assertEqual(fake.writes, [2, 4, 5, 6])
        self.assertTrue(
            any(event == "skipped" and data["pr"] == 1 for event, data in self.events)
        )

    def test_unavailable_inventory_skips_only_its_repository(self):
        fake = Fake(count=1)
        healthy = {**TARGET, "repo": "example/healthy"}
        fake.targets = [TARGET, healthy]
        def inventory(repo):
            if repo == REPO:
                raise watch.GitHubReadShapeError("inventory changed")
            return [pr()]
        fake.open_pulls = inventory
        self.run_pass(fake)
        self.assertEqual(fake.discovered, [REPO, healthy["repo"]])
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "skip_inventory_unavailable" and data["repository"] == REPO for event, data in self.events))
        self.assertTrue(all(data["repository"] == healthy["repo"] for event, data in self.events if event == "enqueued"))
        self.args.repo = [healthy["repo"]]
        self.run_pass(fake)
        self.assertEqual(fake.discovered, [healthy["repo"]])

    def test_another_pr_labeled_after_discovery_blocks_the_first_write(self):
        fake = Fake(count=2)
        original = fake.pages
        def label_another(path, **params):
            if path.endswith("/1/files"):
                fake.pulls[2]["labels"] = [{"name": TARGET["label"]}]
            return original(path, **params)
        fake.pages = label_another
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_live_discovery_adapts_standalone_reader_errors(self):
        live = object.__new__(watch.Live)
        live.reader = Mock()
        pulls = [pr()]
        with patch.object(watch.github_open_prs, "scan", return_value={REPO: pulls}):
            live.discover([REPO])
        self.assertEqual(live.open_pulls(REPO), pulls)
        error = watch.github_open_prs.GitHubReadError("unavailable",
            result=watch.github_api.ApiResult(ok=False, status=503, body=None), diagnostics={})
        with patch.object(watch.github_open_prs, "scan", return_value={REPO: error}) as scan:
            live.discover([REPO])
        scan.assert_called_once_with(live.reader, [REPO])
        with self.assertRaises(watch.GitHubReadError) as caught:
            live.open_pulls(REPO)
        self.assertEqual(caught.exception.result.status, 503)
        error.result.status = 403
        error.result.failure = watch.github_api.FailureDetail(cause="permission_denied", message="Forbidden",
            retryable=False, fallback_eligible=False, disposition="stop")
        fake = Fake()
        fake.open_pulls = live.open_pulls
        with self.assertRaises(watch.GitHubReadError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_repository_filter_cannot_create_enrollment(self):
        fake = Fake()
        self.args.repo = ["example/not-enrolled"]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.calls, 0)
        self.assertEqual(fake.discovered, [])

    def test_lock_refuses_second_watcher_and_pause_updates_use_same_inode(self):
        with watch.hold_lock(self.path):
            with self.assertRaises(BlockingIOError):
                with watch.hold_lock(self.path):
                    pass
        inode = self.path.with_name(self.path.name + ".lock").stat().st_ino
        watch.edit_holds(["--hold-file", str(self.path), "--release-ready", REPO])
        self.assertTrue(watch.held(watch.read_holds(self.path), REPO))
        watch.edit_holds(
            ["--hold-file", str(self.path), "--release-finished", REPO, "--pause", REPO]
        )
        self.assertEqual(watch.read_holds(self.path)["client_releases"], [])
        self.assertTrue(watch.held(watch.read_holds(self.path), REPO))
        self.assertEqual(
            self.path.with_name(self.path.name + ".lock").stat().st_ino, inode
        )
        watch.edit_holds(["--hold-file", str(self.path), "--resume", REPO.upper()])
        self.assertFalse(watch.held(watch.read_holds(self.path), REPO))

    def test_remove_legacy_product_hold_preserves_other_delivery_holds(self):
        other = "example/shared"
        watch.edit_holds(["--hold-file", str(self.path), "--coordinated-repo", REPO,
                          "--coordinated-repo", other, "--pause", REPO,
                          "--release-ready", other])
        before = watch.read_holds(self.path)
        watch.edit_holds(["--hold-file", str(self.path), "--uncoordinate", REPO.upper()])
        after = watch.read_holds(self.path)
        self.assertNotIn(REPO, after["coordinated_repositories"])
        self.assertIn(other, after["coordinated_repositories"])
        self.assertEqual(after["paused_repositories"], before["paused_repositories"])
        self.assertEqual(after["client_releases"], before["client_releases"])
        self.assertTrue(watch.held(after, REPO))
        watch.edit_holds(["--hold-file", str(self.path), "--resume", REPO])
        self.assertFalse(watch.held(watch.read_holds(self.path), REPO))

    def test_legacy_hold_removal_waits_for_shared_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        watch.edit_holds(["--hold-file", str(self.path), "--coordinated-repo", REPO])
        self.assertTrue(watch.held(watch.read_holds(self.path), REPO))
        started, finished = Event(), Event()

        def remove():
            started.set()
            watch.edit_holds(["--hold-file", str(self.path), "--uncoordinate", REPO])
            finished.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with watch.hold_lock(self.path):
                edit = pool.submit(remove)
                self.assertTrue(started.wait(timeout=2))
                self.assertFalse(finished.wait(timeout=0.1))
                self.assertTrue(watch.held(watch.read_holds(self.path), REPO))
            edit.result(timeout=2)
        self.assertTrue(finished.is_set())
        self.assertFalse(watch.held(watch.read_holds(self.path), REPO))

    def test_latest_wait_stale_head_missing_head_and_untrusted_ready_refuse(self):
        self.assertTrue(watch.latest_ready(pr(), [comment()], {"engineer"}))
        for entries in (
            [comment(), comment(f"{WAITING} the Client's acceptance", 2)],
            [comment(READY)],
            [comment(f"{READY}\nHead: {'b' * 40}")],
            [comment(login="stranger")],
            [comment(f"> {READY}\nHead: {SHA}")],
        ):
            with self.subTest(entries=entries):
                self.assertFalse(watch.latest_ready(pr(), entries, {"engineer"}))

    def test_unknown_reconciliation_or_candidate_without_queue_never_proves_idle(self):
        for payload in (
            {},
            {"status": "unavailable"},
            response([], controller_action="resume_reconciliation"),
            response([], candidate_record_id="stored"),
            response([], controller_reconciliation_status="required"),
        ):
            self.assertFalse(watch.idle(payload))
        self.assertTrue(watch.idle(response([])))

    def test_renames_group_both_top_level_areas(self):
        self.assertEqual(
            watch.areas([{"filename": "src/x", "previous_filename": "legacy/x"}]),
            {"src", "legacy"},
        )

    def test_behind_base_root_can_accumulate_its_group_without_branch_mutation(self):
        fake = Fake()
        fake.after_label_action = "update_branch"
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertFalse(watch.idle(response([], controller_action="update_branch")))

    def test_live_label_adapter_finishes_the_batch_and_driver_handoff(self):
        fake = Fake()
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        adapter.pages = fake.pages
        fake.label = adapter.label

        def write(_method, path, _body, **_kwargs):
            number = int(path.split("/")[-2])
            fake.writes.append(number)
            fake.pulls[number]["labels"].append({"name": TARGET["label"]})
            return api_result("confirmed")

        with patch.object(watch.github_api, "call_gh_with_retry", side_effect=write):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertTrue(any(event == "driver_handoff" for event, _data in self.events))

    def test_only_held_labeled_prs_allow_the_next_ready_batch(self):
        fake = Fake()
        for number, reason in (
            (1, "held by merge-blocked label"),
            (2, "actor role is not allowed to enqueue"),
        ):
            fake.pulls[number]["labels"] = [{"name": TARGET["label"]}]
            fake.ineligible[number] = [reason]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [3])
        self.assertEqual(fake.pulls[1]["labels"], [{"name": TARGET["label"]}])
        self.assertEqual(fake.pulls[2]["labels"], [{"name": TARGET["label"]}])
        self.run_pass(fake)
        self.assertEqual(fake.writes, [3])

    def test_running_candidate_preserves_held_labeled_members(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[1] = ["held by merge-blocked label"]
        fake.active = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_eligible_labeled_member_preserves_batch_with_held_members(self):
        fake = Fake()
        for number in (1, 2):
            fake.pulls[number]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[1] = ["held by merge-blocked label"]
        fake.ineligible[2] = []
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_labeled_member_missing_from_controller_inventory_stays_busy(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        original_train = fake.train

        def missing(target):
            value = original_train(target)
            value["result"]["dry_run_result"]["queue"] = []
            return value

        fake.train = missing
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_label_added_after_discovery_is_rechecked_before_any_write(self):
        fake = Fake()
        original_pages = fake.pages

        def label_during_read(path, **params):
            fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
            return original_pages(path, **params)

        fake.pages = label_during_read
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_unknown_label_author_preserves_batch(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[1] = [
            f"{TARGET['label']} label ignored: could not read who applied it"
        ]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_unknown_label_author_between_writes_stops_batch(self):
        fake = Fake()

        def before(io):
            if io.calls == 3:
                io.pulls[6]["labels"] = [{"name": TARGET["label"]}]
                io.ineligible[6] = [
                    f"{TARGET['label']} label ignored: could not read who applied it"
                ]

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_eligible_labeled_member_outside_queue_order_preserves_batch(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        original_train = fake.train

        def truncated_order(target):
            value = original_train(target)
            value["result"]["dry_run_result"]["queue_order"] = []
            return value

        fake.train = truncated_order
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        busy = next(data for event, data in self.events if event == "busy")
        self.assertEqual(busy["labeled"], [1])

    def test_held_labels_on_other_enrolled_base_allow_next_batch(self):
        fake = Fake()
        fake.targets.append({**TARGET, "base": "release"})
        fake.pulls[2]["base"]["ref"] = "release"
        for number in (1, 2):
            fake.pulls[number]["labels"] = [{"name": TARGET["label"]}]
            fake.ineligible[number] = ["held by merge-blocked label"]
        original_train = fake.train

        def scoped(target):
            value = original_train(target)
            dry = value["result"]["dry_run_result"]
            dry["queue"] = [
                row
                for row in dry["queue"]
                if fake.pulls[row["number"]]["base"]["ref"] == target["base"]
            ]
            dry["queue_order"] = [
                row["number"] for row in dry["queue"] if not row["ineligible_reasons"]
            ]
            return value

        fake.train = scoped
        self.run_pass(fake)
        self.assertEqual(fake.writes, [3])

    def test_an_edited_wait_invalidates_a_later_created_ready(self):
        waiting = comment(f"{WAITING} the Client's acceptance")
        waiting["updated_at"] = "2026-01-01T00:05:00Z"
        self.assertFalse(
            watch.latest_ready(pr(), [waiting, comment(number=2)], {"engineer"})
        )


class QueueReadTests(unittest.TestCase):
    def setUp(self):
        self.live = object.__new__(watch.Live)
        self.live.deadline = watch.time.time() + 30
        self.live.settings = {"service_url": "https://control.example", "token": "fixture"}
        self.payload = response([pr()])
        self.payload["result"].update(repository=REPO, base_branch=TARGET["base"])

    def test_queue_uses_existing_read_only_plan_and_projects_author_dispositions(self):
        with patch.object(watch.operator, "request_launchplane", return_value=self.payload) as read:
            queue = self.live.queue(TARGET)
        self.assertEqual(queue["queue"][0]["ineligible_reasons"],
                         [f"missing {TARGET['label']} label"])
        self.assertIs(read.call_args.kwargs["body"]["mutate"], False)
        self.assertNotIn("idempotency_key", read.call_args.kwargs)

    def test_denied_wrong_target_or_mutating_queue_never_qualifies(self):
        for field, value in (("repository", "another/repo"), ("base_branch", "other"),
                             ("mode", "mutate"), ("dry_run_result", {})):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload["result"][field] = value
                with patch.object(watch.operator, "request_launchplane", return_value=payload):
                    with self.assertRaises(watch.LaunchplaneReadError):
                        self.live.queue(TARGET)
        for status in (401, 403):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://control.example", status, "denied", {}, None)
                with patch.object(watch.operator, "request_launchplane", side_effect=error):
                    with self.assertRaises(watch.WatchError) as refused:
                        self.live.queue(TARGET)
                    self.assertNotIsInstance(refused.exception, watch.LaunchplaneReadError)

    def test_snapshot_refusals_allow_a_fresh_read_without_stopping_the_watcher(self):
        for status in (400, 409, 500, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://control.example", status, "unavailable", {}, None)
                with patch.object(watch.operator, "request_launchplane", side_effect=error):
                    with self.assertRaises(watch.LaunchplaneReadError) as refused:
                        self.live.queue(TARGET)
                self.assertTrue(watch.transient_read_error(refused.exception))

    def test_malformed_or_truncated_queue_response_is_a_bounded_read_failure(self):
        for error in (watch.operator.ServiceResponseError({}),
                      watch.http.client.IncompleteRead(b"", 100)):
            with self.subTest(error=type(error).__name__):
                with patch.object(watch.operator, "request_launchplane", side_effect=error):
                    with self.assertRaises(watch.LaunchplaneReadError):
                        self.live.queue(TARGET)
        payload = copy.deepcopy(self.payload)
        payload["result"]["dry_run_result"]["queue_order"] = [0]
        with patch.object(watch.operator, "request_launchplane", return_value=payload):
            with self.assertRaises(watch.LaunchplaneReadError):
                self.live.queue(TARGET)

    def test_policy_read_requires_explicit_controller_scheduler_mutation_for_staging(self):
        scheduler = {"enabled": True, "mutate": True, "runner_mode": "controller"}
        for changes in ({}, {"enabled": False}, {"mutate": False}, {"runner_mode": "level1"}):
            with self.subTest(changes=changes):
                raw = {"repository": REPO, "base_branch": TARGET["base"],
                       "enqueue_label": TARGET["label"], "scheduler": {**scheduler, **changes}}
                payload = {"status": "ok", "trace_id": "fixture", "record": {
                    "status": "active", "record_id": "fixture-policy",
                    "updated_at": "2026-01-01T00:00:00Z", "policy_sha256": "a" * 64,
                    "policy": {"policies": [raw]}}}
                with patch.object(watch.operator, "request_launchplane_read", return_value=payload):
                    target = self.live.policies("example")[0]
                self.assertEqual(target["scheduler_drives_train"], not changes)


def api_result(certainty=None):
    result = watch.github_api.ApiResult(ok=True, status=200, body=[])
    if certainty is not None:
        result.retry_summary = watch.github_api.RetrySummary(
            attempts=1,
            elapsed_wait=0,
            retry_eligible=False,
            last_actor="automation",
            last_bucket="rest_core",
            outcome_certainty=certainty,
            reconciliation=None,
            recommended_next_action="none",
            effective_deadline=10_000,
        )
    return result


class QualificationTests(unittest.TestCase):
    def setUp(self):
        states = ready_handoff.acceptance_reader()
        self.client_repository = states.ClientRelease(states.CLIENT)
        release_patch = patch.object(ready_handoff, "client_release", return_value=states.ClientRelease(states.NO_CLIENT))
        self.release = release_patch.start()
        self.addCleanup(release_patch.stop)
        self.io = object.__new__(watch.Live)
        self.io.reader = object()
        self.pull = pr()
        self.comments = [comment()]
        self.reviews = []
        self.requirements = {"checks": [{"context": "CI", "app_id": 7}]}
        self.checks = {
            "summary": {
                "countsComplete": True,
                "unavailableComponents": [],
                "failingCount": 0,
                "pendingCount": 0,
                "unfinishedWorkflowRunCount": 0,
            },
            "checkRuns": [
                {
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "appId": 7,
                }
            ],
            "statuses": [],
        }
        self.io.get = lambda _path: self.requirements
        self.io.pages = lambda path: (
            self.reviews if path.endswith("/reviews") else self.comments
        )
        self.io.acceptance = lambda *_args: False
        self.agent_review_required = False
        mock = patch.object(
            watch,
            "pull_request_checks",
            side_effect=lambda *_args, **_kwargs: self.checks,
        )
        mock.start()
        self.addCleanup(mock.stop)

    def qualified(self):
        return self.io.qualified(TARGET, self.pull, set(), self.agent_review_required)

    def test_review_gap_reads_records_and_base_branch_review_paths(self):
        config = None
        files = [{"filename": "src/tool.py"}]

        def get(path):
            if f"/contents/{ready_handoff.REVIEW_CONFIG_PATH}?ref=trunk" in path:
                if config is None:
                    raise watch.GitHubReadError(
                        "Not Found",
                        result=watch.github_api.ApiResult(ok=False, status=404, body=None),
                        diagnostics={},
                    )
                return {"content": base64.b64encode(json.dumps(config).encode()).decode()}
            raise AssertionError(path)

        self.io.get = get
        self.io.pages = lambda path: files if path.endswith("/files") else self.comments
        gap = lambda: self.io.review_gap(TARGET, self.pull, set())
        self.assertEqual(gap(), "")
        self.comments.append(comment("Supervisor runs the review.", 2))
        self.assertTrue(gap())
        self.comments.append(comment(ready_handoff.review_result_body("Review weighed: none.", SHA), 3))
        self.assertEqual(gap(), "")
        config = {"qualityGate": {"modelReview": {"requiredPaths": ["skills/*"]}}}
        self.assertEqual(gap(), "")
        files = [{"filename": "skills/github/SKILL.md"}]
        self.assertEqual(gap(), "")
        files = [{"filename": "src/tool.py"}]
        self.pull["changed_files"] = 3001
        self.assertTrue(gap())
        self.pull["changed_files"] = 1
        self.assertEqual(gap(), "")
        original_pages = self.io.pages
        def comments_only(path):
            if path.endswith("/comments"):
                return self.comments
            raise AssertionError("Supplied file inventory should not be read again")
        self.io.pages = comments_only
        self.assertEqual(self.io.review_gap(TARGET, self.pull, set(), file_inventory=files), "")
        self.io.pages = original_pages
        files = [{"filename": "skills/github/SKILL.md"}]
        self.comments.pop()
        self.comments.pop()
        self.assertTrue(gap())
        config = {"qualityGate": {"modelReview": {"requiredPaths": [""]}}}
        self.assertTrue(gap())
        for malformed in ([], "bad", {"qualityGate": [1]}, {"qualityGate": {"modelReview": [1]}}):
            config = malformed
            self.assertTrue(gap())

    def test_cli_defaults_accept_automation_review_of_dependency_head(self):
        self.pull["user"]["login"] = "dependabot[bot]"
        login = "configured-automation[bot]"
        self.comments = [comment(login=login)]
        outcomes = []

        def one_pass(_live, args, _emit, **_kwargs):
            outcomes.append(self.io.qualified(
                TARGET, self.pull, set(args.handoff_login), True,
            ))

        with (patch.object(watch, "Live"), patch.object(watch, "run_pass", one_pass),
              patch.object(watch, "automation_login", return_value=login)):
            with patch("builtins.print"):
                self.assertEqual(watch.main([
                    "--owner", "example", "--hold-file", "/unused/holds.json", "--once",
                ]), 0)
        self.assertEqual(outcomes, [True])

    def test_green_required_check_is_bound_to_expected_app(self):
        self.assertTrue(self.qualified())
        self.checks["checkRuns"][0]["appId"] = 99
        self.assertFalse(self.qualified())

    def test_behind_pr_requires_current_head_ready_receipt(self):
        self.pull["mergeable_state"] = "behind"
        self.assertTrue(self.qualified())
        self.pull["head"]["sha"] = "b" * 40
        self.assertFalse(self.qualified())

    def test_dependency_author_handoff_needs_explicit_trusted_agent(self):
        self.agent_review_required = True
        for author in ("dependabot[bot]", "renovate[bot]"):
            with self.subTest(author=author):
                self.pull["head"]["sha"] = SHA
                self.pull["user"]["login"] = author
                self.comments = [comment(login=author)]
                self.assertFalse(self.qualified())
                self.assertFalse(self.io.qualified(TARGET, self.pull, {author}, True))
                self.comments = [comment()]
                self.assertFalse(self.qualified())
                self.assertTrue(self.io.qualified(TARGET, self.pull, {"engineer"}, True))
                self.pull["head"]["sha"] = "b" * 40
                self.assertFalse(self.io.qualified(TARGET, self.pull, {"engineer"}, True))

    def test_conflicting_github_pr_refuses_even_with_green_checks(self):
        self.pull["mergeable"] = False
        self.assertFalse(self.qualified())

    def test_missing_pending_failed_unavailable_required_checks_refuse(self):
        for field, value in (
            ("countsComplete", False),
            ("unavailableComponents", ["checkRuns"]),
            ("pendingCount", 1),
            ("failingCount", 1),
            ("unfinishedWorkflowRunCount", 1),
        ):
            original = copy.deepcopy(self.checks)
            self.checks["summary"][field] = value
            self.assertFalse(self.qualified())
            self.checks = original
        self.checks["checkRuns"] = []
        self.assertFalse(self.qualified())

    def test_client_request_notes_and_label_require_recorded_acceptance(self):
        self.release.return_value = self.client_repository
        for configure in (
            lambda: self.pull["labels"].append({"name": "owner-review"}),
            lambda: self.checks["checkRuns"].append(
                {"name": "launchplane/owner-review"}
            ),
            lambda: self.pull.update(body="## Client test notes\nClient change: a preview.\nTry the preview."),
        ):
            configure()
            self.assertFalse(self.qualified())
        self.io.acceptance = lambda *_args: True
        self.assertTrue(self.qualified())

    def test_handoff_class_mismatch_never_enqueues(self):
        self.release.return_value = self.client_repository
        self.io.acceptance = lambda *_args: True
        for body in (
            f"## Client test notes\n{ready_handoff.ENGINEERING_RELEASE_TESTS}\nCheck the imported parts at release.",
            "## Client test notes\nCheck the imported parts.",
        ):
            with self.subTest(body=body):
                self.pull["body"] = body
                self.assertFalse(self.qualified())

    def test_latest_current_head_changes_requested_refuses(self):
        self.reviews = [
            {
                "id": 1,
                "user": {"login": "reviewer"},
                "state": "CHANGES_REQUESTED",
                "commit_id": SHA,
            }
        ]
        self.assertFalse(self.qualified())
        self.reviews.append(
            {
                "id": 2,
                "user": {"login": "reviewer"},
                "state": "APPROVED",
                "commit_id": SHA,
            }
        )
        self.assertTrue(self.qualified())

    def test_neutral_engineering_advisory_does_not_invent_acceptance_wait(self):
        self.release.return_value = self.client_repository
        self.pull["body"] = "## Client test notes\nNothing for the Client to test: documentation only."
        review = {"name": "launchplane/owner-review", "status": "completed", "conclusion": "neutral"}
        self.checks["checkRuns"].append(review)
        acceptance = Mock(return_value=False)
        self.io.acceptance = acceptance
        self.assertTrue(self.qualified())
        acceptance.assert_not_called()
        for change in (
            {"labels": [{"name": "owner-review"}]},
            {"body": "## Client test notes\nTry the preview."},
        ):
            with self.subTest(change=change):
                original = copy.deepcopy(self.pull)
                self.pull.update(change)
                self.assertFalse(self.qualified())
                self.pull = original
        for status, conclusion in (("in_progress", None), ("completed", "failure"), ("completed", "success")):
            with self.subTest(status=status, conclusion=conclusion):
                review.update(status=status, conclusion=conclusion)
                self.assertFalse(self.qualified())


    def test_acceptance_uses_service_decision_and_exact_head(self):
        self.io.args = argparse.Namespace(
            config=None, env_config=None, url=None, timeout=10
        )
        for decision, head, expected in (
            ("accepted", SHA, True),
            ("accepted", "b" * 40, False),
            ("rejected", SHA, False),
        ):
            with patch.object(
                watch.review,
                "read_review",
                return_value={"decision": decision, "head_sha": head},
            ):
                self.assertEqual(
                    watch.Live.acceptance(self.io, REPO, self.pull), expected
                )

    def test_review_scope_denial_holds_only_that_pr(self):
        self.io.args = watch.operator.parse_args(["merge-train-policy-read"])
        denial = urllib.error.HTTPError(
            "https://example.invalid/review", 403, "scope denied", Message(), None
        )
        with patch.object(watch.review, "read_review", side_effect=denial):
            self.assertFalse(watch.Live.acceptance(self.io, REPO, self.pull))

    def test_live_label_requires_confirmed_or_reconciled_write_and_readback(self):
        self.io.deadline = 10_000
        self.io.pages = lambda _path: [{"name": TARGET["label"]}]
        for certainty in ("confirmed", "reconciled_applied"):
            with patch.object(
                watch.github_api,
                "call_gh_with_retry",
                return_value=api_result(certainty),
            ) as call:
                watch.Live.label(self.io, TARGET, 1)
                self.assertEqual(call.call_count, 1)
        for certainty in (None, "unknown", "not_applicable"):
            with patch.object(
                watch.github_api,
                "call_gh_with_retry",
                return_value=api_result(certainty),
            ) as call:
                with self.assertRaises(ValueError):
                    watch.Live.label(self.io, TARGET, 1)
                self.assertEqual(call.call_count, 1)
        self.io.pages = lambda _path: []
        with patch.object(
            watch.github_api, "call_gh_with_retry", return_value=api_result("confirmed")
        ):
            with self.assertRaises(ValueError):
                watch.Live.label(self.io, TARGET, 1)


class WatchLoopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "holds.json"
        self.path.write_text(json.dumps({
            "paused_repositories": [],
            "coordinated_repositories": [],
            "client_releases": [],
        }))

    def run_watch(self, factory, *, once=False, duration=8):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        argv = [
            "--owner", "example", "--hold-file", str(self.path), "--apply",
            "--interval", "4", "--deadline-seconds", str(duration),
        ]
        if once:
            argv.append("--once")
        with (
            patch.object(watch, "Live", side_effect=factory) as live,
            patch.object(watch.time, "time", side_effect=lambda: now[0]),
            patch.object(watch.time, "sleep", side_effect=sleep) as sleeps,
            patch("builtins.print") as output,
        ):
            result = watch.main(argv)
        events = [json.loads(call.args[0]) for call in output.call_args_list]
        return result, live.call_count, sleeps.call_args_list, events

    def test_transient_reads_end_only_pass_then_fresh_pass_enqueues(self):
        provider_detail = "provider-detail-not-for-output"
        github_error = watch.GitHubReadError(
            provider_detail,
            result=watch.github_api.ApiResult(
                ok=False, status=503, body=None,
                failure=watch.github_api.FailureDetail(
                    cause="network_provider_failure", message=provider_detail,
                    retryable=True, fallback_eligible=False, disposition="retry",
                ),
            ),
            diagnostics={},
        )
        for error in (
            urllib.error.HTTPError(f"https://{provider_detail}.invalid", 503, provider_detail, Message(), None),
            urllib.error.URLError(provider_detail),
            TimeoutError(provider_detail),
            ConnectionResetError(provider_detail),
            github_error,
        ):
            for stage in ("initialize", "scan", "before_write"):
                with self.subTest(error=type(error).__name__, stage=stage):
                    failed = Fake(count=1)
                    healthy = Fake(count=1)
                    if stage == "scan":
                        failed.pages = Mock(side_effect=error)
                    elif stage == "before_write":
                        def before(io):
                            if io.calls == 2:
                                raise error

                        failed.before = before
                    factory = Mock(side_effect=[
                        error if stage == "initialize" else failed, healthy,
                    ])
                    result, calls, sleeps, events = self.run_watch(factory)
                    self.assertEqual(result, 0)
                    self.assertEqual(calls, 2)
                    self.assertEqual([call.args[0] for call in sleeps], [4, 4])
                    self.assertEqual(failed.writes, [])
                    self.assertEqual(healthy.writes, [1])
                    self.assertEqual(
                        sum(event["event"] == "pass_failed" for event in events), 1,
                    )
                    self.assertEqual(events[-1]["payload"]["outcome"], "deadline")
                    self.assertNotIn(provider_detail, json.dumps(events))

    def test_retry_sleep_is_bounded_by_deadline(self):
        result, calls, sleeps, events = self.run_watch(
            Mock(side_effect=TimeoutError()), duration=2,
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps[0].args, (2,))
        self.assertEqual(events[-1]["payload"]["outcome"], "deadline")
        self.assertEqual(events[-1]["payload"]["completed_passes"], 0)
        self.assertEqual(events[-1]["payload"]["failed_passes"], 1)

    def test_shared_read_errors_without_status_recover_on_fresh_pass(self):
        for message in (
            'Get "https://private.invalid": EOF',
            "error connecting to api.github.com",
            "invalid character '<' looking for beginning of value",
        ):
            with self.subTest(message=message):
                provider_result = watch.github_api.legacy_process_result(
                    1, "", message, operation="github.pr.view", is_write=False,
                    failed_step="enqueue_read",
                )
                # Exercise the real reader's exception, not a hand-made cause.
                reader = watch.GitHubReader(gh_cmd="gh", actor="test-bot", expected_actor="test-bot")
                with patch.object(reader, "_transport_request", return_value=provider_result):
                    with self.assertRaises(watch.GitHubReadError) as caught:
                        reader.get_json("/repos/example/tool/pulls", step="enqueue_read")
                failed = Fake(count=1)
                failed.pages = Mock(side_effect=caught.exception)
                healthy = Fake(count=1)
                result, calls, sleeps, events = self.run_watch(Mock(side_effect=[failed, healthy]))
                self.assertEqual((result, calls), (0, 2))
                self.assertEqual([call.args[0] for call in sleeps], [4, 4])
                self.assertEqual(failed.writes, [])
                self.assertEqual(healthy.writes, [1])
                self.assertEqual(events[0]["event"], "pass_failed")
                self.assertEqual(events[0]["payload"]["cause"], "GitHubReadError")
                self.assertNotIn(message, json.dumps(events))

    def test_shared_read_error_after_label_dispatch_never_replays(self):
        provider_result = watch.github_api.legacy_process_result(
            1, "", 'Get "https://private.invalid": EOF',
            operation="github.pr.view", is_write=False, failed_step="enqueue_read",
        )
        error = watch.GitHubReadError("private", result=provider_result, diagnostics={})
        for stage in ("dispatch", "readback", "later_read"):
            with self.subTest(stage=stage):
                fake = Fake()
                if stage == "dispatch":
                    fake.label = Mock(side_effect=error)
                elif stage == "readback":
                    adapter = object.__new__(watch.Live)
                    adapter.deadline = 10_000
                    adapter.pages = Mock(side_effect=error)
                    fake.label = adapter.label
                else:
                    def before(io):
                        if io.writes:
                            raise error

                    fake.before = before
                with patch.object(
                    watch.github_api, "call_gh_with_retry", return_value=api_result("confirmed"),
                ) as write:
                    result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(write.call_count, int(stage == "readback"))
                self.assertEqual(fake.writes, [1] if stage == "later_read" else [])
                self.assertEqual([event["event"] for event in events if event["event"] == "pass_failed"], [])
                self.assertEqual(events[-1]["payload"]["cause"], "GitHubReadError")

    def test_once_reports_failed_read_without_retry_or_success(self):
        result, calls, sleeps, events = self.run_watch(
            Mock(side_effect=urllib.error.URLError("private")), once=True,
        )
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual([event["event"] for event in events], ["pass_failed"])

    def test_nontransient_read_failures_still_stop(self):
        for error in (
            urllib.error.HTTPError("https://private.invalid", 403, "private", Message(), None),
            FileNotFoundError("private"),
            ValueError("private"),
            watch.GitHubReadShapeError("private"),
        ):
            with self.subTest(error=type(error).__name__):
                result, calls, sleeps, events = self.run_watch(
                    Mock(side_effect=error),
                )
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(events[-1]["payload"]["outcome"], "error")
                self.assertNotIn("private", json.dumps(events))

    def test_shared_cli_timeout_recovers_but_setup_and_auth_failures_stop(self):
        for message, expected in (
            ("i/o timeout", True),
            ("connection reset by peer", True),
            ("GitHub CLI execution failed (OSError)", False),
            ("Failed to launch GitHub CLI (FileNotFoundError)", False),
            ("error: incomplete GitHub App configuration; set GITHUB_APP_ID "
             "GITHUB_APP_INSTALLATION_ID GITHUB_APP_PRIVATE_KEY_PATH", False),
            ("error: reader fallback requires the configured main GitHub App", False),
            ("error: GitHub App authentication helper returned an invalid response; "
             "refusing GitHub command", False),
            ("error: gh-with-env-token requires uv or GH_WITH_ENV_TOKEN_PYTHON", False),
            ("/path/gh-with-env-token: line 45: gh: command not found", False),
            ("unknown flag: --include", False),
            ("Traceback (most recent call last):\nKeyError: app_id", False),
            ("permission denied", False),
            ("bad credentials", False),
        ):
            with self.subTest(message=message):
                error = watch.GitHubReadError(
                    "read failed", diagnostics={},
                    result=watch.github_api.legacy_process_result(
                        1, "", message, operation="github.pr.view", is_write=False,
                        failed_step="enqueue_read",
                    ),
                )
                healthy = Fake(count=1)
                result, calls, sleeps, events = self.run_watch(Mock(side_effect=[error, healthy]))
                self.assertEqual(result, 0 if expected else 1)
                self.assertEqual(calls, 2 if expected else 1)
                self.assertEqual(healthy.writes, [1] if expected else [])
                self.assertEqual(len(sleeps), 2 if expected else 0)
                self.assertEqual(events[0]["event"], "pass_failed" if expected else "stop")

    def test_shared_cli_launch_and_execution_errors_stop(self):
        for failure in (FileNotFoundError(), PermissionError(), OSError()):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(watch.github_api.subprocess, "run", side_effect=failure):
                    provider_result = watch.github_api.call_gh(
                        "GET", "/repos/example/tool/pulls", gh_cmd="gh",
                        actor="test-bot", expected_actor="test-bot",
                        operation="github.pr.view", is_write=False,
                        failed_step="enqueue_read", conditional_cache=False,
                    )
                reader = watch.GitHubReader(gh_cmd="gh", actor="test-bot", expected_actor="test-bot")
                with patch.object(reader, "_transport_request", return_value=provider_result):
                    with self.assertRaises(watch.GitHubReadError) as caught:
                        reader.get_json("/repos/example/tool/pulls", step="enqueue_read")
                result, calls, sleeps, events = self.run_watch(Mock(side_effect=caught.exception))
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(events[-1]["payload"]["outcome"], "error")

    def test_controller_read_transport_failure_recovers_without_retrying_rejections(self):
        provider_detail = "provider-detail-not-for-output"
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        request = {"repository": REPO, "base_branch": TARGET["base"], "mutate": False}
        for code, http_status, expected in (
            ("provider_unavailable", None, True),
            ("client_timeout", None, True),
            ("service_error", 503, True),
            ("authorization_denied", 403, False),
            ("unsafe_redirect", None, False),
        ):
            with self.subTest(code=code):
                payload = watch.operator.unavailable_payload(
                    operation="merge-train-controller-run-once", request=request,
                    status="unavailable", code=code, message=provider_detail,
                )
                if http_status is not None:
                    payload["summary"]["http_status"] = http_status
                failed = Fake(count=1)
                failed.train = adapter.train
                healthy = Fake(count=1)
                with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=json.dumps(payload), stderr=provider_detail,
                )):
                    result, calls, sleeps, events = self.run_watch(Mock(side_effect=[failed, healthy]))
                self.assertEqual(result, 0 if expected else 1)
                self.assertEqual(calls, 2 if expected else 1)
                self.assertEqual(len(sleeps), 2 if expected else 0)
                self.assertEqual(healthy.writes, [1] if expected else [])
                self.assertNotIn(provider_detail, json.dumps(events))

    def test_actual_controller_denial_projection_stops_even_with_server_status(self):
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        request = {"repository": REPO, "base_branch": TARGET["base"], "mutate": False}
        for status in (401, 403, 500):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    "https://example.invalid", status, "denied", Message(),
                    stdlib_io.BytesIO(json.dumps({"error": {"code": "authorization_denied"}}).encode()),
                )
                self.addCleanup(error.close)
                payload = watch.operator.summarize_http_error(
                    operation="merge-train-controller-run-once", request=request, exc=error,
                )
                fake = Fake(count=1)
                fake.train = adapter.train
                with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=json.dumps(payload), stderr="private",
                )):
                    result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(fake.writes, [])
                self.assertEqual(events[-1]["payload"]["cause"], watch.WatchError.__name__)

    def test_transport_error_during_label_dispatch_never_replays(self):
        fake = Fake(count=1)
        fake.label = Mock(side_effect=ConnectionResetError("private"))
        result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(fake.label.call_count, 1)
        self.assertEqual(events[-1]["payload"]["outcome"], "error")

    def test_controller_invalid_or_unavailable_read_recovers(self):
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        for stdout, returncode in (("not JSON", 1), ("[]", 0), ("{}", 1)):
            with self.subTest(stdout=stdout, returncode=returncode):
                failed = Fake(count=1)
                failed.train = adapter.train
                healthy = Fake(count=1)
                with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], returncode, stdout=stdout, stderr="private provider body",
                )):
                    result, calls, sleeps, events = self.run_watch(Mock(side_effect=[failed, healthy]))
                self.assertEqual((result, calls), (0, 2))
                self.assertEqual(len(sleeps), 2)
                self.assertEqual(failed.writes, [])
                self.assertEqual(healthy.writes, [1])
                self.assertEqual(events[0]["event"], "pass_failed")
                self.assertNotIn("private provider body", json.dumps(events))

    def test_missing_admin_configuration_recovers_on_fresh_initialization(self):
        live_class = watch.Live
        for settings in (
            {"service_url": "https://example.invalid", "token": ""},
            {"service_url": "", "token": ""},
            {"service_url": "", "token": "test-token"},
            ValueError("invalid_config"),
            ValueError("invalid_env_config"),
        ):
            with self.subTest(settings=settings):
                healthy = Fake(count=1)

                def factory(_deadline):
                    if factory_mock.call_count == 1:
                        with (
                            patch.object(watch.operator, "resolve_settings", side_effect=[settings]),
                            patch.object(watch.operator, "validate_service_url"),
                        ):
                            return live_class(_deadline)
                    return healthy

                factory_mock = Mock(side_effect=factory)
                result, calls, sleeps, events = self.run_watch(factory_mock)
                self.assertEqual((result, calls), (0, 2))
                self.assertEqual(len(sleeps), 2)
                self.assertEqual(healthy.writes, [1])
                self.assertEqual(events[0]["event"], "pass_failed")

    def test_missing_label_readback_and_policy_change_stop(self):
        fake = Fake(count=1)
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        adapter.pages = Mock(return_value=[])
        fake.label = adapter.label
        with patch.object(watch.github_api, "call_gh_with_retry", return_value=api_result("confirmed")) as write:
            result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(write.call_count, 1)
        self.assertEqual(adapter.pages.call_count, 1)
        self.assertEqual(events[-1]["payload"]["outcome"], "error")
        fake = Fake(count=1)
        fake.policies = Mock(side_effect=[[TARGET], []])
        result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(fake.writes, [])
        self.assertEqual(events[-1]["payload"]["cause"], watch.WatchError.__name__)

    def test_controller_read_recovery_respects_once_and_deadline(self):
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        for once in (True, False):
            with self.subTest(once=once):
                fake = Fake(count=1)
                fake.train = adapter.train
                with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout="{}", stderr="private provider body",
                )):
                    result, calls, sleeps, events = self.run_watch(lambda _deadline: fake, once=once, duration=2)
                self.assertEqual((result, calls), (int(once), 1))
                self.assertEqual([call.args[0] for call in sleeps], [] if once else [2])
                self.assertEqual(fake.writes, [])
                self.assertEqual(events[0]["payload"]["cause"], watch.LaunchplaneReadError.__name__)
                if not once:
                    self.assertEqual(events[-1]["payload"]["failed_passes"], 1)

    def test_controller_read_failure_after_label_stops_without_replay(self):
        fake = Fake()
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        original_train = fake.train

        def train(target):
            return adapter.train(target) if fake.writes else original_train(target)

        fake.train = train
        with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, stdout="{}", stderr="private provider body",
        )):
            result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(fake.writes, [1])
        self.assertEqual(events[-1]["event"], "stop")
        self.assertEqual(events[-1]["payload"]["cause"], watch.LaunchplaneReadError.__name__)

    def test_uncertain_label_write_or_failed_readback_stops_without_replay(self):
        for certainty in ("unknown", "confirmed"):
            with self.subTest(certainty=certainty):
                fake = Fake(count=1)
                adapter = object.__new__(watch.Live)
                adapter.deadline = 10_000
                adapter.pages = Mock(side_effect=urllib.error.URLError("private"))
                fake.label = adapter.label
                with patch.object(
                    watch.github_api, "call_gh_with_retry", return_value=api_result(certainty),
                ) as write:
                    result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(write.call_count, 1)
                self.assertEqual(adapter.pages.call_count, int(certainty == "confirmed"))
                self.assertEqual(events[-1]["payload"]["outcome"], "error")

    def test_transient_read_after_confirmed_label_stops_remaining_batch(self):
        fake = Fake()

        def before(io):
            if io.writes:
                raise ConnectionResetError("private")

        fake.before = before
        result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(fake.writes, [1])
        self.assertEqual(events[-1]["payload"]["outcome"], "error")


if __name__ == "__main__":
    unittest.main()
