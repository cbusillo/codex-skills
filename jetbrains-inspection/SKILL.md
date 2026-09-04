---
name: jetbrains-inspection
description: Use JetBrains IDE inspections through the local inspection plugin; trigger for code changes, readiness checks, PR/push validation, IDE warnings, inspection triage, worktree-safe inspection routing, or when code quality should be driven toward zero actionable IDE findings.
metadata:
  short-description: Run JetBrains IDE inspections safely
resources:
  - path: scripts/jb-inspect.py
    kind: script
    description: Route-safe JetBrains inspection helper with lifecycle, stale-result, and cleanup handling.
  - path: tests/test_jb_inspect.py
    kind: reference
    description: Regression tests for JetBrains inspection helper routing and lifecycle behavior.
commands:
  - name: jetbrains-inspection-open-worktree
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "open-worktree", "--repo", "$PWD"]
    purpose: Preferred public command for opening and claiming the exact worktree without running inspections.
  - name: jetbrains-inspection-agent-inspect
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "agent-inspect", "--repo", "$PWD", "--scope", "changed_files"]
    purpose: Runs the one-shot agent inspection flow and emits a compact terminal result envelope.
  - name: jetbrains-inspection-list-projects
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "list-projects"]
    purpose: Lists discovered IDE projects and plugin routes without opening or inspecting.
  - name: jetbrains-inspection-resolve-route
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "resolve-route", "--repo", "$PWD"]
    purpose: Resolves an already-open target IDE/project route without opening or inspecting.
  - name: jetbrains-inspection-prepare-worktree
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "prepare-worktree", "--repo", "$PWD"]
    purpose: Backward-compatible alias for open-worktree.
  - name: jetbrains-inspection-prepare
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "prepare", "--repo", "$PWD"]
    purpose: Backward-compatible alias for open-worktree.
  - name: jetbrains-inspection-inspect
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "inspect", "--repo", "$PWD", "--scope", "changed_files"]
    purpose: Opens the exact worktree if needed, inspects it, fetches problems, and cleans up helper-opened projects.
  - name: jetbrains-inspection-inspect-closeout
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "inspect-closeout", "--repo", "$PWD", "--scope", "changed_files"]
    purpose: Runs the readiness/hand-off inspection flow with route safety, lifecycle cleanup, and stale-result checks.
  - name: jetbrains-inspection-get-status
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "get-status", "--repo", "$PWD"]
    purpose: Reads route-pinned inspection status through the helper.
  - name: jetbrains-inspection-get-problems
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "get-problems", "--repo", "$PWD", "--severity", "error"]
    purpose: Fetches current inspection problems through the helper.
  - name: jetbrains-inspection-summarize-outcomes
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "summarize-outcomes"]
    purpose: Summarizes outcome logs diagnostically or runs strict artifact-pinned sample qualification without inspecting.
  - name: jetbrains-inspection-cleanup-helper-leases
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "cleanup-helper-leases", "--no-dry-run"]
    purpose: Reconciles stale helper-owned leases under the lifecycle lock without path-only project closes.
policy:
  command_policies:
    - id: prefer-jb-inspect-for-plugin-http
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b(127\\.0\\.0\\.1|localhost)[:/]\\S*/(api/)?inspection\\b"
      action: require_preferred
      message: Direct HTTP calls to the JetBrains inspection plugin bypass route resolution, worktree safety, stale-result handling, lifecycle locking, and cleanup. Use the inspection helper instead.
      preferred:
        - kind: script
          path: scripts/jb-inspect.py
          example_argv: ["uv", "run", "scripts/jb-inspect.py", "agent-inspect", "--repo", "$PWD", "--scope", "changed_files"]
          purpose: Runs the one-shot agent inspection flow with route safety, lifecycle cleanup, and terminal result handling.
        - kind: script
          path: scripts/jb-inspect.py
          example_argv: ["uv", "run", "scripts/jb-inspect.py", "get-status", "--repo", "$PWD"]
          purpose: Reads route-pinned plugin status through the helper.
---

# JetBrains Inspection

