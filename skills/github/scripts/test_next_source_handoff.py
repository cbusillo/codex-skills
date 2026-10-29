# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import github_plan_claim as claim
from test_github_next_queue import classify, evidence, item


def test_released_no_pr_source_stays_with_its_integrator() -> None:
    for branch, action, release in [
        (
            "work/opw358-1028",
            "ODOO20LEAD2-1027 integrates the five-path scoped range into canonical323.",
            "Source handoff to ODOO20LEAD2-1027. Pushed source; retain branch until integration.",
        ),
        (
            "work/opw361-1028",
            "ODOO20LEAD2-1027 consumes only the scoped commits into its canonical workflow.",
            "Source qualification is delivered on published branch. No retained PR. Keep the published branch until adoption.",
        ),
        (
            "work/1194-plain-tmdb",
            "Lead replaces TMDB on the item page with plain words",
            "Built and pushed on branch (no PR yet).\nNext action: the lead opens the PR when no other consistency PR is open.",
        ),
    ]:
        row = item(action, f"Source handoff; Branch: {branch}", actor="agent:integrator")
        record = {"worker": "source-worker", "session": "source-session", "branch": branch, "claimed_at": "2026-10-28T18:00:00Z"}
        row["discussion"]["comments"] = [
            {"id": 10, "author": "bot", "created_at": record["claimed_at"], "body": claim.marker(record)},
            {"id": 11, "author": "bot", "created_at": "2026-10-28T19:00:00Z", "body": f"Released claim 10\n\n{release}"},
        ]
        observed = evidence()
        observed["inventory"]["remote_branches"] = [branch]
        assert classify(row, observed)["state"] == "underway"
        row["discussion"]["body"] = item(action)["discussion"]["body"]
        assert classify(row, observed)["state"] == "available"


if __name__ == "__main__":
    test_released_no_pr_source_stays_with_its_integrator()
    print("retained source handoff tests passed")
