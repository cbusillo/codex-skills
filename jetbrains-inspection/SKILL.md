---
name: jetbrains-inspection
description: Use JetBrains IDE inspections through the local inspection plugin; trigger for code changes, readiness checks, PR/push validation, IDE warnings, inspection triage, worktree-safe inspection routing, or when code quality should be driven toward zero actionable IDE findings.
---

# JetBrains Inspection

Use this skill to run and interpret JetBrains IDE inspections through the local
inspection plugin HTTP API. Prefer the bundled helper over direct curl or MCP
tool calls.

## Primary Helper

Run the helper from this skill's `scripts/jb-inspect.py` path with `uv run`.
In the common user-skill install, that path is:

```bash
uv run ~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py run --repo "$PWD"
```

If this skill was loaded from a repo-local or temporary path, use that loaded
skill path instead of `~/.code/skills/...`.

Useful commands:

```bash
HELPER=~/.code/skills/jetbrains-inspection/scripts/jb-inspect.py
uv run "$HELPER" list
uv run "$HELPER" route --repo "$PWD"
uv run "$HELPER" run --repo "$PWD" --scope changed_files
uv run "$HELPER" status --repo "$PWD"
uv run "$HELPER" problems --repo "$PWD" --severity error
```

`run` is the default inspection loop: resolve route, trigger, wait, fetch
problems, and exit non-zero for unresolved findings or inconclusive states.

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

## Result Policy

- `clean`: inspection passed for the selected scope.
- findings: fix real findings in touched code before calling work ready.
- `status` is informational and exits zero only when the helper can retrieve a
  route-pinned status that is not stale, inconclusive, unavailable, ambiguous,
  indexing, running, timed out, or session-drifted.
- `stale_results`, `capture_incomplete`, timeout, indexing, session drift,
  ambiguous route, or unavailable IDE: not clean; retry, narrow scope, open the
  project in the preferred IDE, or report the blocker.
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