Apply [task scope and authorization](../references/execution-scope.md) when
using this workflow; it defines how existing approval and task boundaries apply.

Use this skill to run and interpret JetBrains IDE inspections through the local
inspection plugin HTTP API. The script-backed helper is the primary agent
interface; prefer it over direct curl or MCP tool calls.

When IDE configuration appears in the change set, use
`../references/ide-configuration-policy.md` before deciding whether to keep,
split, revert, ignore, or remove it. Inspect tracking state, the exact diff,
ignore rules, repository policy, and history. Preserve the canonical shared form
plus only safe hunks in mixed tracked files; never use blanket commit, revert,
clean, or stash operations that can absorb machine-local or unrelated IDE state.
Do not stage untracked, non-ignored IDE configuration automatically; check
repository policy and ask when the sharing decision remains unclear.

## Before The First Inspection

If `.github/github.json` sets `qualityGate.inspection.prepare`, run that exact
repository command in the exact linked worktree you plan to inspect. The
preferred public command for opening that worktree is:

```bash
uv run "$HELPER" open-worktree --repo "$PWD"
```

`prepare-worktree` and `prepare` remain compatibility aliases, but they are
not the preferred public command. Do not substitute a different setup command,
even if it seems equivalent.

Preparation may create ignored local worktree state such as `.venv/` and
`.idea/` directories or files. That is allowed. What is not allowed is a
nonzero exit or any tracked-file mutation. If either happens, stop and treat
preparation as a blocker before the first inspection.

Preparation-created ignored IDE state stays untracked and is not a reason to
start versioning IDE configuration. Starting to track it is a durable repository
policy change and requires explicit user direction.

Python repositories should prefer the structured, skill-owned preparation
shape instead of embedding an absolute helper path or copying IDE files between
worktrees:

```json
{
  "qualityGate": {
    "inspection": {
      "prepare": {
        "python": {
          "version": "3.13",
          "moduleName": "example-project",
          "testRoots": ["tests"],
          "sync": true,
          "extras": ["dev"],
          "requiredGeneratedState": [".venv", ".idea"]
        }
      }
    }
  }
}
```

The helper resolves its bundled `prepare-python-project.py`, creates an ignored
worktree-local SDK/project model, and optionally runs `uv sync`. String commands
remain supported for repository-specific preparation. Structured preparation
rejects unknown fields, path traversal, extras without sync, and outer-level
`requiredGeneratedState` ambiguity.

Run preparation before the first inspection assessment, not after an
inspection has already started. Preparation is a repo-specific readiness step,
not an inspection surrogate.

## Primary Helper

Run the helper from this skill's `scripts/jb-inspect.py` path with `uv run`.
In the common user-skill install, that path is:

```bash
uv run ~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py \
  agent-inspect --repo "$PWD" --scope changed_files
```

If this skill was loaded from a repo-local or temporary path, use that loaded
skill path instead of `~/.code/skills/...`.

Useful commands:

```bash
HELPER=~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py
uv run "$HELPER" agent-inspect --repo "$PWD" --scope changed_files
uv run "$HELPER" list-projects
uv run "$HELPER" resolve-route --repo "$PWD"
uv run "$HELPER" open-worktree --repo "$PWD"
uv run "$HELPER" inspect --repo "$PWD" --scope changed_files
uv run "$HELPER" inspect-closeout --repo "$PWD" --scope changed_files
uv run "$HELPER" get-status --repo "$PWD"
uv run "$HELPER" get-problems --repo "$PWD" --severity error
uv run "$HELPER" summarize-outcomes
uv run "$HELPER" summarize-outcomes --qualification-file qualification.json --sample-size 50
uv run "$HELPER" cleanup-helper-leases --no-dry-run
```

Command model:

- `agent-inspect`: primary LLM-facing command; runs the maintained inspection
  and lifecycle flow once, emits a compact JSON envelope, and exits successfully
  whenever it produced an `agent_result`. Read the verdict and retry permission
  from `agent_result`, never from the shell exit code.
