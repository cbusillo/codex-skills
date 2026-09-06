# Inspection lifecycle diagnostics

Read when diagnosing auto-open, ownership, retry, cleanup, trust, or IDE-selection
problems, or changing the helper lifecycle. Commands and paths below are relative
to the `jetbrains-inspection` skill directory.

`open-worktree`, `agent-inspect`, `inspect`, and `inspect-closeout` create a
local lease, serialize helper-owned IDE
opens, open the exact current worktree only when no exact route exists, wait for
indexing/scanning to settle, run the inspection loop, and call the plugin
lifecycle close endpoint only for projects the helper opened. Projects that were
already open before inspection must remain open. Lifecycle opens use macOS
background activation by default to reduce focus stealing; when the target IDE is
not already running, the helper launches the app hidden first and then asks the
plugin to open the exact worktree. Use `--foreground-open` only when debugging
IDE launch behavior.
If an inspection wait times out while a helper-started run is still active, the
helper asks the plugin to cancel that exact `inspection_run_id` and waits briefly
for cancellation to settle. A compatible run adopted from a concurrent 409 is
foreign-owned and is never cancelled. If the run changed, the helper leaves the
newer run untouched. If the trigger/wait transport itself times out, the helper
probes status first; an unproven or foreign active run, or unreachable status,
keeps the owned project warm instead of closing it blindly.
Once settled, it closes the helper-owned project normally. If indexing,
scanning, or inspection churn remains active, the helper leaves the project warm
and reports `cleanup.status=deferred` with `cleanup_deferred=true`. Treat that as
`UNKNOWN`, rerun `inspect-closeout` after the IDE settles, and use
`cleanup-helper-leases` only if the warm lease becomes stale. Full inspection
timeouts are not retried internally; the one automatic retry is reserved for
safe stale/capture-readiness outcomes. That retry is gated by a bounded,
route-pinned IDE activity quiet period instead of a blind sleep. A previous
stale/capture outcome remains visible in status until the next run and does not
itself prove current activity; indexing, scanning, inspection, route, and
lifecycle signals control the gate. If activity does not remain quiet, the
helper preserves the first `UNKNOWN`, skips the second inspection, and reports
`internal_retry_readiness`. The retry result must still independently prove
`GREEN` or `RED`; another stale/capture result remains `UNKNOWN`.
`retry_policy.retry`, `agent_result.next_action`, and `agent_report` are one
contract: terminal execution-proof failures must not tell agents to rerun, and
retryable native-run interruption may request only the single maintained fresh
run. Never infer permission to retry from prose when `retry_policy.retry=false`.
Preparation is failure-atomic for handled failures and interrupts. With plugin
protocol `lease_bound_v1`, the helper persists `state=open_requesting` before
sending its local `lease_id` to `lifecycle/open`. An open response registers the
request but does not prove ownership. Only `lifecycle/claim` can bind that lease
to the exact project instance and return a close token; the helper claims before
the readiness wait and ignores token-shaped responses that lack the protocol's
ownership proof. `already_open`, another lease's `already_opening`, legacy
plugin responses, and session-mismatched routes never authorize close.
After the plugin accepts a lifecycle open, the helper waits only for that
lease-bound request; it does not issue a second app-level open that could create
an unowned window or trust prompt. A timed-out lifecycle-open response is
treated as ambiguous rather than absent: the helper waits for the exact route
and requires a successful lease-bound claim before it can close anything.
When the plugin advertises `lifecycle_open_diagnostic_version >= 1`, route
waiting uses the strictly non-scheduling `probe=true` contract and disables the
legacy blind `already_opening` reschedule. Timeout JSON preserves the latest
`lifecycle_open_probe`, and terminal output emits `LIFECYCLE_OPEN_PROBE:` with
the latest phase, outcome, elapsed time, and ownership/readiness booleans.
Each probe is capped by the route wait's remaining deadline, skips requests
when too little useful time remains, and continues across matching IDE
instances so one unavailable identity cannot hide a later healthy diagnostic.
That potential ownership evidence remains recoverable through route/claim
timeouts and is discarded only after a definitive `not_owned` claim.
If preparation then fails, the helper closes immediately when a live claim
proves ownership, except when the readiness status still reports active
indexing, scanning, or inspection work; active churn keeps the owned project
warm for a bounded retry. Otherwise it records `state=cleanup_pending` with the
available evidence and cleanup action instead of leaving a generic `preparing`
lease. `cleanup-helper-leases` may recover even a route-less pending lease by
asking the live plugin to prove the same lease binding. A definitive
`not_owned` response releases the local lease without closing; unavailable or
legacy proof remains an explicit nonzero `unresolved` result. Path/session
matching selects a candidate route but is never itself permission to close.
Stale cleanup may release a route-less pending lease without discovery only
when the handled project-open timeout records explicit negative ownership and
acceptance, no open attempts, and no route/session identity. Missing, legacy,
interrupted, or transport-ambiguous evidence remains fail-closed and unresolved.
It may also release only the local `cleanup_pending` lease, without closing a
project, when successful discovery proves the lease-bound accepting IDE session
is absent, the recorded IDE process is definitively dead, and no live route in
any session resolves the exact target path. Missing process identity, ambiguous
acceptance, PID permission errors, and same-path routes in a new session remain
fail-closed and unresolved.
Preparation failures for projects that were already open release only the local
lease and never call lifecycle close. `cleanup-helper-leases` uses the same
lifecycle lock as inspection commands so stale reconciliation cannot race a new
helper-owned open or close.

