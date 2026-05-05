# Repo Workflow Metadata Schema

Repo-specific, non-secret defaults may live in `.github/github-repo-workflow.json`. This keeps shared GitHub/deploy expectations with the repository while avoiding `AGENTS.md` bloat.

Before writing this file in a repo, show the proposed JSON and ask for approval for that specific repo. If the repo has an `upstream` remote, is a fork, or is used as an overlay, ask whether the config should be committed before writing it. For shared repos whose behavior is validated by consuming repos, confirm the `validatedThrough` list with the user.

Config fields are advisory unless the main skill says otherwise. All fields are optional except `defaultBranch` when the repo default is known.

## Fields

- `defaultBranch`: string branch name such as `main` or `master`.
- `importantWorkflows`: array of GitHub Actions workflow display names to watch. Prefer workflows that affect readiness, deploy, preview, cleanup, or shared environment state. Exclude reminder-only, notification-only, or auxiliary tooling workflows unless their failure should change the repo status answer.
- `qaLabels`: labels that mean human QA/review state.
- `deployLabels`: labels that trigger or influence deploys, previews, or shared environments. Changing these labels requires approval.
- `healthUrls`: array of URLs or `{ "name": string, "url": string }` objects.
- `relatedRepos`: repo slugs or local repo names that commonly move with this repo.
- `validatedThrough`: repo slugs or local repo names that validate this repo's changes, useful for shared addon or overlay repos.
- `projectType`: short advisory role label such as `control-plane`, `frontend`, `python-package`, or `shared-addons`. Do not use it as a substitute for repo docs.
- `docs`: object of repo-relative docs routing paths. Common keys include `index`, `architecture`, `operations`, `records`, `style`, and `policies`; values may be strings or arrays of strings.
- `qualityGate`: optional object describing how to run repo gates. Global skills decide that tests, lint/static analysis, IDE inspection, and docs freshness matter; JSON describes commands and routing.
  - `test.default`: primary test command to run when no narrower gate applies.
  - `test.targeted`: optional command template for targeted tests.
  - `lint.default`: whole-repo lint/static-analysis command.
  - `lint.changedFiles`: optional changed-file command template using `{files}`.
  - `format.check`: optional format-check command.
  - `typecheck.default`: optional whole-repo typecheck command.
  - `build.default`: optional build command.
  - `inspection`: optional IDE/static-inspection routing such as `tool`, `ide`, `openProjectPath`, and `scopePreference`.
  - `docsRequiredWhen`: array of change categories that require docs freshness checks.
- `jetbrains`: optional object describing JetBrains IDE/inspection expectations for closeout workflows.
  - `ide`: app name such as `PyCharm`, `IntelliJ IDEA`, or `WebStorm`.
  - `required`: boolean indicating whether JetBrains inspections are part of the normal closeout gate.
  - `openProjectPath`: repo-relative path to open when the IDE project is not already open; defaults to `.`.
  - `inspectionPreference`: human-readable policy such as `changed_files_then_git_scope` or `changed_files_then_whole_project`.
- `cleanup.deleteMergedLocalBranches`: when true, safe merged local branch deletion is allowed; when false or absent, report candidates instead.
- `cleanup.removeMergedCleanWorktrees`: when true, safe merged clean worktree removal is allowed; when false or absent, report candidates instead.
- `metadataFreshness.updateWhen`: array of change categories that should trigger review of this JSON, such as docs routing, validation gates, primary commands, important workflows, health endpoints, repo relationships, cleanup policy, or ownership boundaries.
- `githubSignals`: optional object describing post-merge and GitHub security/quality signal routing.
  - `postMerge.waitForActions`: whether post-merge target/default-branch Actions should be waited on before full closeout.
  - `postMerge.checkSecurityAndQuality`: whether to inspect available GitHub security/quality signals.
  - `capabilities`: per-signal capability hints. Use `available`, `not_enabled`, `unavailable_private_repo`, `unavailable_permissions`, `unsupported`, or `unknown`.
  - `refreshWhen`: conditions that should trigger rechecking capabilities.

