# Native reminders

Read when the Director asks for a plain reminder or a reminder to return to a repository or session.
`work-closeout` owns this workflow for both Codex and Claude Code. A scheduled
human check-in is separate from an unattended watcher or a harness queue.

## Plain reminders

Use the shared helper for ordinary reminders too; do not substitute one-off
AppleScript or EventKit writers. Prepare a dry run with the exact chosen list:

```bash
uv run <skill-dir>/scripts/reminder_save.py --plain \
  --title "Open the weekly motor report and assign this week's 3 dismantles" \
  --notes 'Review the report before assigning work' --list 'Your exact list name' \
  --due 2026-10-19T08:00 --timezone America/New_York \
  --repeat weekly --weekday monday
```

Plain mode needs no harness, directory or session ID. Use `--notes` for ordinary
notes and optionally `--url https://example.com/report` for a web attachment;
`--link` stays reserved for session issue/PR notes. For international domains,
use the browser-copied ASCII/punycode hostname; raw Unicode hostnames are refused
rather than converted to a potentially different site. Unicode paths are escaped. Choose `--repeat daily`,
`weekdays`, `weekly --weekday monday` (any full lowercase weekday), or `monthly`;
omit it for a one-off. The first due date must match the weekly day or fall on a
weekday for `weekdays`. Monthly repeats use the first due date's day with native
calendar recurrence; choose a day present in every month if every month must fire.
Session recurrence options remain unchanged.

Use `--repo /original/repository` instead of `--list` to resolve a privately
configured list as described below. Review the prepared fields, then add `--apply`
only under existing reminder-write authority. A dry run never opens or queries
Reminders; apply performs the exact-list lookup and duplicate checks before saving.

Plain reminder identity is the exact title in the selected list, rather than the
optional link (several different tasks can share one report). The shared writer
refuses duplicate incomplete matches. Completed items with the same title never
block creating or updating a reminder. An existing
incomplete match needs its explicit `--update-id` and update
authority; the same title is required. The same ID-based repair and deletion
rules below apply. A renamed title is a different target, so do not rename to
bypass an existing-item refusal.

