# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compatibility adapter for pure waits and repository automation identities."""
from __future__ import annotations

try:
    import github_identity
    from github_plan_status.waits import (PREFIX, current, current_record, live_lines, marker,
                                          records, reviewed, validate, without_records)
except ModuleNotFoundError:
    from skills.github.scripts import github_identity
    from skills.github.scripts.github_plan_status.waits import (PREFIX, current, current_record,
        live_lines, marker, records, reviewed, validate, without_records)


def authors(repo: str, *, expected_actor: str | None = None) -> set[str]:
    return set(github_identity.configured_bot_logins()) | {
        name for name in (github_identity.automation_login(), expected_actor, repo.split("/")[0]) if name}
