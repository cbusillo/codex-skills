# Native PyCharm backend

Read before configuring, diagnosing or changing the native runner. On macOS,
`agent-inspect` defaults to stock PyCharm `inspect.sh` for Odoo repositories and
Python-only selections in root PyCharm lanes. Other platforms retain their
existing selection. Explicit Qodana options still select Qodana. Use
`--backend native|api|qodana` or `JB_INSPECT_DEFAULT_BACKEND` for an explicit
choice. Native never starts a desktop IDE or falls back to its normal profile.

## Independent worker

The Director activates an independent profile through the product UI using
existing entitlements. On Chris-Studio this is **JBTEST-D1**, confirmed in
[#1833](https://github.com/cbusillo/codex-skills/issues/1833#issuecomment-6251613312).
Use it in place. Never copy, export or move its config, activation, account
store, licence or credential files, and never open activation-bearing files.
The agent reads only the public `idea.properties` launcher file and the
non-secret `options/jdk.table.xml` needed for its temporary SDK registration.
The product itself uses its existing activation normally.

Configure the **helper's** shared JSON config at
`~/.config/jetbrains-inspection/config.json`, outside the activated profile:

```json
{
  "jetbrains": {
    "trustedAutoOpenRoots": ["/absolute/trusted/source/root"],
    "nativeWorker": {
      "path": "/absolute/independently-activated/worker",
      "app": "/absolute/PyCharm.app",
      "frameworkRoots": ["/absolute/pinned-odoo-framework"]
    }
  }
}
```

The standard `JETBRAINS_INSPECTION_GLOBAL_CONFIG` override selects a helper
config. `--native-worker` (or `JETBRAINS_INSPECTION_NATIVE_WORKER`), `--ide-app`
and repeatable `--native-framework-root` provide one-run selections. Framework
roots must contain `odoo/release.py`; omit them for ordinary Python projects.
Record the framework commit, not just its directory name.

The worker contains `config/`, `plugins/`, `home/`, `idea.properties` and
`worker.lock`. Its properties must point config and plugins into that exact
worker. Its GUI launcher must hold the same kernel `worker.lock` for its whole
lifetime. Close that independent GUI before using the CLI. Never open the
Director's normal PyCharm to repair a native prerequisite. Public plugin ZIPs
can supply code, but never activation or credentials from another installation.

If activation has lapsed, a licence prompt appears, or any trial is offered,
the runner stops its process group, cleans its private state and writes a
count-only `native-activation-stop.json` beside the worker. Post an
`Owner question:` on the item asking the Director to restore the independent
profile through the normal product UI with existing entitlements. Do not start
a trial, purchase or grant access. After the Director's recorded confirmation,
the authorized continuation removes only that stop receipt before reassessing;
do not clear it merely to retry. Privacy-consent setup remains Director-held too.

## Execution and cleanup

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend native --repo "$PWD" --scope files --file addons/example/models.py
```

Selections support `.py` and Odoo `.xml` in a root PyCharm model. Unsupported
languages, filters or nested models remain UNKNOWN with a supported next step.
Use explicit source files when a broader scope also contains documentation or
other file types. Native assessment requires the same declared trusted roots
as host-side API preparation.

The runner snapshots tracked and nonignored source into a private Git project.
The shared credential-location guard refuses credential-named inputs before
reading or transferring them. It is not a content scanner; secrets under
arbitrary source filenames must remain outside project inputs or ignored by Git.
The exact source worktree is never opened by PyCharm. Named scope selection
limits inspection while other source remains available for references.

Structured `prepare.python` runs the maintained environment generator in that
snapshot, with an isolated preparation home. Otherwise the exact source
worktree must already have its documented `.venv`. Native does not guess a
Python version or read a desktop SDK table. A unique non-secret SDK entry is
added to the independent profile's table in place and removed after execution;
unrelated entries remain. The snapshot gets canonical project and framework
roots; no source project metadata is changed.

A per-user native lock and the independent profile's lock allow one worker.
The child has its own process group, nice 10, a 1,536 MiB JVM heap and two active
processors. The latter is a JVM concurrency cap, not an OS CPU quota. System,
cache, logs, temporary files and inspection results live in fresh private paths
under the selected helper cache. Runs pause above load1 64 and resume below
that threshold; the wall deadline continues while paused.

A guardian keeps the lock and watches the invoking process's lifetime pipe.
Parent exit, cancellation, timeout and licence events stop the owned process
group, including while paused. Raw engine logs and the private project are
deleted; only sanitized findings, coverage and cleanup receipts remain.
Use `JETBRAINS_INSPECTION_CACHE_DIR` under the host's designated task-evidence
root. The config, activation, plugin installation and independent home stay put.

If a crash interrupts cleanup, `native-run.json` beside the worker retains
custody. The next run reconciles only its journaled SDK and private directory,
checking the private ownership marker and storage identity. A live worker's
lock, changed storage or unproved ownership retains state. Recover without
inspection, using the same worker/app selection:

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend native --native-recover-only
```

Recovery does not clear activation stops. A killed guardian may leave the
engine holding the inherited profile lock until it exits; do not bypass it.

## Coverage and qualification

Results name `backend: native`, the selected scope, source/profile/helper and
framework provenance, coverage, findings and separate cleanup. Reports must
name the backend and any `coverage.unavailable` or `coverage.unproven` entries.

The effective descriptions prove registration only. Native findings prove
execution. Private positive controls exercise `DuplicatedCode`, `HttpUrlsUsage`,
Python type/unresolved checks, and Odoo's named `OdooPy` and `OdooXml` checks.
They are included in the exact native scope and excluded from user findings.
Debug traversal observations must also cover every selected physical file.
Source, selection and profile changes invalidate the result.
Oversized files and scoped disabled-tool overrides cannot borrow the private
controls' proof; they remain UNKNOWN until their selected-file execution is
proved by a qualified route.

`coverage.profile_requested` lists the repository's enabled entries;
`not_applicable` separates other-language tools from this source selection.
Applicable requested tools without positive execution evidence remain unproven,
even when registered and the engine exits successfully. Any such gap makes the
verdict UNKNOWN, with current findings retained. No missing check is accepted.
This conservative rule can keep a completed clean run UNKNOWN; it is not a
claim that every unproven tool failed to execute. Complete execution evidence
and zero user findings permit GREEN; current findings with complete evidence
produce RED. Cleanup failures cannot produce GREEN or RED.

The offline routing, lock, coverage, disposal and recovery tests need no IDE.
The opt-in `JB_NATIVE_LIVE=1` test in `tests/test_native_backend.py` uses the
tracked planted/corrected Odoo fixture through the configured independent
worker. It requires actual Odoo XML, quoted `odoo.model.*` type, duplicate and
named Odoo findings, with corrected controls clean. `JB_NATIVE_LIVE_OUT` can
retain its sanitized result. Qualify with a real tenant change too; a synthetic
RED alone does not prove that repository's profile or model is complete.
