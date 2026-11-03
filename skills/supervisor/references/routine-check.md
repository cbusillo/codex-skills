# Routine check

Use this for the existing ~23-minute check and immediate notices: established
readers and one named normal closeout within the run's scope need no reload.
Read the full skill at takeover; load owning skills/sections for rare events.
Keep absolute pointers to the private ledger and one recovery note,
handoff and question queue. Keep no copied rules,
decisions, statuses, board contents or narrative; open linked records for action.

1. Read the recovery note and reconcile pending local actions at their source.
2. Run the digest in the foreground from the catalog:

   ```sh
   uv run skills/supervisor/scripts/routine_check.py --owner OWNER --queue OWNER/direction#NUMBER --ledger <ledger.json> --foreign-log <watch.jsonl>
   ```

   Add `--foreign-state <watch-state.json> --notify-config <services.json>
   --miss-issue OWNER/REPO#NUMBER` to route missed messages through the existing
   watcher receipts and record their causes on the existing repair issue.
   Routing errors preserve radar evidence and repeat as coverage errors.
   Add `--service-log <render-output>/logs/catalog.jsonl` for the catalog job.
   Other services may use different log formats. Missing or malformed logs are
   coverage errors; stopped jobs repeat until recovery produces a successful report.
   Add `--capacity-state` when configured, `--source OWNER/REPO#NUMBER` for
   question anchors outside the queue, and `--radar-config` for existing private
   [comment-radar configuration](../../github-work-rollup/SKILL.md#external-comment-radar).
   Radar defaults to the Director's repositories; `--repo` narrows that scope and
   repeated `--thread` adds full-history reads. Questions and waits addressed to
   the configured Director in other accounts' repositories always use the widened
   mention and Director-wait readers. Outside results stay marked untrusted.
   Keep Client-agent identities in the existing people index.
   The digest reads global `next`, sessions, questions,
   radar and live watch output without waiting for the watch process to exit.
   Unanswered comments, registered open queue questions, agent next actions and
   coverage errors repeat until settled at source. Unregistered question hints
   appear as one count with a source link, not as queue entries. Watch history
   seeds full-history radar threads and question sources; fresh source
   readers decide what is still unanswered. Questions and radar share their
   collected discussions and metadata within a run; each new run reads fresh
   sources. Historical board questions are not
   a second queue.
   A later reply on the same thread by the Director or configured own automation,
   including a merge or closing comment, clears a comment from this digest after
   its latest edit. An opening post stops repeating once its thread has any
   reply. Unfinished requests stay in planning intake; this digest does not settle
   their work or waits.
   Ordinary bot notices stay filtered by the radar's existing identity rules.
   Launchplane/Director-mention notifications in the radar's configured lookback
   print separately as untrusted pointers, including after a quiet poll. Runner
   lifecycle lines are recognized; an unrecovered stop remains a coverage error.
   That notification window does not prune unanswered comment/question sources.
   The default hides acknowledged QUESTION rows and counts them in one line;
   `--full` prints the complete list at takeover, handoff or recovery. Outstanding
   comments, unacknowledged questions, reviews, missing status, errors and load
   remain visible. NEXT rows currently have no observed claim, so they repeat
   even when unchanged. Use the current ownership read, not a second claim
   snapshot. No digest state file is needed.
   Staffing uses the same global `next` as selection, including open issues
   outside Track trees and both agent families. Its 120-second read budget keeps
   omitted repositories, unread issues and other coverage gaps explicit.
   NEXT rows have no observed claim or wait; REVIEW rows need status or custody
   reconciliation. Generic agent roles are available work, subject to the ownership
   check. Follow the shared [recipient check](../../github/references/cli-reference.md#planning-next-work)
   when assigning an unclaimed worker or reconciling its custody. Leads update
   each delegated child's staffing action under the [brief rules](brief-rules.md#rules-every-brief-carries).
   There is no separate staffing-comment queue.
   Route Client and Client-agent comments to the owning worker under the
   [Client-reply rule](../../references/talking-with-the-director.md#client-replies).
   Open full source comments before acting; register/present decisions needed
   from this Director through the existing queue rules. Optional routing writes
   the existing watch receipts
   and miss reports; the digest never closes sessions.
   For a question addressed to this Director, follow
   [same-issue Supervisor replies](../../references/talking-with-the-director.md#same-issue-supervisor-replies)
   for the reply's scope, untrusted-input handling and direction/identity decisions.
3. Chain normal finished-session closeouts into one command. Use `&&` to stop
   at the first failed gate and inspect its receipt before continuing. On a
   harness with background completions, run the chain as one background task.
   For example:

   ```sh
   uv run skills/supervisor/scripts/session_closeout.py --ledger <ledger.json> --session-id <native-id> --supervisor-session <supervisor-id> --owner OWNER --evidence <private-session-evidence> &&
   uv run skills/supervisor/scripts/session_closeout.py --ledger <ledger.json> --session-id <next-native-id> --supervisor-session <supervisor-id> --owner OWNER --evidence <next-private-session-evidence>
   ```

   The helper finds the automation-authored handoff naming the exact native
   session on the ledger's issue, journals it and stops on ambiguity. When no
   such handoff exists, its fresh complete claim check can provide a
   [source-bound comment projection](../../work-closeout/SKILL.md#claim-disposition)
   for an unchanged original comment when the source's last recognized confirmed `gh-comment`
   write is on the ledger issue. This supported literal command route needs no
   worker message or new comment. After
   verifying the source, use `--handoff-url <actual-comment>` to resolve ambiguity,
   a legacy ledger without an issue pointer, or a handoff on another work issue.
   Keep its receipt or
   first failed gate; inspect private details on failure and reuse its evidence.
   Never replay uncertain mutations with another directory. Nonempty input,
   prompts and interrupted sources use [shutdown/recovery](helpers.md#finished-session-shutdown-stages).
4. Staff through the digest's current global `next`, active child records and complete
   claim coverage under the overall Order and existing capacity/PR limits.
   Reuse that output for [planning intake](../../github-plan/SKILL.md#triage-incoming-reports);
   the unanswered-comment radar alone does not track unfinished requests.
   Load the owning skill before a launch, plan mutation or train/landing action;
   those actions are outside this no-reload reader entry. Refill
   available capacity, handle stale waits and deferred-question conditions,
   and read the stranded-work digest's oldest Supervisor actions and unasked
   Director waits. Nudge stalls with exact evidence and process finished
   notices immediately. Polling watchers keep own automation question notices and
   verified own issue-mention activity in the log without exiting for them.
   Mention proof covers the latest visible message versions; unknown activity,
   including PR mentions, wakes normally. Other notices settle briefly into
   one exit per burst. `--once` still performs one immediate poll.
   Re-arm bounded watchers; do not duplicate live services.

Every check links the question queue and reports source errors and untrusted
record warnings alongside unknown coverage. Readers
rebuild current sources; missing proof stays unknown.
Assess changed Track proof at source; preserve holds, reviews and active work.
Compare workers with DIRECTION.md and escalate mismatches. Use the full board
at takeover/handoff; after compaction or a truncated/interrupted check, rebuild
full board/questions. Watermarks are not delivery records;
unpresented queue questions stay outstanding.