- `list-projects`: discover plugin-visible projects only.
- `resolve-route`: probe for an already-open exact route; it does not open or
  inspect.
- `open-worktree`: preferred public command; open and claim the exact
  worktree; it does not inspect.
- `prepare-worktree` and `prepare`: backward-compatible aliases for
  `open-worktree`.
- `inspect`: open if needed, inspect, fetch problems, and clean up
  helper-opened projects.
- `inspect-closeout`: readiness/hand-off inspection; use before saying a change
  is ready, safe to push, safe to merge, safe to hand off, or safe to exit.
- `get-status` and `get-problems`: route-pinned diagnostics for
  already-routable projects.
- `summarize-outcomes`: keep the existing diagnostic verdict/bucket/retry
  summary when no qualification file is supplied. With
  `--qualification-file`, run the strict post-boundary assessment gate described
  below; strict incomplete or failed gates exit nonzero.
- `cleanup-helper-leases`: reconcile stale helper-owned leases under the
  lifecycle lock; unresolved identity or close failures return nonzero.

The helper owns route selection, trusted auto-open, lease-bound cleanup, and
bounded retries. Inspect the exact worktree; never close a preexisting or
foreign-owned project, bypass trust/preparation checks, or add an outer retry
loop. Read verdict and retry permission from `agent_result` and
`retry_policy.retry`, not the process exit code. Deferred cleanup and stale or
unproven results remain `UNKNOWN`; they are not readiness evidence.

Before diagnosing auto-open, ownership, retry, cleanup, trust, or IDE-selection
problems, read [lifecycle diagnostics](references/lifecycle-diagnostics.md).
Also read it before changing lifecycle configuration or helper behavior. Normal
inspection uses the maintained helper and its terminal result; do not reproduce
its lifecycle algorithm manually.

## When To Run

- During the edit loop after meaningful code changes.
- Before saying code is ready, safe to push, safe to merge, or safe to hand off.
- When repo instructions mention JetBrains, PyCharm, IntelliJ IDEA, WebStorm,
  IDE warnings, static analysis, or inspection quality gates.
- When normal tests pass but IDE-only analysis may catch framework/plugin issues.

For docs-only or non-code edits where no runtime behavior changed, record a
one-line not-run reason, such as `docs-only change, no code paths affected`,
when an inspection would be disproportionate.

## Scope Selection

Start narrow while iterating: changed files, touched files, or touched directory.
Before readiness, broaden when the changed behavior, findings, or an explicit
repo requirement warrant it. Otherwise reuse a current clean result covering
the affected surface. Complete every required repo gate; an ordered scope
preference is not by itself a requirement to run each scope in succession.
The helper reads `.github/github.json` when present:

- `qualityGate.inspection.scopePreference`
- `qualityGate.inspection.ide`
- `qualityGate.inspection.lanes`
- `qualityGate.inspection.profile`
- `qualityGate.inspection.prepare` (command string or structured `python` object)
- `qualityGate.inspection.requiredGeneratedState`
- `jetbrains.ide`
- `jetbrains.ideChannel` / `jetbrains.ide_channel`
- `jetbrains.ideVersion` / `jetbrains.ide_version`
- `jetbrains.ideApp` / `jetbrains.ide_app`
- `jetbrains.openProjectPath`
- `jetbrains.mainWorktreePath`
- `jetbrains.worktreeStrategy`
- `jetbrains.scopePreference`

Mixed-language repositories may replace the single
`qualityGate.inspection.ide` with ordered `qualityGate.inspection.lanes`. Each
lane names a unique `id`, an `ide`, whether it is `required`, repository-relative
`include` globs, optional `exclude` globs, and an optional repository-relative
`projectPath` directory when that IDE must open a nested project. The helper
resolves the selected scope once, validates every file and lane project path
against the exact worktree, assigns files by first matching lane, and records
unmatched and explicitly excluded files. Files assigned to a lane with
`projectPath` must resolve inside that project directory. It
does not open an IDE for an empty lane. Exclusions apply to ordinary changed-file,
directory, and whole-project readiness; an explicit `files` scope records an
override and still runs the selected fixture in its lane. Non-empty lanes run
sequentially with an exact `files` scope and independent route, session, cleanup,
mutation, IDE, and plugin provenance. Required lanes aggregate deterministically:
any `RED` wins, otherwise any `UNKNOWN` wins, otherwise the result is `GREEN`.
Optional-lane failures remain visible without changing the required-lane
aggregate. When `lanes` is absent, the existing single-IDE path remains
unchanged.

