# Inspection Configuration

Read before defining or diagnosing lane routing, preparation configuration,
preparation receipts, or preparation override flags.

For the macOS native backend's independently activated worker, host configuration,
snapshot preparation and recovery, read [native PyCharm](native-backend.md).
`jetbrains.nativeWorker` belongs in the shared helper config, not repository
metadata or a desktop IDE profile. The API lane/lifecycle behavior below remains
an explicit API-session route for other supported language models.

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
When no lane runs, the aggregate is `UNKNOWN` with
`no_required_lane_files`; empty selection does not prove inspection readiness.
Optional-lane failures remain visible without changing the required-lane
aggregate, including when only optional lanes run. When `lanes` is absent, the existing single-IDE path remains
unchanged.

For an intended API session in lane-configured repositories, start
`agent-inspect --backend api`, `inspect`, or `inspect-closeout` directly with the
desired scope. These API commands are prohibited when the task forbids desktop
IDE use. They perform
shared repository preparation before opening any non-empty lane, then select
each lane's IDE and optional `projectPath`. No separate `open-worktree` or
top-level IDE is needed.

`open-worktree` opens one project, not the lane set. When only opening a project
at the worktree root without inspecting, select that lane's IDE explicitly:

```bash
uv run "$HELPER" open-worktree --repo "$PWD" --ide PyCharm
```

Use the IDE declared by the intended lane; PyCharm above is an example. To
open a nested `projectPath` without inspecting, pass that directory as `--repo`
when it contains project markers such as `settings.gradle` or `.idea`:

```bash
uv run "$HELPER" open-worktree --repo "$PWD/<projectPath>" --ide "IntelliJ IDEA"
```

Preparation still runs at the worktree root. The assessment flow resolves
`projectPath` automatically, including directories without project markers.

The Python generator stores its module file under `.idea` and anchors its
content, test roots and exclusions one directory above that module file, at
the worktree root. Generated metadata is not the source root.
`prepare-worktree` and `prepare` are compatibility aliases for `open-worktree`
and have the same single-project behavior.

For a mixed JVM/Python repository, `prepare.python.projectPath` selects an
existing nested Python project directory, for example `scripts`. Give the
Python lane the same `projectPath`, and keep the JVM lane at the repository
root. The generator creates the nested `.venv` and `.idea` without replacing
the root Gradle/Java model. `testRoots`, `pep723Scripts` and
`requiredGeneratedState` are relative to that preparation project; receipts
report their paths relative to the Git worktree. Ignore those generated files
before preparing. The path must be relative and stay inside the exact worktree,
including after symlink resolution. Git mutation checks still cover the whole
worktree, and only the nested Python project receives SDK provisioning. Omit
the field for root preparation. The Qodana and native backends currently refuse
this nested preparation shape; configured API lanes require an explicit API
session, for example `agent-inspect --backend api`.
[SKILL.md](../SKILL.md) owns selection. This does not authorize opening a desktop IDE.

An assessment invoked with `--native-project-scope --repo <projectPath>`
opts into one declared nested project, uses that lane's IDE and
keeps the requested native scope, including `whole_project`, inside that
project. This lets dedicated smoke projects retain native broad-scope proof
without widening their request to the parent repository. A native request
that does not name exactly one nested lane refuses before inspection.
Without this opt-in, root and subdirectory assessments dispatch the selected
worktree lanes.

When `qualityGate.inspection.prepare` is configured, `open-worktree`,
`agent-inspect`, `inspect`, and `inspect-closeout` run that exact repository
command in the exact target worktree before IDE lifecycle open or claim.
`claim-worktree` and read-only commands do not run preparation. Automatic
execution is allowed only below a configured trusted auto-open root; an
untrusted root returns an actionable manual-preparation result and never runs
repository-controlled argv.
The helper uses `shlex`-validated argv with `shell=False`, a dedicated bounded
`--repository-preparation-timeout-ms`, and a recursion guard. It snapshots Git
status plus the worktree's index bytes before and after. Nonzero exit, timeout,
tracked mutation, hidden index mutation, missing `requiredGeneratedState`, or
recursive invocation is a terminal preparation result. Do not continue with an
unprepared project model or substitute a different command.

