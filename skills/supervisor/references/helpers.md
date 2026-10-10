# Supervisor helpers

Read this before rebuilding a ledger, watcher or digest, or sending terminal
input. Run every Python helper with `uv run` from any directory using its catalog
path. The status, question and candidate helpers are read-only. iTerm controls
require macOS, iTerm2's Python API enabled and the session's existing authority;
API connection failure is a stop, not a reason to change personal settings.

## Private ledger

Create a private JSON array from the handoff's actual session identities. Paths
are relative to that ledger file; never commit transcripts, account names,
briefs or operational coordinates. No helper searches accounts or copies auth.
Use `iterm_tab.py list` to record the exact window, session and TTY; titles are
labels, not identity. Match the harness process's PID and transcript yourself. The ledger loader
normalizes iTerm's `/dev/` TTY prefix for `ps` matching.
Exclude the Director's sessions or set `supervisor_owned` to false.

```json
[
  {
    "session_id": "native-thread-or-session-id",
    "harness": "codex",
    "transcript": "sessions/task.jsonl",
    "supervisor_owned": true,
    "repository": "OWNER/REPO",
    "issue": 123,
    "brief": "brief-task.md",
    "iterm_session_id": "exact-iterm-session-id",
    "pid": 12345,
    "tty": "ttys123",
    "handoff_url": "https://github.com/OWNER/REPO/issues/123#issuecomment-456"
  }
]
```

Each native session id is unique. Codex UUIDs have no date-prefix restriction.
Missing/malformed or changing transcripts are errors, never idle/finished
proof. The watcher reports an error for that session and keeps watching the
other sessions; a malformed ledger itself ends the watch. Status output includes the latest response and can be private; keep it
out of public comments. Context counts may be absent and are not a reason to
hand off. For Claude, the count includes cached input.

## Checks and morning digest

```sh
uv run skills/supervisor/scripts/status.py --ledger <private-ledger.json>
uv run skills/supervisor/scripts/codex_idle_watch.py --ledger <private-ledger.json> --deadline-seconds 1800
uv run skills/supervisor/scripts/oq.py OWNER/REPO#123 --owner <Director-login> --decision-author <recording-automation-login>
uv run skills/supervisor/scripts/finished_map.py --ledger <private-ledger.json>
```

Rebuild the ~23-minute check and morning digest using the harness's scheduler.
For each check, read statuses, verify turn-end notices, nudge stalls with exact
facts, gather Director questions and perform the skill's finished-session step.
The watcher exits at its wall-clock bound; re-arm it with the harness's built-in
monitor/scheduler. A new watcher may repeat a notice. `clean: null` means a turn
ended, not that work succeeded; an abort is also a notice. Use Claude Code's
`notify_when_idle` when available, not another watcher.

The question helper reads every comment via the maintained GitHub helper and
returns every question, not just the newest. Configure the Director's login
and only the trusted decision-recording authors for this run. A later reply
must link a question's URL or comment id to mark it answered; unlinked plain
answers remain `needs_review` for the Supervisor to reconcile from the exact
decision record. A script's match does not itself establish what was decided.
For the digest, also use `github-work-rollup` for human comments, `github` for
Dependabot/security signals and `infra-ops` for authorized live-site checks.
Include the run's privately recorded capacity deadlines. Post the Director's
questions in one batch with recommendations; use only the messaging authority
already granted for this run.

## Foreign-post notices

```sh
uv run skills/supervisor/scripts/foreign_watch.py --owner OWNER --repo OWNER/REPO --own-login OWN-LOGIN --own-login AUTOMATION-LOGIN --launchplane-login DELIVERY-LOGIN --state <private-watch-state.json> --deadline-seconds 1800
```

Repeat `--repo` and login flags for the run's configured scope. Initial startup
begins at the current time; `--since` supplies an explicit first watermark for
a historical catch-up; on an existing state it rewinds the selected repositories
only when earlier than their stored watermark. Unlisted repository state stays
preserved. The host's UTC clock must be correct. Restart with the same state file after a notice or
deadline. `--once` performs one pass. Only one watcher owns each state file.
Run it with the harness's built-in background completion notice so its exit
wakes the Supervisor. During takeover verify the old watcher's recorded process
has ended before re-arming; retain an uncertain process and its unread output.
Concurrent watchers using that state file refuse before reading GitHub. The
helper reads one since-scoped repository issue-comment listing per pass
(including PR timeline comments), paginating through the shared reader within
the wall-clock deadline. It uses the shared bulk quota reserve, identity,
conditional cache and retry policy; no separate quota probe or history scan.
A failed, partial or malformed read preserves that repository's watermark,
reports an error and exits for Supervisor reconciliation. Wait until a reported
quota retry time before re-arming; increase the bounded deadline when a historical
catch-up cannot finish. Partial pages never advance past a failed read.
Successful repositories
advance to the pass start, with a one-second overlap on their next read.