If config is absent, the helper infers from git and the current working tree. For
a one-off inspection, a missing inspection config can use the safe default
`changed_files` scope when the helper can infer the correct route. Do not
silently turn that inference into durable repo policy. If the configured IDE,
scope, project path, or worktree strategy is blank, contradictory, or feels
wrong for the active worktree, ask the user before changing policy or treating
the value as authoritative; otherwise report the mismatch as a not-clean
readiness blocker.

When `qualityGate.inspection.prepare` is configured, `agent-inspect`, `inspect`,
and `inspect-closeout` run that exact repository command in the exact target
worktree before IDE lifecycle open or claim. Automatic execution is allowed
only below a configured trusted auto-open root; an untrusted root returns an
actionable manual-preparation result and never runs repository-controlled argv.
The helper uses `shlex`-validated argv with `shell=False`, a dedicated bounded
`--repository-preparation-timeout-ms`, and a recursion guard. It snapshots Git
status plus the worktree's index bytes before and after. Nonzero exit, timeout,
tracked mutation, hidden index mutation, missing `requiredGeneratedState`, or
recursive invocation is a terminal preparation result. Do not continue with an
unprepared project model or substitute a different command.

Successful preparation writes a bounded durable receipt under the helper cache.
The receipt is reused only when the command/configuration hashes, exact worktree
identity, post-preparation Git state, and required generated state still match.
Use `--skip-preparation` or `--no-repository-preparation` only after manually
running the configured command, and use `--force-preparation` or
`--force-refresh-preparation` to refresh a valid receipt. Preparation is
idempotent and may create ignored local environment state, but it must not leave
tracked-file mutations or hidden index mutations before inspection begins.

## Worktree Safety

Inspect the worktree being edited. Do not silently inspect the main worktree
when Code is operating in a linked worktree. If routing resolves to another
worktree, treat that as a blocker unless the user explicitly approves it.

For readiness inspection, require an exact worktree route. A containing main
checkout is not enough; `inspect-closeout` may open the linked worktree in the
preferred IDE and must clean it up afterward when it owns the open.

A linked worktree isolates Git checkout state; it does not serialize processes
inside that worktree or stop IDE VFS refreshes, indexing, and project-model
updates. Before inspection, await builds, installs, generators, formatters, and
other same-worktree writers, then avoid new writes until lifecycle cleanup
finishes. The helper's readiness barrier observes IDE status only; it cannot
identify arbitrary writers or prove that ignored files are quiet.

## Result Policy

- `GREEN`: inspection worked and found no actionable findings for the selected
  scope/filter.
  `whole_project` and `directory` GREEN additionally require plugin capability
  `inspection_execution_proof_version >= 2`, which attests the exact native IDE
  inspection run with affirmative physical-file traversal and file-scoped tool
  completion; global-only activity is insufficient. A missing/older capability is
  `UNKNOWN/plugin_deployment_mismatch`; update the plugin, restart the IDE, and
  resolve the route again instead of trusting an older broad-scope GREEN.
- `RED`: inspection worked and returned actionable current findings. Fix real
  findings in touched code before calling work ready. Exact-scope responses may
  retain `execution_not_proven` in `proof_failures` when the current findings
  are decisive but clean completeness remains unproven; preserve the RED verdict
  and the proof gap together. Unexpected route, run, profile, or freshness proof
  failures still make the result `UNKNOWN`.
