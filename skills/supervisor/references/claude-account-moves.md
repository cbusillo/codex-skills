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
After an interpreter upgrade, preview the same install command with `--refresh`;
add `--write` to replace only this helper's existing wrapper and hook interpreter.
To remove enrollment, preview `claude_account.py uninstall --settings
/absolute/shared/settings.json`, then add `--write` within settings-write
authority. Removal preserves other hooks/settings, transcripts and move state.
Run these commands with `uv run` and a currently installed Python; they do not
invoke the old pinned interpreter. Uninstall preserves the private move directory
and requests while removing this helper's entrypoint and settings.
The installed `env` has `CLAUDE_CODE_PROCESS_WRAPPER` and
`CLAUDE_ACCOUNT_MOVE_DIR`; the wrapper is one absolute executable path. It
registers `StopFailure` for `rate_limit` and
`SessionStart` for `resume`. No plugin hook or shell export is needed. Restart
only the sessions covered by the enrollment authority so they read the settings.
Do not stop a shared background service or the Director's sessions as a test.

Use an absolute directory outside account homes, owned by the user with mode
700. Write enrollment creates it and its managed `process-wrapper` entrypoint. Protect this directory
as private session evidence: requests contain working and transcript paths, but
never error strings, tokens or credentials. Only sessions carrying `CLAUDE_ACCOUNT_MOVE_ENABLED=1` record or execute moves.
The Supervisor terminal launcher contains this marker in a subshell for its
Anthropic invocation; it does not persist in the tab shell. Sessions without the
marker remain inert. Before enabling service-hosted sessions, qualify whether
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
   line. Preserve input whose author is uncertain. Write `/restart` to a private
   message file, send it once with `--session-id` and `--verified-target`, then
   read back. Never replay an uncertain send or start a second copy of the
   transcript while the old process runs.
3. Verify the old process ended and the replacement occupies the same tab. Read
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
   within existing scope or hand it off; never sign in, change login, clear
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
startup. Ordinary commands go straight to exec. Only a pending marked UUID
enters the absolute Python interpreter with `-I`; missing Python or helper source
falls back to the original command. The entrypoint preserves pre-Python locale
state across Python's C-locale coercion. The account reader loads lazily only for
a pending move. Install refuses if the OS entrypoint interpreter is unavailable;
qualify the supported host before enrollment. Failed exec/startup leaves the move request for recovery. A new rate limit
replaces that session's previous attempt and clears its old resume receipt.

## Version qualification

[Process wrappers](https://code.claude.com/docs/en/corporate-launcher) and
[StopFailure](https://code.claude.com/docs/en/hooks#stopfailure) are documented.
The approved design verified `/restart` preserving the session through the
wrapper in Claude Code 2.1.294 by source reading; `/restart` remains undocumented.
Before live rollout on another version, qualify that route in throwaway homes.
Before live enrollment, also qualify detached-service marker inheritance,
managed/CLI credential overrides and actual restart cwd/argv on that version.
These fixture tests cannot prove a signed-in model turn across accounts.
Per-home rewind checkpoints may remain unavailable after a move; transcript
context is shared, while login and per-home state stay separate.
