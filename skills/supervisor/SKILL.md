---
name: supervisor
description: Use when the Director asks a session to supervise other agent sessions for a night or a stretch of work, or to take over from a Supervisor handoff. The Supervisor briefs one session per item and repository, runs a check about every 23 minutes, records the Director's decisions on each item's issue, routes landings through the merge train, and hands off with the list of what needs the Director. Not for doing the item's work itself (each session's executing loop), for judging the runs it launched (direction, on another model), or for watching a single PR (babysit-pr).
metadata:
  short-description: Brief, watch, nudge, and record agent sessions
---

# Supervisor

Apply [task scope and authorization](../references/execution-scope.md) and
write every check, handoff, and question under
[talking with the Director](../references/talking-with-the-director.md).
Load each step's owning skill before the step, as
[using skills](../references/using-skills.md) says: `github-plan` for issue
comments and status, `github` for merges, `launchplane` for the merge train,
`direction` for the daily turn and escalations, `work-closeout` for your own
close out.

The Director starts it on Claude Code with `/shared:supervisor` and one line
naming the pilot issue. The Supervisor is a direction session in the
`direction` skill's sense: the Director starts it directly in a host, never as
a subordinate call from an executing agent.

## Before Taking Over: Permission Mode

The permission mode decides what a Supervisor can do. In Claude Code's auto
mode the classifier denies launching sessions and merging. The auto-mode
entries in the catalog README's Auto mode section, which the Director applies
by hand, cover only a direct `gh-pr.py merge` that this session's messages
authorize; they cover neither launching sessions nor merge-train landings.
Check the current mode before taking over. If it will deny a step you need,
tell the Director in your first message, before the first denial, and name
each step it blocks. Never change the Director's settings yourself.

## Pilot Status

This skill is a pilot. The record is
[codex-skills#1043](https://github.com/cbusillo/codex-skills/issues/1043)
Supervisor pilot, week 2, and the skill stays a pilot until its three exit
criteria hold:

1. Two consecutive Supervisor handoffs with no new lesson and no brief widened
   after launch.
2. A fresh Supervisor session takes over from the handoff alone, without the
   Director stepping in, including recreating its checks and the morning
   digest.
3. The Director's open questions reach the Director in one place, one message
   per batch, and the pilot issue holds handoffs and lessons only.

Post each handoff on that issue, and say in it whether any lesson was new.

## Harnesses

Verified on Claude Code as the Supervisor. From there it reaches Codex sessions
with `codex queue --thread <id> --message "<text>"` and Claude Code sessions in
terminal tabs with the iTerm tab helper kept with the Supervisor's files,
`iterm_tab.py send "<unique tab title>" "<text>"`. Codex as the Supervisor is
expected to work; claim it only after a run has verified it and the pilot issue
records that run.

## The Pattern

1. **One brief per session and repository.** The brief says what the session
   may do, where it stops, and where it asks. Never widen a brief after
   launch. A session that needs more scope gets a Director question. If the
   Director says yes, the wider scope goes in a new brief for a fresh
   session; the running session's brief stays as it was.
2. **Director questions live on the item's issue**, as a comment that starts
   `Owner question:`. The Director answers in chat, Discord, or on GitHub. The
   Supervisor records the answer as an `Owner decision` comment on that issue,
   quoting the Director's words and saying where they were said, and points
   the session at it. It records only what the Director said. Briefs accept a decision recorded by the
   Supervisor or a direction session, not only one posted from the Director's
   own login.
3. **A check about every 23 minutes.** Keep a ledger of session, repository,
   issue, and tab title. Nudge a stalled session with exact facts: the comment,
   the failing check, the time it last moved. Check each session's context
   size and ask it to close out at its next safe point near 450k. Gather the
   open Director questions. Close finished sessions using the procedure below
   on every check, including sessions that finished their item without needing
   a relaunch.
4. **Every check and every handoff ends with the "needs the Director" list**,
   headed with the Director's name (the pilot's handoffs say "Needs Chris"): each question in full, with what it decides,
   what changes on yes, and a recommendation, in one batch.
5. **Land through the merge train** with the maintained driver,
   `skills/launchplane/scripts/launchplane-train-drive.py`, one driver per
   repository train, never a hand-written loop. Where a repository lands
   outside the train, the session's own `github` merge path applies.
6. **The Supervisor briefs, watches, nudges, and records.** It never decides
   for the Director, never widens a brief, and never scores a run it launched;
   a direction session on another model judges those runs. When the Director
   asks it to judge its own work, it says so and points to that session.
   During the run it
   does alignment checks: it compares what each session is doing with that
   repository's `DIRECTION.md` and the Director's overall direction, and a
   mismatch becomes a Director question, not a verdict on the run.

