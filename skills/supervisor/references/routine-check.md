# Routine check

Use this for the existing ~23-minute check and immediate notices: established
readers and one named normal closeout within the run's scope need no reload.
Read the full skill at takeover; load owning skills/sections for rare events.
Keep absolute pointers to the private ledger, one recovery note, reader state,
handoff, question queue and latest landing receipt. Keep no copied rules,
decisions, statuses, board contents or narrative; open linked records for action.

1. Read the recovery note and reconcile pending local actions at their source.
2. Run the in-process digest from the catalog:

   ```sh
   uv run skills/supervisor/scripts/routine_check.py --owner OWNER --queue OWNER/direction#NUMBER --ledger <ledger.json> --foreign-log <watch.jsonl> --watch-state <watch-reader.json>
   ```

   Add `--capacity-state` when configured, `--source OWNER/REPO#NUMBER` for
   question anchors outside the queue, and `--radar-config` for existing private
   [comment-radar configuration](../../github-work-rollup/SKILL.md#external-comment-radar).
   Radar defaults to the owner's repositories; `--repo` narrows that scope and
   repeated `--thread` adds full-history reads. Keep Client-agent identities in
   the existing people index. The digest reads current board, sessions, questions,
   radar and live watch output without waiting for the watch process to exit.
   Unanswered comments, open questions, staffing requests and coverage errors
   repeat until settled at source. Only collected watch notices use fingerprints;
   they never clear radar attention. `STAFF NOW` on an open program issue is a
   staffing candidate, not launch authority; verify its linked items and ownership.
   Route Client and Client-agent comments to the owning worker for an answer.
   Open full source comments before acting; register/present questions through
   the existing queue rules. The digest never writes delivery records or closes sessions.
3. For one normal finished session, run:

   ```sh
   uv run skills/supervisor/scripts/session_closeout.py --ledger <ledger.json> --session-id <native-id> --supervisor-session <supervisor-id> --owner OWNER --evidence <private-session-evidence>
   ```

   The helper finds the automation-authored handoff naming the exact native
   session on the ledger's issue, journals it and stops on ambiguity. Legacy
   ledgers without an issue pointer can supply `--handoff-url <actual-comment>`.
   Keep its receipt or
   first failed gate; inspect private details on failure and reuse its evidence.
   Never replay uncertain mutations with another directory. Nonempty input,
   prompts and interrupted sources use [shutdown/recovery](helpers.md#finished-session-shutdown-stages).
4. Catch up merged catalog PRs against the latest verification link, including
   service-led landings. Use [catalog landing verification](helpers.md#catalog-landing-verification)
   for an unverified SHA; retain earlier missed-SHA gaps explicitly.
5. Staff through current `gh-plan.py next`, active child records and complete
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
full board/questions. Watermarks are not delivery records;
unpresented queue questions stay outstanding.
