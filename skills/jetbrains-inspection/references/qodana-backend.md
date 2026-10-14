# Local Qodana backend

Read before selecting `agent-inspect --backend qodana`, diagnosing its result,
or changing the runner. The API remains the default. No language is qualified
for a default switch by this backend's availability.

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
generator inside the Linux snapshot, without desktop SDK-table access. String
preparation and non-Python preparation report UNKNOWN until their isolated
execution is supported. For Python, provide `--qodana-uv /path/to/linux/uv` when
the image has no compatible uv on PATH; the binary must match the image's
architecture and the repository's declared uv requirement. It is copied into
and removed with the private run. This does not prepare or alter the host IDE.

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
folder at the top level. The extracted package is mounted read-only under a
run-specific folder in the image's plugins directory; it never replaces a
built-in image plugin or copies a desktop plugin directory. Supply only
existing IDE keys or paid-plugin activation files through repeatable
`--qodana-license-file`; no account directory, token store or Keychain source.
Files are read from their local sources into the private profile, filesystem
read-only and mounted read-only, then deleted after success, refusal, timeout
or handled interruption. Never print, log, upload, archive or retain their
contents. Do not use this local licensing route on CI. Desktop IDEs and their
configuration/plugin installations stay untouched.

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

`JETBRAINS_INSPECTION_CACHE_DIR` selects the existing helper cache root; on
hosts that require external task storage, set it to the task-private directory
on that volume. Each run uses a fresh `qodana/private-*` directory with
owner-only permissions. Raw console output is never returned or archived;
private engine logs/config/home/cache/results are removed with the profile.
Only sanitized findings and safe count/hash/resource/deletion receipts survive.
`findings_artifact` references the complete selected findings after cleanup;
copy needed safe evidence to the owning issue/task-evidence, then remove the
run artifact during closeout. A failed deletion reports UNKNOWN and requires
recovery; process exit zero is not a green gate.

The state directory is resolved through the operating system's UID home,
independently of a harness's HOME override. An owner-only journal records each run before
license copying. After SIGKILL/crash, the next invocation holds the kernel lock
and recovers only journaled profiles with matching ownership markers and
containers with the exact run label and private mount. The marker is deleted
last, so partial cleanup can resume. Missing or changed storage, unknown ownership
or unavailable Docker retains the record and returns UNKNOWN; other proved
records are still recovered. Changing task cache
roots neither loses recovery custody nor lifts a trial stop. A forcibly killed
helper cannot report cleanup at that instant; recovery confirms deletion on
the next invocation. The Docker client inherits the lock descriptor, so a
surviving client cannot create a late container after another caller recovers
its journal. The native analysis timeout is also supplied to Qodana; wait for
an inherited live client to finish rather than bypassing its lock.

Recover without starting a new inspection or copying licenses:

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
Zero findings always report UNKNOWN while clean completeness is unqualified.
Unsupported filters, unavailable/malformed SARIF, changed inputs and cleanup
failures likewise remain UNKNOWN; no automatic retry is authorized.

Carry the [full difference inventory](https://github.com/cbusillo/jetbrains-inspection-api/issues/503#issuecomment-6158434269)
forward: Python's effective profile omitted `DuplicatedCode` and `HttpUrlsUsage`;
Odoo lacks frozen-default, interpreter/model-root and named OdooXml execution
proof, and optional integrations differ. A loaded plugin, an enabled inspection,
or generic XML findings do not prove named Odoo coverage. This backend checks
requested IDs against the effective profile as additional gap evidence and
retains the measured gaps. Qualification and any language default switch are
separate work under [the parent goal](https://github.com/cbusillo/codex-skills/issues/1833).

Qodana's [profile documentation](https://www.jetbrains.com/help/qodana/inspection-profiles.html)
and [scope configuration](https://www.jetbrains.com/help/qodana/configuration-reference.html#Exclude+paths+from+the+analysis+scope)
own the engine configuration syntax; the helper owns selection, disposal and
fail-closed interpretation.