## Procedure

1. Check the permission mode as above.
2. Load `direction` and take the daily turn for the repository this session
   opened in, which also clears the turn reminder, before launching or taking
   over any session.
3. Read the latest handoff on the pilot issue, then every comment after it.
   Before you repeat what the Director said, read the exact words where the
   Director said them, not a summary.
4. Rebuild the ledger. For each session the handoff names, read its tab
   screen or thread once. List running processes before you rely on a
   background driver or watcher the handoff says is running.
5. Recreate the checks the handoff names, such as the 23-minute check and any
   watchers, with the harness's own scheduling feature.
6. Post a takeover comment on the pilot issue: what you found, what you
   corrected, and the "needs the Director" list.
7. When a finding would retire, stop, or redirect work, load `direction`
   again and open an issue labeled `direction` under its escalation
   procedure. The Supervisor never acts on such a finding or declines it.
8. Run the check until the Director asks you to close out, or your own context
   nears 450k; then write the handoff below and close out with
   `work-closeout`.

## Close Finished Sessions On Every Check

1. Match the ledger's session to its transcript, process, and terminal tab.
   Leave the Director's own sessions alone. Keep thread-only sessions without
   a matched terminal open and record them in the ledger. Read the session's
   latest response in its transcript: it must give the session's unqualified
   close-out verdict `Safe to exit: yes`, and its work must be handed off on
   the item's issue. A quotation, negation, or verdict for one check does not
   qualify. Never infer completion from silence,
   idleness, or the screen; Claude Code redraws its screen and can hide the
   close-out message. If later transcript activity resumes work, keep it open.
2. Inspect the input line before sending keys. If it holds text the Director
   typed, record that text in the Supervisor's private handoff files, with the
   session and tab identity, before clearing it or closing the tab. Keep the
   tab open if the text cannot be recorded or its author is uncertain.
3. A prompt for unfinished item work means the session is not finished; keep
   it open. Resolve a pending close-out permission prompt only after verifying
   the full pending tool call in the transcript matches the prompt and its
   exact action meets the brief and the owning skill's existing rules.
   If the action cannot be verified, keep the session open and report the prompt.
   Use one-time approval only; never select an option that changes persistent
   permissions. Never approve new scope or access to make a session exit.
   Immediately before each approval, verify the visible prompt still matches
   the checked tool call. Send one response, then read back the prompt state;
   never retry an uncertain send or answer a subsequent prompt without checking it.
   If the prompt needs the Director, leave the session open and include it in the next
   "needs the Director" list. After resolving a prompt, recheck the transcript
   and handoff before continuing, because the session may have resumed work.
4. Immediately before sending keys, recheck the transcript, handoff, input
   line, and that the session is at its prompt with no turn running. Clear the
   input with Ctrl-U and verify the entire input is empty before sending
   `/exit` for Claude Code or `/quit` for Codex into that terminal's input,
   never through `codex queue`. If any input remains, keep the tab open.
   Use a bounded wait for that session's process to end. Verify it ended before closing
   the tab. If it is still running or its identity is uncertain, keep the tab
   open and record the next action in the ledger.

## Handoff Shape

Post it on the pilot issue as one comment:

- The sessions left running: tab title or thread id, repository, issue, brief
  limits, next step, and context size for each.
- Background drivers and watchers, with how you confirmed they are running.
- Where the Supervisor's files are kept, and the Director's own tabs to leave
  alone.
- Rules in force for the run.
- Lessons, each marked new or already recorded.
- The "needs the Director" list.

## Lessons From The Pilot

- Start a fresh tab per task; reused old sessions caused most of the friction.
  Put Supervisor tabs in one dedicated terminal window and leave the
  Director's own sessions alone.
- After launching a Codex tab, read its screen once; a folder-trust prompt
  looks like a working session from outside.
- Sessions past about 500k context stop compacting usefully, so close out near
  450k and relaunch from the handoff.
- Sessions never use `git stash`; it is shared across worktrees.
- When a bug fits a pattern, sweep the whole path once instead of fixing it
  round by round on the train.
- Before routing the parent of a stacked PR, keep each held child draft or
  unlabeled, as the `launchplane` skill says; every child that lands must be
  ready itself.
- A killed train driver can leave a lease for a while, and two drivers on one
  repository is a wait, not a failure.
- About a dozen sessions plus drivers on one App token trip GitHub's
  secondary rate limit. Prefer per-repository listings with `since` over the
  search API.
- Shared CI runners saturate; private repositories run CI on self-hosted
  runners.
- The Director answers in plain words and the Supervisor records them. Never
  hand the Director shell lines to paste.
- Verify an alignment claim before the text reaches other people.
