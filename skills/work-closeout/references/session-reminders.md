# Native session reminders

Read when the owner asks for a reminder to return to a repository or session.
`work-closeout` owns this workflow for both Codex and Claude Code. A scheduled
human check-in is separate from an unattended watcher or a harness queue.

## Prepare

Use an exact saved session UUID and its surviving working directory. For Codex,
use matching `CODEX_SESSION_ID` / `CODEX_THREAD_ID` when available; stop if they
disagree. For Claude Code, obtain the ID from the current session's own status or
transcript metadata. Never substitute the latest session, a picker, or a guessed
ID. Record a parked task's durable issue and next action before making a reminder.
If closeout removes the session's worktree, choose a surviving checkout and verify
resume there, or use a fresh-session prompt that points to the durable issue.

Run from the owning repository (replace placeholders):

```bash
uv run <skill-dir>/scripts/reminder_link.py --harness codex \
  --directory /absolute/surviving/repository --session-id <uuid> --verify-list
```

Use `--harness claude` for `claude --resume <uuid>`. To start a fresh interactive
session, replace `--session-id` with `--prompt 'Check the owning issue and report the next action'`.
The generator shell-quotes each argument and URL-encodes the command and directory
separately. It includes no credentials, approval flags, or environment dump.
Fresh prompts may contain sensitive material: review them before saving to a
synced reminder. The output includes the native URL, a manual shell fallback,
repository name, exact list, and a stable marker for duplicate lookup.

## Private list selection

Store actual list names privately in TOML:

```toml
[reminders]
list = "Your exact list name"
```

Resolution order: one-off `--list`, repository `.local/skill-data/work-closeout.toml`,
`$CODE_HOME/skill-data/work-closeout.toml`, `$CODEX_HOME/skill-data/work-closeout.toml`,
then `~/.code/skill-data/work-closeout.toml`. The first configured field wins;
missing files or absent fields continue, but a present malformed/unreadable file
or invalid field fails. A one-off override does not persist or change private data.
For a linked worktree, use `--repo /original/repository` to select its existing
private repository configuration. Configuration selects a destination; it does
not grant permission to create, change, or delete reminders.

`--verify-list` uses a read-only EventKit lookup on macOS 14 or later with Swift and existing
Reminders access. Zero or multiple exact names fail; there is no default-list
fallback. A renamed list must be corrected in private configuration or chosen
explicitly. The helper never requests access. If access is unavailable, use the
visible native workflow. A terminal with no prior access request may not appear
in System Settings: this helper does not initiate that request. If an existing
terminal entry is disabled, the owner can enable it under System Settings >
Privacy & Security > Reminders. A permission prompt from an available native
tool needs the owner's action; continue independent work while it waits. Without native verification, output says `manual_required`: verify the
exact unique list visibly before any write. Same-name lists in different accounts
are ambiguous, even if one looks preferable.

## Save and verify in Apple Reminders

Use existing external-write authorization; otherwise prepare the concrete title,
list, schedule, notes and URL and ask the owner before saving. An explicit reminder
request supplies that authority within its stated scope. Do not automatically
create one merely because a task is parked for several days.

If this harness has no native UI controls, give the owner the prepared fields
and the steps below; explicitly record that saving and verification await the
owner. Do not invent UI tools or retry a hanging AppleScript writer.

1. Open the exact unique target list. Search only that list's incomplete items
   for the output marker in Notes. The marker identifies harness, canonical
   directory and exact session (or fresh prompt). Update the one match; create
   only if none exists. Multiple matches require resolving the duplicates before
   saving. Completed items do not silently reactivate. Never use a title-only or
   global search as permission to change another reminder.
2. Set a title naming the repository and check-in purpose. Save the marker,
   durable issue/next action, and `fallback` in Notes. Put `url` in the native URL
   field rather than creating an application, URL handler or background service.
3. Set an explicit first date, time and local timezone, then the requested Repeat
   interval (for example Custom > Every 2 days). Confirm ambiguous relative dates
   with the owner. Do not invent a recurrence. For a one-off reminder set Repeat
   to Never.
4. Reopen Details and verify the saved title, exact list, first date/time,
   recurrence, notes/marker and URL. Click the saved attachment and verify the
   command and directory in iTerm2's **Run Command from URL** dialog. Keep that
   dialog; never enable silent execution or replace confirmation with automation.
   Resume only when the original worker has stopped; cancel the dialog during a
   live-session link test. Actual resume acceptance needs a stopped session and
   inspection of the resumed thread and directory.
5. For cancellation/removal, locate the marker again within the same exact list
   and act only on the intended incomplete reminder under existing removal
   authority. Do not sweep completed occurrences, other lists or similarly titled
   reminders. If a save is uncertain, read back before retrying. On partial save,
   correct or remove only the item created by this operation; preserve preexisting
   reminders. Record any cleanup that needs owner help.

This implementation deliberately generates URLs and verifies destination lists;
it leaves reminder mutation and recurrence in the native UI. AppleScript writes
hung in the original dogfood environment. No idempotent writer or installed helper
app is required; the scoped marker gives the visible workflow its update contract.

## Limits and sources

The supported adapter is iTerm2 on macOS. Native list verification requires
macOS 14 or later; older macOS uses visible list verification. Other terminals and other platforms use
the recorded manual command in the named directory. No iPhone, Watch, cross-device
click behavior, unattended execution, or account/profile portability is claimed.
The receiving machine needs the directory, installed CLI, intended account and
saved transcript. If the transcript is unavailable, report it rather than resuming
the most recent thread; use a separately requested fresh-session reminder instead.

- [iTerm2 command URL and confirmation](https://iterm2.com/documentation-url-scheme.html)
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and installed `codex resume --help`
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
