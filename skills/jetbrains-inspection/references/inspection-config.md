# Inspection Configuration

Read before defining or diagnosing lane routing or preparation configuration.

For the macOS native backend's independently activated worker, host configuration,
snapshot preparation and recovery, read [native backend](native-backend.md).
`jetbrains.nativeWorkers` belongs in the shared helper config, not repository
metadata or a desktop IDE profile.

The helper reads `.github/github.json` when present:

- `qualityGate.inspection.scopePreference`
- `qualityGate.inspection.ide`
- `qualityGate.inspection.lanes`
- `qualityGate.inspection.profile`
- `qualityGate.inspection.prepare` (structured `python` object)
- `qualityGate.inspection.coveredBy`
- `jetbrains.scopePreference`

`ide` names the product that inspects the repository: `PyCharm`,
`IntelliJ IDEA` or `WebStorm`. [SKILL.md](../SKILL.md) lists which selections
each native worker serves; anything else is UNKNOWN.

Mixed-language repositories may replace the single
`qualityGate.inspection.ide` with ordered `qualityGate.inspection.lanes`. Each
lane names a unique `id`, an `ide`, whether it is `required`, repository-relative
`include` globs, optional `exclude` globs, and an optional repository-relative
`projectPath` directory for a nested project. The helper resolves the selected
scope once, validates every file and lane project path against the exact
worktree, assigns files by first matching lane, and records unmatched and
explicitly excluded files. Exclusions apply to ordinary changed-file, directory,
and whole-project readiness; an explicit `files` scope records an override and
still selects the file. A native run covers one worker's product at a time.
PyCharm and IntelliJ IDEA require root lanes. WebStorm also supports one nested `projectPath`: every
selected file must lie beneath it, and dependencies are prepared from that
project's npm, pinned Yarn 4 or pinned pnpm lockfile. Its engine model and findings remain
relative to the repository root. Mixing projects or products is UNKNOWN;
inspect each project's files in a separate assessment. Structured
`prepare.python` may coexist with WebStorm lanes and is ignored by WebStorm; PyCharm still requires root Python preparation.

## Python preparation

Native PyCharm runs structured root `prepare.python` inside its private
snapshot, never in the source worktree:

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
          "extras": ["dev"]
        }
      }
    }
  }
}
```

Without it, the exact source worktree must already have its documented `.venv`.
Native does not guess a Python version. A command string, or a `projectPath`
inside `prepare.python`, is not run; the selection is UNKNOWN with
`native_preparation_shape_unsupported` unless the root `.venv` is prepared and
`--backend native` is selected explicitly. Structured preparation rejects
unknown fields, path traversal and extras without sync. `requiredGeneratedState`
is accepted for older configurations and has no effect.

For repositories of standalone scripts, `prepare.python.pep723Scripts` accepts
up to 25 worktree-relative script paths in place of `sync`. The generator reads
their PEP 723 metadata through uv's native script support and resolves their
`dependencies` arrays together with `uv pip compile` for the selected Python
version; conflicting pins stop preparation with a diagnostic naming the script
set. Select scripts whose dependencies cover the inspected files, and keep
dependency pins in script metadata rather than copying them into config. This
option cannot be combined with `sync` or its `extras`. For example:

```json
{"python": {"version": "3.12", "moduleName": "scripts", "pep723Scripts": ["scripts/install.py", "scripts/test_install.py"]}}
```

## Inspections Covered By Repository Gates

Some IDE inspections, such as `Eslint`, run as annotators and leave no per-file
proof in a headless run. A repository may declare that one of its own gates
covers such an inspection:

```json
{
  "qualityGate": {
    "lint": { "eslint": "npm run lint" },
    "inspection": { "coveredBy": { "Eslint": "lint.eslint" } }
  }
}
```

Each key is an inspection short name; each value is the dotted key of a gate
already declared in the same file. A declaration naming a missing or empty gate
is refused as `native_covered_by_invalid`. A covered inspection that the run
cannot prove is reported under `covered_by` rather than making the verdict
UNKNOWN; its findings still count when the IDE does report them. Declare only
inspections whose gate runs the same tool in readiness and CI; nothing is
covered by default.
