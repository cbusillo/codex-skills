#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from typing import Any
from unittest import mock


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "github_workflow_babysit",
    SCRIPT_DIR / "github_workflow_babysit.py",
)
assert SPEC and SPEC.loader
workflow_babysit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow_babysit
SPEC.loader.exec_module(workflow_babysit)


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FakeWorkflowClient:
    repo = "example/repo"

    def __init__(
        self,
        *,
        runs: Sequence[workflow_babysit.RunSnapshot],
        pending: Sequence[Sequence[workflow_babysit.PendingEnvironment]] = (),
        jobs: Sequence[Sequence[workflow_babysit.JobSnapshot]] = (),
        actors: tuple[str, str] = ("automation-bot", "human-owner"),
        approved_environment_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.runs = list(runs)
        self.pending = [tuple(value) for value in pending]
        self.jobs = [tuple(value) for value in jobs]
        self.automation_login, self.reviewer_login = actors
        self.approved_environment_ids = approved_environment_ids
        self.approvals: list[tuple[int, tuple[int, ...], str]] = []
        self.dispatch_calls: list[dict[str, Any]] = []
        self.pending_calls = 0
        self.approval_history_calls = 0
        self.job_calls = 0

    def resolve_actors(self) -> tuple[str, str]:
        return self.automation_login, self.reviewer_login

    def dispatch(
        self,
        *,
        workflow: str,
        ref: str,
        inputs: Mapping[str, str | int | float | bool],
    ) -> workflow_babysit.RunReference:
        self.dispatch_calls.append({"workflow": workflow, "ref": ref, "inputs": dict(inputs)})
        return workflow_babysit.RunReference(
            run_id=self.runs[0].run_id,
            run_url=self.runs[0].run_url,
        )

    def get_run(self, run_id: int) -> workflow_babysit.RunSnapshot:
        value = self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
        assert value.run_id == run_id
        return value

    def get_pending_environments(
        self,
        run_id: int,
    ) -> tuple[workflow_babysit.PendingEnvironment, ...]:
        del run_id
        self.pending_calls += 1
        return self.pending.pop(0) if self.pending else ()

    def get_approved_environment_ids(self, run_id: int) -> frozenset[int]:
        del run_id
        self.approval_history_calls += 1
        return self.approved_environment_ids

    def get_jobs(self, run_id: int) -> tuple[workflow_babysit.JobSnapshot, ...]:
        del run_id
        self.job_calls += 1
        return self.jobs.pop(0) if self.jobs else ()

    def approve_environments(
        self,
        run_id: int,
        environment_ids: Sequence[int],
        comment: str,
    ) -> None:
        self.approvals.append((run_id, tuple(environment_ids), comment))

    def diagnostics(self) -> dict[str, Any]:
        return {}


def run_snapshot(
    status: str,
    *,
    conclusion: str | None = None,
    triggering_actor: str = "automation-bot",
    run_attempt: int = 1,
) -> workflow_babysit.RunSnapshot:
    return workflow_babysit.RunSnapshot(
        run_id=123,
        run_url="https://github.com/example/repo/actions/runs/123",
        status=status,
        conclusion=conclusion,
        event="workflow_dispatch",
        actor=triggering_actor,
        triggering_actor=triggering_actor,
        head_branch="main",
        head_sha="a" * 40,
        run_attempt=run_attempt,
    )


def pending_environment(
    *,
    can_approve: bool,
    name: str = "protected-admin",
    reviewers: tuple[dict[str, str], ...] = (
        {"type": "User", "identity": "human-owner"},
    ),
    wait_timer: int = 0,
) -> workflow_babysit.PendingEnvironment:
    return workflow_babysit.PendingEnvironment(
        environment_id=77,
        name=name,
        current_user_can_approve=can_approve,
        wait_timer_minutes=wait_timer,
        wait_timer_started_at=None,
        reviewers=reviewers,
    )


class WorkflowBabysitterTests(unittest.TestCase):
    def test_waiting_approvable_environment_is_approved_and_completes(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting"), run_snapshot("completed", conclusion="success")],
            pending=[(pending_environment(can_approve=True),)],
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.dispatch_and_watch(
            workflow="operator.yml",
            ref="main",
            inputs={"mode": "dry_run"},
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "completed_success")
        self.assertEqual(client.approvals, [(123, (77,), "Reviewed protected action.")])
        self.assertEqual(result["approvals"][0]["environments"], ["protected-admin"])
        self.assertEqual(client.pending_calls, 1)
        self.assertEqual(client.approval_history_calls, 1)
        self.assertEqual(client.job_calls, 0)

    def test_already_submitted_environment_is_not_approved_twice(self) -> None:
        pending = pending_environment(can_approve=True)
        client = FakeWorkflowClient(
            runs=[
                run_snapshot("waiting"),
                run_snapshot("waiting"),
                run_snapshot("completed", conclusion="success"),
            ],
            pending=[(pending,), (pending,)],
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "completed_success")
        self.assertEqual(client.approvals, [(123, (77,), "Reviewed protected action.")])
        self.assertEqual(client.pending_calls, 2)
        self.assertEqual(client.approval_history_calls, 1)

    def test_existing_review_history_prevents_duplicate_approval_after_resume(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting")],
            pending=[(pending_environment(can_approve=True),), (pending_environment(can_approve=True),)],
            approved_environment_ids=frozenset({77}),
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=5,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "bounded_timeout")
        self.assertEqual(client.approvals, [])
        self.assertEqual(client.approval_history_calls, 1)

    def test_self_review_denial_stops_after_first_waiting_poll(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting", triggering_actor="human-owner")],
            pending=[(pending_environment(can_approve=False),)],
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "self_review_denied")
        self.assertEqual(result["polls"], 1)
        self.assertEqual(clock.value, 0)
        self.assertEqual(client.approvals, [])

    def test_ineligible_reviewer_stops_without_polling_to_timeout(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting")],
            pending=[(pending_environment(can_approve=False),)],
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "reviewer_not_eligible")
        self.assertEqual(result["polls"], 1)
        self.assertEqual(clock.value, 0)

    def test_runner_queue_is_distinct_and_times_out_boundedly(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting")],
            pending=[(), (), ()],
            jobs=[
                (
                    workflow_babysit.JobSnapshot(
                        name="operator",
                        status="queued",
                        conclusion=None,
                        runner_name=None,
                    ),
                )
            ]
            * 3,
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset(),
            approval_comment="Reviewed protected action.",
            timeout_seconds=10,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "bounded_timeout")
        self.assertEqual(result["last_diagnosis"]["category"], "runner_queued")
        self.assertEqual(result["last_diagnosis"]["pending_deployments"], "none")
        self.assertEqual(result["elapsed_seconds"], 10)

    def test_waiting_without_pending_deployment_is_reported(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting")],
            pending=[(), ()],
            jobs=[(), ()],
        )
        clock = ManualClock()
        babysitter = workflow_babysit.WorkflowBabysitter(
            client,
            clock=clock.now,
            sleep=clock.sleep,
        )

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset(),
            approval_comment="Reviewed protected action.",
            timeout_seconds=5,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "bounded_timeout")
        self.assertEqual(
            result["last_diagnosis"]["category"],
            "waiting_without_pending_deployment",
        )

    def test_pending_approval_requires_exact_operator_authorization(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting")],
            pending=[(pending_environment(can_approve=True),)],
        )
        babysitter = workflow_babysit.WorkflowBabysitter(client)

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset(),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "protected_environment_approval_required")
        self.assertEqual(client.approvals, [])

    def test_approvable_run_must_have_been_dispatched_by_automation_actor(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting", triggering_actor="human-owner")],
            pending=[(pending_environment(can_approve=True),)],
        )
        babysitter = workflow_babysit.WorkflowBabysitter(client)

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "protected_workflow_dispatch_actor_mismatch")
        self.assertEqual(client.approvals, [])

    def test_missing_triggering_actor_blocks_protected_approval(self) -> None:
        base = run_snapshot("waiting")
        run = workflow_babysit.RunSnapshot(
            run_id=base.run_id,
            run_url=base.run_url,
            status=base.status,
            conclusion=base.conclusion,
            event=base.event,
            actor="automation-bot",
            triggering_actor=None,
            head_branch=base.head_branch,
            head_sha=base.head_sha,
            run_attempt=base.run_attempt,
        )
        client = FakeWorkflowClient(
            runs=[run],
            pending=[(pending_environment(can_approve=True),)],
        )
        babysitter = workflow_babysit.WorkflowBabysitter(client)

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "protected_workflow_dispatch_actor_mismatch")
        self.assertEqual(client.approvals, [])

    def test_protected_rerun_requires_new_dispatch(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting", run_attempt=2)],
            pending=[(pending_environment(can_approve=True),)],
        )
        babysitter = workflow_babysit.WorkflowBabysitter(client)

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "protected_workflow_rerun_unsupported")
        self.assertEqual(client.approvals, [])

    def test_same_dispatch_and_reviewer_identity_blocks_before_dispatch(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("queued")],
            actors=("same-user", "same-user"),
        )
        babysitter = workflow_babysit.WorkflowBabysitter(client)

        result = babysitter.dispatch_and_watch(
            workflow="operator.yml",
            ref="main",
            inputs={},
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "self_review_identity_conflict")
        self.assertEqual(client.dispatch_calls, [])

    def test_same_dispatch_and_reviewer_identity_blocks_watch_approval(self) -> None:
        client = FakeWorkflowClient(
            runs=[run_snapshot("waiting", triggering_actor="same-user")],
            pending=[(pending_environment(can_approve=True),)],
            actors=("same-user", "same-user"),
        )
        babysitter = workflow_babysit.WorkflowBabysitter(client)

        result = babysitter.watch(
            run_id=123,
            run_url=None,
            authorized_environments=frozenset({"protected-admin"}),
            approval_comment="Reviewed protected action.",
            timeout_seconds=30,
            poll_interval_seconds=5,
        )

        self.assertEqual(result["outcome"], "self_review_identity_conflict")
        self.assertEqual(client.approvals, [])