- `UNKNOWN`: inspection did not prove green or red. Do not summarize this as
  "no problems found"; report the verdict reason and next action, because the
  IDE, plugin, helper, route, or environment needs attention first.
  Prefer the helper's `agent_result` envelope for normal reporting. It contains
  `verdict`, `bucket`, `retry_policy`, `next_action`, and `agent_report`, plus
  bounded `proof_failures` and `inspection_proof` when a decisive RED retains a
  clean-completeness gap; do not
  inspect raw route, cleanup, wait, or capture diagnostics unless debugging the
  helper itself.
  For `stale_results` and `inspection_inputs_changed`, `unknown_diagnosis`
  separates proven snapshot invalidation from unproven source-edit or process
  attribution. Do not blame an agent or source edit without changed-file or
  process evidence.
  A bounded internal retry may extend to the stricter policy of a later UNKNOWN
  result, such as `stale_results` followed by `project_analysis_not_ready`; all
  attempts remain part of one terminal assessment and stop at the latest policy.
  Every `GREEN`, `RED`, or `UNKNOWN` result and cleanup anomaly carries
  `inspection_attribution` schema version 1 with a stable classification, code,
  phase, endpoint, HTTP status, helper/plugin provenance, cleanup state, and
  bounded evidence IDs. Preserve plugin attribution and prefer its IDE channel
  over selector fallback. The helper supplies one `client_run_id` per invocation
  and preserves plugin `request_id` values. `unattributed_unknown: true` is a
  helper/tool failure, not a neutral unknown bucket.
  Dirty plugin fingerprints remain provenance rather than presumed causation.
  Qualification must use the exact intended fingerprint, but normal diagnostics
  should report `plugin_build_dirty` without claiming it caused the verdict.
  A concurrent `inspection_in_progress` response is adoptable only when it
  includes an unambiguous positive run ID plus explicit scope/profile proof that
  exactly matches the trigger request. For `changed_files`, it must also prove
  the same unversioned-file policy and changed-files mode. Legacy, missing,
  partial, mismatched, or contradictory conflict payloads fail closed as
  `inspection_proof_failed` at the trigger phase and must not supply GREEN/RED
  evidence. Adopted conflict runs remain foreign-owned and are never cancelled.
  During `agent-inspect`, `inspect`, and `inspect-closeout`, problems retrieval reuses the trigger's
  scope, unversioned-file policy, changed-files mode, profile, and targeted file
  selectors rather than reconstructing a broader request.
  If the reason is `ide_selection_required`, `ide_config_ambiguous`, or
  `ide_config_missing`, say directly that the repo needs preferred JetBrains IDE
  metadata in `.github/github.json`; do not frame that as merely optional when
  the same repo will be inspected again.
  The helper appends each `UNKNOWN` verdict to
  `${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection/unknown-verdicts.jsonl`
  so repeated blockers can be fixed later. Set `JB_INSPECT_UNKNOWN_LOG=0` to
  disable logging, set it to a path to override the log file, or set
  `JB_INSPECT_ROLLOUT_FILE` to include the current rollout/session transcript in
  the record. Set `JB_INSPECT_DEPLOYMENT_MANIFEST` to the immutable deployment or
  runtime-reconciliation manifest used for qualification; its content SHA-256
  takes precedence over rollout-file fallback. JSONL appends are locked,
  complete short writes, and roll back failed writes; a malformed persisted row
  still fails strict qualification. New outcome rows use schema version 2 with a unique event ID,
  assessment/observation kind, assessment ID copied only from `client_run_id`,
  millisecond timestamp, final evidence IDs, repo/worktree/project hashes, repo
  HEAD, canonical scope descriptor/hash, full helper content SHA-256, exact
  plugin version/fingerprint, authoritative IDE product/version/channel,
  deployment-manifest content SHA-256, inspection-started state, cleanup, and
  ordered internal-attempt summaries. Durable rows hash local paths and project
  keys and redact token-like fields and path tokens in diagnostic prose.
  When inspection evidence is used to qualify changes to this helper or another
  installed runtime-bound skill, compare the recorded helper/source revision
  with the intended landed revision or a fresh runtime-reconciliation receipt.
  A missing or mismatched revision makes the installed-runtime claim `UNKNOWN`;
  do not count it as current evidence. A repo-local helper may still provide
  valid branch evidence when its exact path and revision are recorded and match
  the source being evaluated.

