# Local Qodana backend

Read before diagnosing a Qodana result or changing the runner. On platforms
without native PyCharm, `agent-inspect` selects it for only Python files when the
pinned image is local and `qualityGate.inspection.prepare.python` is declared,
outside Odoo repositories and PyCharm-only lanes
([#2155](https://github.com/cbusillo/codex-skills/issues/2155)); other
selections keep the API. On macOS, [native PyCharm](native-backend.md) is the
Python/Odoo default. The envelope's `backend_selection` names the reason.
`--backend native|api|qodana` or `JB_INSPECT_DEFAULT_BACKEND` overrides it.

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend qodana --repo "$PWD" --scope directory --dir scripts \
  --timeout-ms 300000
```

Use the existing `files`, `changed_files`, `directory`, or `whole_project`
selectors. Selection is anchored at the exact Git worktree. The runner copies
tracked and nonignored untracked files into a fresh private snapshot, including
local changes, and creates its own Git index. Other project files stay available
for references; the private Qodana configuration excludes them from inspection
when outside the requested file set. The source worktree is never mounted in
the container. Before accepting findings, source and selected snapshot hashes
must still match. Symlinks fail closed rather than pulling in external data.

The profile must be a populated XML profile in the worktree, selected through
`--profile` (its name or relative path), or `qualityGate.inspection.profile`.
With neither, exactly one populated project profile must exist. Empty or
ambiguous profiles report UNKNOWN. The isolated configuration inherits that
profile; it adds only the requested scope exclusions. It does not load a
repository's Qodana bootstrap, baseline, fixes or upload configuration.

Structured `qualityGate.inspection.prepare.python` runs the maintained Python
generator inside the Linux snapshot, without desktop SDK-table access. It
prepares only `.venv`: Qodana registers its own SDK from that interpreter, and
a generated desktop model names an SDK Qodana never registers, which analysed
modern annotations at the wrong language level. String preparation and
non-Python preparation report UNKNOWN until their isolated execution is
supported. The image has no uv, so the run installs the repository's declared
`required-version` range (`uv.toml` or `[tool.uv]`) from PyPI into the private
profile. `--qodana-uv /path/to/linux/uv` supplies an existing binary instead;
it must match the image's architecture and that range. Either is removed with
the private run. This does not prepare or alter the host IDE.

The default image is the pinned Python Community image used by the reviewed
[parity report](https://github.com/cbusillo/jetbrains-inspection-api/issues/503#issuecomment-6158381880).
`--qodana-image` accepts a digest-pinned Python or JVM Community image already
present in local Docker. Mutable tags and paid images are refused; no image is
pulled automatically. JVM qualification still needs equivalent module/source
roots and intended feature coverage. TypeScript remains UNKNOWN: desktop
WebStorm licensing does not grant a Qodana JS token, and this backend neither
loads nor obtains one. Native Qodana and macOS Keychain loading are outside this
backend.

Use `--qodana-plugin-zip` for an existing public plugin ZIP with its distribution
folder at the top level. The extracted package is copied into a run-specific
folder in the container's plugins directory; it never replaces a
built-in image plugin or copies a desktop plugin directory. Supply only
public plugin code, without activation-bearing files. Desktop IDEs and their
configuration/plugin installations stay untouched.

## Director activation

The former license-copy route is removed; use an independently Director-activated
headless profile, never a copied license or credential file.

On Chris-Studio, Chris activated the independent **JBTEST-D1** PyCharm and Odoo
IDE profile on October 18, 2026 ([activation decision](https://github.com/cbusillo/codex-skills/issues/1833#issuecomment-6251613312)).
Use that profile in place only through its qualified native runner, with exclusive
use and isolated system/cache/log paths. Do not copy its config, activation,
account store or credentials into another profile or a container. Native-runner
integration and qualification live in [#2401](https://github.com/cbusillo/codex-skills/issues/2401)
and the [native runner reference](native-backend.md);
this Qodana backend does not implement that native route or import its activation.
If no independently activated, qualified profile is available, stop the licensed
run with an `Owner question:` asking the Director to activate an independent
profile through the product UI using existing entitlements. Do not start a
trial, purchase, grant access, or open activation-bearing files.

`--qodana-license-file` remains recognized only to return UNKNOWN and this
supported route before accessing the supplied path or starting Docker.
Credential locations in the source snapshot, explicit uv/plugin inputs and plugin
archive members are refused before reading or transferring them. Keep credentials
outside project inputs or ignored by Git, and supply public plugin code separately.
The location guard recognizes common credential names and directories; it cannot
identify secrets stored under arbitrary source filenames. No contents are scanned.
`diagnostic.qodana.refused_input` identifies the refused relative path, or only
the basename for an external input. This refusal asks the agent to remove the
input from staging, not to obtain activation. The guard also refuses ambiguous
public names, such as REUSE sidecars ending in `.license`, Keynote `.key` files,
and public `.pem` fixtures. Use the existing `--backend api` route for those
projects without copying these files; do not weaken the guard or move credentials
into another profile to continue.

## Execution and recovery

Trial availability and dry-run refusal are permitted under
[Q261](https://github.com/cbusillo/jetbrains-inspection-api/issues/498#issuecomment-6153109838).
An observed trial start, issuance, activation, obtaining or purchase step stops
the worker immediately, deletes its private state, and records a count-only
`boundary-stop.json` under `~/.local/state/jetbrains-inspection/qodana`, outside
the task cache and its retention. Stop work and report the boundary to
the Director; subsequent runs refuse while that receipt exists. Do not remove
it to continue without a recorded Director decision. A log guard is an
observation of this run, not a guarantee or server-side licensing audit.

Runs serialize under a shared user lock and refuse admission if another Qodana
container is visible or load1 exceeds 64. The container has hard 2-CPU, 4-GiB
memory-and-swap and 512-PID caps, host/container nice 10, and Docker logging
turned off. It pauses above load1 64 and resumes when load falls below it;
the wall deadline still applies while paused. Removal addresses only the
run's unique container, including its writable layer. On Linux the container
runs with the caller's UID/GID to keep generated state removable; native Linux
runtime qualification is separate from this Mac's measured run.

The engine's project, venv, caches, SDK state and results live in the
container's own layer under `/work`. `docker create` makes the container,
`docker cp` streams the private snapshot, configuration and public plugins
in, and the results and the venv's `pyvenv.cfg` come back out after exit.
Docker Desktop keeps a host handle for every bind-mounted path the engine
touches, even after the container exits; with the whole profile mounted, runs
left tens of thousands held and contributed to a host-wide descriptor
exhaustion ([shiny-infra-ops#320](https://github.com/cbusillo/shiny-infra-ops/issues/320)).
Only the engine log directory is shared, so the live trial/purchase guard can
read it, and recovery uses that mount as its ownership proof. The opt-in
`LiveDescriptorTests` (set `JB_QODANA_LIVE=1`) runs two real inspections and
fails if Docker Desktop's VM keeps more than a few descriptors per run.

`JETBRAINS_INSPECTION_CACHE_DIR` selects the existing helper cache root; on
hosts that require external task storage, set it to the task's folder in the
location the host names for task output, such as the evidence folder its
worktree helper prints (see
[task artifacts](../../references/repo-cleanup.md#task-artifacts-and-retention)).
Never create a folder of your own at the volume root. Each run uses a fresh `qodana/private-*` directory with
owner-only permissions. Raw console output is never returned or archived;
private engine logs/config/home/cache/results are removed with the profile.
Only sanitized findings and safe count/hash/resource/deletion receipts survive.
`findings_artifact` references the complete selected findings after cleanup;
copy needed safe evidence to the owning issue/task-evidence, then remove the
run artifact during closeout. A failed deletion reports UNKNOWN and requires
recovery; process exit zero is not a green gate.

The state directory is resolved through the operating system's UID home,
independently of a harness's HOME override. An owner-only journal records each run before
input staging. After SIGKILL/crash, the next invocation holds the kernel lock
and recovers only journaled profiles with matching ownership markers and
containers with the exact run label and that run's log mount, on the journaled local
Docker socket. The marker is deleted last, so partial cleanup can resume.
macOS recovery compares volume UUIDs across remounts. A deleted task directory
on that verified mounted volume can be cleared after container absence is proved.
Unavailable or changed storage, unknown ownership
or unavailable Docker retains the record and returns UNKNOWN; other proved
records are still recovered. Changing task cache
roots neither loses recovery custody nor lifts a trial stop. A forcibly killed
helper cannot report cleanup at that instant; recovery confirms deletion on
the next invocation. A detached local monitor retains the lock and watches a
parent-lifetime pipe through execution and result collection. Parent EOF stops
its exact client/container, even while paused, then deletes the private profile.
Client termination precedes container removal to prevent late creation. The
native analysis timeout is also supplied to Qodana. A monitor or host crash
keeps the journal for proved recovery; never bypass a live monitor's lock.

Recover without starting a new inspection:

```sh
uv run <skill-dir>/scripts/jb-inspect.py agent-inspect \
  --backend qodana --qodana-recover-only
```

This emits UNKNOWN because no inspection runs, with recovered/unresolved counts.
Restore the recorded storage or Docker prerequisite and use this command again
to finish proved cleanup. It preserves trial stops and unproved ownership.
Do not force-delete unproved state.

The backend returns the normal schema-v1 agent envelope with additive
`backend: qodana`, pinned image/run/source provenance, findings by file and
line, coverage gaps and separate cleanup receipts. It does not feed Qodana
observations into the API-only strict deployment qualification log. A current
finding from a completed run can prove RED with coverage gaps preserved.

Python proves execution with a canary planted in the private snapshot in every
directory that holds a selected Python file. Each canary's unresolved name must
be reported by `PyUnresolvedReferencesInspection`. Its valid 3.10+ annotations
must not be reported by `PyTypeHintsInspection`, which is what a module without
the attached interpreter reports; then no finding from that run is decisive.
Both inspections must be enabled in the effective profile. Qodana's results
show neither stub version gates nor version-specific syntax, so the requested
version is proved by the prepared `.venv`, which `QODANA_PYTHON_PATH` names:
its version must equal `prepare.python.version`. A repository without
structured Python preparation never gets GREEN and is not auto-selected. A
tracked module file with an `excludePattern` blocks GREEN, since no directory
canary detects a per-file exclusion. A selected file larger than the engine's code-insight limit, set explicitly through `idea.max.intellisense.filesize`, also blocks GREEN: the engine skips it while its canary still reports. `canary_rules` in the receipt lists the
rule IDs seen on canaries. Zero findings are GREEN only when the run completed,
every canary proved execution, every selected file is Python, and every enabled
profile inspection is in the effective profile. Inspections for other file
types do not apply to a Python-only selection. `DuplicatedCode` and
`HttpUrlsUsage`, which the Community image does not run, are listed in
`uncovered_inspections`. When the profile enables them, they block GREEN.
[Q481 was retired without accepting a gap](https://github.com/cbusillo/codex-skills/issues/1833#issuecomment-6254396907);
use the independently activated native route to run them. RED still works. Any other missing
enabled inspection, such as a renamed ID, keeps the result UNKNOWN. JVM and other
languages report UNKNOWN on zero findings. Unsupported filters,
unavailable/malformed SARIF, changed inputs and cleanup failures remain
UNKNOWN; no automatic retry is authorized.

Carry the [full difference inventory](https://github.com/cbusillo/jetbrains-inspection-api/issues/503#issuecomment-6158434269)
forward: Python's effective profile omitted `DuplicatedCode` and `HttpUrlsUsage`;
Odoo lacks frozen-default, interpreter/model-root and named OdooXml execution
proof, and optional integrations differ. A loaded plugin, an enabled inspection,
or generic XML findings do not prove named Odoo coverage. This backend checks
requested IDs against the effective profile as additional gap evidence and
retains the measured gaps. Odoo, Kotlin/JVM and TypeScript qualification are
separate work under [the parent goal](https://github.com/cbusillo/codex-skills/issues/1833).

Qodana's [profile documentation](https://www.jetbrains.com/help/qodana/inspection-profiles.html)
and [scope configuration](https://www.jetbrains.com/help/qodana/configuration-reference.html#Exclude+paths+from+the+analysis+scope)
own the engine configuration syntax; the helper owns selection, disposal and
fail-closed interpretation.
