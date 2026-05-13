# GitHub Plan Config Schema

Planning config lives in `.github/github.json` under the `planning` key for
repo-local policy, and in `~/.code/github-planning.json` for workspace defaults.
Repo-local values override workspace defaults.

```json
{
  "defaultBranch": "main",
  "qualityGate": {
    "test": {
      "default": "npm test"
    }
  },
  "importantWorkflows": ["CI"],
  "jetbrains": {
    "ide": "IntelliJ IDEA",
    "mainWorktreePath": "~/Developer/repo",
    "openProjectPath": ".",
    "worktreeStrategy": "prefer-current",
    "scopePreference": "changed_files"
  },
  "healthUrls": [],
  "relatedRepos": [],
  "metadataFreshness": {
    "updateWhen": ["validation gates change", "important workflows change"]
  },
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

## Workflow Metadata

Repo workflow metadata is stored in top-level keys in `.github/github.json`.
The planning helper reads the nested `planning` object, while snapshot and
closeout helpers summarize the broader repo workflow metadata.

Common top-level keys:

- `defaultBranch`: repo default branch expected by agents.
- `docs`: important repo documentation and routing references.
- `qualityGate`: local and CI commands that establish readiness for this repo.
- `importantWorkflows`: GitHub Actions workflows agents should watch closely.
- `qaLabels` and `deployLabels`: labels that change QA or deploy behavior.
- `healthUrls`: product, lane, or deploy health endpoints relevant to readiness.
- `relatedRepos`: repos agents should consider during cross-repo work.
- `jetbrains`: preferred IDE inspection target when it is not obvious. Use
  `ide` for the macOS app name, `mainWorktreePath` for the canonical checkout
  path when linked worktrees exist, `openProjectPath` for the repo-relative path
  to open, `worktreeStrategy` for current-worktree safety, and
  `scopePreference` for the default inspection scope.
- `githubSignals`: post-merge and security/quality signal expectations.
- `cleanup`: repo-local cleanup policy for merged branches and worktrees.
- `metadataFreshness`: events that should trigger metadata review.

Rules:

- Keep labels fixed and small. Do not generate dynamic labels from arbitrary
  user prose.
- Do not duplicate machine-owned source-of-truth data, such as package
  manifests, Odoo addon manifests, dependency lists, or generated inventories,
  into `.github/github.json`. Prefer routing pointers to canonical files and
  let manifests own dependency and inventory facts.
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
