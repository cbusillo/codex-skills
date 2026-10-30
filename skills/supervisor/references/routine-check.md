# Routine check

Use this for the existing ~23-minute check and immediate notices: established
readers and one named normal closeout within the run's scope need no reload.
Read the full skill at takeover; load owning skills/sections for rare events.
Keep absolute pointers to the private ledger, one recovery note, reader state,
handoff, question queue and latest landing receipt. Keep no copied rules,
decisions, statuses, board contents or narrative; open linked records for action.

1. Read the recovery note and reconcile pending local actions at their source.
2. Run `goal_board.py --owner OWNER --changes-state <board-state.json>`, then
   `compact_check.py --ledger <ledger.json> --state <session-state.json>` from
   the catalog's `skills/supervisor/scripts/`, using `uv run`. Add the existing
   `--capacity-state` when configured. Each session is read once; changed
   sessions and outstanding actions/errors print one JSON line each, followed
   by current coverage. These are candidates, never shutdown authority.
3. Read existing foreign/watch output, including live service logs, and its
   coverage gaps on every check; never wait for a process to exit to read it.
   Route each Client comment, including one posted by their agent, to the PR's
   owning session for an answer. Use the existing
   [comment radar](../../github-work-rollup/SKILL.md#external-comment-radar)
   to retain unanswered comments; a watch watermark proves collection only.
   Use
   `oq.py --owner OWNER --queue OWNER/direction#NUMBER --changes-state <question-state.json>`
   for the queue, and source anchors outside watcher coverage with their own
   state file. The reader trusts the configured automation identity by default;
   recorded decisions remove their matching questions. Open full new comments
   before acting. Register/present questions
   through the existing queue rules; changes mode never writes delivery records.
4. For one normal finished session, run:

   ```sh
   uv run skills/supervisor/scripts/session_closeout.py --ledger <ledger.json> --session-id <native-id> --supervisor-session <supervisor-id> --owner OWNER --evidence <private-session-evidence>
   ```

   Add `--handoff-url <actual-comment>` only when absent. Keep its receipt or
   first failed gate; inspect private details on failure and reuse its evidence.
   Never replay uncertain mutations with another directory. Nonempty input,
   prompts and interrupted sources use [shutdown/recovery](helpers.md#finished-session-shutdown-stages).
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

Every check links the question queue and reports source errors and untrusted
record warnings alongside unknown coverage. Readers
rebuild current sources; fingerprints suppress output, never attest unread proof.
Assess changed Track proof at source; preserve holds, reviews and active work.
Compare workers with DIRECTION.md and escalate mismatches. Use the full board
at takeover/handoff; after compaction or a truncated/interrupted check, rebuild
full board/questions before changes mode. Watermarks are not delivery records;
unpresented queue questions stay outstanding.
