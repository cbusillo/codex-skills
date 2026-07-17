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
    purpose: Opens and claims the exact worktree without running inspections.
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
    purpose: Summarizes helper outcome JSONL logs by verdict, bucket, and retry without running an inspection.
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
          example_argv: ["uv", "run", "scripts/jb-inspect.py", "inspect-closeout", "--repo", "$PWD", "--scope", "changed_files"]
          purpose: Runs the readiness inspection flow with route safety, lifecycle cleanup, and stale-result checks.
        - kind: script
          path: scripts/jb-inspect.py
          example_argv: ["uv", "run", "scripts/jb-inspect.py", "get-status", "--repo", "$PWD"]
          purpose: Reads route-pinned plugin status through the helper.
---

# JetBrains Inspection

Use this skill to run and interpret JetBrains IDE inspections through the local
inspection plugin HTTP API. The script-backed helper is the primary agent
interface; prefer it over direct curl or MCP tool calls.

## Primary Helper

Run the helper from this skill's `scripts/jb-inspect.py` path with `uv run`.
In the common user-skill install, that path is:

```bash
HELPER=~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py
uv run "$HELPER" inspect --repo "$PWD" --scope changed_files
```

If this skill was loaded from a repo-local or temporary path, use that loaded
skill path instead of `~/.code/skills/...`.

Useful commands:

```bash
HELPER=~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py
uv run "$HELPER" list-projects
uv run "$HELPER" resolve-route --repo "$PWD"
uv run "$HELPER" prepare-worktree --repo "$PWD"
uv run "$HELPER" inspect --repo "$PWD" --scope changed_files
uv run "$HELPER" inspect-closeout --repo "$PWD" --scope changed_files
uv run "$HELPER" get-status --repo "$PWD"
uv run "$HELPER" get-problems --repo "$PWD" --severity error
uv run "$HELPER" summarize-outcomes
uv run "$HELPER" cleanup-helper-leases --no-dry-run
```

Command model:

- `list-projects`: discover plugin-visible projects only.
- `resolve-route`: probe for an already-open exact route; it does not open or
  inspect.
- `prepare-worktree`: open and claim the exact worktree; it does not inspect.
- `inspect`: open if needed, inspect, fetch problems, and clean up
  helper-opened projects.
- `inspect-closeout`: readiness/hand-off inspection; use before saying a change
  is ready, safe to push, safe to merge, safe to hand off, or safe to exit.
- `get-status` and `get-problems`: route-pinned diagnostics for
  already-routable projects.
- `cleanup-helper-leases`: reconcile stale helper-owned leases under the
  lifecycle lock; unresolved identity or close failures return nonzero.

`inspect` and `inspect-closeout` create a local lease, serialize helper-owned IDE
opens, open the exact current worktree only when no exact route exists, wait for
indexing/scanning to settle, run the inspection loop, and call the plugin
lifecycle close endpoint only for projects the helper opened. Projects that were
already open before inspection must remain open. Lifecycle opens use macOS
background activation by default to reduce focus stealing; when the target IDE is
not already running, the helper launches the app hidden first and then asks the
plugin to open the exact worktree. Use `--foreground-open` only when debugging
IDE launch behavior.
If an inspection wait times out while a run is still active, the helper asks the
plugin to cancel that exact `inspection_run_id` and waits briefly for
cancellation to settle. If the run changed, the helper leaves the newer run
untouched. If the trigger/wait transport itself times out, the helper probes
status first; an unproven active run or unreachable status keeps the owned
project warm instead of closing it blindly.
Once settled, it closes the helper-owned project normally. If indexing,
scanning, or inspection churn remains active, the helper leaves the project warm
and reports `cleanup.status=deferred` with `cleanup_deferred=true`. Treat that as
`UNKNOWN`, rerun `inspect-closeout` after the IDE settles, and use
`cleanup-helper-leases` only if the warm lease becomes stale. Full inspection
timeouts are not retried internally; the one automatic retry is reserved for
safe stale/capture-readiness outcomes.
Preparation is failure-atomic for handled failures and interrupts. With plugin
protocol `lease_bound_v1`, the helper persists `state=open_requesting` before
sending its local `lease_id` to `lifecycle/open`. An open response registers the
request but does not prove ownership. Only `lifecycle/claim` can bind that lease
to the exact project instance and return a close token; the helper claims before
the readiness wait and ignores token-shaped responses that lack the protocol's
ownership proof. `already_open`, another lease's `already_opening`, legacy
plugin responses, and session-mismatched routes never authorize close.
After the plugin accepts a lifecycle open, the helper waits only for that
lease-bound request; it does not issue a second app-level open that could create
an unowned window or trust prompt. A timed-out lifecycle-open response is
treated as ambiguous rather than absent: the helper waits for the exact route
and requires a successful lease-bound claim before it can close anything.
That potential ownership evidence remains recoverable through route/claim
timeouts and is discarded only after a definitive `not_owned` claim.
If preparation then fails, the helper closes immediately when a live claim
proves ownership, except when the readiness status still reports active
indexing, scanning, or inspection work; active churn keeps the owned project
warm for a bounded retry. Otherwise it records `state=cleanup_pending` with the
available evidence and cleanup action instead of leaving a generic `preparing`
lease. `cleanup-helper-leases` may recover even a route-less pending lease by
asking the live plugin to prove the same lease binding. A definitive
`not_owned` response releases the local lease without closing; unavailable or
legacy proof remains an explicit nonzero `unresolved` result. Path/session
matching selects a candidate route but is never itself permission to close.
Preparation failures for projects that were already open release only the local
lease and never call lifecycle close. `cleanup-helper-leases` uses the same
lifecycle lock as inspection commands so stale reconciliation cannot race a new
helper-owned open or close.
Lifecycle inspections are serialized by a bounded local lock. If another helper
inspection is already opening, inspecting, or cleaning up a project, wait for it
or increase `--lifecycle-lock-timeout-ms`; do not start parallel auto-open
inspections and expect independent IDE windows to race safely.