Foreign authors (including deleted/unknown authors) and configured Launchplane
authors or first-line product-review markers
produce metadata notices marked `untrusted`; comment bodies are omitted. A
marker is a notification hint, never proof of authorship or accepted direction.
A foreign author stays `foreign` even when its comment has a review marker.
Configure known service bots among own logins when their ordinary posts should
not wake the Supervisor; no suffix rule silently excludes unknown posters.
Read the canonical record before acting under existing authority. The watcher
does not launch agents, post messages, grant access or widen briefs. New issue
bodies, inline review comments and Discussions are outside this bounded comment
feed; the existing work rollup still supplies broader discussion coverage.

## Launching and nudging

```sh
uv run skills/supervisor/scripts/iterm_tab.py window
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <dedicated-window-id> --command-file <private-launch-file> --account-provider openai
uv run skills/supervisor/scripts/iterm_tab.py list
uv run skills/supervisor/scripts/iterm_tab.py read --session-id <iterm-session-id>
uv run skills/supervisor/scripts/iterm_tab.py send --session-id <iterm-session-id> --text-file <private-message-file> --verified-target
```

Use one dedicated Supervisor window, never the window with the most tabs. The
`new` creates the tab in the background without selecting it or moving keyboard
focus. It waits up to 10 seconds for that exact tab's session, refreshing the
iTerm hierarchy before launching. A session-wait timeout names the tab for
inspection. If creation returns no tab identity, run `list` and inspect first;
the tab may still exist. Do not create another tab or replay the launch without
checking it. `window` still restores the previous tab after creation.
Launch files contain the exact brief and model settings already authorized,
without the Discord channels flag.
After the catalog's [pinned Chrome server](https://github.com/cbusillo/codex-skills/blob/main/README.md#chrome-across-claude-account-homes)
is installed for the selected Claude home, omit `--chrome` from launch files:
the user MCP entry already supplies those tools regardless of the session account.
Every `new` launch requires
`--account-provider` as below; keep account variables out of launch files.
Use `--account` for an explicit override through the same mapping and receipt
path. Run one agent invocation, without restart loops or commands that continue
after it exits. Read the launched screen once for folder trust or another
blocking prompt. Record the new native thread and exact transcript in the
ledger. Prefer `codex queue` for Codex nudges with the verified thread id and
its configured home; never queue exit commands. The terminal helper suppresses
broadcast input, uses bracketed paste for multiline messages, and sends text
and Return separately. Verify input and prompt
before `--verified-target`, then read back once; do not replay an uncertain send.

## Choosing the account

