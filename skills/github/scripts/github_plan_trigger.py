# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

try:
    from github_plan_status.events import PREFIX, INDEX_LABEL, FIELDS, TARGET, date, version, validate, describe, marker, current, fired
except ModuleNotFoundError:
    from skills.github.scripts.github_plan_status.events import PREFIX, INDEX_LABEL, FIELDS, TARGET, date, version, validate, describe, marker, current, fired
