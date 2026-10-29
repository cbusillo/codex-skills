# Routine check

Use this entry for the existing ~23-minute check and immediate worker notices.
An established check means the already scheduled readers and one named normal
closeout, within the existing run's scope. Load the full skill at takeover; for rare events load only their
owning skill and linked section. This entry covers the established check readers
and normal closeout, without reloading skills every scheduled turn.

Keep absolute pointers to the run's private ledger, one recovery note,
reader state files, handoff record, question queue and latest catalog landing
receipt. Keep no copied rules, decisions, statuses or board contents in context
or a private narrative. Read a linked record when it needs action.

1. Read the recovery note and reconcile pending local actions at their source.
2. Run `goal_board.py --owner OWNER --changes-state <board-state.json>`, then
   `compact_check.py --ledger <ledger.json> --state <session-state.json>` from
   the catalog's `skills/supervisor/scripts/`, using `uv run`. Add the existing
   `--capacity-state` when configured. Each session is read once; changed
   sessions and outstanding actions/errors print one JSON line each, followed
   by current coverage. These are candidates, never shutdown authority.
3. Read existing foreign/watch notices and their coverage gaps. Use
   `oq.py --owner OWNER --queue OWNER/direction#NUMBER --changes-state <question-state.json>`
   for the queue, and source anchors outside watcher coverage with their own
   state file. Open full new comments before acting. Register/present questions
   through the existing queue rules; changes mode never writes delivery records.
4. For one normal finished session, run `session_closeout.py --ledger <ledger.json>
   --session-id <native-id> --supervisor-session <supervisor-id> --owner OWNER
   --evidence <private-session-evidence>`. Supply `--handoff-url <actual-comment>`
   when the ledger has none; the helper verifies and saves it. It preserves every claim, handoff,
   verdict, process, input and terminal gate. Keep the receipt or first failed
   gate; open detailed private evidence only on failure. Never replay uncertain
   mutation steps with another evidence directory. Nonempty input, prompts and
   interrupted sources use [shutdown and recovery](helpers.md#finished-session-shutdown-stages).
5. Catch up merged catalog PRs against the latest verification link, including
   service-led landings. Use [catalog landing verification](helpers.md#catalog-landing-verification)
   for an unverified SHA; retain earlier missed-SHA gaps explicitly.
6. Staff through current `gh-plan.py next`, active child records and complete
   claim coverage under the overall Order and existing capacity/PR limits.
   Load the owning skill before a launch, plan mutation or train/landing action;
   those actions are outside this no-reload reader entry. Refill
   available capacity, handle stale waits and deferred-question conditions,
   and read the stranded-work digest's oldest Supervisor actions and unasked
   Director waits. Nudge stalls with exact evidence and process finished
   notices immediately. Re-arm bounded watchers; do not duplicate live services.

Every check links the Director's question queue and reports unknown coverage.
Changes readers rebuild current sources; fingerprints suppress output only.
An empty diff is not a new pass for an unread record. Assess changed Track proof
at its source; use the full goal board at takeover and handoff. Preserve real
holds, pending reviews, active work and uncertain operations.
Compare workers with DIRECTION.md and escalate mismatches. After compaction or
an interrupted/truncated check, rebuild the full board and question inventory
before continuing changes-only reads; their fingerprints are check watermarks,
not delivery acknowledgements. Unpresented queue questions stay outstanding.