Launch with `--account-provider openai|anthropic|google`. Context Panel is the
only account-choice source, under the decisions on
[codex-skills#1270](https://github.com/cbusillo/codex-skills/issues/1270).
A single launch follows `answers.useNext`; repeat `--command-file` for a batch,
and the helper uses the first corresponding IDs in the provider's published
`launchOrder`. It never re-ranks, substitutes a configured account or applies
a private reserve. Account configuration only maps snapshot rows to launch
environments. An unmatched or ambiguous choice refuses before creating tabs.

Read Context Panel's
[Use next ranking and launch receipts contract](https://github.com/cbusillo/context-panel/blob/main/docs/provider-usage-access.md#use-next-ranking-and-launch-receipts)
for ranking, stale-list eligibility, count-only receipt fields and retention.
The launcher checks the published stale list's observation time, refuses once
it reaches 30 minutes, and reports `nextCapacityAt` when nothing is rankable.
Reader failure refuses; `--account <name>` is the explicit override, but still
needs a snapshot row so its launch receipt uses the correct opaque ID.
Configuration errors and Context Panel's plain reset prompt lines are reported
in launch output. Never apply a reset; Chris does that.

The helper exports the account's `env` ahead of the command, including an agent
started after `cd`. A command that sets or unsets account variables refuses, including through
common shell wrappers. Keep brief text separate from shell settings: quote it
with `shlex.quote`, or use a private brief file and a quoted `$(cat <brief-file>)`
argument for complex text rather than shell-specific ANSI-C quoting.
A temporary write probe checks receipt storage before any tab is created; it
is removed and never counted as a launch. A probe failure names the error type
and asks for write access to the reader's storage root. Each receipt is written
atomically immediately before the launch command is
submitted, after its tab has a session. A failed or uncertain submit retains its
receipt; inspect the tab before retrying. Receipts use the snapshot command's
`--storage-root`, or the reader's documented App Group default. The launcher
prunes only its own day-old files (identified by the `supervisor-` filename
suffix). A batch failure reports the completed launch identities and the failed tab,
including whether its receipt was written and whether submission was attempted.
List and read these tabs before retrying, rather than replaying the whole batch.

```sh
# Read-only previews; these do not write receipts.
uv run skills/supervisor/scripts/account_choice.py --provider anthropic
uv run skills/supervisor/scripts/account_choice.py --provider openai --count 3
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <launch-file> --account-provider anthropic
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <first-launch> --command-file <second-launch> --account-provider openai
```

Read the returned account, source and reason before recording the session.
Successful launch output reports environment variable names, without their
private path values. A daemon refusal includes the selected home in its recovery
diagnostic; keep that diagnostic out of public comments.

For Codex's shared app server, launch with `codex --remote unix://`: the empty
Unix endpoint resolves through the selected `CODEX_HOME`, so each account uses
its own daemon. A fixed socket or WebSocket endpoint attaches to that server's
account regardless of the exported home. Before creating any tab or launch
receipt, `iterm_tab.py new --account-provider openai` checks each selected home's
`app-server-control/app-server-control.sock` with a one-second connection timeout.
If any connection fails (including a missing or stale socket), the whole launch
refuses, naming that home and its shell-quoted recovery command:
`CODEX_HOME=<selected-home> codex app-server daemon start`. Run that command and
retry. The launcher does not start daemons itself, read tokens or change account
configuration. The check proves the socket accepts connections; it cannot
guarantee that the daemon stays running until the launch command executes.

Accounts live only in private config, the first `[accounts]` table in
`$CODE_HOME`, then `$CODEX_HOME`, then `~/.code`, under
`skill-data/supervisor.toml`, or `--account-config`. List each provider's
accounts in any order:

```toml
[accounts]
snapshot_command = ["<path to ContextPanelAccountSnapshot>"]

[[accounts.account]]
name = "<local nickname>"
provider = "anthropic"
context_panel_configuration_id = "<configurationID from the snapshot>"  # or context_panel_label
env = { CLAUDE_CONFIG_DIR = "<account config dir>" }

[[accounts.account]]
name = "<default-profile nickname>"
provider = "anthropic"
context_panel_label = "<default profile label in the snapshot>"
env = {}
```

Private-config migration: delete the `reserve =` lines under `[accounts]`
and in each `[[accounts.account]]`, including any per-account reserve.
Keep the `[accounts]` table, snapshot command and account environment mappings;
remove obsolete comments that prescribe a fallback order.
They are ignored by the launcher for compatibility with existing files; account
order now only helps people read the mapping. Configure Use last and other
capacity choices in Context Panel. Document this change for the Director;
do not edit their private config yourself.

Codex accounts must set `CODEX_HOME` to the account's home. Claude Code
accounts set `CLAUDE_CONFIG_DIR` for an alternate profile or use `env = {}`
for the default profile. A nonempty env must include the provider's variable.
The launcher unsets an inherited
`CLAUDE_CONFIG_DIR` for the default profile; setting it to `~/.claude` selects
an alternate profile, not the default. The helper never reads credentials and
never changes a
login, including the desktop app's.

## Finished-session shutdown stages

`finished_map.py` offers candidates only. Its conservative last-line parser
rejects quoted, negated, conditional and aborted verdicts. The shared parser used
by status, candidates and terminal closure ignores one complete trailing
`<oai-mem-citation>` block when evaluating the verdict, while retaining the full
response in status output. Text after the block or an incomplete block remains
unrecognized. A complete Claude local `/exit` record sequence preserves
the preceding verdict; other commands or new work invalidate it. Read the transcript
and issue handoff yourself under the skill's full shutdown procedure; an
unrecognized format remains manual verification, never permission to close.

Resolve verified close-out prompts one time by the owning skill, preserving
Director text privately. Once the session is idle at its prompt, use
`iterm_tab.py clear --session-id <id> --verified-target` for Ctrl-U, then `read`
to verify the entire input is empty. Send `/exit` or `/quit` with `send --text`
only after that check, then wait a bounded time for the matched process to end.
Update the ledger's handoff URL from the actual issue record.

```sh
uv run skills/supervisor/scripts/close_ttys.py --ledger <private-ledger.json> --session-id <native-id> --verified-handoff --verified-input
uv run skills/supervisor/scripts/close_ttys.py --ledger <private-ledger.json> --session-id <native-id> --verified-handoff --verified-input --apply
```

The two flags attest the Supervisor checked the issue handoff and empty input
(or recorded the Director's text). Default is a dry run. This helper never
answers prompts or sends exit commands: it checks transcript, ownership, exact
iTerm id and TTY, then proves the recorded PID is absent and only `login` and idle login/interactive shell processes
remain on that TTY. Other jobs, including prompt helpers and shell scripts, preserve the tab.
Process arguments are inspected privately and never emitted. Apply rechecks activity and processes and requests
one non-force close. An uncertain inventory or remaining/reused PID preserves
the tab. Read back `iterm_tab.py list` to confirm closure; never force or retry
an uncertain close. Other terminal applications use the same skill procedure
through their supported controls, not this iTerm adapter.