class GitHubWorkflowClientTests(unittest.TestCase):
    def test_dispatch_uses_current_api_and_exact_returned_run(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_call(
            method: str,
            path: str,
            body: Any = None,
            **kwargs: Any,
        ) -> Any:
            calls.append({"method": method, "path": path, "body": body, **kwargs})
            if path == "/user":
                response_body = {"login": "automation-bot"}
            else:
                response_body = {
                    "workflow_run_id": 123,
                    "run_url": "https://api.github.com/repos/example/repo/actions/runs/123",
                    "html_url": "https://github.com/example/repo/actions/runs/123",
                }
            return workflow_babysit.github_api_core.ApiResult(
                ok=True,
                status=200,
                body=response_body,
                operation=kwargs.get("operation"),
            )

        client = workflow_babysit.GitHubWorkflowClient(
            "example/repo",
            expected_automation_login="automation-bot",
        )
        with mock.patch.object(
            workflow_babysit.github_api_core,
            "call_gh_with_retry",
            side_effect=fake_call,
        ):
            reference = client.dispatch(
                workflow="operator.yml",
                ref="main",
                inputs={"mode": "dry_run"},
            )

        self.assertEqual(reference.run_id, 123)
        dispatch_call = calls[-1]
        self.assertEqual(dispatch_call["method"], "POST")
        self.assertEqual(dispatch_call["api_version"], "2026-03-10")
        self.assertEqual(dispatch_call["operation"], "github.workflow.dispatch")
        self.assertNotIn("runs?", dispatch_call["path"])

    def test_dispatch_without_exact_run_response_fails_without_list_lookup(self) -> None:
        calls: list[str] = []

        def fake_call(method: str, path: str, body: Any = None, **kwargs: Any) -> Any:
            del method, body, kwargs
            calls.append(path)
            response_body = {"login": "automation-bot"} if path == "/user" else None
            return workflow_babysit.github_api_core.ApiResult(
                ok=True,
                status=204 if path != "/user" else 200,
                body=response_body,
            )

        client = workflow_babysit.GitHubWorkflowClient(
            "example/repo",
            expected_automation_login="automation-bot",
        )
        with mock.patch.object(
            workflow_babysit.github_api_core,
            "call_gh_with_retry",
            side_effect=fake_call,
        ):
            with self.assertRaises(workflow_babysit.WorkflowBabysitError):
                client.dispatch(workflow="operator.yml", ref="main", inputs={})

        self.assertEqual(len(calls), 2)
        self.assertFalse(any("/runs?" in path for path in calls))

    def test_reviewer_calls_explicitly_clear_automation_tokens(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_call(
            method: str,
            path: str,
            body: Any = None,
            **kwargs: Any,
        ) -> Any:
            del method, body
            calls.append({"path": path, **kwargs})
            return workflow_babysit.github_api_core.ApiResult(
                ok=True,
                status=200,
                body={"login": "human-owner"},
            )

        client = workflow_babysit.GitHubWorkflowClient("example/repo")
        with mock.patch.object(
            workflow_babysit.github_api_core,
            "call_gh_with_retry",
            side_effect=fake_call,
        ):
            self.assertEqual(client._resolve_reviewer_login(), "human-owner")

        prefix = calls[0]["gh_prefix_args"]
        self.assertIn("GH_TOKEN", prefix)
        self.assertIn("GITHUB_TOKEN", prefix)
        self.assertIn("CODEX_GITHUB_TOKEN", prefix)
        self.assertEqual(prefix[-1], workflow_babysit.ACTIVE_GH)

    def test_json_input_values_are_never_needed_for_terminal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "inputs.json"
            path.write_text('{"reason":"sensitive-value","mode":"dry_run"}', encoding="utf-8")
            inputs = workflow_babysit.load_json_inputs(path)

        self.assertEqual(sorted(inputs), ["mode", "reason"])

    def test_dispatch_rejects_more_than_twenty_five_inputs(self) -> None:
        with self.assertRaisesRegex(
            workflow_babysit.WorkflowBabysitError,
            "at most 25 inputs",
        ):
            workflow_babysit.validate_inputs({f"input_{index}": "value" for index in range(26)})

    def test_argument_errors_use_terminal_json_contract(self) -> None:
        with mock.patch("builtins.print") as print_mock:
            exit_code = workflow_babysit.main([])

        self.assertEqual(exit_code, 2)
        rendered = print_mock.call_args.args[0]
        self.assertIn('"outcome": "invalid_arguments"', rendered)
    def test_non_finite_timeout_and_poll_interval_are_rejected(self) -> None:
        for option, value in (
            ("--timeout-seconds", "nan"),
            ("--timeout-seconds", "inf"),
            ("--poll-interval-seconds", "nan"),
            ("--poll-interval-seconds", "inf"),
        ):
            args = workflow_babysit.parse_args(
                [
                    "watch",
                    "--repo",
                    "example/repo",
                    "--run-id",
                    "123",
                    option,
                    value,
                ]
            )
            with self.assertRaises(workflow_babysit.WorkflowBabysitError):
                workflow_babysit.validate_runtime_arguments(args)


if __name__ == "__main__":
    unittest.main()
