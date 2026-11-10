# Routine check

Use this for the existing ~30-minute check and immediate notices: established
readers and one named normal closeout within the run's scope need no reload.
Read the full skill at takeover; load owning skills/sections for rare events.
Keep absolute pointers to the private ledger and one recovery note,
handoff and question queue. Keep no copied rules,
decisions, statuses or narrative; open linked records for action.

For separate seats, use this table as the scheduled check prompt: read the
seat's first source, run the command below with its `--role`, then take its row
kinds within its lane. Repeat `--role` to hold multiple roles; omit it to keep
the existing single-Supervisor check unchanged. The role digest prints this
table's instructions. No running-seat inventory is kept or read.

| Seat | First read | Lane rule | Row kinds it takes |
| --- | --- | --- | --- |
| milestone | Current milestone blockers at source in DIRECTION order | Never fixes tools or closes records | Milestone work, lead coordination, Client waits and its Director questions |
| tooling | Current tool stops on the queue, then digest | Never redirects a milestone lead or edits a milestone issue status | Tool defects, refusals, Renovate, train, catalog verification, our helpers and services, and its Director questions |
| cleanup | Stale, ask-link, closed-dependency, finished-session and watcher rows, then issue trial | Never launches product work; fits none: cleanup sorts it | Record reconciliation, finished sessions, watcher recovery and its Director questions |

Address a seat with `Next actor: agent:supervisor-milestone`,
`agent:supervisor-tooling` or `agent:supervisor-cleanup`. Plain
`agent:supervisor` reaches every seat as UNROUTED; route it using the table.
Another seat's row becomes
visible to all after two check intervals without a new status disposition;
an unknown timestamp stays visible with unknown age. Claim or route at source
before acting, preserving existing claims and waits. The steps below apply
within the held roles; with no role they all apply.

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
   collected discussions and metadata within a run. On one host, other Supervisor
   sessions using the same `--owner`, queue, resolved ledger and identity
   reuse successful GraphQL query responses for five minutes. The digest says
   how many were reused and the oldest response's age; roles and `--full` are
   applied after every source is evaluated again. A repeat by the producing
   session still reads fresh, including commands without `--role`. Use `--fresh`
   to invalidate the requested responses and verify an action just taken.
   Failed requests stay live; a failed refresh leaves no earlier success reusable.
   All reports and errors are recomputed. Local state, routing preconditions and
   primary release/acceptance reads stay fresh. There is no whole-digest cache,
   daemon or seat registry; private query bodies use the existing reader cache
   and publisher beside the ledger, with cleanup at each check.
   Checks outside the reuse window still read GitHub; run one check before the
   other seats check within that window to get the saving. Open the source before
   claiming, merging, closing or acting on a candidate.
   The other readers run at the same time. The radar's repository
   comment scan keeps a watermark beside the ledger (`<ledger>-radar-scan.json`)
   and reads only comments changed since it; a missing, unreadable or
   other-identity watermark, or one that no longer covers the window, rereads
   the whole window.
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
   The milestone summary includes every Track and its source decision links,
   with holds and unread Tracks explicit. Partial global coverage remains an
   error even when individual Tracks were read.
   The default hides acknowledged QUESTION rows and counts them in one line;
   `--full` prints the complete list at takeover, handoff or recovery. Outstanding
   comments, unacknowledged questions, reviews, missing status, errors and load
   remain visible.
   Staffing uses the same global `next` as selection, including open issues
   outside Track trees and both agent families. Its 120-second read budget keeps
   omitted repositories, unread issues and other coverage gaps explicit.
   NEXT rows have no observed claim or wait and repeat until one appears; use the
   current ownership read. REVIEW rows need status or custody
   reconciliation. Generic agent roles are available work, subject to the ownership
   check. Follow the shared [recipient check](../../github/references/cli-reference.md#planning-next-work)
   when assigning an unclaimed worker or reconciling its custody. Leads launch
   within their assigned capacity under the [lead brief](brief-rules.md#milestone-lead-briefs).
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

   The helper selects the session's verified claim-release comment on the ledger's
   issue, journals it and keeps the existing handoff validators. Multiple qualifying
   releases need explicit selection. When no release qualifies, it finds a unique
   automation-authored handoff naming the exact native session, or uses the fresh
   complete claim check's
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
   those actions are outside this no-reload reader entry. Preview each
   provider with `account_choice.py` and read its `unused_at_pace`. When its
   `refillsUnusedPoints` is above a few points and `runsOutBeforeRefill` is
   false, start more productive work on that provider, or say in the check that
   no work is available to fill it. When `accountsSpendingUsageCredits` is above
   zero while points will refill unused, start new work on the first account in
   the published launch order (`account_choice.py --count N`) whose weekly and
   five-hour windows both have quota left, by name with `--account`, and note
   the sessions still running on the empty one. A refused preview or `null`
   value means the signal is unavailable; say so and continue. Refill
   available capacity, handle `STALE` rows and deferred-question conditions,
   and act on the oldest `SUPERVISOR_ACTION` rows and unasked Director waits.
   A `PRODUCTION_READ_REFUSED` row follows its handoff in
   [task scope](../../references/execution-scope.md).
   A `SUPERVISOR_ACTION` repeats as an error after one interval until its issue
   names another actor, a wait or a claim. A `NO_HANDOFF` row is an open
   non-draft PR whose checks pass by Ready's rule and whose current head has no
   Ready, Client handoff, stated hold or recorded wait. It repeats with its age
   until one appears; the session or lead holding it readies it, states the
   hold or makes it draft.
   Nudge stalls with exact evidence and process finished
   notices immediately. Polling watchers keep own automation question notices and
   verified own issue-mention activity in the log without exiting for them.
   Mention proof covers the latest visible message versions; unknown activity,
   including PR mentions, wakes normally. Other notices settle briefly into
   one exit per burst. `--once` still performs one immediate poll.
   Re-arm bounded watchers; do not duplicate live services.

Report a wait only with its primary read: the check's output, production's
commit, the registry line and the service that acts on it, the anchor's status
section, the clock. A label, a handoff sentence or a digest row is a claim.

Every check links the question queue and reports source errors and untrusted
record warnings alongside unknown coverage. Readers
rebuild current sources; missing proof stays unknown.
Assess changed Track proof at source; preserve holds, reviews and active work.
Compare workers with DIRECTION.md and escalate mismatches. Run the digest with `--full`
at takeover/handoff, after compaction or a truncated/interrupted check.
Watermarks are not delivery records;
unpresented queue questions stay outstanding.
