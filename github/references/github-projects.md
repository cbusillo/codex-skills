# GitHub Projects & Roadmaps

GitHub Projects are optional view layers over issue data. Use them to prioritize
work, track focus, and visualize roadmap dates.

## Focus States

Use the `Focus` field to indicate the current priority of a plan:

- **Now**: The single thing the user and agent are actively trying to finish.
  Prefer at most one `Now` item.
- **Next**: Ready to be picked up after the current `Now` item is done.
- **Waiting**: Blocked or awaiting an external decision/event.
- **Later**: Real work but intentionally out of focus.

## Manager Routing

The `Manager` field should hold the human owner or reviewer. Resolve this from:
- `~/.code/githubning.json` (`workflow.default_manager` or `workflow.repo_managers`)
- Repository instructions or `AGENTS.md`.

## Roadmap Dates

Roadmap dates are planning anchors, not hard commitments.

- **Now**: Set `Roadmap Start` to today (or the actual start date); set
  `Roadmap Target` to a realistic finish window.
- **Next**: Set near-term dates only when picking up soon.
- **Waiting**: Date only if the blocker has a known revisit window.
- **Later**: Leave blank unless intentionally scheduled.

Prefer week or month anchors (e.g., "End of Q2") over specific days when the
exact date would be artificial.

## Field Synchronization

Do not duplicate the entire issue body into Project fields. Keep Project
fields (like `Finish Line`) compact and observable.
