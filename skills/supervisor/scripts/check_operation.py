# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import json
import subprocess
from pathlib import Path

from capacity_retry import save_state


class GateFailed(ValueError):
    def __init__(self, gate: str, reason: str) -> None:
        super().__init__(reason)
        self.gate = gate


class Operation:
    def __init__(self, evidence: Path, identity: list[str]) -> None:
        self.evidence = evidence.resolve()
        self.evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.evidence / "operation.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"identity": identity, "steps": {}}
        if (
            not isinstance(self.state, dict)
            or self.state.get("identity") != identity
            or not isinstance(self.state.get("steps"), dict)
        ):
            raise GateFailed("identity", "evidence belongs to another operation")

    def run(self, gate: str, command: list[str], *, once: bool = False, timeout: int = 120) -> dict | list:
        saved = self.state["steps"].get(gate)
        if once and saved:
            if saved.get("complete") is not True:
                raise GateFailed(gate, "previous mutation requires reconciliation; inspect evidence before any retry")
            return saved["result"]
        if once:
            self.state["steps"][gate] = {"complete": False}
            save_state(self.path, self.state)
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        save_state(
            self.evidence / (gate + ".json"), {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        )
        if result.returncode:
            raise GateFailed(gate, f"helper exited {result.returncode}; inspect {gate}.json")
        try:
            payload = json.loads(result.stdout)
        except ValueError as error:
            raise GateFailed(gate, "helper returned malformed evidence") from error
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