Auto-open is allowed only for worktrees under globally trusted roots. Before a
lifecycle auto-open, the helper adds the matching trusted root to the selected
JetBrains product's Trusted Locations config, ensures project opening is set to
new-window/no-prompt, launches the selected IDE hidden if no matching plugin is
already running, then asks the running inspection plugin to schedule the exact
worktree open with the IDE's current `session_id`. Current plugin builds use
that session-verified lifecycle request to mark the path trusted inside the
running IDE immediately before `ProjectManagerEx.openProject`, which avoids
stale on-disk Trusted Locations state in already-running IDEs. The helper polls
until the exact route appears before it inspects; a lifecycle open response alone
is not proof that the IDE finished opening the project.
Configure trusted roots in `${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection.json`:

```json
{
  "jetbrains": {
    "trustedAutoOpenRoots": ["/Users/me/Developer", "/Users/me/.code/working"]
  }
}
```

If an exact worktree is not already open and is outside those roots, `inspect`,
`inspect-closeout`, and `prepare-worktree` must fail before opening the IDE. Do
not use random temp directories for agent inspection worktrees.

If multiple JetBrains products or stable/EAP installs are present, the repo must
declare its preferred IDE in `.github/github.json` so lifecycle opens and
Trusted Locations seeding target the same product/config. Product-level metadata
such as `jetbrains.ide: "WebStorm"`, `"PyCharm"`, or `"IntelliJ IDEA"` means
the latest installed stable/non-EAP app for that product. For a deliberate EAP
or exact-version run, use explicit metadata or CLI fields such as
`jetbrains.ideChannel: "eap"`, `jetbrains.ideVersion: "2026.2"`,
`jetbrains.ideApp`, `--ide-channel`, `--ide-version`, or `--ide-app`.
Never infer EAP from the presence of an EAP install. EAP requires an explicit
repo, CLI, exact app/version, or user-task signal; it is not a fallback when no
stable IDE is discovered.
Treat `--ide`/`--ide-app` as a one-off unblocker; for recurring repo work,
tell the user to add preferred IDE metadata rather than leaving the next agent to
guess again.
If a first-time open still stalls after trusted-location and project-opening
policy seeding, treat it as a blocker: check for unsupported IDE config layout,
settings sync overwriting the config, a missing inspection plugin, or a product
that accepted the scheduled open but never registered the worktree. Real-session
smokes have validated unattended lifecycle inspection on IntelliJ IDEA, PyCharm,
and WebStorm 2026.1 with trusted worktrees under `$HOME/.code/working`.

Use `inspect` for ordinary iteration and `inspect-closeout` for final readiness
notes so cleanup status is explicit. Use `--include-stale` only when explicitly
diagnosing cached stale findings; stale results still exit non-zero and are not
clean.

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
Before readiness, broaden to the largest practical scope for the change and repo
policy. The helper reads `.github/github.json` when present:

- `qualityGate.inspection.scopePreference`
- `qualityGate.inspection.ide`
- `jetbrains.ide`
- `jetbrains.ideChannel` / `jetbrains.ide_channel`
- `jetbrains.ideVersion` / `jetbrains.ide_version`
- `jetbrains.ideApp` / `jetbrains.ide_app`
- `jetbrains.openProjectPath`
- `jetbrains.mainWorktreePath`
- `jetbrains.worktreeStrategy`
- `jetbrains.scopePreference`