A plain reminder without a link uses EventKit alone and needs existing Reminders
access. A reminder with a link also needs the one-time shortcut described under
[Save and verify](#save-and-verify-in-apple-reminders). The `saved` receipt requires saved-field readback in both cases;
`visible_url_verified` is false when no URL was requested. Removing an existing
link requires the visible manual workflow; a no-link update to an item with an
EventKit URL refuses before writing, because EventKit absence alone cannot prove
that the native attachment disappeared. A `saved` receipt without a requested URL
verifies no EventKit URL; it does not prove the absence of a UI-only attachment
on an item created outside this helper. Use the visible manual workflow to remove
those attachments. Never recreate after
`partial`, a timeout or an uncertain receipt: inspect the same item first. For
manual fallback, check the exact title in the unique list and use the dry-run
fields. Terminal confirmation below applies only to session links.

The sections below describe the session path and the shared list/write rules.

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

Omit `--verify-list` to prepare fields for visible manual verification on unsupported
platforms or when native access is unavailable; output then says `manual_required`.
If a verification attempt fails for access or platform support, rerun the same
command without that flag. Missing or ambiguous target lists still need correction
before any save; omitting the flag does not establish a valid destination.

`--verify-list` uses a read-only EventKit lookup on macOS 14 or later with Swift and existing
Reminders access. Zero or multiple exact names fail; there is no default-list
fallback. A renamed list must be corrected in private configuration or chosen
explicitly. The helper never requests access. If access is unavailable, use the
visible native workflow. A terminal with no prior access request may not appear
in System Settings: this helper does not initiate that request. If an existing
terminal entry is disabled, the Director can enable it under System Settings >
Privacy & Security > Reminders. A permission prompt from an available native
tool needs the Director's action; continue independent work while it waits. Without native verification, output says `manual_required`: verify the
exact unique list visibly before any write. Same-name lists in different accounts
are ambiguous, even if one looks preferable.

## Save and verify in Apple Reminders

Use existing external-write authorization; otherwise prepare the concrete title,
list, schedule, notes and URL and ask the Director before saving. An explicit reminder
request supplies that authority within its stated scope. Do not automatically
create one merely because a task is parked for several days.

Both harnesses use the same writer. Prepare a dry run first:

```bash
uv run <skill-dir>/scripts/reminder_save.py --harness codex \
  --directory /absolute/surviving/repository --session-id <uuid> \
  --due 2026-10-20T09:00 --timezone America/New_York \
  --link https://github.com/OWNER/REPO/issues/123
```

Use `--harness claude` for Claude Code, or replace `--session-id` with `--prompt`
for a separately requested fresh session. Add `--link` again for the PR. The default
title names the repository, issue numbers (or session ID), provider and local due
time (`every N days from` the first due time for a repeat), so a session's next
check-in does not collide with its completed one. `--title` can name the requested purpose. Notes contain only the provider and session ID
(or “fresh session”), followed by issue/PR links. Keep next actions on the owning
issue. The fallback command and machine marker are dry-run fields, not visible notes.

Add `--apply` under existing write authority. `--due` is an explicit local date and
time; `--timezone` is a named timezone. Ambiguous/nonexistent daylight-saving times
are refused. Omit `--repeat-days` for a one-off; use `--repeat-days 2` only for a
requested every-two-days recurrence. Other session repeat patterns use the manual workflow;
do not approximate them or invent a recurrence.

The writer resolves the same private configuration as `reminder_link.py`, including
`--list` and `--repo`. It requires exactly one writable list and existing Reminders
access; it never requests permissions. A reminder with a link is created by the
**Agent Reminder Link** shortcut, which runs Shortcuts' built-in Add New Reminder
action with the title, notes and URL. EventKit then moves it to the exact list and
sets the schedule, alarm and recurrence. The writer clicks nothing, so it works with
Reminders closed or in the background, in any language.

Install the shortcut once per Mac: `uv run <skill-dir>/scripts/reminder_shortcut.py`
signs it on this Mac and opens it; the Director clicks **Add Shortcut** and keeps the
name. `--check` reports whether it is installed. Shortcuts may ask once for Reminders
access. Without the shortcut, a linked save refuses before any change and names this
step.

**EventKit's `url` property is not the visible URL attachment** and never counts
as success. The writer sets it for duplicate lookup; older reminders can also be
found by the generator's marker in Notes. It verifies the visible link by reading
the Reminders store read-only and never writes ReminderKit internals or the
Reminders database. Diagnose reminder problems read-only, through EventKit and the
store; never press buttons in, or script, the Director's Reminders app.

A single existing incomplete match is reported with its ID and left untouched.
Pass `--update-id <id>` only when changing that existing reminder is authorized.
Shortcuts cannot add a link to an existing reminder, so an update with a link
requires that item to already show the same visible link; otherwise the writer
refuses and the item is deleted with `--delete-id` and created again.
For an authorized cleanup of existing Notes, also pass `--preserve-existing`.
It requires `--update-id` and the unchanged title (pass the existing title with
`--title`, or the original `--due` for a default title), changes only Notes, and
reads back the same ID with unchanged list, title, due/start dates, timezone,
priority, completion, alarms and recurrence. The required dry-run schedule fields
are validated but are not applied in this mode. Ordinary updates keep their
explicit schedule behavior.
Missing update targets and duplicate incomplete matches are refused.
Completed matches are never changed or reactivated and do not block a new reminder. A `saved` receipt confirms list,
title, schedule, alarm, recurrence, short notes and the exact visible URL. A `partial`
receipt includes the created/updated ID and missing proof: do not claim completion
or repeat creation. Finish that same item with its ID or, under removal authority,
use the same target arguments with `--delete-id <id> --apply`. Deletion verifies
absence and accepts only the intended incomplete reminder in the exact list.
A timeout or missing receipt is uncertain: read back before retrying.

### Visible manual fallback

On unsupported platforms or when existing access is unavailable, use the writer's
dry-run fields. Report saving as pending; do not substitute an ad hoc writer. On
macOS, open the exact unique list and check its incomplete items for the exact
session URL (or the old marker). Update only an authorized exact match; resolve
multiple matches first and leave completed items alone.

Set the prepared title, short Notes, explicit first date/time, requested recurrence
and native URL field. Leave the URL field and close Details, then reopen it to verify
all fields and the visible attachment. Escape can discard the URL edit. Without
native list verification, verify the unique destination visibly before any save;
missing/ambiguous lists never justify a default-list fallback. When no UI tools exist,
give the Director these fields and one paste step for the native URL.

### Terminal confirmation

Open the saved attachment (double-click if a single click only selects it) and verify
the exact command and directory in iTerm2's **Run Command from URL** dialog. Keep
that dialog; never enable silent execution or replace confirmation with automation.
Resume only when the original worker has stopped; cancel during a live-session link
test. Actual resume acceptance needs a stopped session and inspection of the resumed
thread and directory. Neither a `saved` receipt nor URL readback claims that resume.

For cancellation/removal, act only on the exact incomplete item under existing
removal authority. Never sweep completed occurrences, other lists or similarly named
reminders. Record any partial-save cleanup that needs the Director's help.

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
- [Apple EventKit recurrence rules](https://developer.apple.com/documentation/eventkit/ekrecurrencerule)
