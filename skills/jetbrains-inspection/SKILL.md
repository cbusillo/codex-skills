---
name: jetbrains-inspection
description: Use JetBrains inspections through isolated headless native PyCharm, IntelliJ IDEA and WebStorm workers on macOS; trigger for code changes, readiness checks, PR/push validation, IDE warnings, inspection triage, or when code quality should be driven toward zero actionable IDE findings.
metadata:
  short-description: Run JetBrains IDE inspections safely
resources:
  - path: scripts/prepare-native-worker.py
    kind: script
    description: Prepares a fresh isolated macOS worker and prints its nativeWorkers entry without activation data.
  - path: scripts/jb-inspect.py
    kind: script
    description: Native-worker inspection helper with outcome logging and retirement of IDE state the desktop route left behind.
  - path: tests/test_jb_inspect.py
    kind: reference
    description: Regression tests for the helper's lane routing, outcome logs and worktree retirement.
commands:
  - name: jetbrains-inspection-prepare-native-worker
    source: skill
    resource_path: scripts/prepare-native-worker.py
    example_argv: ["uv", "run", "scripts/prepare-native-worker.py", "--product", "pycharm", "--destination", "/absolute/new-worker", "--app", "/Applications/PyCharm.app", "--cache-root", "/absolute/existing/host-caches/native-inspection"]
    purpose: Creates a new worker profile and locked activation launcher; never starts an IDE or copies an existing profile.
  - name: jetbrains-inspection-agent-inspect
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "agent-inspect", "--repo", "$PWD", "--scope", "changed_files"]
    purpose: Runs one assessment on an isolated native worker and emits a compact terminal result envelope.
  - name: jetbrains-inspection-summarize-outcomes
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "summarize-outcomes"]
    purpose: Summarizes outcome logs diagnostically without inspecting.
  - name: jetbrains-inspection-cleanup-helper-leases
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "cleanup-helper-leases", "--no-dry-run"]
    purpose: Reconciles stale leases the retired desktop route left, under the lifecycle lock.
  - name: jetbrains-inspection-remove-worktree
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "remove-worktree", "--repo", "/exact/task/path"]
    purpose: Dry-runs local retirement proof; with --no-dry-run repeats the safety checks before non-force linked worktree removal.
  - name: jetbrains-inspection-cleanup-helper-sdks
    source: skill
    resource_path: scripts/jb-inspect.py
    example_argv: ["uv", "run", "scripts/jb-inspect.py", "cleanup-helper-sdks"]
    purpose: Dry-runs helper-owned removed-worktree SDK cleanup through running IDEs.
policy:
  command_policies:
    - id: prefer-jb-inspect-for-plugin-http
      match:
        shell_regex: "\\b(curl|wget|http)\\b.*\\b(127\\.0\\.0\\.1|localhost)[:/]\\S*/(api/)?inspection\\b"
      action: require_preferred
      message: Agents do not inspect through the desktop JetBrains plugin's HTTP API. Use the inspection helper, which runs an isolated native worker.
      preferred:
        - kind: script
          path: scripts/jb-inspect.py
          example_argv: ["uv", "run", "scripts/jb-inspect.py", "agent-inspect", "--repo", "$PWD", "--scope", "changed_files"]
          purpose: Runs one assessment on an isolated native worker and emits a compact terminal result envelope.
---

# JetBrains Inspection

Apply [task scope and authorization](../references/execution-scope.md) when
using this workflow; it defines how existing approval and task boundaries apply.

Use the script-backed helper to run and interpret JetBrains inspections. Every
agent assessment runs on an isolated headless native worker; agents never
inspect through the desktop IDE or its Inspection API plugin, which stays a
Marketplace product for people. Do not call the plugin's HTTP or MCP API.

On macOS, `agent-inspect` routes Python/YAML selections and Odoo
Python/XML/YAML selections in root PyCharm lanes to native PyCharm when an
eligible independent worker, interpreter and profile are configured. In a
repository whose configured IDE is WebStorm, npm or manifest-pinned Yarn 4 and
pnpm projects, including a nested WebStorm lane, route TypeScript, JavaScript
and JSON selections to native
WebStorm, and in one whose configured IDE is IntelliJ IDEA, root Gradle projects
route Kotlin and Java selections that include a `.kt` file to native IntelliJ
IDEA. Any other selection, and any host without an eligible worker, returns
UNKNOWN with `isolated_backend_unavailable` before starting a worker, naming
the files, lanes or prerequisite behind it and a next step that fits. Keep that coverage UNKNOWN; there is no fallback route.
Read [native backend](references/native-backend.md) before configuring,
diagnosing or changing that runner. It uses the independently Director-activated
worker in place, with its shared exclusive lock and private system/cache/log
paths. Never copy, export or move its config, activation, accounts, licence or
credentials, or read activation-bearing files. On a licence prompt, lapsed
activation or trial offer, stop the licensed run and post an `Owner question:`
on the item. The Director restores activation through the normal product UI
using existing entitlements.

