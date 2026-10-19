# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""CLI close adapter: reuse planning projection without selecting another actor."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


def reconcile(*, repo: str, number: int, request: Callable[..., tuple[str | None, Any]],
              actor: str | None, gh_cmd: str, expected_actor: str | None, operation: str,
              issue_core: Any, apply: bool = True, include_dependents: bool = True) -> dict[str, Any]:
    name = "_github_plan_confirmed_close"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("gh-plan.py"))
    assert spec is not None and spec.loader is not None
    plan = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = plan
    try:
        spec.loader.exec_module(plan)
        plan.github_issue_core = issue_core
        # All provider reads use the original close transport, including Python
        # fixtures. The projection never calls gh-plan's actor-selecting run_raw.
        def bound_request(method, path, body=None, **kwargs):
            try:
                return request(method, path, body, **kwargs)
            except plan.github_issue_core.IssueError as provider_error:
                raise plan.PlanError(str(provider_error), failure=provider_error.failure,
                                     api_result=provider_error.api_result, payload=provider_error.payload) from provider_error
        plan.api_json = bound_request
        plan.comment_route = lambda: (actor, gh_cmd, expected_actor)
        plan.CURRENT_OPERATION = operation
        plan.EXPECTED_ACTOR = expected_actor
        # The workstation radar is an additional landing-adapter gate. Generic
        # close uses its confirmed closure and complete structured wait reads.
        plan.planning_comment_attention = lambda *_args: False
        try:
            return plan.refresh_planning_targets([(repo, number)], apply=apply,
                include_dependents=include_dependents, config_optional_unless_planning=True)
        except (plan.PlanError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
            return plan.github_plan_landing.discovery_failure(plan, repo, number, error)

    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