When recovering one known stale lease, scope both the dry-run and cleanup with
`cleanup-helper-leases --lease-id <UUID>`. The selector normalizes one UUID
(including uppercase or hyphenless input), filters before staleness checks and
route discovery, and runs under the
existing lifecycle lock. It does not force a live lease stale or relax ownership
proof. First use `--dry-run`; add `--no-dry-run` only when the exact project's
current route is verified and indexing, scanning, and inspection have settled.
Do not use a global sweep to recover one project.

Scoped results preserve these boundaries:

- Malformed or repeated selectors, unknown IDs, duplicate lease identities, and
  filename/identity mismatches return nonzero without lifecycle requests or
  deletion. Their reason codes are `invalid_lease_selector`, `lease_not_found`,
  `lease_identity_ambiguous`, and `lease_identity_mismatch`.
- A selected lease that fails the existing age/PID staleness predicate returns
  nonzero with `lease_not_stale`, including in dry-run mode. A matched stale
  dry-run lists only that lease and does not discover routes or mutate it.
- Discovery failures, unresolved ownership, route/session mismatches, and
  refused closes retain the selected lease with the existing failure evidence.
- Only the maintained helper removes the selected lease after confirmed close
  or an existing proved no-close outcome: `open_not_attempted`,
  `project_not_open`, `project_preexisted`, `ownership_not_proven` (a definitive
  negative claim), or `original_ide_process_dead_no_route`. Other leases remain
  untouched. Running without a selector retains the existing global behavior.

Lifecycle inspections are serialized by a bounded local lock. If another helper
inspection is already opening, inspecting, or cleaning up a project, wait for it
or increase `--lifecycle-lock-timeout-ms`; do not start parallel auto-open
inspections and expect independent IDE windows to race safely.
Outcome logging uses a cross-platform, bounded routing lock acquired before
resolving configured log paths through `current`, followed by a bounded concrete
JSONL lock. Deployment reconciliation must take that routing lock before the
lifecycle and concrete outcome-log locks so completed inspections cannot retain
a pre-switch log path and append to an immutable parent deployment.

Auto-open is allowed only for worktrees under globally trusted roots. Before a
lifecycle auto-open, the helper adds the matching trusted root to the selected
JetBrains product's Trusted Locations config, ensures project opening is set to
new-window/no-prompt, launches the selected IDE hidden if no matching plugin is
already running, then asks the running inspection plugin to schedule the exact
worktree open with the IDE's current `session_id`. Current plugin builds use
that session-verified lifecycle request to mark the path trusted inside the
running IDE immediately before `ProjectManagerEx.openProject`, which avoids
stale on-disk Trusted Locations state in already-running IDEs. The helper polls
until the exact route appears, claims cleanup authority, and then requires two
consecutive ready status observations before it inspects. For helper-owned
projects, `lifecycle_readiness.ready` also requires a content root covering the
requested worktree after project configuration stabilizes. If configurators
remove the initial raw-directory module, current plugin builds may install a
non-persistent fallback module rooted at the requested worktree. Repair is
limited to the exact lease-bound project instance, is exposed as
`fallback_module_count`, and must not alter preexisting/coalesced projects or
create tracked `.idea` or `.iml` files. Readiness that appears too close to the
guard deadline remains fail-closed as `project_configuration_unstable`.
`no_content_roots` and `content_roots_outside_target` fail
preparation as `project_content_roots_missing` and trigger lease-bound cleanup;
a lifecycle open response or routable project alone is not readiness proof.
Configure trusted roots in `${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}/jetbrains-inspection.json`:

```json
{
  "jetbrains": {
    "trustedAutoOpenRoots": ["/Users/me/Developer", "/Users/me/.code/working"]
  }
}
```

If an exact worktree is not already open and is outside those roots,
`open-worktree`, `agent-inspect`, `inspect`, and `inspect-closeout` must fail
before opening the IDE. Do not use random temp directories for agent
inspection worktrees.

If multiple JetBrains products or stable/EAP installs are present, the repo must
declare its preferred IDE in `.github/github.json` so lifecycle opens and
Trusted Locations seeding target the same product/config. Product-level metadata
such as `jetbrains.ide: "WebStorm"`, `"PyCharm"`, or `"IntelliJ IDEA"` means
the latest installed stable/non-EAP app for that product. For a deliberate EAP
or exact-version run, use explicit metadata or CLI fields such as
`jetbrains.ideChannel: "eap"`, `jetbrains.ideVersion: "2026.2"`,
`jetbrains.ideApp`, `--ide-channel`, `--ide-version`, or `--ide-app`.
Never infer EAP from the presence of an EAP install. EAP requires an explicit
repo, CLI, exact app/version, or user-task signal; it is not a fallback when no
stable IDE is discovered.
Treat `--ide`/`--ide-app` as a one-off unblocker; for recurring repo work,
tell the user to add preferred IDE metadata rather than leaving the next agent to
guess again.
If a first-time open still stalls after trusted-location and project-opening
policy seeding, treat it as a blocker: check for unsupported IDE config layout,
settings sync overwriting the config, a missing inspection plugin, or a product
that accepted the scheduled open but never registered the worktree. Real-session
smokes have validated unattended lifecycle inspection on IntelliJ IDEA, PyCharm,
and WebStorm 2026.1 with trusted worktrees under `$HOME/.code/working`.

Use `inspect` for ordinary iteration and `inspect-closeout` for final readiness
notes so cleanup status is explicit. Use `--include-stale` only when explicitly
diagnosing cached stale findings; stale results still exit non-zero and are not
clean.
