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

## Launching and nudging

```sh
uv run skills/supervisor/scripts/iterm_tab.py window
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <dedicated-window-id> --command-file <private-launch-file>
uv run skills/supervisor/scripts/iterm_tab.py list
uv run skills/supervisor/scripts/iterm_tab.py read --session-id <iterm-session-id>
uv run skills/supervisor/scripts/iterm_tab.py send --session-id <iterm-session-id> --text-file <private-message-file> --verified-target
```

Use one dedicated Supervisor window, never the window with the most tabs. The
helper restores the previous tab after launch. Launch files contain the exact
brief and account/model settings already authorized, without the Discord
channels flag; `--account-provider` can choose the account instead (see
below). Run one agent invocation, without restart loops or commands
that continue after it exits; put required environment settings on that launch
command (for example with `env`). Read the launched screen once for folder trust or another
blocking prompt. Record the new native thread and exact transcript in the
ledger. Prefer `codex queue` for Codex nudges with the verified thread id and
its configured home; never queue exit commands. The terminal helper suppresses
broadcast input, uses bracketed paste for multiline messages, and sends text
and Return separately. Verify input and prompt
before `--verified-target`, then read back once; do not replay an uncertain send.

## Choosing the account

Launch with `--account-provider openai|anthropic|google` and the helper follows
Context Panel's own **Use next** choice for that provider, as the Director
corrected on [codex-skills#1108](https://github.com/cbusillo/codex-skills/issues/1108).
It reads `answers.useNext` from the same agent snapshot and maps its `accountID`
to one configured launch account. Context Panel owns the ranking, including
banked reset expiries and Use last. The existing capacity reserve still applies;
a choice without room or without a unique configured match refuses instead of
silently launching a different account.

The helper exports that account's `env` settings ahead of the launch command,
so they also reach an agent started after `cd`, and reports the source, account,
reason and other accounts in the launch output. Read that output before
recording the session.

```sh
uv run skills/supervisor/scripts/account_choice.py --provider anthropic
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <launch-file> --account-provider anthropic
```

Capacity and the choice come from Context Panel's agent account snapshot
(schema 1, its `ContextPanelAccountSnapshot` reader). When a provider has no
Use next choice, the helper reports `fallback` and keeps the previous behavior:
it chooses the soonest weekly reset with room above the account's reserve
(or the latest reset when no weekly window is labeled). If the reader fails
or every configured account has no current reading (unknown, stale, refreshing,
unavailable, not connected or off), it uses the configured order without a
capacity check and says why. Known readings with no room and ambiguous matches
still refuse and create no tab. `--account <name>` launches on a named configured
account instead. A launch file that already sets the account's variable is refused.

For Codex's shared app server, launch with `codex --remote unix://`: the empty
Unix endpoint resolves through the selected `CODEX_HOME`, so each account uses
its own daemon. A fixed socket or WebSocket endpoint attaches to that server's
account regardless of the exported home. The chosen home must already have its
daemon running when using `--remote`; use Codex's built-in daemon start command
for that home when needed.

Accounts live only in private config, the first `[accounts]` table in
`$CODE_HOME`, then `$CODEX_HOME`, then `~/.code`, under
`skill-data/supervisor.toml`, or `--account-config`. List each provider's
accounts in the Director's fallback order:

```toml
[accounts]
snapshot_command = ["<path to ContextPanelAccountSnapshot>"]
reserve = 0.05  # default share of the tightest window kept unused

[[accounts.account]]
name = "<local nickname>"
provider = "anthropic"
context_panel_configuration_id = "<configurationID from the snapshot>"  # or context_panel_label
env = { CLAUDE_CONFIG_DIR = "<account config dir>" }
reserve = 0.2  # optional per-account reserve
```

Codex accounts must set `CODEX_HOME` to the account's home and Claude Code
accounts `CLAUDE_CONFIG_DIR`; config without its provider's variable is
refused. The helper never reads credentials and never changes a
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
