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
uv run ~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py inspect --repo "$PWD" --scope changed_files
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
```

Command model:

- `list-projects`: discover plugin-visible projects only.
- `resolve-route`: probe for an already-open exact route; it does not open or inspect.
- `prepare-worktree`: open and claim the exact worktree; it does not inspect.
- `inspect`: open if needed, inspect, fetch problems, and clean up helper-opened projects.
- `inspect-closeout`: readiness/hand-off inspection; use before saying a change
  is ready, safe to push, safe to merge, safe to hand off, or safe to exit.
- `get-status` and `get-problems`: route-pinned diagnostics for already-routable projects.

The legacy command names remain supported: `list`, `route`, `prepare`, `run`,
`closeout`, `status`, and `problems`. Prefer the self-descriptive names above in
new instructions and reports.

`inspect` and `inspect-closeout` create a local lease, serialize helper-owned IDE
opens, open the exact current worktree only when no exact route exists, wait for
indexing/scanning to settle, run the inspection loop, and call the plugin
lifecycle close endpoint only for projects the helper opened. Projects that were
already open before inspection must remain open. Lifecycle opens use macOS
background activation by default to reduce focus stealing; when the target IDE is
not already running, the helper launches the app hidden first and then asks the
plugin to open the exact worktree. Use `--foreground-open` only when debugging
IDE launch behavior.
Lifecycle inspections are serialized by a bounded local lock. If another helper
inspection is already opening, inspecting, or cleaning up a project, wait for it or increase
`--lifecycle-lock-timeout-ms`; do not start parallel auto-open closeouts and
expect independent IDE windows to race safely.

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

If multiple JetBrains products are installed, repo config or CLI arguments must
select the intended IDE so the helper updates the right Trusted Locations file.
If a first-time open still stalls after trusted-location and project-opening
policy seeding, treat it as a blocker: check for unsupported IDE config layout,
settings sync overwriting the config, a missing inspection plugin, or a product
that accepted the scheduled open but never registered the worktree. Real-session
smokes have validated unattended lifecycle inspection on IntelliJ IDEA, PyCharm, and
WebStorm 2026.1 with trusted worktrees under `$HOME/.code/working`.

`run` and `closeout` are retained as backward-compatible names for inspection
flows. Prefer `inspect` for ordinary iteration and `inspect-closeout` for final
readiness notes so cleanup status is explicit. Use `--include-stale` only when
explicitly diagnosing cached stale findings; stale results still exit non-zero
and are not clean.

## When To Run

- During the edit loop after meaningful code changes.
- Before saying code is ready, safe to push, safe to merge, or safe to hand off.
- When repo instructions mention JetBrains, PyCharm, IntelliJ IDEA, WebStorm,
  IDE warnings, static analysis, or inspection quality gates.
- When normal tests pass but IDE-only analysis may catch framework/plugin issues.

For docs-only or non-code edits, record a concise not-run reason when an
inspection would be disproportionate.

## Scope Selection

Start narrow while iterating: changed files, touched files, or touched directory.
Before readiness, broaden to the largest practical scope for the change and repo
policy. The helper reads `.github/github.json` when present:

- `qualityGate.inspection.scopePreference`
- `qualityGate.inspection.ide`
- `jetbrains.ide`
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

For closeout/readiness, require an exact worktree route. A containing main
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
  The helper appends each `UNKNOWN` verdict to
  `${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection/unknown-verdicts.jsonl`
  so repeated blockers can be fixed later. Set `JB_INSPECT_UNKNOWN_LOG=0` to
  disable logging, set it to a path to override the log file, or set
  `JB_INSPECT_ROLLOUT_FILE` to include the current rollout/session transcript in
  the record.
- Red-lane proof requires current actionable findings in the helper response,
  such as `total_problems > 0`; a paginated current page may have an empty
  `problems` list even when matching findings exist.
  A non-clean response with `capture_incomplete`, `non_empty_unmapped_tree`, or
  zero returned problems proves only that the plugin could not prove clean; it
  is not proof that agents can see and act on the IDE's red state.
- readiness closeouts should use `inspect-closeout`, not plain `get-status`. If lifecycle
  cleanup is skipped or fails for a helper-opened project, the closeout is not
  clean; report both the inspection result and cleanup reason.
- `get-status` is informational and exits zero only when the helper can retrieve a
  route-pinned status that is not stale, inconclusive, unavailable, ambiguous,
  indexing, running, timed out, or session-drifted.
- `stale_results`, `capture_incomplete`, timeout, indexing, session drift,
  ambiguous route, or unavailable IDE: not clean; retry, narrow scope, open the
  project in the preferred IDE, or report the blocker.
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

Summarize the inspection route, scope, status, and actionable findings. Include
file and line when available. If not run or inconclusive, state why and the next
smallest useful action.
