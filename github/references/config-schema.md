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
  "launchplane": {
    "enabled": true,
    "service": {
      "contextUrlEnv": "LAUNCHPLANE_CONTEXT_URL",
      "operatorUrlEnv": "LAUNCHPLANE_OPERATOR_URL",
      "localConfigExample": "launchplane/references/launchplane-operator.local.example.json"
    },
    "context": {
      "enabled": true,
      "helper": "launchplane/scripts/launchplane-context.py"
    },
    "operator": {
      "enabled": true,
      "helper": "launchplane/scripts/launchplane-write-action.py",
      "requiresPrivateConfig": true
    },
    "mergeTrain": {
      "enabled": true,
      "controller": true,
      "readyLabel": "ready-to-merge",
      "baseBranch": "main",
      "githubActionsRunner": {
        "repo": "OWNER/launchplane",
        "workflow": "merge-train-runner.yml",
        "ref": "main",
        "runnerMode": "controller",
        "mutateDefault": false
      }
    }
  },
  "githubSettings": {
    "expected": {
      "deleteBranchOnMerge": true
    }
  },
  "cleanup": {
    "deleteMergedLocalBranches": true,
    "removeMergedCleanWorktrees": true,
    "commands": [
      {
        "name": "git status",
        "command": "git status --short --branch",
        "when": "routine",
        "description": "Confirm the checkout is clean before closeout."
      },
      {
        "name": "prune generated caches",
        "command": "rm -rf .cache/example-generated",
        "when": "explicit",
        "description": "Example cold cleanup; run only when intentionally requested."
      }
    ],
    "handoffArtifacts": {
      "temporaryGlobs": ["handoff*.md", "*-handoff.md"],
      "durableSurface": "GitHub issue or PR comment"
    }
  },
  "metadataFreshness": {
    "updateWhen": ["validation gates change", "important workflows change"]
  },
  "planning": {
    "labels": {
      "plan": "plan",
      "active": "plan:active",
      "blocked": "plan:blocked",
      "waiting": "plan:waiting",
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
      "default_manager": "person:manager-id",
      "repo_managers": {
        "OWNER/REPO": "person:repo-manager-id"
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
  Concrete app-facing URLs may be committed in private implementation repos when
  they are intentionally repo-facing operational metadata.
- `relatedRepos`: repos agents should consider during cross-repo work.
- `prWorkflow`: repo-specific PR workflow hints such as whether a green,
  mergeable PR is only readiness evidence, whether explicit user approval is
  required before merge, and which watch skill/helper should monitor long PR
  fix trains.
- `release`: repo-specific release intent and batching metadata. Use this for
  facts such as which file or workflow expresses release intent, release
  metadata files, immediate-release triggers, and defer/batch conditions. Do not
  put repository-specific release semantics in global skills.
- `launchplane`: public-safe routing metadata for Launchplane context,
  operator, and merge-train surfaces. It may name environment variable names,
  helper paths, workflow names, labels, local config examples, and expected
  capabilities. It must not contain tokens, cookies, secret values, concrete
  Launchplane service URLs, private credential paths, provider payloads, or
  plaintext runtime configuration. Do not treat app, preview, deploy, or
  health-check URLs as Launchplane service URLs when they are owned by the repo's
  runtime contract; keep those in `healthUrls` or operations docs, and avoid
  duplicating Launchplane-managed lane coordinates in this block.
- `jetbrains`: preferred IDE inspection target when it is not obvious. Use
  `ide` for the macOS app name, `mainWorktreePath` for the canonical checkout
  path when linked worktrees exist, `openProjectPath` for the repo-relative path
  to open, `worktreeStrategy` for current-worktree safety, and
  `scopePreference` for the default inspection scope.
- `githubSignals`: post-merge and security/quality signal expectations.
- `githubSettings`: expected GitHub repository settings that snapshot helpers
  report as `ok`, `warning`, or `unavailable` without silently mutating.
- `cleanup`: repo-local closeout cleanup policy. Boolean fields may authorize
  routine branch/worktree hygiene, `commands` may list named cleanup or audit
  commands, and `handoffArtifacts` may describe temporary local handoff files
  that must be migrated to the durable surface before deletion.
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
- Launchplane repo metadata is routing, not authorization. Context helpers may
  use it to discover helper paths and the names of service URL environment
  variables, while write-capable helpers still source concrete service URLs and
  credentials only from private operator config, environment variables, GitHub
  Actions OIDC, or signed-in Launchplane UI sessions.
- Concrete product, app, preview, deploy, and health-check URLs are allowed when
  they are intentionally part of a private implementation repo's operational
  contract. This exception does not apply to Launchplane service/operator/context
  URLs, trace URLs, provider payloads, copied runtime evidence, or private
  control-plane topology.
- Omit `launchplane` or set `launchplane.enabled` to `false` for repos that do
  not use Launchplane. Snapshot and readiness helpers should treat missing,
  disabled, unavailable, or unauthorized Launchplane access as reportable state,
  not as permission to bypass Launchplane with direct provider mutation.
- Put cross-repo defaults in the workspace config, not in a single repo.
- Project fields should reduce human lostness. Prefer `Focus`, `Manager`, and
  `Finish Line`; avoid duplicating the whole issue body into fields.
- Use `workflow.repo_managers` for repo-specific human ownership. Fall back to
  `workflow.default_manager` only when a repo has no specific manager.
- Manager values may be raw Project field values, GitHub handles, or explicit
  `person:<id>` references resolved through the `people` skill's private local
  `.local/people.yaml` contract when available. Raw values are never rewritten
  through people context. Resolved people use Project field labels, preferring
  `preferred_reference` and then `display_name`; unresolved `person:<id>` values
  are skipped rather than written literally.
- Treat `cleanup.commands[].when == "routine"` as ordinary closeout evidence:
  agents may run or report the command during closeout. Treat any other value,
  such as `explicit`, `cold`, or `aggressive`, as report-only unless the user or
  repo guidance explicitly asks for that cleanup.
- Keep recovery-critical handoff content in the owning GitHub issue or PR
  comment for GitHub-backed work. Local files matching configured temporary
  handoff globs are scratch unless intentionally committed as part of a PR.
- Live manager routing belongs in `~/.code/github-planning.json`; keep this
  reference generic so ownership changes do not require doc edits.
