# Inspection Configuration

Read before defining or diagnosing lane routing, preparation configuration,
preparation receipts, or preparation override flags.

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
For lane-configured repositories, shared Python preparation resolves the unique
PyCharm lane before opening any project. A sole non-PyCharm lane can supply its
own SDK table. Ambiguous lane ownership uses the provisional fallback rather
than an unrelated top-level IDE selection.

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

Older plugins keep the existing IDE discovery path and report
`plugin_capability_unavailable` in this diagnostic. If registration remains
missing, configure the worktree interpreter in the selected IDE and rerun
preparation. Read-only commands never invoke the provisioning operation.

Successful preparation writes a bounded durable receipt under the helper cache.
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
