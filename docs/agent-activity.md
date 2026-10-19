# Agent activity producer

`scripts/observe_agent_activity.py` and the session-alert hook produce the
count-only lifecycle contract for Context Panel ([#1648](https://github.com/cbusillo/codex-skills/issues/1648)).
The observer uses the Supervisor's existing private account configuration and
Context Panel snapshot to match each harness home to the same opaque account ID
used by [launch receipts](https://github.com/cbusillo/context-panel/blob/main/docs/provider-usage-access.md#use-next-ranking-and-launch-receipts).
It reads no credentials and makes no model requests.

## Start and install

After landing and runtime reconciliation, refresh the existing advisory Codex
alerts with the catalog installer if needed. Codex trust remains owned by Codex:
review new or changed definitions through `/hooks`. Its activity observer does
not depend on hooks. Claude Code loads the new plugin declarations in its next
session. No producer operation enables or trusts a hook.

Run the observer in the foreground:

```sh
uv run scripts/observe_agent_activity.py run
```

`--config` selects an existing private Supervisor config. The accounts come
from that config's snapshot command; unavailable or ambiguous account mapping
fails rather than inventing an ID. For one bounded qualification pass, use
`once`. Use `--state-root` to isolate qualification output, private state and
hook bindings from the live `CODE_HOME`.

On macOS, `install` previews a LaunchAgent. `install --write` writes that exact
job, preserving an existing different job. Installation from a linked task
checkout is refused. It does not load or restart the job.
The job pins the installed Claude executable and uses only the selected CLI
directories plus system paths. Temporary or virtual-environment executables
are refused; `install --claude-cli /durable/path/to/claude` selects the supported
replacement. Run from a shell with durable uv and node installations on PATH.
Use the printed `launchAgent` path with the native launcher:

```sh
launchctl bootstrap "gui/$(id -u)" /path/from/preview.plist
```

The job runs the reconciled checkout's observer, keeps it alive and retries
unavailable local sources about every 30 seconds. Stop it with
`launchctl bootout "gui/$(id -u)/com.codex-skills.agent-activity"`.
Set `AGENT_ACTIVITY_DISABLED=1` in a hook's environment to suppress that hook's
activity independently of the existing advisory session alerts. To stop all
activity, stop the observer and suppress the hooks; leases then expire.

## Contract

Hooks and the observer write each event to the private producer inbox,
`$CODE_HOME/agent-activity/inbox/` (default `~/.code`). The inbox and the
private state directory around it are mode 0700, and a symlinked inbox or state
directory is refused. Each event is one atomic JSON file of at most 1 KB, mode
0600, written to a temporary file and renamed. These are the launch-receipt
file rules.

codex-skills never writes the Context Panel App Group. macOS refuses that store
to processes with no app behind them. Context Panel imports inbox events through
its own `--record-activity` mode, which is separate Context Panel work.

The producer prunes its own valid regular event files after 24 hours;
it preserves foreign, malformed and redirected files. The hook prunes at most
once an hour, keyed on a private 0600 stamp file, so private state stays
bounded even when the observer is not running. The observer also prunes on
each poll. Readers ignore malformed,
oversized and old files, deduplicate by `eventID` and fold by account, harness,
session and run. Order by `observedAt`, then `eventID`; IDs are monotonic for a
session within one millisecond. Delivery is best effort. Repeated quiet polls
do not create event files, and quiet cannot overwrite a terminal outcome.

The version 1 record has:

- `schemaVersion`, `eventID` (UUIDv7), `producerID` (`codex-skills`), `harness`
  (`codex` or `claude`), `accountID`, `sessionID`, `runID`, `parentSessionID`,
  `agentID`. Nullable hierarchy IDs are UUIDs; account IDs match launch receipts.
- `state`: working, attention, quiet, completed, failed or cancelled.
  `attentionKind` is approval, input or null; `outcomeSource` identifies the
  native signal used (`codex-status`, `codex-turn`, `claude-hook` or
  `claude-inventory`).
- `observedAt` and `leaseExpiresAt`: UTC timestamps. Live records have a
90-second lease, renewed by the 30-second observer cadence while working or
awaiting attention. Quiet records need no heartbeat. Private session records
expire after 24 hours too; lock buckets have a fixed upper bound of 256 files.

An expired live record is **stale**, never completed. A terminal record is a
recent milestone, not a live worker. No signal is interpreted as blocked.
Completion describes a native turn ending, not tests passing, an issue being
done, or Client acceptance. Hooks never return a decision or restart a turn.

No prompt, message, transcript, tool, command, path or environment content is
written to activity records. Arbitrary source fields are discarded. The
producer does not publish repo/branch labels from the study's optional
workstream proposal: these are content rather than counts or lifecycle state.
Private hook bindings hold only the producer and account IDs. A hook ignores
the storage location in a binding written before the inbox existed.

## Renewal check

`renewal-check` shows whether live leases were renewed before they lapsed. It
reads the inbox only. It does not write, prune or start anything.

```sh
uv run scripts/observe_agent_activity.py renewal-check --window 600 --harness both
```

It groups records by harness, account, session and run, ordered by
`observedAt`, then `eventID`. A lapse is any record that arrives at or after
the end of the lease of the live record before it. A record after a quiet or
terminal record is never a lapse. A lease mismatch is any record whose lease is
not the 90-second v1 lease. A qualifying run has an unbroken live chain longer
than one lease. A run whose last live lease has already expired counts as
`expiredOpen`, not as a failure.

Each harness fails on any lapse or lease mismatch. It passes with at least one
qualifying run, and is otherwise inconclusive. No runs or only short runs never
pass. With `--harness both`, both harnesses must pass.

`--window` is in seconds, above one lease and at most 24 hours, because older
files are pruned. `--state-root` selects an isolated inbox. Exit codes: 0 pass,
1 inbox unavailable, 2 usage error, 3 fail, 4 inconclusive.

The JSON output holds counts only: runs, live records, qualifying runs, lapses,
lease mismatches, the longest gap after a live record, the longest live span,
expired open runs and each verdict. It also counts ignored, malformed and
out-of-window files. It prints no session, run, account or event ID, and never
echoes file content.

## Source semantics and coverage

Codex: one passive Unix-websocket observer per configured `CODEX_HOME`. It lists
loaded threads and reads metadata without turns. It joins an **active** thread
using `thread/resume` with `excludeTurns: true` and no configuration overrides,
then unsubscribes at a structured terminal outcome. The latest summary turn
closes the join/finish and idle-before-completion races. Native completed,
failed and interrupted statuses map to completed, failed and cancelled.
Status flags supply approval/input attention; spawned-thread metadata supplies
parentage. Server approval requests are never answered; item and message
notifications are discarded. Disconnects stop renewal and retry without
inventing outcomes.

This covers daemon-hosted sessions. Codex lifecycle hooks still provide the
advisory alert path.

Claude Code: UserPromptSubmit starts a run, notifications and PermissionRequest
report attention, StopFailure reports API failure, and SubagentStart/Stop
retain child identity and parentage. Native child IDs, including `agent-` IDs,
are mapped to opaque UUIDs. Child attention and API failures stay on the child,
using the hooks' common `agent_id` field. Stop and SubagentStop become quiet because another Stop
hook can continue the turn, and an interruption of that continuation also
becomes idle. Even Stop followed by idle cannot prove success. Completion is
emitted only from a native background-session `done` state; native `failed`
and `stopped` map to failed and cancelled. Interactive idle remains quiet,
including after Stop. This corrects an unproven assumption in the #377 study:
stock interactive Claude interfaces cannot provide an unambiguous successful
turn outcome. Busy/waiting inventory records renew the run; a missing session
expires.
There is no native interrupt hook on Claude Code; interruption without an
outcome becomes quiet or stale. Child hook records have short leases; Claude's
inventory does not enumerate subagents, so an independently renewed child
lease cannot be inferred from its parent's busy state.

Not covered:

- Codex sessions outside the daemon. `--no-daemon`, `--profile` and most `-c`
  overrides can run there.
- Codex hooks. They are not an activity source. The daemon holds Codex run
  identity and outcomes, so hooks never create duplicate Codex runs.
- Claude sessions without the plugin hooks get only what the inventory poll
  sees: a busy session starts a run and renews its lease, but prompts,
  attention and subagents are not reported.

Hooks loaded before an observer starts produce no events until their account
binding exists. Existing runs can be observed without a start hook, but their
end cannot be promoted to success from idle alone. After an observer restart,
shared private state retains run identity for hooks and lease renewal.

## Verified sources

The implementation uses [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Claude Code hooks](https://code.claude.com/docs/en/hooks) and
[Claude's supported session inventory](https://code.claude.com/docs/en/agent-view#list-sessions-as-json).
Local qualification on October 11, 2026 used Codex 0.162.1 and Claude Code
2.1.296. Metadata reads returned no turn items; an active-thread subscription
with no overrides was accepted and unsubscribed successfully. This host had
trusted command/session-start definitions but no lifecycle alert declarations;
new Codex hook trust requires the native review step after installation.

Run `uv run scripts/test_agent_activity.py` for payload privacy, inbox-only
output, throttled pruning, hook-process identity, lease/outcome, renewal check and real
Unix-websocket lifecycle fixtures. The existing
installer and hook tests cover catalog registration and native trust preservation.
