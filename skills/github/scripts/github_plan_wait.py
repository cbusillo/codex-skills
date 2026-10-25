# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compatibility adapter for pure waits and repository automation identities."""
from __future__ import annotations

try:
    import github_identity
    from github_plan_status.waits import (PREFIX, current, current_record, live_lines, marker,
                                          records, resolve, reviewed, validate, without_records, trusted_authors, author_is_trusted, release_event_hint)
except ModuleNotFoundError:
    from skills.github.scripts import github_identity
    from skills.github.scripts.github_plan_status.waits import (PREFIX, current, current_record,
        live_lines, marker, records, resolve, reviewed, validate, without_records, trusted_authors, author_is_trusted, release_event_hint)


def authors(repo: str, *, expected_actor: str | None = None) -> set[str]:
    return trusted_authors(repo.split("/")[0], configured_bots=github_identity.configured_bot_logins(),
                           automation=github_identity.automation_login(), expected_actor=expected_actor)
