# GitHub Plan Config Schema

Planning config lives in `.github/github.json` under the `planning` key for
repo-local policy, and in `github-planning.json` under the runtime home for
workspace defaults: `$CODE_HOME`, then `$CODEX_HOME`, then `~/.code` when it
contains a `skills/` directory, otherwise `~/.codex`.
Repo-local values override workspace defaults.

```json
{
  "defaultBranch": "main",
  "docs": {
    "executionEnvironment": "github/references/execution-environment.md"
  },
  "qualityGate": {
    "inspection": {
      "prepare": {
        "python": {
          "version": "3.12",
          "moduleName": "example-project",
          "testRoots": ["tests"],
          "sync": true,
          "extras": ["dev"],
          "requiredGeneratedState": [".venv", ".idea"]
        }
      },
      "scopePreference": "changed_files",
      "lanes": [
        {
          "id": "jvm",
          "ide": "IntelliJ IDEA",
          "required": true,
          "include": ["**/*.kt", "**/*.java", "**/*.gradle", "**/*.gradle.kts"],
          "exclude": ["test-fixtures/inspection-red-lane-*/**"]
        },
        {
          "id": "python",
          "ide": "PyCharm",
          "required": true,
          "projectPath": "services/python",
          "include": ["services/python/**/*.py"],
          "exclude": ["test-fixtures/inspection-red-lane-*/**"]
        }
      ]
    },
    "test": {
      "local": "npm test -- --runTestsByPath <changed-test-paths>",
      "ci": "npm test -- --ci"
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

## Label Taxonomy

Planning labels and the `audit` label are the only labels created by
`gh-plan.py ensure-labels` by default. They describe durable issue-backed plans, not transient PR execution
state. Before assigning a planning status label, read github-plan's
[Status Labels](../../github-plan/SKILL.md#status-labels).

| Label          | Purpose                                                                                                                                                        |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `plan`         | Durable planning issue.                                                                                                                                        |
| `audit`        | Question or completed work for the weekly direction audit.                                                                                                     |

Workflow labels are repo-local opt-ins. Agents may recognize these names, but
helpers should not create them unless repo metadata documents them.

| Label            | Declare In                                                | Meaning                                                                                          |
| ---------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `awaiting-qa`    | `qaLabels`                                                | Optional repo-local manual QA handoff for concrete implementation or bug work.                   |
| `preview-ready`  | `deployLabels` or repo workflow docs                      | A preview environment is available for review; this is not QA approval.                          |
| Merge-train enqueue label | Launchplane active merge-policy read | Service-configured readiness signal; still requires fresh checks and existing merge authorization. |

Avoid generic labels such as `waiting`, `blocked`, `ready`, or `qa` unless the
repo documents a narrow convention for them. Prefer the namespaced `plan:*`
labels for durable planning state and repo-local `qaLabels` / `deployLabels` for
execution workflow state.

Label audit checklist:

- Run `gh label list -R OWNER/REPO --limit 500` and inspect labels that overlap
  with `plan:waiting`, `plan:blocked`, QA, preview, or merge readiness.
- Run `gh issue list -R OWNER/REPO --state open --label LABEL` before retiring
  or renaming a suspicious label; treat zero-open-use labels as candidates for
  review, not automatic deletion.
- Run `gh pr list -R OWNER/REPO --state open --json number,title,labels` to see
  whether planning labels are being used on PRs or whether PR workflow labels
  are undocumented in `.github/github.json`.
- If a repo uses `awaiting-qa`, list it in `qaLabels`. If it uses
  `preview-ready`, list it in `deployLabels` or repo workflow docs. If it uses
  a merge-train enqueue label, resolve it from Launchplane's active policy;
  do not duplicate that authority in repository metadata. Other merge conventions
  belong in repository workflow docs.
- Run `uv run scripts/gh-plan.py --repo OWNER/REPO ensure-labels` from the
  `github` skill directory only for
  durable planning labels. Do not use it to invent PR workflow labels.

## Workflow Metadata

Repo workflow metadata is stored in top-level keys in `.github/github.json`.
The planning helper reads the nested `planning` object, while snapshot and
closeout helpers summarize the broader repo workflow metadata.

Common top-level keys:

- `defaultBranch`: repo default branch expected by agents.
- `projectType`: the kind of repository, shown in snapshot summaries.
- `docs`: important repo documentation and routing references.
  `docs.openOperationalIssues` names the repository's operational follow-up
  register; the direction audit's
  [stranded-work sweep](../../direction/references/stranded-work.md) reports
  its overdue entries and those waiting on the Director.
- `qualityGate`: local and CI commands that establish readiness for this repo.
  `qualityGate.docsRequiredWhen` lists changes that need a docs update; the
  `repo-readiness` skill checks it.
- `qualityGate.modelReview.requiredPaths`: path patterns (`fnmatch`, where `*`
  also matches `/`) for changes that need a
  [review by another model](../../references/model-review.md). `gh-pr.py ready`
  and the Supervisor's auto-enqueue read it from the base branch and refuse a PR
  that touches a match until an explicit weighed-outcome record identifies its
  current full head SHA (see [ready handoffs](cli-reference.md#ready-handoffs)).
  Without the key, a posted request still needs a later weighed record for the
  current head. Every push invalidates the earlier record; a new disposition
  is needed even there. A new model run follows review policy/task requirements,
  rather than being required merely to update the head-bound record.
- `qualityGate.test.local`: focused local test command string. Select the tests
  covering the changed behavior; replace any documented placeholders before
  running. Run required lint, type checks and IDE inspection alongside it.
  Declaring `local` and `ci` adopts the split; a catalog update alone does not
  migrate repositories. Adopt only when the full-suite CI gate exists.
- `qualityGate.test.ci`: full-suite command string required in CI before merge
  or a `ready` handoff. The complete suite runs only in CI. Safety, authorization,
  approval-rule, destructive-helper and shared-infrastructure changes run
  focused tests plus the full test modules for the safety areas they touch
  locally, such as authorization, secrets, recovery or credentials. Broaden
  local coverage when risk or missing coverage warrants it.
  If required local modules cannot execute, record the missing evidence;
  green CI does not silently waive that local requirement.
  Metadata describes the gate; the repository's CI workflow must actually run
  and require it. Verify passing checks for the current PR head.
  Use the adopting repository's documented change areas to classify changes
  and select affected safety modules, rather than classifying every edit by
  the repository's name.
  Without a documented module mapping, identify complete modules exercising
  the touched safety area through its callers; record any coverage gap as
  missing evidence.
- `qualityGate.test.default`: legacy test command, used until both `test.local`
  and `test.ci` are declared. An incomplete split does not override legacy test
  requirements. Preserve its declared scope until the repository adopts the split;
  do not silently reinterpret an existing full gate as focused. To adopt,
  replace `default` with `local` and `ci` and reconcile repository instructions,
  testing docs and CI. For unset metadata fields, use repository instructions,
  docs, CI and manifests; report missing required gates as missing evidence.
- `importantWorkflows`: GitHub Actions workflows agents should watch closely.
- `qaLabels`: repo-local QA handoff labels such as `awaiting-qa`. These are for
  concrete implementation or bug work waiting on manual verification, not for
  durable plan parking.
- `deployLabels`: repo-local preview, deploy, or environment-readiness labels
  such as `preview-ready`. These are evidence for workflow state, not approval
  to merge.
- `healthUrls`: optional non-authoritative readiness probes for repos whose
  readiness checks are truly repo-owned. Do not use this field for
  Launchplane-managed product, lane, deploy, provider, tenant, or health-check
  coordinates; those values belong in the Launchplane service/API, admin UI,
  managed records, or scoped admin input.
- `relatedRepos`: repos agents should consider during cross-repo work.
- `prWorkflow`: repo-specific PR workflow hints such as whether a green,
  mergeable PR is only readiness evidence, whether explicit user approval is
  required before merge, and which watch skill/helper should monitor long PR
  fix trains. The explicit repository-owner settings planner consumes
  `mergePolicy.method` (`merge`, `squash`, or `rebase`) to select one allowed
  merge method; this metadata does not authorize a settings write.
- `release`: repo-specific release intent and batching metadata. Use this for
  facts such as which file or workflow expresses release intent, release
  metadata files, immediate-release triggers, and defer/batch conditions. Do not
  put repository-specific release semantics in global skills.
- `launchplane`: public-safe routing metadata for Launchplane context,
  admin, and merge-train surfaces. It may name environment variable names,
  helper paths, local config examples, and expected
  capabilities. It must not contain tokens, cookies, secret values, concrete
  Launchplane service URLs, private credential paths, provider payloads,
  product/runtime endpoints, or plaintext runtime configuration.
  The snapshot can display legacy `product`, `publicName`, and string `context`
  fields from existing metadata under `launchplane.routing`. That read-only
  compatibility projection does not authorize storing or refreshing real
  Launchplane-managed product/context coordinates in repository metadata.
  Resolve current values from Launchplane service records, the API/admin UI,
  or explicit scoped admin input under the
  [Runtime Authority Boundary](../../launchplane/SKILL.md#runtime-authority-boundary).
  Generic catalog helper configuration uses an object `context` with `enabled`
  and `helper`; the snapshot displays it under `launchplane.context`. Displaying
  either shape does not enable a helper or grant runtime authority.
  Enrollment and enqueue routing come from the active Launchplane policy:
  `uv run skills/launchplane/scripts/launchplane-write-action.py merge-train-policy-read --repo OWNER/REPO`
  (from the catalog root). The snapshot reads it in both text and JSON mode
  only when Launchplane configuration is present or uncertain. Its local
  `github_launchplane.py` check considers repository routing metadata,
  environment keys beginning `LAUNCHPLANE_` (including empty settings), and
  presence of `~/.config/launchplane/` (including an empty directory), without
  opening private files or reading credential values. Unreadable configuration
  presence or a nonexistent/redirected home directory stays uncertain: absence is checked
  only in the user's native home, independently of harness home overrides.
  Its `launchplane.mergeTrain` summary has `status` (`enrolled`,
  `not_enrolled`, `not_configured` or `unknown`), nullable `enabled`, per-branch `targets`
  with `baseBranch` and `readyLabel`, and policy revision evidence.
  `not_configured` has `source: local_configuration`, `enabled: false`, empty
  targets and no policy or failure reason: no configuration was found on this
  host. The GitHub skill's [Merging](../SKILL.md#merging) procedure checks the
  Director's overall direction, global/repository instructions and task context
  before choosing the protected-branch PR route for a Director without
  Launchplane. Any declared train requirement still makes missing host
  configuration unknown routing. Repository metadata absence alone cannot
  establish this state. The sibling `launchplane.status: no_routing_metadata`
  describes only missing repository metadata, never the landing route.
  For a configured Director, missing settings, denial, failed reads and
  malformed responses mean `unknown`; they never authorize a different merge path. Raw `config.data`
  remains routing metadata. Do not store a `launchplane.mergeTrain` block in
  repository config: enrollment, base branches and enqueue labels belong to
  the service policy. The snapshot ignores legacy copies. The
  [merge-train reference](../../launchplane/references/merge-train.md) describes
  controller invocation and required landing evidence. Launchplane's own
  [merge-train policy documentation](https://github.com/cbusillo/launchplane/blob/main/docs/merge-train-policy.md)
  describes its Actions runner entrypoints. Repository `docs` may point to
  those sources; do not copy runner defaults or response-field paths into config.
- `jetbrains`: preferred IDE inspection target when it is not obvious. Use
  `ide` for the macOS app name, `mainWorktreePath` for the canonical checkout
  path when linked worktrees exist, `openProjectPath` for the repo-relative path
  to open, `worktreeStrategy` for current-worktree safety, and
  `scopePreference` for the default inspection scope.
- `qualityGate.inspection`: repository readiness policy for JetBrains checks.
  Use `ide`, `profile`, and `scopePreference` for the required target, named
  inspection profile, and scope. Optional `prepare` may be an exact idempotent
  command string or a structured `python` object. Structured Python preparation
  resolves the skill-owned project helper, creates a worktree-local `.venv` and
  `.idea` model. Use `sync` for a root Python project or `pep723Scripts` for
  standalone scripts; [inspection configuration](../../jetbrains-inspection/references/inspection-config.md)
  owns the dependency-source options, bounds, conflicts and receipt validation.
  `projectPath` inside the Python object prepares an existing nested project
  without replacing the root JVM model; its generated and input paths become
  relative to that directory. The Python lane uses the same `projectPath`. Put
  `requiredGeneratedState` inside the structured `python` object; command-string
  preparation retains the outer sibling field for backward compatibility.
  `agent-inspect`, `inspect`, and `inspect-closeout` execute preparation in the
  exact target worktree before IDE lifecycle open/claim, but only when the
  worktree is under a configured trusted root. Generated-state paths are a
  bounded array of repository-relative paths that must exist before a successful
  preparation can proceed or be reused from its durable receipt.
  The helper executes the validated argv without a shell, applies a dedicated
  timeout, records a bounded receipt, and fails closed on nonzero exit, timeout,
  tracked worktree mutation, hidden index mutation, missing generated state,
  recursion, or untrusted roots. Use `--skip-preparation` only after running
  the command manually, and `--force-preparation` to refresh a valid receipt.
  For mixed-language repositories, use ordered `lanes` instead of `ide`.
  Each lane requires a unique `id`, an `ide`, and a non-empty `include` array;
  `required` defaults to `true`, `exclude` defaults to an empty array, and an
  optional repository-relative `projectPath` routes the lane to a nested IDE
  project. Selected files must remain inside that project directory.
  Patterns are repository-relative POSIX globs. The first lane whose `include`
  matches owns the file; a matching `exclude` removes that file from ordinary
  changed-file, directory, or whole-project readiness instead of allowing it to
  fall through to a later lane. An explicit `files` scope still assigns the
  file to its lane and records the exclusion override so deliberate red-lane
  smoke fixtures remain runnable. The helper records excluded and unmatched
  selected files, skips empty lanes without
  opening an IDE, executes non-empty lanes sequentially with exact `files`
  scopes, and aggregates required lanes as `RED`, then `UNKNOWN`, then `GREEN`.
  Optional-lane failures remain visible but do not override required-lane
  semantics. When `lanes` is absent, the existing single-IDE `ide` behavior is
  unchanged.
- `githubSignals`: post-merge and security/quality signal expectations.
- `githubSettings`: expected GitHub repository settings that snapshot helpers
  report as `ok`, `warning`, or `unavailable` without silently mutating.
  The explicit [verified repository-owner settings planner](cli-reference.md#verified-repository-owner-administration)
  consumes the existing `expected.deleteBranchOnMerge` boolean plus
  `prWorkflow.mergePolicy.method`. Other REST merge flags can be supplied
  in an explicit admin request; they are not new snapshot expectation keys.
  Config expectations do not authorize administrative writes.
- `cleanup`: repo-local closeout cleanup policy. Boolean fields may authorize
  routine branch/worktree hygiene, `commands` may list named cleanup or audit
  commands, and `handoffArtifacts` may describe temporary local handoff files
  that must be migrated to the durable surface before deletion.
- `metadataFreshness`: events that should trigger metadata review.

A local `.github/github.override.json`, when present, is deep-merged over
`.github/github.json` by `scripts/github-repo-snapshot.sh`.

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
  credentials only from private admin config, environment variables, GitHub
  Actions OIDC, or signed-in Launchplane UI sessions.
- Launchplane-managed product, app, preview, deploy, provider, lane, tenant, and
  health-check coordinates are runtime authority. Do not commit them to
  `.github/github.json`, workflow defaults, product repos, or operations docs as
  real values; use Launchplane service records, the API/admin UI, managed
  secrets, or explicit scoped admin input.
- Omit `launchplane` or set `launchplane.enabled` to `false` for repos that do
  not use Launchplane. Snapshot and readiness helpers should treat missing,
  disabled, unavailable, or unauthorized Launchplane access as reportable state,
  not as permission to bypass Launchplane with direct provider mutation.
- Put cross-repo defaults in the workspace config, not in a single repo.
- Projects display automatic issue metadata and native hierarchy. Do not maintain
  Focus, Manager, Finish Line, Roadmap Start, or Roadmap Target during planning.
- `project_fields` names support explicitly requested field edits. Legacy
  `workflow.repo_managers` and `workflow.default_manager` are accepted for config
  compatibility but no longer populate Project Manager on issue creation.
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
- Keep private people references in local configuration; this reference stays
  generic so identity changes do not require doc edits.
