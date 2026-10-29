# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy

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
        row = item(action, f"Source handoff; Branch: {branch}")
        record = {"worker": "source-worker", "session": "source-session", "branch": branch, "claimed_at": "2026-10-28T18:00:00Z"}
        row["discussion"]["comments"] = [
            {"id": 10, "author": "bot", "created_at": record["claimed_at"], "body": claim.marker(record)},
            {"id": 11, "author": "bot", "created_at": "2026-10-28T19:00:00Z", "body": f"Released claim 10\n\n{release}"},
        ]
        observed = evidence()
        observed["inventory"]["remote_branches"] = [branch]
        result = classify(row, observed)
        assert result["state"] == "underway", result
        handoff = next(entry for entry in result["ownership_evidence"] if entry.get("release_id") == 11)
        assert handoff["branch"] == branch and handoff["release_id"] == 11
        assert handoff["next_action"]
        sentence = copy.deepcopy(row)
        sentence["discussion"]["body"] = sentence["discussion"]["body"].replace(f"Branch: {branch}", f"Branch: {branch}.")
        assert classify(sentence, observed)["state"] == "underway"
        other_branch = copy.deepcopy(row)
        other_branch["discussion"]["body"] = other_branch["discussion"]["body"].replace(
            f"Branch: {branch}", f"Branch: {branch}-other"
        )
        assert classify(other_branch, observed)["state"] != "underway"
        local_only = copy.deepcopy(observed)
        local_only["inventory"]["remote_branches"] = []
        local_only["inventory"]["local_branches"] = [branch]
        assert classify(row, local_only)["state"] != "underway"
        disposed = copy.deepcopy(observed)
        disposed["inventory"]["remote_branches"] = []
        assert classify(row, disposed)["state"] != "underway"
        independent = copy.deepcopy(row)
        independent["discussion"]["body"] = item("Implement the next independent parser.")["discussion"]["body"]
        assert classify(independent, observed)["state"] == "available"
        for adoption_action in [
            "Any agent adopts the released branch.",
            "An agent adopts the released branch.",
            "The next worker integrates source.",
            "Whoever resumes integrates source.",
            "Implement the remaining parser.",
            "Rebase on main and open the PR.",
            "Fix CI and open a PR.",
            "Add tests, then land.",
            "The next session integrates source.",
            "A successor adopts the branch.",
            "Someone opens the PR.",
        ]:
            adoptable = copy.deepcopy(row)
            adoptable["discussion"]["comments"][-1]["body"] = f"Released claim 10\n\nPushed WIP.\nNext action: {adoption_action}"
            assert classify(adoptable, observed)["state"] != "underway", adoption_action
            adoptable["discussion"]["comments"][-1]["body"] = "Released claim 10\n\nPushed WIP."
            adoptable["discussion"]["body"] = item(adoption_action, f"Branch: {branch}")["discussion"]["body"]
            assert classify(adoptable, observed)["state"] != "underway", adoption_action


if __name__ == "__main__":
    test_released_no_pr_source_stays_with_its_integrator()
    print("retained source handoff tests passed")