If config is absent, the helper infers from git and the current working tree. For
a one-off inspection, a missing inspection config can use the safe default
`changed_files` scope when the helper can infer the correct route. Do not
silently turn that inference into durable repo policy. If the configured IDE,
scope, project path, or worktree strategy is blank, contradictory, or feels
wrong for the active worktree, ask the user before changing policy or treating
the value as authoritative; otherwise report the mismatch as a not-clean
readiness blocker.

## Worktree Safety

Inspect the worktree being edited. Do not silently inspect the main worktree
when Code is operating in a linked worktree. If routing resolves to another
worktree, treat that as a blocker unless the user explicitly approves it.

For readiness inspection, require an exact worktree route. A containing main
checkout is not enough; `inspect-closeout` may open the linked worktree in the
preferred IDE and must clean it up afterward when it owns the open.

## Result Policy

- `GREEN`: inspection worked and found no actionable findings for the selected
  scope/filter.
- `RED`: inspection worked and returned actionable current findings. Fix real
  findings in touched code before calling work ready.
- `UNKNOWN`: inspection did not prove green or red. Do not summarize this as
  "no problems found"; report the verdict reason and next action, because the
  IDE, plugin, helper, route, or environment needs attention first.
  Prefer the helper's `agent_result` envelope for normal reporting. It contains
  `verdict`, `bucket`, `retry_policy`, `next_action`, and `agent_report`; do not
  inspect raw route, cleanup, wait, or capture diagnostics unless debugging the
  helper itself.
  Every `UNKNOWN` and cleanup anomaly also carries `inspection_attribution`
  schema version 1 with a stable classification, code, failure phase, endpoint,
  HTTP status, helper/plugin provenance, and bounded evidence IDs. The helper
  supplies one `client_run_id` per invocation and preserves plugin `request_id`
  values. `unattributed_unknown: true` is a helper/tool failure, not a neutral
  unknown bucket.
  If the reason is `ide_selection_required`, `ide_config_ambiguous`, or
  `ide_config_missing`, say directly that the repo needs preferred JetBrains IDE
  metadata in `.github/github.json`; do not frame that as merely optional when
  the same repo will be inspected again.
  The helper appends each `UNKNOWN` verdict to
  `${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection/unknown-verdicts.jsonl`
  so repeated blockers can be fixed later. Set `JB_INSPECT_UNKNOWN_LOG=0` to
  disable logging, set it to a path to override the log file, or set
  `JB_INSPECT_ROLLOUT_FILE` to include the current rollout/session transcript in
  the record. Durable unknown/outcome rows hash local paths and project keys,
  redact token-like fields, and retain helper revision, plugin fingerprint, IDE
  product/build, failure phase, attribution class, cleanup status/reason, and
  evidence IDs.
  When inspection evidence is used to qualify changes to this helper or another
  installed runtime-bound skill, compare the recorded helper/source revision
  with the intended landed revision or a fresh runtime-reconciliation receipt.
  A missing or mismatched revision makes the installed-runtime claim `UNKNOWN`;
  do not count it as current evidence. A repo-local helper may still provide
  valid branch evidence when its exact path and revision are recorded and match
  the source being evaluated.
- Red-lane proof requires current actionable findings in the helper response,
  such as `total_problems > 0`; a paginated current page may have an empty
  `problems` list even when matching findings exist.
  A non-clean response with `capture_incomplete`, `non_empty_unmapped_tree`, or
  zero returned problems proves only that the plugin could not prove clean; it
  is not proof that agents can see and act on the IDE's red state.
- readiness inspections should use `inspect-closeout`, not plain `get-status`.
  If lifecycle cleanup is skipped or fails for a helper-opened project, the
  inspection is not clean; report both the inspection result and cleanup reason.
  If cleanup is deferred because the IDE is still indexing/scanning, report the
  `UNKNOWN` verdict and rerun after indexing settles before calling the work
  inspection-clean.
- `get-status` is informational and exits zero only when the helper can retrieve
  a route-pinned status that is not stale, inconclusive, unavailable, ambiguous,
  indexing, running, timed out, or session-drifted.
- `stale_results`, `capture_incomplete`, timeout, indexing, session drift,
  ambiguous route, or unavailable IDE: not clean. Retry at most once, and only
  when `retry_policy.retry=true`; otherwise narrow scope, open the project in
  the preferred IDE, or report the blocker. Do not invent retry loops.
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
