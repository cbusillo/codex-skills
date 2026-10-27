# Native PyCharm backend

Read before configuring, diagnosing or changing the native runner.
[SKILL.md](../SKILL.md) owns backend selection; the result names its route and
any native ineligibility reason. Native runs stock PyCharm `inspect.sh` without
opening a desktop IDE or falling back to its normal profile.

## Independent worker

The Director activates an independent profile through the product UI using
existing entitlements. On Chris-Studio this is **JBTEST-D1**, confirmed in
[#1833](https://github.com/cbusillo/codex-skills/issues/1833#issuecomment-6251613312).
Use it in place. Never copy, export or move its config, activation, account
store, licence or credential files, and never open activation-bearing files.
The agent reads only the public `idea.properties` launcher file, the
non-secret `options/jdk.table.xml` needed for its temporary SDK registration,
and, for [required plugins](#required-plugins), plugin descriptors and
`config/disabled_plugins.txt`.
The product itself uses its existing activation normally.

Prepare a fresh worker on macOS with one command. Its destination's parent and
host cache directory must already exist on the intended storage:

```sh
uv run <skill-dir>/scripts/prepare-native-worker.py \
  --product pycharm --destination /absolute/new-worker \
  --app /Applications/PyCharm.app \
  --cache-root /absolute/existing/host-caches/native-inspection
```

Choose a product and its installed app. WebStorm also needs `--node /absolute/bin/node`
with executable `npm` beside it; IntelliJ IDEA needs `--jdk /absolute/java-home`
with `bin/java` and `release`. Existing destinations are refused. Merge the printed
`config.jetbrains.nativeWorkers` entry into `~/.config/jetbrains-inspection/config.json`,
preserving other settings and permitted `trustedAutoOpenRoots`. The older
`nativeWorker` object is the PyCharm entry; use it or `nativeWorkers`, never both.

The only human setup step is to open the printed `.command` launcher, sign in
with an existing entitlement, and quit. It isolates config, plugins, home, system,
log and temp paths and holds the backend's `worker.lock`. IDE restarts retain that
lock and paths, using JetBrains' [restart exit-code mode](https://github.com/JetBrains/intellij-community/blob/master/platform/platform-impl/src/com/intellij/util/Restarter.java).
That mode cannot run app-update commands; update the installed app normally,
then reopen the worker launcher. Preparation never launches an IDE or handles
activation data. Required plugins and Odoo framework roots are covered below.

### WebStorm

The configured `node` directory goes first on the preparation and engine PATH.
A missing or non-executable runtime is UNKNOWN with its setup step.

A selection routes to WebStorm when the repository's `ide` (or each owning
lane's `ide`) is WebStorm and every file is `.ts`, `.tsx`, `.mts`, `.cts`,
`.js`, `.jsx`, `.mjs`, `.cjs` or `.json`, with at least one script file so the
TypeScript canary applies. JSON-only selections and other files, including
`.css`, `.scss` and `.svelte`, stay UNKNOWN. In the snapshot the runner runs
`npm ci --ignore-scripts --no-audit --no-fund` with `--cache
cacheRoot/npm-cache` and no user or global npm config, honouring only the
repository's own `.npmrc`, through the same journalled, unlicensed preparation
path as Python. It needs a root `package.json` and `package-lock.json` and never runs
`npm install`. A root Yarn 4 project instead needs `yarn.lock`, an exact
`packageManager` pin in `package.json`, and executable `corepack` beside the
configured `node`. It runs that pinned Yarn with `install --immutable
--mode=skip-build`, disables scripts and repository `yarnPath` overrides, and
forces the `node-modules` linker inside the snapshot. Repository patches and
Yarn configuration stay available; Yarn and Corepack caches live under
`cacheRoot`, with a fresh HOME and no inherited user configuration or tokens.
Tracked Yarn plugins still load as trusted repository code; disabling lifecycle
scripts does not disable plugins. An exact Yarn 4 pin selects Yarn when both
lockfiles exist; otherwise `package-lock.json` retains npm preparation.
A structured root `prepare.python` may coexist with WebStorm lanes; only
PyCharm selections execute it. `pnpm-lock.yaml` is refused for now. The snapshot excludes
ignored files, so `node_modules` comes only from that step.

The snapshot's tracked `.idea` is used when it has `modules.xml`; otherwise a
minimal `WEB_MODULE` is generated. No SDK is registered. JavaScript, TypeScript
and JSON tools must execute on each selected file of their language, and a
TypeScript defect control and its clean twin (`WEBSTORM_CANARY_FILES` and
`WEBSTORM_CLEAN_TWINS` in `native_coverage.py`) must both behave. ESLint stays
UNKNOWN. `inspect.sh` does not report TypeScript compiler errors
([WEB-44161](https://youtrack.jetbrains.com/issue/WEB-44161)); `tsc`,
`svelte-check` and ESLint belong in the repository's tests and CI. The canary
rule and trace evidence still need a licensed run, and no WebStorm-specific
healthy licence lines are allowed. `--native-recover-only` recovers the PyCharm
worker unless `--native-worker` names another.

### IntelliJ IDEA

Use a `jdk` version the project's Gradle build supports. A missing or unusable
runtime is UNKNOWN with its setup step before any custody record.

A selection routes to IntelliJ IDEA when the repository's `ide` (or each owning
lane's `ide`, without `projectPath`) is IntelliJ IDEA, every file is `.kt`,
`.kts` or `.java`, and at least one is `.kt` so the Kotlin controls apply. Other
files, including Groovy `.gradle`, stay UNKNOWN. The repository needs a root
`build.gradle.kts` or `build.gradle`; the declared Python `prepare` is not used.

The snapshot's `.idea/modules.xml`, `.idea/*.iml`, `.idea/modules/`,
`.idea/gradle.xml` and `misc.xml` are removed, so a stale tracked model is never
inspected. The `jdk` is registered as a `JavaSDK` in the worker's SDK table under
the same journalled lease as the Python SDK, set as the project SDK so the Gradle
JVM resolves to it without a download, and removed afterwards. The engine gets
`JAVA_HOME` set to it and a shared `GRADLE_USER_HOME` at `cacheRoot/gradle-home`,
whose `gradle.properties` limits the detached Gradle daemon's idle life.
`inspect.sh` only schedules a Gradle sync it does not wait for, so the run first
launches the product's headless `warmup --project-dir=<snapshot>` on the same
private system, config and log paths. Warmup links and imports the root Gradle
build, executing the repository's build scripts, and the imported model carries
into `inspect.sh`. Warmup is a licensed launch under the same journal, licence
scan and `--timeout-ms` deadline; it must log `IDE Warm-up finished.`, both
Gradle stages below and a closed process group, and complete its licence
observation, or the inspection never starts. Both launches share one log
observation, so `inspect.sh` scans only bytes warmup has not. On a linked
project, stock `inspect.sh`'s Gradle configurator ends the analysis task without
inspecting anything, so `inspect.sh` runs with
`ide.inspect.activity.based.inspections.enabled=true`: it skips the configurators,
awaits project configuration and schedules no second sync. Gradle gives each
source set its own content root, such as `src/main`, so the selection scope also
matches each file's path below any parent directory. A file elsewhere that shares
that tail is traversed and makes the run UNKNOWN with
`native_unselected_file_traversal`. A cold Gradle cache needs network access and
can take minutes on the first run. `gradle-import.log` holds the build's raw
output, joins progress messages into unbounded lines, and is not an IDE log, so
it is not observed.

Before the private directory is deleted, and during recovery from the journal's
recorded `gradle_home`, Gradle daemons whose command line runs
`org.gradle.launcher.daemon.bootstrap.GradleDaemon` from that `gradle-home` are
stopped: first with each wrapper distribution's `gradle --stop`, then SIGTERM and
SIGKILL, each bounded. The receipt records `gradle_daemons_stopped`; a daemon that
survives keeps the private directory and custody. Recovery reclaims a private
directory without its ownership marker whose only content is a bounded
`project/.gradle` tree, which a daemon recreated after deletion.

The run counts only when its launches log both `[Gradle]: Gradle resolve stage
finished with success` and `[Gradle]: Gradle data import stage finished with
success`; otherwise it is UNKNOWN with `native_gradle_import_unproven`, or
`native_gradle_import_failed` after a cancelled import or an engine error before
the data import finished. Kotlin and Java tools must execute on each selected
file of their language. Two Kotlin control pairs (`INTELLIJ_CANARY_FILES` and
`INTELLIJ_CLEAN_TWINS` in `native_coverage.py`) are planted beside the first
selected `.kt` file, inside its source root: an unused variable with a used
twin, and a `map { }.joinToString()` chain that `SimplifiableCallChain` reports
only when the Kotlin standard library resolves, with a single-call twin, which
proves the imported model resolves that source root and its libraries. Batch
inspection does not report Kotlin compiler diagnostics such as unresolved
references unless the profile's `Annotator` runs highlight visitors. A profile
inspection this edition lacks, such as `DuplicatedCode` in IntelliJ IDEA's free
mode, stays UNKNOWN unless the repository's `coveredBy` names a gate for it.

The standard `JETBRAINS_INSPECTION_GLOBAL_CONFIG` override selects a helper
config. `--native-worker` (or `JETBRAINS_INSPECTION_NATIVE_WORKER`), `--ide-app`
and repeatable `--native-framework-root` provide one-run selections. Framework
roots include one Odoo core repository containing `odoo/release.py` and any
required addon dependency repositories, each as a clean pinned Git checkout.
Persist these as the PyCharm entry's optional `frameworkRoots` array of absolute
paths; omit it for ordinary Python projects.
Record the framework commits and follow the tenant workspace's declared dependency
locations and ref policy, rather than borrowing another tenant's roots. The
runner compares the selected addon's manifest and `workspace.toml` build series
with public `odoo/release.py`; an absent or mismatched declaration stays UNKNOWN.
For a different tenant or branch, supply its matching roots with repeatable
`--native-framework-root`, following that workspace's dependency refs.
For a declared `repos.shared_addons`, the runner resolves its location from the
source worktree or primary checkout, verifies the attached checkout belongs to
that Git repository, and requires HEAD to match the declared ref. Linked
checkouts are supported. Ref resolution uses existing local Git metadata; run
the workspace's maintained dependency setup/sync before assessment rather than
claiming those refs prove remote freshness. Context is checked again at the end.
No framework Python is executed to discover its version.
The canonical project content root includes `addons` as a source folder when present;
it does not create separate addon content roots that would change scope paths.

`cacheRoot` is an existing host cache directory outside the activated worker.
On Chris-Studio keep it under `/Volumes/Developer-Artifacts/host-caches/`. A missing
configured root is not recreated. Optional `cacheStorageIdentity` pins the same
public storage identity the helper uses in custody records; a changed identity
stops the run. `JETBRAINS_INSPECTION_CACHE_DIR` still places individual run artifacts
in task evidence. Preparation uses shared non-secret uv cache and Python-install
paths under `cacheRoot`, with a fresh preparation HOME; it forwards no account
store, token or desktop-home settings.

Close the independent GUI before using the CLI. Never open the Director's
normal IDE to repair a native prerequisite. Public plugin ZIPs can supply code,
but never activation or credentials from another installation.

If activation has lapsed, a licence prompt appears, or any trial is offered,
the runner stops its process group, cleans its private state and writes a
count-only `native-activation-stop.json` beside the worker. Post an
`Owner question:` on the item asking the Director to restore the independent
profile through the normal product UI with existing entitlements. Do not start
a trial, purchase or grant access. After the Director's recorded confirmation,
the authorized continuation removes only that stop receipt before reassessing;
do not clear it merely to retry. Privacy-consent setup remains Director-held too.

## Required plugins

A repository declares the IDE plugins its inspections need in the
JetBrains-standard Required Plugins file, `.idea/externalDependencies.xml`,
tracked with its shared IDE configuration:

```xml
<project version="4">
  <component name="ExternalDependencies">
    <plugin id="dev.ngocta.pycharm-odoo" min-version="2026.6" />
  </component>
</project>
```

Before any launch, each native run checks every listed plugin ID against the
selected worker. Bundled plugins come from the app's
`Contents/Resources/product-info.json` `bundledPlugins`, at the app's build
number. Installed plugins come from `META-INF/plugin.xml` in every jar of each
entry in the worker's `plugins/` directory; each ID found counts, and an
installed copy overrides a bundled version. Entries, `lib` folders and jars
that resolve outside `plugins/` are skipped. IDs in `config/disabled_plugins.txt`
are disabled. Optional `min-version` and `max-version` are inclusive and compare
only the leading dotted number of each version, ignoring trailing zeros and
anything after it, so `1.0`, `1.0.0`, `1.0-rc1` and `1.0-beta2` are equal. A
plugin whose version is empty or has no leading number never satisfies a bound.
A plugin that is absent, disabled or outside its range is UNKNOWN with
`native_required_plugin_missing`, naming the plugin and the worker; install or
enable it once through that worker's Plugins screen, then quit the worker.

The Required Plugins file, `plugin.xml` entries, `disabled_plugins.txt` and
`product-info.json` are each read only up to 4 MiB. The XML files and
`disabled_plugins.txt` must be UTF-8, and XML with a DOCTYPE or ENTITY
declaration is refused. A Required Plugins file that breaks these rules,
is not valid XML, has no `ExternalDependencies` component, or lists a plugin
without an `id` or with a bound that has no leading number is UNKNOWN with
`native_required_plugins_unreadable`. A plugin descriptor or
`disabled_plugins.txt` that breaks them, including a linked one, is UNKNOWN with
`native_worker_plugins_unreadable`; an unusable `product-info.json` keeps its
worker configuration reason. Without the Required Plugins file nothing
in the worker's plugins is read. Verified plugins are listed in the receipt's
`required_plugins`.

## Missing shared profile

`native_populated_profile_required` means the repository has no unique populated
profile matching its declaration. If a populated profile is already tracked,
point `qualityGate.inspection.profile` at that file. Otherwise obtain the product's
default project inspection profile. Tracking a new shared profile follows the existing Director-approval rule;
reuse approval already recorded. Track only
`.idea/inspectionProfiles/Project_Default.xml`, keep other IDE state ignored, and
set `qualityGate.inspection.profile` to that repository-relative path. Do not
disable or tune inspections to make a prerequisite pass. A fresh assessment then
reports actual findings or another explicit gap.

## Execution and cleanup

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend native --repo "$PWD" --scope files --file addons/example/models.py
```

PyCharm selections support `.py`, `.yaml`, `.yml` and Odoo `.xml` in a root PyCharm model;
WebStorm and IntelliJ IDEA selections are listed above. Unsupported
languages, filters or nested models remain UNKNOWN; the result names the files,
lanes or prerequisite behind it and a next step that fits, or says the coverage
is unavailable.
Use explicit source files when a broader scope also contains documentation or
other file types. Native assessment runs only for a repository under one of the
config's `trustedAutoOpenRoots`; `JETBRAINS_INSPECTION_TRUSTED_AUTO_OPEN_ROOTS`
overrides that list.

The runner snapshots tracked and nonignored source into a private Git project.
The shared credential-location guard refuses credential-named inputs before
reading or transferring them. It is not a content scanner; secrets under
arbitrary source filenames must remain outside project inputs or ignored by Git.
The exact source worktree is never opened by PyCharm. Named scope selection
limits inspection while other source remains available for references.

Structured `prepare.python` runs the maintained environment generator in that
snapshot, with an isolated preparation home. Otherwise the exact source
worktree must already have its documented `.venv`. Native does not guess a
Python version. YAML selections keep this prerequisite because each run includes
Python defect controls. Command-string and nested-project preparation remain ineligible
for automatic native selection; they return UNKNOWN before a worker starts. Explicit native uses the prepared root `.venv` instead
of treating those commands as a structured root generator. Native does not read a
desktop SDK table. A unique non-secret SDK entry is
added to the independent profile's table in place and removed after execution;
unrelated entries remain. The snapshot gets canonical project and framework
roots; its private `PyProjectModelSettings.usePyprojectToml=false` preserves that
model when uv workspace discovery would otherwise replace module roots and
break exact named scopes. No source project metadata is changed.

A per-user lock and the independent profile's lock allow one worker.
An ordinary request takes a ticket in its own queue under the per-user state
directory, using the helper's kernel-locked FIFO queue, then
waits for the control, per-user and profile locks without taking them from a
live holder.
Queue time is unbounded but interruptible: parent exit or cancellation stops a
queued request before launch. Waiting for the locks themselves, which covers
an independent GUI and older callers that take no ticket, is bounded by
`--lifecycle-lock-timeout-ms`. Admission time is recorded as
`admission_wait_ms` and does not consume `--timeout-ms`. The request then runs
its one inspection; activation stops and journal holds still refuse it. An
expired wait is `UNKNOWN/native_worker_busy` with no engine launch and the
original no-retry policy. `--native-recover-only` never waits.
The child has its own process group, nice 10, a 1,536 MiB JVM heap and two active
processors. The latter is a JVM concurrency cap, not an OS CPU quota. System,
cache, logs, temporary files and inspection results live in fresh private paths
under the selected helper cache. Above load1 64, a request at the head of the
queue waits for load to fall before taking any worker lock, within
`--lifecycle-lock-timeout-ms`, reporting `load_wait_ms`; it returns
`native_host_load_paused` only when that bound passes. Recovery-only requests
and the worker's GUI are not held up by that wait. An active run pauses above 64 and resumes below that threshold.
Time suspended by the helper does not consume `--timeout-ms`. The cumulative
pause allowance across preparation, warmup and inspection is bounded by
`--lifecycle-lock-timeout-ms`; exhausting it returns UNKNOWN with
`native_host_load_paused` and the usual owned-process cleanup. Cancellation and
parent-exit checks continue while paused. Outcome logs retain pause count and
duration alongside admission wait and total wall time.
An admitted run can hold the shared worker for `--timeout-ms` plus this pause
allowance and bounded cleanup. Admission has its own earlier load/lock wait;
FIFO queue time remains unbounded and interruptible.

A guardian in a separate session keeps the locks and watches the invoking
process's lifetime pipe, including when a harness kills the caller's whole group.
An additional control lock belongs only to the guardian. Before the licensed
engine can exec, a pipe gate waits for its PID, process-group ID, birth stamp
and exact private-path identity to be durably journaled. The gate closes across
exec, and the engine inherits only the global and profile locks.
Parent exit, cancellation, timeout and licence events stop the owned process
group, including while paused. Raw engine logs and the private project are
deleted; only sanitized findings, coverage and cleanup receipts remain. Every
received chunk is scanned for a licence event before another error can abort
observation. After kill/wait, a bounded licence-only drain covers remaining pipe
and log bytes. An incomplete observation cannot attest a known outcome without
a licensing event; a detected event remains a stop even when other bytes are lost.
Use `JETBRAINS_INSPECTION_CACHE_DIR` under the host's designated task-evidence
root. The config, activation, plugin installation and independent home stay put.

If a crash interrupts cleanup, `native-run.json` retains custody. Its immutable
identity matches `owner.json`; mutable phases record reservation, materialization,
SDK attempt, licensed launch and the terminal outcome. The exact UUID directory
is journaled before any source or environment write. Empty reserved directories
and a sole partial ownership marker can be reclaimed; other unproved state is
preserved. Recovery failures never attest that retained resources were closed.

One `active_process` record identifies the current preparation or inspection
group. Both use the same journal-before-exec gate and inherit only the shared
global/profile locks. Preparation is unlicensed; its interrupted process can be
recovered without an activation decision. The separate licensed-launch phase
retains its unknown-outcome hold even after process recovery. Older engine-only
journals remain recoverable through the same ownership proof.

The durable licensed-launch phase is a hold. Resource recovery alone cannot
clear an unresolved outcome and start another licensed process, with one
exception: before disposing of the private directory, recovery reads every
retained engine log. The hold clears, recording
`interrupted_run_cleared: licensed_execution_observed`, only when every log is a
regular file that reads completely, no line is a licensing event or mentions a
licence, trial or evaluation outside the two known healthy startup lines, and a
`GlobalInspectionContextImpl` log line shows local inspections running on a file
inside that run's project. The engine had then passed licensing. The next
assessment starts normally. Anything else keeps the hold. Output that only
reached the killed guardian's pipe cannot be read, so this rule relies on the
logs alone. A licensing
boundary persists independently in a count-only stop receipt or journal, even
when a later write fails. Artifact and stop-receipt persistence failures do not
skip independently permitted cleanup or lock release. SDK/private disposal
requires proved process-group exit; even a durable activation stop cannot
discard resources under an unproved group. The original run
ID survives the guardian's outer exception boundary.

Recover without inspection:

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend native --native-recover-only
```

If a known terminal result with no licensing event was returned but its journal update
failed, reconcile that saved JSON envelope for the same run and backend:

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend native --native-recover-only --native-reconcile-result /task-evidence/result.json
```

Reconciliation verifies the run identity, backend revision, known terminal
outcome, exited worker and explicit absence of an observed licensing event.
It refuses licensing results, existing activation stops and profile-local
inputs. It never starts an inspection or clears an activation stop. Without
terminal evidence, preserve `native_interrupted_run_unresolved`; it is not a
claim that activation expired. Escalate the missing outcome for review rather
than guessing or deleting the hold. A recorded Director confirmation after
checking the independent product UI can resolve activation uncertainty using
existing entitlements; never start a trial or inspect activation files. After
that recorded confirmation and a recovery receipt proving SDK/private cleanup,
the authorized continuation uses the supported exact-run release:

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend native --native-recover-only --native-release-run <recorded-run-uuid>
```

Use that flag only after the Director's recorded UI confirmation on the owning
item. It recovers SDK/private resources, verifies the exact run and any stop
receipt, and removes only matching helper metadata. It never inspects or changes
product activation, accounts or credentials and never launches an inspection.
A stop receipt without a remaining journal can be released by the same exact-run
command after that recorded confirmation. Never use the flag merely to retry.

A killed guardian may leave preparation or inspection holding both inherited
locks. Every run first validates the public launcher properties before creating
worker locks, then holds the separate control lock and checks the active child
before resource recovery. It proves the journaled PID birth, group and private
path identity, and stops that exact orphan group before taking the normal locks.
A bounded group-exit proof follows both a kill and an absent leader. Neither
leader exit nor released locks alone permits SDK/private deletion. A live
guardian or an unproved process identity is preserved.
A different process birth at the journaled PID proves reuse; recovery sends it
no signal and proceeds with resource disposal while retaining the outcome hold,
unless the retained logs clear it as described above.
Birth comparisons require the same recorded UTC/C-locale clock; legacy or
incomparable observations remain unproved.
The same birth with a changed command/group remains unproved. A dead leader
with live, unproved descendants remains UNKNOWN.
The group-exit check tolerates a transient permission error only within its
bounded wait; only an absent process group proves exit. A persistent error
retains the journal, SDK and private directory, with the PGID and last errno
in the receipt. KILL, STOP and CONT share a non-throwing signal observation;
none of their errors proves exit. Finalization bounds the leader reap as well
as group proof, and attempts licence draining even if signalling or reaping
fails. Both successful reaping and group absence are required to close custody.
A failed load-control signal aborts a still-live worker into this finalization.
After process closure is proved, an `ENOTEMPTY` removal failure may rescan the
same inode at most once per top-level entry in each contents pass, plus one root
rescan. Other errors and a second failure remain custody holds. Receipts
record counts, errno and fixed deletion stages, never raw paths or error text.
If final root removal fails, the exact ownership marker is restored through the
held directory descriptor without overwriting a new marker. Every root rescan
skips ownership markers. A replaced inode
or ownership marker is preserved for recovery.
Leader exit is observed without reaping, so the original PID still reserves the
owned group while cleanup stops any sleeping or paused descendants. The exit
observer must register before the launch gate opens; registration failures
abort that launch. Its exit state stays latched across load-control checks.
The guardian resets its own SIGCHLD disposition without changing the caller's.
Only the post-group drain finalizes licence lines, including output inherited
by descendants.
After leader exit, ordinary stdout and log reads share a two-second, 4 MiB
budget. Exhaustion reports `native_final_drain_limit` as UNKNOWN and proceeds
to group cleanup. The post-group licence scan has its own fresh budget, so a
noisy helper cannot consume the time or bytes reserved for licensing events.

The observer uses [waitid with WNOWAIT](https://docs.python.org/3.13/library/os.html#os.waitid)
when available. Python 3.12 on macOS uses
[kqueue process-exit events](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/kqueue.2.html),
preserving the declared Python minimum. Neither mechanism reaps before cleanup.
If recovery cannot prove the group or identity, run a read-only census with
`ps -axo pid=,pgid=,stat=,command=` against the reported PID/PGID. Let unrelated
processes finish, and bring the Director one recommendation before ending an
owned process whose identity remains unproved. Do not signal it automatically.
After the group is gone, repeat `--native-recover-only`; the original licensed
outcome hold still requires its separate reconciliation or Director decision.
Recovery alone retains an unknown-outcome hold; it does not infer expired
activation or authorize another licensed run.

Log observation follows file identity through ordinary size-based rotation,
preserving offsets and partial licence lines across a rename. A disappeared
observed inode, truncation, unsafe path or bounded-drain failure still leaves
licence observation incomplete and preserves the outcome hold.

## Coverage and qualification

PyCharm also uses the activity-based startup described above for SDK configuration.
The wait shares the execution timeout and custody; missing controls or selected-file
spans, including package inspections, still mean UNKNOWN.

Results name `backend: native`, the selected scope, source/profile/helper and
framework provenance, coverage, findings and separate cleanup. Reports must
name the backend and any `coverage.unavailable` or `coverage.unproven` entries.

For `changed_files` and `files` scopes the verdict counts a finding when its
line was added or modified since the merge base of `HEAD` and `origin/HEAD`,
or its file is new since then. Committed, staged, unstaged and untracked
changes all count. A changed file whose lines cannot be read from the diff
counts in full. A finding that an edit causes on an unchanged line, including
after a deletion, is not counted; tests, type checks and broad scopes catch
those. The findings artifact keeps every finding with its `introduced` value;
the result lists counted findings, `advisory_findings` for spelling and grammar
severities that never count, and `preexisting_finding_count`. Without a
resolvable merge base or diff, `changed_lines` is `unavailable` and every
finding counts. Coverage proof is unaffected. `directory` and `whole_project`
scopes count every finding.

The effective descriptions prove registration and supply language identity.
Completed stock `inspectionRun` telemetry proves each applicable inspection on
each selected physical file, including clean files. `coverage.selected_execution`
and `missing_execution` expose the exact matrix. Findings or controls cannot
substitute for a missing selected-file pair. Unknown applicability stays UNKNOWN;
Python tools apply to Python, OdooXml to XML, YAML tools to YAML, and explicit
universal tools to every selected file. General whitespace, formatting, line
separator and TODO checks require completed tool/file pairs too. `EmptyDirectory`
checks directories and is inapplicable to exact-file and changed-file selections;
broad scopes retain UNKNOWN until directory execution can be proved.

Distinct private defect canaries independently prove the named tools detect
known defects. `coverage.canary_results` records their expected source and
outcome; their findings never appear as user findings. They do not duplicate the
qualification fixture. A missing canary, failed span, unexpected traversal,
untraversed file or unavailable applicable tool keeps the verdict UNKNOWN.
Oversized selected files are refused before execution.

Parse the per-run local Jaeger trace only after final `Done.`, exit zero, and no
engine error or cancellation. Truncated/malformed traces stay UNKNOWN. Independent
line framers keep complete event prefixes; a long line fails without inventing
a new event from its suffix. Preparation output is not parsed as IDE output.
Error receipts retain only a sanitized logger category, never raw messages.
Raw logs and traces are deleted with the private run. Source, selection, profile
and dependency changes invalidate the result.

`coverage.profile_requested` lists enabled repository entries, and
`not_applicable` explains tools for other file types. The vendor migration from
`PyUnusedLocalInspection` to `PyUnusedLocalVariableInspection`,
`PyUnusedParameterInspection` and `PyUnusedFunctionInspection` is explicit when
all successors register; every applicable successor must execute. This follows
JetBrains [PyUnusedSymbolInspectionMerger](https://github.com/JetBrains/intellij-community/blob/master/python/python-psi-impl/src/com/jetbrains/python/inspections/unusedLocal/PyUnusedSymbolInspectionMerger.kt).
No check is dropped. Complete evidence and no user findings permit GREEN;
complete evidence with findings produces RED. Coverage, lifecycle or persistence
failures keep UNKNOWN with current findings retained.

The offline routing, lock, coverage, disposal and recovery tests need no IDE.
The opt-in `JB_NATIVE_LIVE=1` test in `tests/test_native_backend.py` uses the
committed planted/corrected Odoo fixture through the configured independent
worker. It copies committed Git blobs into a committed disposable repository,
so local edits and generated files cannot change its provenance. It requires actual Odoo XML, quoted `odoo.model.*` type, duplicate and
named Odoo findings, with corrected controls clean. The fixture's collection-valued
package option also exercises completed inspection on every selected Python file.
`JB_NATIVE_LIVE_OUT` can retain its sanitized result. Qualify with a real tenant change too; a synthetic
RED alone does not prove that repository's profile or model is complete.
