# Claude account moves

Read this when enrolling account moves or moving an enrolled Supervisor Claude
session after a rate limit. The helper uses the existing
[account reader](helpers.md#choosing-the-account); private `supervisor.toml`
maps Context Panel rows to already signed-in homes. It never logs in, reads
credentials, changes shells, or creates account homes or history links.

## Enrollment

Enrollment is an explicit user-settings write, separate from delivering the
source change. Install and refresh refuse a task worktree or a dirty/non-default runtime
checkout, using the maintained runtime binding lookup. Uninstall recognizes its
private wrapper directly, so removal also works while the runtime is blocked. Preview first, using the catalog's stable runtime path:

```sh
uv run skills/supervisor/scripts/claude_account.py install --settings /absolute/shared/settings.json --move-dir /absolute/private/claude-moves
```

Add `--write` only within authority to change those user settings. If account
homes share `settings.json` through symlinks, pass the canonical shared file;
symlink destinations refuse. The helper preserves unrelated settings and hooks,
and refuses an existing different process wrapper or move directory. Resolve
such a conflict explicitly rather than replacing another launcher's controls.
Its hooks carry `hook --enrollment codex-skills-account-move`; refresh and
uninstall remove only those, plus older unmarked hooks at this catalog's
`skills/supervisor/scripts/claude_account.py` path, which refresh rewrites
with the marker. Another tool's script with the same filename is kept.
After an interpreter upgrade, preview the same install command with `--refresh`;
add `--write` to replace only this helper's existing wrapper and hook interpreter.
To remove enrollment, preview `claude_account.py uninstall --settings
/absolute/shared/settings.json`, then add `--write` within settings-write
authority. Removal preserves other hooks/settings, transcripts and move state. It replaces
the entrypoint with an inert pass-through so sessions retaining its old path can
still relaunch; keep that file until all such sessions have ended.
Run these commands with `uv run` and a currently installed Python; they do not
invoke the old pinned interpreter. Uninstall preserves the private move directory
and requests while disabling this helper's entrypoint and removing its settings.
The installed `env` has `CLAUDE_CODE_PROCESS_WRAPPER` and
`CLAUDE_ACCOUNT_MOVE_DIR`; the wrapper is one absolute executable path encoded as a JSON argument array
to preserve spaces and punctuation. It
registers `StopFailure` for `rate_limit` and
`SessionStart` for `resume`. No plugin hook or shell export is needed. Restart
only the sessions covered by the enrollment authority so they read the settings.
Do not stop a shared background service or the Director's sessions as a test.

Use an absolute persistent private configuration directory outside account
homes, owned by the user with mode 700. Keep it off disposable task worktrees
and removable volumes: this entrypoint is configuration required for relaunch,
not a cache or task artifact. If it is missing, uninstall still removes matching
enrollment settings and hooks without recreating the directory. Write enrollment creates it and its managed `process-wrapper` entrypoint. Protect this directory
as private session evidence: requests contain working and transcript paths, but
never error strings, tokens or credentials. Only sessions carrying `CLAUDE_ACCOUNT_MOVE_ENABLED=1` record or execute moves.
The [Supervisor terminal launcher](../scripts/iterm_tab.py) supplies this marker
only for its Anthropic invocation; it does not persist in a returning tab shell.
Use a final simple `exec` form for Supervisor launch files; the
[launch-shell setup](helpers.md#native-shell-input-proof) describes its marker scope.
Sessions without the marker remain inert.
Before enabling service-hosted sessions, qualify whether
Claude's detached service propagates the marker to unrelated sessions; this
branch does not establish that isolation. Leave the Director's sessions alone.
With no pending move, the wrapper execs the inherited command, arguments and
environment unchanged and never reads account config or queries Context Panel.

Target homes must already share the same `projects/` history. The source
transcript must exist as `<session-id>.jsonl` in it. The wrapper requires the
same working directory, a fresh `useNext` answer, and a different existing home.
It requires the same canonical shared `settings.json` so resume confirmation
runs on the target home, and changes only `CLAUDE_CONFIG_DIR`, unsetting it when Context Panel maps the
choice to Claude's default account. An explicit `~/.claude` selects different
login storage from an unset variable in the qualified Claude version. An inherited
authentication override or a configured `apiKeyHelper` makes
a home move ineffective, so the move remains pending rather than changing or
removing the override. Context Panel failure, stale choice, missing shared
history or an unchanged account also leave the request pending and launch Claude
on its inherited home, allowing its ordinary wait-for-reset fallback.

## Supervisor move step

1. Read private move status with the same enrolled move directory:

   ```sh
   CLAUDE_ACCOUNT_MOVE_DIR=/absolute/private/claude-moves uv run skills/supervisor/scripts/claude_account.py status
   ```

   Match a pending UUID, transcript and working directory to a Supervisor-owned
   ledger session, its process and exact iTerm session. Leave the Director's
   sessions alone. Status contains private paths; do not paste it publicly.
2. Using the [terminal helper](helpers.md#launching-and-nudging), read the exact
   tab. Verify it is at an idle prompt with no turn running and an empty input
   line. Preserve input whose author is uncertain. Use `/restart` only after
   its availability and wrapper/resume behavior have been qualified on the
   running version under [Version qualification](#version-qualification).
   If it reports `Unknown command`, keep the session and pending request in
   place and hand off qualification; do not force a vendor feature flag or
   substitute a settings-changing command in a real home.
   If the session later continues in place after reset, use step 5's verified
   cancellation before a later relaunch, so the old request cannot move it.
   Write `/restart` to a private
   message file, send it once with `--session-id` and `--verified-target`, then
   read back. Never replay an uncertain send or start a second copy of the
   transcript while the old process runs.
3. Read back the same tab and resume arguments for the same UUID, then use the
   resume receipt below to prove the move completed. Tab, PID and argv alone
   cannot prove a relaunch if the original launch already used `--resume`.
   Native re-exec can replace the process image while retaining its
   PID; PID disappearance is not required and PID continuity alone does not
   prove resume. If the PID changes, verify the old process ended before
   accepting the replacement. Never launch another copy to obtain this proof. Read
   move status: `<session-id>.resumed.json` must say `resumed`, naming the same
   UUID and selected account. This receipt names the configured account/home; it does not authenticate the
   account identity. It is written only by `SessionStart`
   with source `resume`, the selected home, and the same transcript and working
   directory; until then the request remains. Update the ledger's process and
   home context, retaining the native session and transcript.
4. At the verified replacement prompt, send a private message file containing
   `continue`, once, and read back. Verify its next turn continues the prior
   work. A receipt proves resume identity, not a successful model turn.
5. If the request remains, inspect its state, any `problem`, and the tab before taking any
   further action. A prepared attempt retries the same selected home without
   choosing another account. Repair the documented config/history problem
   within existing scope or hand it off; a real cwd mismatch or repeatedly slow
   snapshot means hand off for native qualification before another move attempt; never sign in, change login, clear
   history, or apply a reset. Preserve requests until recovery is verified.
   If the account resets and the session continues in place instead, cancel its
   obsolete move with `claude_account.py cancel --session-id <native-uuid>` using
   the same move-directory environment. Verify the session first; this removes
   only its request and keeps the transcript. Do this before a later restart.
   If the prepared target itself is limited, a new rate-limit hook after a
   verified `continue` replaces the attempt and queries useNext again. If resume
   succeeds but login fails, hand off the failed turn; the receipt does not
   authorize a login or claim authentication succeeded.

A move records one count-only launch receipt through the existing account
reader when committing to the target, and retains it on an uncertain exec just
as the launcher does. Retries reuse that receipt.

The hook uses a per-session lock and atomic writes. It records only the affected
session. The wrapper bounds its snapshot subprocess to 1.5 seconds to stay
inside the launcher's roughly three-second startup budget; it is silent before
exec. The entrypoint uses the operating system's `/usr/bin/perl` on supported
POSIX hosts to preserve the inherited environment without a shell or Python
startup on its direct-exec path. Only a pending marked UUID
enters the absolute Python interpreter with `-I`; missing Python or helper source
falls back to the original command. The entrypoint preserves pre-Python locale
state across Python's C-locale coercion. The account reader loads lazily only for
a pending move. Install refuses if the OS entrypoint interpreter is unavailable;
qualify the supported host before enrollment. Failed exec/startup leaves the move request for recovery. A new rate limit
replaces that session's previous attempt and clears its old resume receipt.

## Version qualification

[Process wrappers](https://code.claude.com/docs/en/corporate-launcher) and
[StopFailure](https://code.claude.com/docs/en/hooks#stopfailure) are documented.
The approved design read `/restart`'s resume path in Claude Code 2.1.294;
source reading did not qualify command availability or native execution.
In 2.1.296, `/restart` registration additionally requires an eligible native
interactive launch and vendor feature enablement. A process wrapper does not
enable the command. The bounded `Unknown command` observation and current
qualification evidence belong to [#1634](https://github.com/cbusillo/codex-skills/issues/1634);
they do not establish a vendor-wide defect or identify every prior probe's
feature state.
An exec pass-through wrapper retains the native binary's install identity;
it neither supplies vendor enablement nor inherently excludes an otherwise
eligible native launch. This record does not qualify `/restart` account moves.

The documented [renderer switch](https://code.claude.com/docs/en/fullscreen#enable-fullscreen-rendering)
provides a disposable lifecycle probe: start a conversation in an isolated
home through `exec env`, with a pass-through process wrapper; run `/tui` to
identify the renderer, then switch to the other renderer. Record the exact tab,
TTY, PID/PPID, cwd and wrapper argv before/after, the resumed UUID and visible
idle prompt, and verify the next request carries the earlier conversation.
The 2.1.296 probe on #1634 retained the PID, terminal and transcript through the
wrapper's `--resume` invocation. It used a local fake API, so it proves native
lifecycle and conversation forwarding, not signed-in inference or an account move.
It qualifies that exec launch shape only, not the launcher's returning subshell
form or detached-service sessions. Qualify the actual launch shape separately
before using it for moves.

`/tui` saves a user renderer preference and is not a qualified drop-in account
move. Keep this probe in disposable homes. The wrapper recognizes a pending
move only from the shared parser's single exact native resume, including a renderer switch.
The parser must reach that canonical UUID before unknown options; all resume values
must agree, without continue or fork. Otherwise the inherited account and argv stay. Do not use an unrelated
relaunch to bypass qualification or recovery; use the existing verified
cancellation step when continuation in place makes the request obsolete.
Launch restrictions such as
`--setting-sources` can make the native command refuse to relaunch; preserve
that refusal rather than dropping restrictions. Before live rollout on another
version, qualify the actual move command in throwaway sessions.
Before live enrollment, also qualify detached-service marker inheritance,
managed/CLI credential overrides and actual restart cwd/argv on that version.
These fixture tests cannot prove a signed-in model turn across accounts.
Per-home rewind checkpoints may remain unavailable after a move; transcript
context is shared, while login and per-home state stay separate.