A repository declares the IDE plugins its inspections need in the
JetBrains-standard Required Plugins file, `.idea/externalDependencies.xml`. A
required plugin the selected worker lacks or disables makes the run UNKNOWN
before launch, naming the plugin and the worker to install it in; see
[required plugins](references/native-backend.md#required-plugins).

In repositories with inspection lanes, native selection follows the lane
router and omits unmatched files (such as Markdown). Receipts name unmatched
and excluded files. Explicit `--scope files` overrides lane exclusions;
implicit scopes honor them. Run one capped, niced headless worker at a time and
pause above load1 64. Report the backend and coverage with every verdict.
Missing or unproven required checks remain UNKNOWN. Native retains current
findings alongside UNKNOWN and separates registration from positive execution
evidence.

When IDE configuration appears in the change set, use
`../references/ide-configuration-policy.md` before deciding whether to keep,
split, revert, ignore, or remove it. Inspect tracking state, the exact diff,
ignore rules, repository policy, and history. Preserve the canonical shared form
plus only safe hunks in mixed tracked files; never use blanket commit, revert,
clean, or stash operations that can absorb machine-local or unrelated IDE state.
Do not stage untracked, non-ignored IDE configuration automatically; check
repository policy and ask when the sharing decision remains unclear.

## Primary Helper

Run the helper from the client repository with `uv run`, naming it through this
skill's base directory. `<skill-dir>` is the folder that holds this `SKILL.md`;
your host shows it when the skill loads.

```bash
uv run <skill-dir>/scripts/jb-inspect.py \
  agent-inspect --repo "$PWD" --scope changed_files
```

Command model:

- `agent-inspect`: the assessment. It emits a compact JSON envelope and exits
  successfully whenever it produced an `agent_result`. Native preparation runs
  inside its private snapshot from structured `qualityGate.inspection.prepare`
  (see [inspection configuration](references/inspection-config.md)); there is
  no separate open or preparation step. `inspect` and `inspect-closeout` are
  aliases. `--backend native` forces the native worker for one run.
- `summarize-outcomes`: diagnostic verdict/bucket/retry summary of the outcome
  log, without inspecting.
- `remove-worktree` uses local cleanup proof and never contacts a desktop IDE.
  It retains worktrees with legacy leases or IDE state requiring cleanup.
  `cleanup-helper-sdks` and `cleanup-helper-leases` remain separate legacy
  cleanup commands; they contact only already-running IDEs and never open a
  project or launch an app. Read [IDE cleanup](references/ide-cleanup.md)
  before using them.

Read verdict and retry permission from `agent_result` and `retry_policy.retry`,
not the process exit code. A RED assessment with more findings than the compact
output shows names `findings_artifact`; read that file instead of rerunning.
Every assessment, including UNKNOWN, is appended to the outcome log under
`${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection/` (`outcomes.jsonl`,
plus `unknown-verdicts.jsonl` for UNKNOWN). Set `JB_INSPECT_OUTCOME_LOG` or
`JB_INSPECT_UNKNOWN_LOG` to a path to move a log, or to `0` to disable it.

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

`changed_files` covers working-tree changes, not committed branch changes.
For a committed change, use `--scope files` with a repeatable `--file` for each
tracked file. When constructing shell arguments, use an array such as
`args=(--file "src/a.py" --file "src/b.py")` and pass `"${args[@]}"`; a string
of flags can reach the helper as one argument, especially in zsh.
The compact result reports `selected_file_count` (null when unavailable).
Selection counts describe the requested scope, not successful IDE execution;
read the verdict and coverage too. A scope that resolves to zero files is
UNKNOWN and supplies file-selection advice.

Start narrow while iterating: changed files, touched files, or touched directory.
For final readiness, apply repository requirements and the whole-project
inspection default and exceptions in
[repo-readiness Gate Selection](../repo-readiness/SKILL.md#gate-selection).
Broaden when required coverage, changed behavior, or findings warrant it. Reuse
a current clean result covering the required scope for the same revision and
environment. Complete every required repo gate; an ordered scope preference is
not by itself a requirement to run each scope in succession.
Before defining or diagnosing lane routing or preparation configuration, read
[inspection configuration](references/inspection-config.md).

If config is absent, the helper infers from git and the current working tree. For
a one-off inspection, a missing inspection config can use the safe default
`changed_files` scope. Do not silently turn that inference into durable repo
policy. If the configured IDE, scope or lanes are blank, contradictory, or feel
wrong for the active worktree, ask the user before changing policy or treating
the value as authoritative; otherwise report the mismatch as a not-clean
readiness blocker. A repository without inspection metadata should record its
IDE under `qualityGate.inspection` in `.github/github.json`, naming the IDE that
fits its main language; recommend it and ask before writing it, because it is
durable repository policy.

## Worktree Safety

Inspect the worktree being edited. Do not inspect the main worktree when the
agent is operating in a linked worktree. The native worker snapshots the exact
worktree's tracked and nonignored source into a private project and never opens
the source worktree. Before an assessment, await builds, installs, generators,
formatters, Anthropic or Google model reviews (their `.model-review-*` scratch
lives in the checkout), and other same-worktree writers, then avoid new writes
until the result returns; a source input that changes during the run makes it
UNKNOWN.

## Result Policy

- `GREEN`: inspection worked and found no actionable findings for the selected
  scope/filter. Native `changed_files` and `files` runs count only findings on
  lines the change adds or modifies since the default branch's merge base, and
  all findings in new files. They report the rest as
  `preexisting_finding_count`; fix those only when they fall in the task's
  scope. When the merge base cannot be found, `changed_lines` is `unavailable`
  and every finding counts. Native spelling and grammar findings (`TYPO`,
  `GRAMMAR_ERROR`, `STYLE_SUGGESTION`) never make a verdict RED; they are
  listed beside it as `advisory_findings` with `advisory_finding_count`. Fix
  real mistakes, and add real tool, library and product names to the
  repository's tracked `.idea/dictionaries/project.xml` rather than renaming
  or suppressing them.
  An inspection a headless run cannot prove, such as `Eslint`, may be declared
  covered by one of the repository's own gates with
  `qualityGate.inspection.coveredBy` (see
  [inspection configuration](references/inspection-config.md#inspections-covered-by-repository-gates));
  it is then listed under `covered_by` instead of making the verdict UNKNOWN.
  An unproven grammar checker (`Grazie*`) is listed under
  `advisory_coverage_gaps`.
- `RED`: inspection worked and returned actionable current findings. Fix real
  findings in touched code before calling work ready.
- `UNKNOWN`: inspection did not prove green or red. Do not summarize this as
  "no problems found"; report the verdict reason and `next_action`, because the
  worker, its configuration, the selection, or the environment needs attention
  first. Native runs are not retried automatically and `retry_policy.retry` is
  false; resolve the reported prerequisite before one fresh assessment, and do
  not add a retry loop.
  When inspection evidence is used to qualify changes to this helper or another
  installed runtime-bound skill, compare the recorded helper/source revision
  with the intended landed revision or a fresh runtime-reconciliation receipt.
  A missing or mismatched revision makes the installed-runtime claim `UNKNOWN`;
  do not count it as current evidence. A repo-local helper may still provide
  valid branch evidence when its exact path and revision are recorded and match
  the source being evaluated.
- `helper_usage_error`: the arguments were not accepted. Use only the documented
  `agent-inspect` arguments and report the mismatch instead of trying another
  inspection command. Options of the retired desktop route, such as `--ide` or
  `--backend api`, return this result with the replacement named.
- Existing broad noise is not invisible. Fix straightforward findings in the
  affected area or track a cleanup item.

## Suppressions And Inspection Configuration

Prefer fixing code or narrowing the scope first. In every repository, an agent
may add a suppression only when it covers one line or one rule, never a whole
file, carries a written reason on the same line, and is named in the PR body
so the second-model review sees it. Reviewers may still reject it.

Disabling inspections, changing inspection profiles, or changing baselines
requires explicit approval unless the repo already has an established approved
convention.
Adding real names to a repository's tracked `.idea/dictionaries/project.xml`
needs no approval; keep it sorted and add only words that are not misspellings.

## Reporting

Report the compact helper envelope: verdict (`GREEN`/`RED`/`UNKNOWN`), backend,
scope, one-line finding summary with file and line when available, and next
action. Do not include raw `diagnostic` fields in normal reports; use them only
when debugging the helper. If not run or inconclusive, state a one-line not-run
or blocker reason and the next smallest useful action.
