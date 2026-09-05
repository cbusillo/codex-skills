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

