# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import release_session_claims as release
from capacity_retry import save_state

if TYPE_CHECKING:
    from skills.github.scripts import github_identity
    from skills.github.scripts import github_plan_closure as closure
    from skills.github.scripts import github_plan_native as native
    from skills.github.scripts import github_plan_release as claim_release
    from skills.github.scripts.github_plan_sessions import observation_homes
    from skills.github.scripts.github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args
else:
    import github_identity
    import github_plan_closure as closure
    import github_plan_native as native
    import github_plan_release as claim_release
    from github_plan_sessions import observation_homes
    from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args


__all__ = [
    "release",
    "closure",
    "native",
    "claim_release",
    "github_identity",
    "observation_homes",
    "GitHubReader",
    "GitHubReadError",
    "GitHubReadShapeError",
    "automation_only_gh_prefix_args",
    "Operation",
    "GateFailed",
]


class GateFailed(ValueError):
    def __init__(self, gate: str, reason: str) -> None:
        super().__init__(reason)
        self.gate = gate


class Operation:
    def __init__(self, evidence: Path, identity: list[str]) -> None:
        self.evidence = evidence.resolve()
        self.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.evidence / "operation.json"
        self.gate = "identity"
        state = json.loads(self.path.read_text()) if self.path.exists() else {"identity": identity, "steps": {}}
        if not isinstance(state, dict) or state.get("identity") != identity or not isinstance(state.get("steps"), dict):
            raise GateFailed("identity", "evidence belongs to another target or launch generation")
        self.state: dict[str, Any] = state

    def run(self, gate: str, command: list[str], *, once: bool = False, timeout: int = 120) -> dict:
        result = self._run(gate, command, once=once, timeout=timeout)
        if not isinstance(result, dict):
            raise GateFailed(gate, "helper result must be an object")
        return result

    def rows(self, gate: str, command: list[str], *, once: bool = False, timeout: int = 120) -> list[dict]:
        result = self._run(gate, command, once=once, timeout=timeout)
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise GateFailed(gate, "helper result must be an array of objects")
        return result

    def _run(self, gate: str, command: list[str], *, once: bool, timeout: int) -> dict | list:
        self.gate = gate
        saved = self.state["steps"].get(gate)
        if once and saved and not saved.get("retryable"):
            if saved.get("complete") is not True:
                raise GateFailed(gate, "previous mutation requires reconciliation; inspect evidence before any retry")
            return saved["result"]
        if once:
            self.state["steps"][gate] = {"complete": False}
            save_state(self.path, self.state)
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            save_state(self.evidence / (gate + ".json"), {"error": str(error), "mutation_uncertain": once})
            raise GateFailed(gate, f"{type(error).__name__}; inspect {gate}.json") from error
        save_state(
            self.evidence / (gate + ".json"), {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        )
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            payload = None
        if result.returncode:
            if once and isinstance(payload, dict) and payload.get("mutation_started") is False:
                self.state["steps"][gate] = {"complete": False, "retryable": True}
                save_state(self.path, self.state)
            raise GateFailed(gate, f"helper exited {result.returncode}; inspect {gate}.json")
        if not isinstance(payload, (dict, list)):
            raise GateFailed(gate, "helper result must be an object or array")
        if once:
            self.state["steps"][gate] = {"complete": True, "result": payload}
            save_state(self.path, self.state)
        return payload

    def receipt(self, identity: str, error: GateFailed | None = None) -> dict:
        return {
            "ok": error is None,
            "identity": identity,
            "gate": error.gate if error else "complete",
            "reason": str(error) if error else None,
            "evidence": str(self.evidence),
        }