The structured Python generator reads the selected IDE configuration's
`options/jdk.table.xml` and binds the generated project/module to a registered
Python SDK with the same normalized worktree-local `.venv/bin/python` home.
SDK display names may differ from environment paths. Interpreter symlinks are
not resolved to a shared base Python, so another worktree's SDK cannot match.
Multiple matching SDK names or an unreadable table block preparation. A missing
table or matching SDK uses the existing provisional path-based name and reports
that IDE registration is still unconfirmed; inspection readiness must independently
prove registration and assignment. The generator never edits the global SDK table.
For direct generator use, `--sdk-table` selects the same read-only input; normal
agent flows supply it from the resolved IDE, without repository-specific paths.
An explicit `JETBRAINS_INSPECTION_IDE_CONFIG_DIR` also selects this read-only
SDK table, keeping isolated IDE preparation consistent with its trust settings.
For lane-configured repositories, shared Python preparation resolves the unique
PyCharm lane before opening any project. A sole non-PyCharm lane can supply its
own SDK table. Ambiguous lane ownership uses the provisional fallback rather
than an unrelated top-level IDE selection, even when a config override is set.

After opening and claiming a project, the lifecycle helper uses the plugin's
explicit Python SDK preparation operation when version 1 of that capability is
advertised. This is part of configured `prepare.python` behavior. It requires
server-proven helper ownership and a prepared `.venv` in that exact project;
WebStorm lanes and user-owned windows are left alone. The plugin registers or
reuses the exact local interpreter through IDE APIs and assigns the real Python
modules, refusing conflicting SDK assignments. Registration remains separate
from the subsequent inspection proof. The operation's result is recorded as
`python_sdk_preparation` in preparation output, compact agent diagnostics, and
durable outcome records. A failed request is not retried automatically.
SDK preparation has its own 60-second server deadline and a 75-second HTTP
budget; `--prepare-timeout-ms` still controls each route/readiness wait. If a
timed-out SDK worker has not exited, project cleanup retains the exact lease
and reports `python_sdk_preparation_in_progress` with a deferred cleanup action.

For an otherwise eligible, helper-owned Python project, older plugins keep the
existing IDE discovery path and report `plugin_capability_unavailable` in this diagnostic. If registration remains
missing, configure the worktree interpreter in the selected IDE and rerun
preparation. Read-only commands never invoke the provisioning operation.

Successful preparation writes a bounded durable receipt under the helper cache.
For repositories of standalone scripts, structured `prepare.python.pep723Scripts`
accepts up to 25 worktree-relative script paths. The generator reads their
PEP 723 metadata through uv's native script support. A script named `setup.py` is
compiled from a temporary copy with a script filename so uv reads its PEP 723
metadata instead of treating it as setuptools project metadata. Select scripts whose
dependencies cover the inspected files; update the set when that scope adds
dependencies. The generator resolves their
`dependencies` arrays together with `uv pip compile`
for the selected Python version. The complete generated command, including
the selected SDK-table path, must fit the helper's existing command reporting
bound; oversized sets fail before execution rather than supplying a partial
manual rerun. An explicit preparation skip or a reusable receipt can still
proceed when only the added SDK-table path makes the runtime command too long.
Choose fewer representative scripts covering the inspected
dependencies, or shorter paths, when that diagnostic appears. Resolution runs
before changing the worktree SDK. Resolution
failure, including conflicting pins, stops preparation with a diagnostic naming
the script set. `uv pip sync` installs the resolved set into `.venv`, removing
dependencies from previous sets. Use a compatible set for the inspection;
keep dependency pins in script metadata rather than copying them into config.
This option is separate from root-project `sync` and cannot be combined with it
or its `extras`. For example:

```json
{"python": {"version": "3.12", "moduleName": "scripts", "pep723Scripts": ["scripts/install.py", "scripts/test_install.py"]}}
```

Script contents participate in receipt validation, so changing their metadata
or removing a configured script invalidates preparation reuse.

The receipt is reused only when the command/configuration hashes, exact worktree
identity, post-preparation Git state, and required generated state still match.
For structured Python, required state always includes the generated module,
`modules.xml`, and `misc.xml` as well as `.venv`. File contents, the generator
revision, and the selected SDK table are included in receipt validation, so a
renamed SDK or removed/rewritten ignored module invalidates a previous receipt.
Use `--skip-preparation` or `--no-repository-preparation` only after manually
running the configured command, and use `--force-preparation` or
`--force-refresh-preparation` to refresh a valid receipt. Preparation is
idempotent and may create ignored local environment state, but it must not leave
tracked-file mutations or hidden index mutations before inspection begins.

Python preparation bounds content indexing by excluding recognized ignored
build and cache directories. A candidate containing tracked files, or a
declared test root, stays in content. These exclusions do not remove the SDK's
library roots or disable inspections. Their generated module model participates
in the same preparation receipt and mutation checks as the other project files.