### Strict Outcome Qualification

Use strict mode only with an explicit schema-v1 qualification file:

```json
{
  "schema_version": 1,
  "boundary": {
    "since": "2026-07-26T00:00:00.000Z",
    "after_event_id": "optional-boundary-event-id"
  },
  "helper_revision": "sha256:<64 hex characters>",
  "plugin_build_fingerprint": "<exact full-commit fingerprint>",
  "deployment_manifest_sha256": "sha256:<64 hex characters>"
}
```

Strict mode considers only schema-v2 `inspection_assessment` events after the
boundary and groups them deterministically by assessment ID. The gate freezes
at the first requested number of qualifying assessments so later log appends
cannot rewrite that sample; post-sample failures remain separately visible. It
records internal retry attempts inside their single terminal assessment event;
distinct terminal events cannot reuse one assessment ID, and later invocations
cannot rewrite a frozen sample. Event IDs seen before the boundary are retained
for replay detection, so copied post-boundary rows are excluded rather than
counted again. Helper-opened projects
intentionally left open with `--keep-warm` record `cleanup=kept_warm` and cannot
qualify as cleanup-clean evidence. It
reports every post-boundary exclusion, exact duplicate, repeated repo/project
concentration, ordered attempts, UNKNOWN-to-decisive recovery,
verdict/classification/phase/cleanup rollups, decisive rate, and remaining
sample count. A configuration-
blocked event is a harmless exclusion only when `inspection_started=false` and
the exact `ide_selection_required`, `ide_config_ambiguous`, or
`ide_config_missing` code is recorded at phase `selection`. Missing provenance,
artifact mismatch, attribution mismatch, unattributed UNKNOWN, non-clean
cleanup, conflicting decisive outcomes, invalid configuration-blocked rows, or
hidden terminal failures inside the frozen sample window fail the gate. `pass`
requires the requested sample size, at least 95% decisive, and zero hard
failures; otherwise the gate is `incomplete` or `fail`. If a log contains
malformed rows, provide `boundary.after_event_id` so the helper can prove which
side of the boundary they occupy.

- `scope_semantic_coverage_missing` is `UNKNOWN`: one or more requested scoped
  files resolved only as generic TextMate/PlainText PSI, were invalid, or were
  outside project content. This overrides an otherwise clean or plugin-provided
  `GREEN`, including mixed-language scopes where only some files have semantic
  support. `in_source: false` alone is not a failure because language-aware PSI
  can inspect valid project-content files outside a configured source root.
  Valid in-content JetBrains project metadata identified by the authoritative
  `IDEA_MODULE` file role is classified as `project_metadata`, even when the IDE
  exposes it as `PsiPlainTextFile`; it does not require language-aware PSI. The
  helper reports that classification with no-action guidance instead of
  suggesting a language plugin. A recognized dependency lockfile may likewise
  be classified as `excluded_dependency_lockfile`, but only when the plugin
  reports `is_excluded: true` and the matching stable role. A basename without
  explicit IDE exclusion, an unknown role, or a blanket `*.lock` pattern stays
  fail-closed. When a file is outside project content, the
  next action points to the intended module/content root rather than language
  support. Otherwise, install or enable the needed language plugin, select a
  compatible IDE, or update the repo's preferred IDE metadata before rerunning.
  Use `--allow-text-only-coverage` only when generic text coverage is intentionally
  sufficient; it does not allow invalid files or files outside project content.
  Check the selected files before starting a readiness assessment. When every
  target is intentionally generic data, schema, or text and semantic PSI is not
  expected or required, include the override on the first assessment rather
  than generating a post-start configuration failure and rerunning. Never use
  the override for source code or a mixed scope containing source code.
  For older plugin builds that left this settled state waiting until timeout,
  the helper accepts the explicit override only when the same-run snapshot is
  clean, complete, current, and blocked solely by `non_semantic_fallback`.
  Generic, indexing, stale, incomplete, or contradictory timeouts remain
  `UNKNOWN`.
  The helper preserves actionable `RED` findings while attaching the semantic
  coverage gap so real findings are not hidden.
