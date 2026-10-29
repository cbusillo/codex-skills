# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import hashlib
import json
from pathlib import Path

from capacity_retry import save_state


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def changes(rows: list[dict], path: Path, scope: list[str], *, key: str, complete: bool) -> list[dict]:
    previous = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(previous, dict):
        raise ValueError("check state must be an object")
    before = previous.get("fingerprints", {}) if previous.get("scope") == scope else {}
    if not isinstance(before, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in before.items()):
        raise ValueError("check fingerprints must be an object of strings")
    fingerprints = {row[key]: fingerprint(row) for row in rows}
    selected = [
        row
        for row in rows
        if before.get(row[key]) != fingerprints[row[key]]
        or row.get("action")
        or row.get("error")
        or row.get("unread")
        or row.get("read") is False
    ]
    if complete:
        selected.extend({key: identity, "removed": True} for identity in before.keys() - fingerprints.keys())
    else:
        fingerprints = {**before, **fingerprints}
    path.parent.mkdir(parents=True, exist_ok=True)
    save_state(path, {"scope": scope, "fingerprints": fingerprints})
    return selected
