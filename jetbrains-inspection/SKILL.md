---
name: jetbrains-inspection
description: Use JetBrains IDE inspections through the local inspection plugin; trigger for code changes, readiness checks, PR/push validation, IDE warnings, inspection triage, worktree-safe inspection routing, or when code quality should be driven toward zero actionable IDE findings.
metadata:
  short-description: Run JetBrains IDE inspections safely
---

# JetBrains Inspection

Use this skill to run and interpret JetBrains IDE inspections through the local
inspection plugin HTTP API. The script-backed helper is the primary agent
interface; prefer it over direct curl or MCP tool calls.

## Primary Helper

Run the helper from this skill's `scripts/jb-inspect.py` path with `uv run`.
In the common user-skill install, that path is:

```bash
uv run ~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py closeout --repo "$PWD" --scope changed_files
```

If this skill was loaded from a repo-local or temporary path, use that loaded
skill path instead of `~/.code/skills/...`.

Useful commands:

```bash
HELPER=~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py
uv run "$HELPER" list
uv run "$HELPER" route --repo "$PWD"
uv run "$HELPER" closeout --repo "$PWD" --scope changed_files
uv run "$HELPER" run --repo "$PWD" --scope changed_files
uv run "$HELPER" status --repo "$PWD"
uv run "$HELPER" problems --repo "$PWD" --severity error
```

`closeout` is the readiness/hand-off command and must be used before saying a
change is ready, safe to push, safe to merge, or safe to exit. It creates a local lease,
serializes helper-owned IDE opens, opens the exact current worktree only when no
exact route exists, waits for indexing/scanning to settle, runs the same
inspection loop, and calls the plugin lifecycle close endpoint only for projects
the helper opened. Projects that were already open before `closeout` must remain
open. Lifecycle opens use macOS background activation by default to reduce focus
stealing; when the target IDE is not already running, the helper launches the app
hidden first and then asks the plugin to open the exact worktree. Use
`--foreground-open` only when debugging IDE launch behavior.
Lifecycle closeouts are serialized by a bounded local lock. If another closeout
is already opening, inspecting, or cleaning up a project, wait for it or increase
`--lifecycle-lock-timeout-ms`; do not start parallel auto-open closeouts and
expect independent IDE windows to race safely.

Auto-open is allowed only for worktrees under globally trusted roots. Before a
lifecycle auto-open, the helper adds the matching trusted root to the selected
JetBrains product's Trusted Locations config, ensures project opening is set to
new-window/no-prompt, launches the selected IDE hidden if no matching plugin is
already running, then asks the running inspection plugin to schedule the exact
worktree open. The helper polls until the exact route appears before it
inspects; a lifecycle open response alone is not proof that the IDE finished
opening the project.
Configure trusted roots in `${CODEX_HOME:-$HOME/.code}/jetbrains-inspection.json`:

```json
{
  "jetbrains": {
    "trustedAutoOpenRoots": ["/Users/me/Developer", "/Users/me/.code/working"]
  }
}
```

If an exact worktree is not already open and is outside those roots, `closeout`
must fail before opening the IDE. Do not use random temp directories for agent
inspection worktrees.

If multiple JetBrains products are installed, repo config or CLI arguments must
select the intended IDE so the helper updates the right Trusted Locations file.
If a first-time open still stalls after trusted-location and project-opening
policy seeding, treat it as a blocker: check for unsupported IDE config layout,
settings sync overwriting the config, a missing inspection plugin, or a product
that accepted the scheduled open but never registered the worktree. Real-session
smokes have validated unattended closeout on IntelliJ IDEA, PyCharm, and
WebStorm 2026.1 with trusted worktrees under `$HOME/.code/working`.

`run` is retained as an iterative/backward-compatible inspection loop: prepare
the exact worktree when needed, trigger, wait, fetch problems, and clean up any
project it opened. Prefer `closeout` in final validation notes so cleanup status
is explicit. Use `--include-stale` only when explicitly diagnosing cached stale
findings; stale results still exit non-zero and are not clean.

## When To Run

- During the edit loop after meaningful code changes.
- Before saying code is ready, safe to push, safe to merge, or safe to hand off.
- When repo instructions mention JetBrains, PyCharm, IntelliJ IDEA, WebStorm,
  IDE warnings, static analysis, or inspection quality gates.
- When normal tests pass but IDE-only analysis may catch framework/plugin issues.

For tiny docs-only or non-code edits, record a concise not-run reason when an
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

If config is absent, the helper infers from git and the current working tree.

## Worktree Safety

Inspect the worktree being edited. Do not silently inspect the main worktree
when Code is operating in a linked worktree. If routing resolves to another
worktree, treat that as a blocker unless the user explicitly approves it.

For closeout/readiness, require an exact worktree route. A containing main
checkout is not enough; `closeout` may open the linked worktree in the preferred
IDE and must clean it up afterward when it owns the open.

## Result Policy

- `clean`: inspection passed for the selected scope.
- findings: fix real findings in touched code before calling work ready.
- readiness closeouts should use `closeout`, not plain `status`. If lifecycle
  cleanup is skipped or fails for a helper-opened project, the closeout is not
  clean; report both the inspection result and cleanup reason.
- `status` is informational and exits zero only when the helper can retrieve a
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