- `scope_semantic_coverage_truncated` is `UNKNOWN` for an otherwise clean result:
  the plugin resolved more files than it proved through either detailed rows or
  the aggregate semantic-coverage summary, so the helper cannot prove complete
  scope coverage. Bounded detail rows alone are not truncation when the
  aggregate summary proves every resolved file and preserves missing-coverage
  counts/examples. Text-only allowance does not override genuinely unproven
  files; update the plugin/helper proof path and rerun. Actionable current RED
  findings remain visible with the coverage gap attached.
- Red-lane proof requires current actionable findings in the helper response,
  such as `total_problems > 0`; a paginated current page may have an empty
  `problems` list even when matching findings exist.
  A non-clean response with `capture_incomplete`, `non_empty_unmapped_tree`, or
  zero returned problems proves only that the plugin could not prove clean; it
  is not proof that agents can see and act on the IDE's red state.
- readiness inspections should use `agent-inspect` or `inspect-closeout`, not
  plain `get-status`.
  `open-worktree`, `prepare-worktree`, `prepare`, `agent-inspect`, `inspect`,
  and `inspect-closeout` always lifecycle-open the
  exact worktree when needed. Use `resolve-route`, `get-status`, `get-problems`,
  or `claim-worktree` for observation-only workflows that must not open an IDE;
  do not turn an assessment command into a route-only probe.
  If lifecycle cleanup is skipped or fails for a helper-opened project, the
  inspection is not clean; report both the inspection result and cleanup reason.
  Before cleanup, the helper compares bounded porcelain status snapshots and
  emits `worktree_mutation_evidence` with counts and at most 25 relative paths.
  New tracked or untracked IDE metadata is lifecycle evidence; preserve it in
  the report rather than silently treating forced worktree removal as clean.
  If cleanup is deferred because the IDE is still indexing/scanning, report the
  `UNKNOWN` verdict and rerun after indexing settles before calling the work
  inspection-clean.
- `get-status` is informational and exits zero only when the helper can retrieve
  a route-pinned status that is not stale, inconclusive, unavailable, ambiguous,
  indexing, running, timed out, or session-drifted.
- `stale_results`, `capture_incomplete`, `inspection_inputs_changed`, timeout, indexing, session drift,
  ambiguous route, or unavailable IDE: not clean. Retry at most once, and only
  when `retry_policy.retry=true`; otherwise narrow scope, open the project in
  the preferred IDE, or report the blocker. Before a new helper invocation,
  await same-worktree writers and let IDE indexing/project-model updates settle.
  Do not invent retry loops.
- A freshly prepared PyCharm worktree may briefly report `language_sdk_missing`
  after the initial readiness wait while the IDE registers the generated
  `.venv`. When repository preparation
  succeeded and proved that the active lane project contains its generated
  `.venv`, the helper performs exactly one additional route-readiness wait,
  bounded by the internal retry timeout and gated by route-pinned status. A
  surfaced `language_sdk_missing` means that internal
  retry was unavailable or exhausted and remains a terminal configuration
  blocker; agents must not add another retry loop.
- Stale findings are withheld by default. Use `--include-stale` or
  `--allow-stale` only for explicit diagnostics, and do not treat returned
  cached findings as current inspection results.
- Existing broad noise is not invisible. Fix straightforward findings in the
  affected area or track a cleanup item.

Do not hide findings casually. Suppressions, disabled inspections, inspection
profile changes, or baseline changes require explicit approval unless the repo
already has an established approved convention. Prefer fixing code or narrowing
the scope first.

## Reporting

Report the compact helper envelope: verdict (`GREEN`/`RED`/`UNKNOWN`), scope,
one-line finding summary with file and line when available, and next action. Do
not include raw diagnostic fields such as `capture_diagnostic` in normal
reports; use them only when explicitly debugging an extractor or capture
failure. If not run or inconclusive, state a one-line not-run or blocker reason
and the next smallest useful action.