Prefer a consistent object-map `qualityGate` shape (`test`, `lint`, `format`, `typecheck`, `build`, `inspection`) rather than prose-heavy command fields. A stricter `gates[]` schema can come later if deterministic automation needs it.

## Local Overrides

Local overrides may live in `.github/github-repo-workflow.override.json` and should be ignored by git through the workspace `*.override.*` convention.

Override files are for local structured metadata only: local IDE paths, local tool paths, and token-specific GitHub capability observations. Keep secrets in `.env` or secret stores, and reference env var names rather than values in docs or JSON. Overrides are deep-merged over committed JSON; they may add or narrow local execution details, but must not disable shared gates, weaken quality policy, hide security checks, or change shared cleanup safety.

When work changes default branch policy, important workflows, deploy/QA labels, health endpoints, related or validated-through repos, validation commands, lint or inspection routing, docs routing, JetBrains inspection expectations, cleanup policy, or repo ownership boundaries, check `.github/github-repo-workflow.json`. If the user did not explicitly ask for metadata edits, propose the update or record it as a remaining item instead of silently editing the file.

When GitHub signal checks discover durable capability facts, propose a repo metadata update by default instead of writing automatically. Capability metadata usually changes once per repo and should stay intentional.

Do not add or update `jetbrains` config speculatively. Suggest it when IDE inference was wrong, ambiguous, or corrected by the user, then ask before editing the repo config.

Keep secrets, tokens, credentials, private host notes, and local-only operator details out of this file. Use untracked local files or environment-specific secret stores for those.

## Example

```json
{
  "defaultBranch": "main",
  "projectType": "control-plane",
  "docs": {
    "index": "docs/README.md",
    "architecture": "docs/architecture.md",
    "operations": "docs/operations.md",
    "style": ["docs/style/python.md", "docs/style/testing.md"]
  },
  "qualityGate": {
    "test": {
      "default": "uv run python -m unittest"
    },
    "lint": {
      "default": "uv run ruff check .",
      "changedFiles": "uv run ruff check {files}"
    },
    "format": {
      "check": "uv run ruff format --check ."
    },
    "inspection": {
      "tool": "jetbrains",
      "ide": "PyCharm",
      "scopePreference": ["changed_files", "directory", "whole_project"]
    },
    "docsRequiredWhen": [
      "behavior changes",
      "api changes",
      "config changes",
      "operations changes",
      "ownership boundaries change",
      "user-visible UI changes"
    ]
  },
  "qaLabels": ["awaiting-qa"],
  "deployLabels": ["preview"],
  "healthUrls": [
    {
      "name": "testing",
      "url": "https://ver-testing.example.com/api/health"
    }
  ],
  "importantWorkflows": ["CI", "Publish Testing Image"],
  "relatedRepos": ["example/shared-addons"],
  "validatedThrough": ["example/tenant-app"],
  "cleanup": {
    "deleteMergedLocalBranches": true,
    "removeMergedCleanWorktrees": true
  },
  "metadataFreshness": {
    "updateWhen": [
      "docs routing changes",
      "validation gates change",
      "primary commands change",
      "important workflows change",
      "health endpoint changes",
      "repo relationship changes",
      "cleanup policy changes",
      "ownership boundaries change"
    ]
  },
  "githubSignals": {
    "postMerge": {
      "waitForActions": true,
      "checkSecurityAndQuality": true
    },
    "capabilities": {
      "codeScanning": "unknown",
      "secretScanning": "unknown",
      "dependabotAlerts": "unknown",
      "securityAdvisories": "unknown"
    },
    "refreshWhen": [
      "repo visibility changes",
      "GitHub plan changes",
      "security settings change",
      "token permissions change"
    ]
  }
}
```
