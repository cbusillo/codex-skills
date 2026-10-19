"""Pure selection of current source-control workflow executions for one head."""

from typing import Any, Optional


def workflow_execution_identity(run: dict[str, Any]) -> Optional[tuple[Any, ...]]:
    numbers = [run.get(field) for field in ("id", "workflow_id", "run_number", "run_attempt", "check_suite_id")]
    repository_id = (run.get("head_repository") or {}).get("id")
    valid_numbers = all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                        for value in [*numbers, repository_id])
    if not valid_numbers or not run.get("head_sha") or not run.get("event") or not run.get("head_branch"):
        return None
    identity = (run["head_sha"], run["workflow_id"], run["event"], run["head_branch"], repository_id)
    pull_targets = tuple(sorted(
        (pull.get("number"), (pull.get("base") or {}).get("ref"),
         ((pull.get("base") or {}).get("repo") or {}).get("id"))
        for pull in run.get("pull_requests", [])
    ))
    identity += (pull_targets,)
    # Dispatches can use different inputs on the very same head.
    if run["event"] not in {"push", "pull_request", "pull_request_target"}:
        identity += (run["id"],)
    return identity


def current_workflow_runs(runs: list[dict[str, Any]], head_sha: str) -> dict[str, Any]:
    """Separate execution history only with complete Actions identity.

    Workflow names and update times are not identities: an old cancellation
    can finish after its replacement starts. Run numbers order executions;
    attempts order reruns of the same execution.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    current = []
    for run in runs:
        if not isinstance(run, dict) or run.get("head_sha") != head_sha:
            continue
        key = workflow_execution_identity(run)
        if key is None:
            current.append(run)
            continue
        groups.setdefault(key, []).append(run)
    superseded = []
    for group in groups.values():
        group = [candidate for index, candidate in enumerate(group) if candidate not in group[:index]]
        winner = max(group, key=lambda candidate: (candidate["run_number"], candidate["id"], candidate["run_attempt"]))
        winner_started = str(winner.get("run_started_at") or "")
        later_attempts = [candidate for candidate in group
                          if candidate["run_attempt"] > 1 and winner_started
                          and str(candidate.get("run_started_at") or "") > winner_started]
        if later_attempts:
            winner = max(later_attempts, key=lambda candidate: candidate["run_started_at"])
        current.append(winner)
        for candidate in group:
            if candidate is winner:
                continue
            uncertain_attempt = candidate["run_attempt"] > 1 and (
                not candidate.get("run_started_at") or not winner.get("run_started_at")
            )
            if candidate["id"] != winner["id"] and (candidate.get("status") != "completed" or uncertain_attempt):
                current.append(candidate)
            else:
                superseded.append(candidate)
    return {"current": current, "superseded": superseded}
