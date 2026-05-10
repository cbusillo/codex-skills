# GitHub Plan Config Schema

Planning config may live in `.github/github.json` under the
`planning` key for repo-local policy, and in `~/.code/github-planning.json` for
workspace defaults. Repo-local values override workspace defaults.

For compatibility, helpers also read legacy `.github/github-repo-workflow.json`
when `.github/github.json` is absent.

Helpers read `~/.code/github-planning.json` first and then fall back to the
legacy `~/.code/githubning.json` filename when the new file is absent.

```json
{
  "planning": {
    "labels": {
      "plan": "plan",
      "active": "plan:active",
      "blocked": "plan:blocked",
      "stale": "plan:stale",
      "done": "plan:done"
    },
    "label_defs": {
      "plan": {
        "color": "5319e7",
        "description": "Durable planning issue"
      }
    },
    "default_sections": [
      "Finish Line",
      "Current Status",
      "Relationships",
      "Acceptance Criteria",
      "Open Questions"
    ],
    "projects": {
      "enabled": true,
      "owner": "OWNER",
      "default_project": null
    },
    "workflow": {
      "default_manager": "@manager-login",
      "repo_managers": {
        "OWNER/REPO": "@repo-manager-login"
      }
    },
    "project_fields": {
      "focus": "Focus",
      "manager": "Manager",
      "finish_line": "Finish Line"
    }
  }
}
```

Rules:

- Keep labels fixed and small. Do not generate dynamic labels from arbitrary
  user prose.
- Use native GitHub dependencies and sub-issues as canonical relationships.
- Projects are views. Do not require Project writes for planning to work.
- LaunchPlane is not a published config surface in this version. If a future
  release adds LaunchPlane settings, document the supported keys here and wire
  them into `gh-plan.py` at the same time.
- Put cross-repo defaults in the workspace config, not in a single repo.
- Project fields should reduce human lostness. Prefer `Focus`, `Manager`, and
  `Finish Line`; avoid duplicating the whole issue body into fields.
- Use `workflow.repo_managers` for repo-specific human ownership. Fall back to
  `workflow.default_manager` only when a repo has no specific manager.
- Live manager routing belongs in `~/.code/github-planning.json`; keep this
  reference generic so ownership changes do not require doc edits.
