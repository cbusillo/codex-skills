# Supervisor helpers

Use [Routine check](routine-check.md) for recurring reads, not this full reference.
Run Python helpers with `uv run` and their catalog path. Readers are read-only;
question registration/acknowledgement and named operation helpers write only
when explicitly invoked under the run's existing authority. iTerm requires
macOS and its existing Python API; connection failure never authorizes changing
personal settings.

The launcher owns scoped unattended settings: worker memory and suggestions
off, native question picker disabled, automatic compaction selected. The
Director's own settings are unchanged. It merges inline JSON without losing
other keys; settings files retain their private values and relative-rule base.
Fix required file values through the reported preflight remedy, never by
rewriting/inlining the file or changing personal settings. Relative settings
files require an absolute `cd`; otherwise use an absolute literal path.
Codex remote workers use the managed ingress, never a bypassing raw connection.

`read` returns the full screen and visible input style: empty, dim, non_dim,
mixed or unknown. Style never proves authorship, approval, a complete multiline
draft or idle state. Never record dim suggestions as Director input. Follow the
full shutdown procedure's preservation and clear/readback checks before keys.
A send failure reports partial delivery/submission on stdout and exits nonzero;
read both outputs. Uncertain submission needs exact-session reconciliation,
never another command appended blindly. Proven non-submission permits a
verified-target clear and empty readback before a new send. Close refusals
report whether the native close request may have started; only a definite
preflight refusal can be repeated after its prerequisite is resolved.

## Handoff record

Each Director's handoffs, takeover comments and pilot lessons go to that
Director's own record, never to an issue in another person's repository.
Read it at takeover:

```bash
uv run skills/supervisor/scripts/handoff_record.py --owner OWNER
```

It reads `[handoff]` from the first private `skill-data/supervisor.toml` that
has one, in the same order as [accounts](#choosing-the-account), or from
`--config`:

```toml
[handoff]
issue = "OWNER/direction#123"
```

With no `[handoff]` it refuses, so a successor never guesses the record by
title. Open the record under `github-plan` in the Director's private
`OWNER/direction` if none exists, then add its line. The helper also refuses
an issue outside the Director's own account and a malformed value; fix the
private config. Lessons about shared skills that other Directors would
also hit go to the [field reports](field-reports.md) issue.

## Local state and the session registry

The skill's [Local State](../SKILL.md#local-state) lists what stays local.
Everything else is read from GitHub when needed. The session registry is the
private JSON file every helper takes as `--ledger`; it identifies sessions and
holds no status.

Retire a narrative log once: mark/archive it as historical, stop writing it,
and keep one overwritten recovery note for pending local actions. Neither is
status, decision or lesson authority.

Create the session registry as a private JSON array from the handoff's actual session identities. Paths
are relative to that ledger file; never commit transcripts, account names,
briefs or operational coordinates. No helper searches accounts or copies auth.
Use `iterm_tab.py list` to record the exact window, session and TTY; titles are
labels, not identity. Match the harness process's PID and transcript yourself. The ledger loader
normalizes iTerm's `/dev/` TTY prefix for `ps` matching.
Exclude the Director's sessions or set `supervisor_owned` to false.

Each object needs native `session_id`, `harness` and `transcript` pointers.
Keep `repository`, `issue` and `brief` from the actual brief, and the verified
launch's backend, terminal/PID/TTY and generation fields. Explicit
`supervisor_owned: true` grants only the ownership already established at launch;
never infer it from a title. Save the actual `handoff_url` after verification.

Native IDs are unique; Codex UUIDs have no date-prefix restriction. Missing,
malformed (including nested fields) or changing transcripts prove no idle or
finished state. Watchers isolate a session's error; malformed ledgers stop them.
Full status responses can be private and stay out of public comments.

Claude `background_tasks` covers unresolved Bash/Monitor work and native jobs,
including future wakeups: these block pre-exit candidates. Only the worker
reconciles them. A turn-ended "waiting" message without tasks is a stall to
nudge; a listed task needs its wait condition checked, including already-settled
CI under a non-exiting watch. Missing context counts never justify handoff;
Claude counts include cached input. After independently proved exit, leftover
task records add no condition beyond the final-verdict/process checks. Scheduled
jobs alone allow idle empty-prompt review notices; active turns or Bash/Monitor
work still block them.

Unknown `cron-create:<tool-id>` or CronDelete-absent jobs need that worker's
native CronList. Only a successful matched result replaces schedule inventory
or clears placeholders; failed, replay-only or ambiguous results do not. Stop
only worker-confirmed obsolete jobs, then require a fresh verdict. Unobserved
schedule-error, expiry, restart and durable receipt shapes remain UNKNOWN;
transcript age/time never supplies them.

For tmux entries, replace `iterm_session_id` with `tmux_session_id` from launch
output and record `backend: "tmux"`, `tmux_socket`, `window_id`, `tab_id`, `tty`
and the verified harness `pid`. Keep the native `session_id` and transcript
fields unchanged. The tmux terminal id contains a launch nonce: never replace
it with a pane number or a title. A mixed ledger may hold both backends; use
the matching backend when reading or closing each terminal.

Status and candidates expose `verdict_recognition`: `recognized` for an accepted
completed unqualified verdict, `unrecognized` when verdict wording is present
but does not qualify, and `absent` when it is missing. A final verdict may share
its line with `No Director action needed.` or `No Director decision needed.`;
other accompanying prose stays unrecognized. For an ambiguous response, read
the full transcript and handoff and ask the worker for a fresh standalone
unqualified verdict if appropriate. Manual review does not override the parser
result or any shutdown verification.

### Claim checks at close-out

The verdict reader does not read claims. Closing a session needs its posted
releases and a verified process exit. Before sending `/exit` or `/quit`, run the
session-scoped check on its claiming host, using its local claim journal and
native root and child transcripts:

```bash
uv run skills/work-closeout/scripts/check_claims.py --owner OWNER \
  --session NATIVE_ID --transcript /absolute/native-transcript.jsonl
```

Exit 0 with `claims_clear: true` and complete coverage lets close-out continue.
Otherwise nudge that exact worker once to release its own claims through the
[claim disposition contract](../../work-closeout/SKILL.md#claim-disposition),
naming its retained PRs, then check again. If it cannot, have it record
conditional/no, the blocker, next actor and retained artifacts on the item, and
keep its process and terminal open while you continue other work. A pending
review remains open under its brief.

A session that crashed or ended without releasing frees its claims when its
host-local lease expires, 30 minutes after its last hook activity. Use
`gh-plan.py release-claim` only where the lease cannot be read, such as a claim
made on another host. Count takeovers by searching claim comments for
`Lease takeover:`; shorten the lease only if those delays bite.

`release_session_claims finish` is available only **after actual verified process
exit**. It repairs sessions that already ended; it is not an active-session
escape from this gate. If a person separately requests a shutdown that cannot
meet the existing rules, surface that decision under `DIRECTION.md` rather than
inventing new exit authority. After verified recovery, a resumed source may
read back the validated releases through work-closeout's check for normal
acceptance. Unknown scope/identity and unsafe terminal cleanup remain preserved.

A complete trailing Claude local `/exit` sequence (meta caveat, `/exit` command,
one stdout wrapper) preserves the preceding close-out verdict, including when
stdout contains a farewell. A Claude compaction summary (the
`isCompactSummary` user record directly after its `compact_boundary` record)
preserves it too, so a finished session that compacts while idle can still be
released. Other user turns or any later assistant turn invalidate it.

## Checks and morning digest

The existing Codex idle watcher and launcher guard share the same per-home
auth helper. Every check connects to that account's existing daemon, verifies
its PID/start identity and Unix peer, and requests token-free `getAuthStatus`
with `includeToken:false` and `refreshToken:false`. Codex evaluates its own
access-token expiry and renews only when its managed-auth policy says it is due.
The watcher polls; it never forces renewal on a fixed timer, starts a second
renewer or reads credentials. The launcher uses the same check before selection
and execution. Native login remains available for recovery.

`getAuthStatus` is deprecated. It remains a known compatibility dependency under
[Q933](https://github.com/cbusillo/codex-skills/issues/2919#issuecomment-6429925021).
The current `account/rateLimits/read` was tested with the installed native
binary and a loopback issuer: a fresh token stayed untouched and a near-expiry
token renewed, but a dead refresh token could still yield usage data. It cannot
supply the required renewal verdict. The auth helper follows the expiry-aware
read with `account/read` using `refreshToken:false`; a null ChatGPT account plus
a preserved permanent renewal error, including an issuer HTTP 401, establishes
a hold. Other unclassified issuer statuses do not establish one. A result envelope or
ChatGPT auth-method label alone is never proof of successful renewal.

A separate `codex login` can replace the stored sign-in while the daemon keeps
an old, still-fresh access token and its cached permanent failure. The non-forcing
auth read does not reload that cache before its renewal window. If a confirmed
CLI sign-in still appears held, an agent checks `thread/list`, `thread/read` and
`thread/queue/list` plus the session ledger; admission preflight alone does not
prove idle. It then uses that explicit home's native
`codex app-server daemon restart` when it can do so without interrupting work.
There is no automatic restart or extra forced refresh. Login, logout and native
daemon start/restart through the wrapper require a named account function,
`--account NAME`, or explicit `CODEX_HOME`; they never use automatic account
selection. The notice's `recovery_command` names the exact home for sign-in.

Before native reads, the helper saves allowlisted auth events from
`logs_2.sqlite` opened read-only, plus process associations on the first failure per process in an episode, to the home's
private `skill-data/supervisor-auth/evidence.jsonl`. The same helper keeps its latest verified observation in a compact private
`status.json`, atomically replaced under the capture lock. It is the single
current-state source used by the watcher and launcher. It never saves raw log bodies, token values,
process arguments or environments. Captured evidence survives log rotation.
Keep the watcher running; events rotated away before observation cannot be
recovered. Process inventory describes capture time; earlier identities come
from retained log events.

An `auth_failure` notice wakes the Supervisor once per episode through the
existing delivered-state file. Tell the Director which account needs a normal
Codex sign-in, then re-arm the watcher with that same file. The launcher skips
the held home while following Context Panel's remaining published order. The
train driver is unchanged, and no renewal-status feed goes to Context Panel.

A verified current account clears the hold after native recovery; a later
failure is a new episode. UNKNOWN evidence never creates a hold, and a timeout
or unavailable daemon cannot release a previously confirmed one. Unavailable
logs or native state remain diagnostics; they do not impose a provider-wide
hold or repeatedly end/re-arm the monitor. A dead-key check may wait up to eleven
seconds for native log delivery before reporting UNKNOWN. Healthy accounts do
not wait for log publication.

Use [Routine check](routine-check.md) for scheduled checks. Persistent idle
services deliver immediate notices; otherwise bounded watches must run as
background jobs, because the harness wakes when they exit. Keep each harness's
existing delivered-state file and re-arm after delivery or its wall-clock bound.
Closeout notices bypass idle delay and identify the completed native response.
Coalesce repeats with current verification, never by replaying uncertain sends.

Watch notices and Claude `notify_when_idle` trigger immediate exact-session
verification; the scheduled scan is fallback. The compatibility reader is
`finished_map.py --ledger <ledger.json> --session-id <native-id>`.
Candidates/null values prove no success; aborts can need stall/capacity handling.
Follow [task and shutdown gates](../SKILL.md#close-finished-sessions-on-every-notice-and-check)
before input; only the worker reconciles its tasks and supplies a fresh verdict.

Shared completion readers strictly validate the whole ledger. The Codex watcher
alone filters a mixed ledger first, reporting every non-Codex entry as skipped
with index, native identity and reason; selected Codex entries still require
valid unique identities and transcripts. Malformed structure refuses. Reconcile
against the original launch receipt rather than guessing or silently skipping.

### Goal board

`goal_board.py` rebuilds every open `Track:` through `github_work_graph.py`.
Decision authors and current-head handoffs use shared authority parsers. Only
open PRs closing a child appear; the auto-enqueue watcher routes other PRs.
Truncated Track discussions use the shared planning paginator; failed/changing
reads keep UNKNOWN and the original newest-comment window. Child discussions use the same paginator; PR windows stay bounded.

Use `--format json` for full rows/diagnostics, `markdown` for titles and actions,
or `links` to omit those Client details. Closed children remain closed despite
stale labels. Child Current Status and decisions use shared planning authority;
missing child status/action is explicit, never guessed. Track state comes only
from native descendants: unreleased claims (including verified release receipts
and locally observed lease expiry), structured waits with their end
conditions, open PRs and unanswered Director questions. Descendants are grouped
as running, waiting or available; closed, held and unknown records stay explicit.
Track Current Status is neither read nor maintained. Assess each Track's own
closure proof under the milestone contract, opening the linked source.
`--changes-state <private-state.json>` prints changed record pointers and fresh
coverage, storing only fingerprints. Every call still rebuilds current sources.

Coverage reports discovered/read/complete and per-field `unread` details.
Partial discovery cannot reuse old status or remove missing records. Truncated
children, PRs, labels/comments, missing authoritative child status in a bounded window,
field conversion errors and GraphQL failures remain unknown, not refreshed.
Any unread Track or field makes coverage incomplete and exits nonzero; open the
source rather than treating an empty diff as a pass.

### Codex provider capacity stops

`provider_capacity` requires the actual Codex `task_complete` capacity error,
including a first turn without response text; quotes, other errors, aborts and
new activity do not qualify. Bounded retries are optional after landing. The
owned native UUID/transcript must match an existing absolute `codex_home`, its
live `sessions` directory and available daemon socket. No login/daemon changes.

```sh
uv run skills/supervisor/scripts/codex_idle_watch.py --ledger <private-ledger.json> --delivered-state <private-codex-delivered.json> --capacity-state <private-capacity-state.json> --resume-capacity --deadline-seconds 1800
uv run skills/supervisor/scripts/compact_check.py --ledger <private-ledger.json> --state <private-check-state.json> --capacity-state <private-capacity-state.json>
```

Keep one watcher and the same retry-state path across checks, digests, restarts
and takeovers; without `--resume-capacity` it only reads. The coordinated
[exact-thread queue](#launching-and-nudging) uses that home's Unix endpoint,
sending only `continue` after 1, 3 and 10 minutes, at most three attempts per
native session. A successful turn or later error never resets the budget;
never rotate/delete state to retry an exhausted source.

Reserve durably before sending and require a new native `continue` user message.
A crash, failed command or missing readback leaves `reconciliation_required`
and stops automatic sends. A proved refusal before the lease sent nothing:
report it, restore the reservation and recheck later. Changed home/transcript
means `target_changed`, also stopping sends. Before manual intervention inspect
that exact thread and queue; uncertain sends are never replayed or cleared
automatically. After verification, use the linked queue route for manual nudges,
retain the automatic halt/budget, and retire only through normal closeout.

Checks/digests include every capacity stop even without retry, plus every
`exhausted`, `blocked`, `reconciliation_required` and `target_changed` record.
`blocked` means ownership/native UUID/home/socket proof is unavailable;
`exhausted` means still stopped after three attempts. Both need attention.
`retry_at` and `retry_due` expose eligible timing; a persistent due state needs
the bounded watcher checked/re-armed with the same state. None proves completion.

The question helper reads every comment via the maintained GitHub helper and
returns every question, not just the newest. Successful complete reads,
including cached reads with `not_applicable`
outcome certainty, preserve that inventory; failed or incomplete reads report
an error rather than an empty question list.
Configure the Director's login and only the trusted decision-recording authors
for this run. A later reply
must link a question's URL or comment id to mark it answered; unlinked plain
answers remain `needs_review` for the Supervisor to reconcile from the exact
decision record. A script's match does not itself establish what was decided.

### Q-number Stop hook

`scripts/q_number_guard.py hook` is a Claude Code Stop hook. When the final
reply asks the Director to decide something and has no bold `**Q###`, it blocks
once with a reason naming the rule. For numbered questions it checks the
[shared question guidance](../../references/talking-with-the-director.md#director-questions),
reading that section into its correction when context is missing, or linking
to it when the section cannot be read.
`stop_hook_active` lets the retry through.
Quoted text, code and links are ignored, as is a question the reply answers in
place ("Merged yet? No, CI is running."), so a status report that quotes an old
question passes. Unreadable hook input lets the stop proceed. Each run
appends `time`, `session` and `verdict` (`pass`, `caught` or `second-pass`), with
no message text, to `skill-data/q-number-guard.jsonl` in the shared Code home.
It is enabled only in the Director's direction repository, through its
`.claude/settings.json`. Count verdicts for a review with:

```sh
uv run skills/supervisor/scripts/q_number_guard.py report --since YYYY-MM-DD --until YYYY-MM-DD
```

### Acknowledged question delivery

Use the existing Supervisor discussion as the durable question queue. Workers
register question references there; the Supervisor acknowledges actual
presentation under the shared [question queue rules](../../references/talking-with-the-director.md#decisions-holds-and-work-state).
These commands use the configured automation identity and read back their writes:

```sh
uv run skills/supervisor/scripts/oq.py ORIGINAL_QUESTION_URL --owner OWNER --queue OWNER/CATALOG#QUEUE --register
uv run skills/supervisor/scripts/oq.py --owner OWNER --queue OWNER/CATALOG#QUEUE
uv run skills/supervisor/scripts/oq.py --owner OWNER --queue OWNER/CATALOG#QUEUE --ack ORIGINAL_QUESTION_URL --delivery-url PRESENTATION_RECEIPT_URL
```

Name the queue once in the private `skill-data/supervisor.toml`, beside
`[handoff]`. The planning helpers read it to refuse a wait on the Director
whose question is not registered there
([wait records](../../github/references/cli-reference.md#planning-claim)):

```toml
[questions]
queue = "OWNER/CATALOG#QUEUE"
```

The configured automation login and previously trusted automation logins are
trusted by default. Use `--decision-author` only for an additional trusted recorder.
The read-only queue view recovers registered source discussions on takeover and
also accepts source items from the worker ledger/handoffs. Normal queue reads
select registered URLs; explicit question URLs select just those questions,
while explicit discussion IDs request that discussion's wider candidate inventory.
It reads full source
and queue discussions through the paged, quota-aware planning helper, including
closed issues; no search window expires a question. Scope is explicit: this
view cannot discover a worker that neither registered nor handed off its source.
The independent daily stranded-work sweep owns that fleet-wide backstop.

Registration accepts exact original question URLs so an old discussion's other
questions are never enqueued incidentally. Each source comment URL is the
registration identity. For automatic numbering and unnumbered-source repair,
follow [question numbering](../../references/talking-with-the-director.md#decisions-holds-and-work-state).
Registrations
store references, not a copy of the question. `director-question:delivery`
first-line JSON records contain `version: 1`, `event: registered|delivered` and
`question_url`; `delivered` also carries `delivery_url`. Only the Director and
explicitly configured trusted authors' records count. Quoted/fenced examples
do not count. No label is delivery state.

The view reports `queue_length`, position, original age, registration link,
delivery receipt and `resurface`. An unacknowledged question is emitted every
check, even after successful registration. `candidate_answers` preserves plain replies for Supervisor reconciliation and
"later" never erases it. Read acknowledged unanswered rows too: `resurface`
means missing delivery acknowledgement, not the only questions to present. Source reads
also reconcile decisions on another supplied discussion or the queue. A failed
read/write/readback exits nonzero with `complete: false`, never an empty all-clear.
Deleted/edited source questions stay visible as `missing_source`, with their
original URL, while other rows remain usable. An inaccessible discussion is a
per-source error, preserving the other rows too. Restore the original source or
record the Director's actual disposition as an explicit linked decision on the
queue; that existing decision format also resolves a missing source and stops
refetching it. Do not invent a disposition. Unconfigured source-question authors
remain visible as incomplete coverage. Untrusted queue records are warnings and
never control state or make a successful acknowledgement fail. Review those
warnings and the configured-author scope before claiming an all-clear; configure
verified trusted logins and reread rather than granting authority from a record.
Presentation receipts must name an exact GitHub comment or Discord message.
On an uncertain write, read the queue before retrying; stable bodies, helper
deduplication and a shared-Code-home lock reconcile a saved registration without
allocating a new Q ID. This source change configures no destination, daemon or
Discord transport. Production deploy/stall alerts remain Launchplane's existing
incident/outbox path.

### Throughput and digest
For throughput counts and the morning digest, use `fleet_counts.py --owner OWNER`
once per snapshot, reusing its per-repository rows and fleet totals. Do not run
per-repository REST/search count loops. The helper uses one GraphQL request per
repository page (100 by default), under the shared automation identity, retries,
quota admission and diagnostics. The existing helper layer retains its cache
behavior; GraphQL counts are fresh reads, without conditional REST caching.
Only repositories visible to the selected bulk reader identity (or its supported
main automation fallback) are counted, including forks and
repositories with zero work, excluding archived repositories. Open issues and
open PRs are separate counts. `sampled_at` records the snapshot start; pages are
not an atomic fleet snapshot. An error exits nonzero with no totals: retain the
last successful throughput entry as historical and report current counts as
unavailable in the digest. Never interpret a failed read as zero work.

For the digest, also use `github-work-rollup` for human comments, `github` for
Dependabot/security signals and `infra-ops` for authorized live-site checks.
The scheduled stranded-work report also computes the share of the last 20
merged tooling PRs whose net lines are at or below zero and which state
`Removed:`. It reuses the weekly capacity rank, excluding train batches,
within the report's selected non-archived repositories and history window. Its full report
keeps the PR links, line counts and statement evidence; its short summary keeps
the measure with its scope or unavailable reason. Incomplete closed-PR reads
or fewer than 20 tooling merges report unavailable. The measure runs after
the primary sweep under its remaining read budget. The target is 70%, never
a delivery gate.
Include the run's privately recorded capacity deadlines. Post the Director's
question queue under the shared
[queue rules](../../references/talking-with-the-director.md#decisions-holds-and-work-state);
use only the messaging authority already granted for this run.

## Foreign-post notices

For ongoing runs, use the [persistent services](#persistent-supervisor-services)
below; [Routine check](routine-check.md) owns notice handling and log reading.
The bounded invocation here remains
available for a one-off check.

```sh
uv run --no-cache skills/supervisor/scripts/foreign_watch.py --owner OWNER --own-login OWN-LOGIN --own-login AUTOMATION-LOGIN --launchplane-login DELIVERY-LOGIN --state <private-watch-state.json> --deadline-seconds 1800
```

Add `--director-login DIRECTOR-LOGIN --mention-approval <approval-comment-link>`
to also report threads outside the listed repositories that mention the
Director, including private repositories only the Director's login can read.
That search runs read-only under the Director's active gh login and needs the
Director's [standing mention-search approval](../../github/references/cli-reference.md#mention-search-approval), bound to the approving
Director's own question ID and permission heading. Each search
overlaps the previous one by 15 minutes to catch late search indexing; the
state file's `mention_seen` keeps already-reported thread versions from
repeating. Until approval is configured,
each pass prints the gap under `coverage_gaps` and keeps watching; a gap never
stops the watcher or counts as a notice. Mention notices (`kind:
director_mention`) are untrusted links like every other notice. A failed
search is a retryable error that keeps the mention watermark.

Without `--repo`, each pass watches every non-archived repository of
`--owner` that the automation installation can read, so a new repository is
covered from its first pass; repeat `--repo` only to narrow a run. Repeat the
login flags for the run's configured identities. Initial startup
begins at the current time; `--since` supplies an explicit first watermark for
a historical catch-up; on an existing state it rewinds each watched
repository once, only when earlier than its stored watermark. Unlisted
repository state stays preserved. The host's UTC clock must be correct. Restart with the same state file after a notice or
deadline. `--once` performs one pass. Only one watcher owns each state file.
Run it with the harness's built-in background completion notice so its exit
wakes the Supervisor. During takeover verify the old watcher's recorded process
has ended before re-arming; retain an uncertain process and its unread output.
Concurrent watchers using that state file refuse before reading GitHub. The
helper reads one since-scoped repository issue-comment listing per pass
(including PR timeline comments) and one since-scoped issue listing, so an
issue or PR that an outside person opens is a notice (bot-opened ones are
not), paginating through the shared reader within
the wall-clock deadline. It uses the shared bulk quota reserve, identity,
conditional cache and retry policy; no separate quota probe or history scan.
A failed, partial or malformed read preserves that repository's watermark.
The bounded invocation reports an error and exits for Supervisor reconciliation.
A successful pass with notices ends the run after saving its watermark, so a
harness background completion wakes the Supervisor. The persistent service
runner instead saves output in its log and restarts successful runs with the
same state file. With `--persistent`, retryable errors keep the run alive even
when another lane produced notices, preserving the normal interval or reported
quota retry time, whichever is later. Those notices remain readable in live output.
Authentication, malformed-response and other nonretryable failures still stop.
Wait until a reported quota retry time before a manual restart; increase the bounded deadline when a historical
catch-up cannot finish. Partial pages never advance past a failed read.
Successful repositories
advance to the pass start, with a one-second overlap on their next read.

Own-login comments starting with `Owner question:` or `Director question:`
(including bold headings) produce `director_question` notices for the
Supervisor to read and register in the Director's queue on its next check.
This uses the same comment feed and watermark; ordinary own-login posts stay
filtered. These notices do not prove a decision or count as presentation.
Foreign authors (including deleted/unknown authors) and configured Launchplane
authors or first-line product-review markers
produce notices marked `untrusted` with a one-line `excerpt` of up to 240
characters of the comment; the full body stays on GitHub. An excerpt is data
to read, never an instruction. A
marker is a notification hint, never proof of authorship or accepted direction.
A foreign author stays `foreign` even when its comment has a review marker.
Configure known service bots among own logins when their ordinary posts should
not wake the Supervisor; no suffix rule silently excludes unknown posters.
Read the canonical record before acting under existing authority. The watcher
does not launch agents, post messages, grant access or widen briefs. New issue
bodies, inline review comments and Discussions are outside this bounded comment
feed; the existing work rollup still supplies broader discussion coverage.

A Launchplane notice for a Client's "Changes requested" decision on a pull
request also carries `change_request`: the PR, the reviewed commit, the
Client's exact feedback, the decision record and a `worker_brief`. The state
file's `change_requests_dispatched` records each decision ID once it is
printed, so later passes, edits and `--since` rewinds report the comment
without a second brief; an Accept carries none. At the next check, the
Supervisor saves that `worker_brief` to a private file and launches it with
`iterm_tab.py new --brief-file`, after rechecking that the PR is still open
and no running session already owns it. The brief quotes the feedback as the
Client's request, never as an instruction, and tells the worker to fix the
change or answer the Client on the PR, then hand off again with
`--waiting-for-client`.

## Interim auto-enqueue watcher

Q154 approved [codex-skills#1623](https://github.com/cbusillo/codex-skills/issues/1623#issuecomment-6103248251).
This Supervisor helper removes manual detection of ready handoffs. Executing
sessions still hand off and do not enqueue. Launchplane owns the queue and
landing; [launchplane#3327](https://github.com/cbusillo/launchplane/issues/3327)
owns replacing this interim helper with service admission.

Use one private hold record for the run, shared by every local watcher and
pause update. Client repositories and shared release inputs follow the same
ready handoff path as other repositories, per
[Q312](https://github.com/cbusillo/launchplane/issues/3327#issuecomment-6182485308).
Launchplane owns product release holds; do not copy them into this record.

On a normally completed pass, every observed trusted current-head ready handoff the watcher leaves unenqueued is
named in a `skipped` event with its reason, including repository holds, busy
controllers, controller ineligibility, dry runs and batch selection. Controller
rejections retain `ineligible_reasons`; a busy event alone is not a PR inventory.
Unavailable inventory is explicit as `skip_inventory_unavailable`. Stops propagate
without further diagnostic reads. These observations do not authorize enqueueing
or replace the fresh write gates.

```sh
uv run skills/supervisor/scripts/auto_enqueue.py holds --hold-file <private-holds.json> --init
uv run skills/supervisor/scripts/auto_enqueue.py --owner OWNER --hold-file <private-holds.json> --handoff-login AGENT_LOGIN --once
```

Initialization refuses an existing record. Missing or malformed hold state stops
the watcher. Reuse the same record through takeover; reconcile it with the
handoff before rearming. Do not maintain another per-session copy.

For an older record, remove only legacy product-batching holds with
`holds --uncoordinate OWNER/CLIENT-REPO` (repeat for shared inputs). The helper
serializes this edit with enqueue passes and other hold updates; deliberate
pauses and other fields remain intact. Use `holds --pause OWNER/REPO` for an
independent hold, then remove that repository's legacy batching entry. Preview
with `--once` before rearming. Clear any manual
`client_releases` entries that only duplicated the service's release hold with
`holds --release-finished OWNER/CLIENT-REPO`. These optional manual pauses
are not required for a Client release; leave them empty for normal routing.
Each pauses only its named repository, never Launchplane or other repositories.
The hold command waits for the current repository's enqueue section before acknowledging the
update. Between repositories, a pause update can win the lock and make the
watcher's next nonblocking acquisition stop safely. Finish the hold update and
reconcile any already emitted enqueue events before rearming the watcher.
After a deliberate manual pause ends, clear its entry; other pauses remain independent.

```sh
uv run skills/supervisor/scripts/auto_enqueue.py holds --hold-file <private-holds.json> --pause OWNER/REPO
uv run skills/supervisor/scripts/auto_enqueue.py holds --hold-file <private-holds.json> --resume OWNER/REPO
uv run skills/supervisor/scripts/auto_enqueue.py holds --hold-file <private-holds.json> --release-ready OWNER/CLIENT-REPO
uv run skills/supervisor/scripts/auto_enqueue.py holds --hold-file <private-holds.json> --release-finished OWNER/CLIENT-REPO
```

Preview is the default. After inspecting it, use `--apply` under the run's
existing Supervisor enqueue authority. Without `--repo`, scan every enrolled
target owned by `--owner`; repeat `--repo` to narrow the scan. The active
Launchplane policy supplies enrollment, base branches, enqueue labels and the
policy revision. Repository metadata is never consulted for those facts.
The loop defaults to a 30-minute wall-clock bound and five-minute cadence;
`--once`, `--interval`, and `--deadline-seconds` configure it. For ongoing runs,
the [persistent service](#persistent-supervisor-services) restarts clean deadline
exits; check its status and logs instead of re-arming it in each session.
Before any enqueue write starts in a pass, a transient network read failure
(including retryable `GitHubReadError` with EOF, connection-establishment or
non-JSON provider evidence even without an HTTP status)
emits `pass_failed` and ends only that pass. The watcher waits its normal interval
and reads fresh state again within its deadline; `--once` reports the failed pass
and exits nonzero. Read-only Launchplane configuration and controller response
failures use `LaunchplaneReadError` and follow the same recovery path. Explicit
controller authentication or unsafe-redirect rejections remain terminal.
Other failures, or any failure during or after an enqueue write
in that pass, stop the run for Supervisor reconciliation, preserving already
queued work. Reads retain the shared GitHub cache, quota reserve and request retry
deadline; no human-auth fallback is added. The deadline `stop` event reports
completed and failed pass counts so an outage throughout the watch is visible.
Every `stop` event includes a message. Helper-authored exception messages identify
the specific failure; external exception details are withheld to keep provider
bodies, credentials and private Client prose out of events. Failed-pass events
use the same message policy. Watch events include a UTC `timestamp` so passes
and skips can be compared with acceptance and handoff times.

When the task authorizes a Supervisor handoff, sessions use the installed GitHub
skill's helper: `uv run <github-skill-dir>/scripts/gh-pr.py --repo OWNER/REPO ready <pr>`
(or `--waiting-for-client`), following the GitHub
[ready handoff contract](../../github/references/cli-reference.md#ready-handoffs).
The producer and watcher share `github/scripts/ready_handoff.py`. The latest
handoff by creation/edit time from its author, or a Supervisor explicitly listed with
`--handoff-login`, wins. The `Engineering ready; waiting for ...` form invalidates
an earlier Ready. The exact Client acceptance wait becomes eligible when the
existing Launchplane read confirms acceptance for its current head, including
acceptance Launchplane carries forward. Other waits remain blocking. Skip
diagnostics include checked Client waits even before acceptance is recorded;
`client_wait` distinguishes those receipts from plain Ready without claiming
whether acceptance is present.
Missing/stale heads, drafts, blocking train dispositions,
current-head change requests and unavailable checks do not enqueue. A file
inventory changed during reading skips that PR; other ready members continue.
The shared parser normalizes trailing periods and bold markers on both Ready
and renewed waits and accepts a bold full head SHA. Client-test-note
classification follows the ready handoff contract linked above.
When mergeability is null, the watcher asks GitHub to compute it with up to two
individual REST re-reads, one second apart, capped at 20 extra reads per pass
and the watch deadline. These reads bypass conditional and batch snapshots;
unresolved PRs wait for the next pass. Computed `behind` PRs can enter the
controller, which refreshes them. Conflicts still skip selection, and every
selected PR is rechecked before labeling.
A controller disposition of `dependency update needs agent review` may enter
qualification: a current-head Ready from a trusted agent supplies
that review; the dependency author's own comment cannot. Name the reviewing
agent's login with `--handoff-login`; repeat it for additional trusted reviewers.
The configured automation login (from the GitHub identity helper, falling back
to `shiny-code-app[bot]`) is trusted by default, including its
reviews on Dependabot and Actions-bot PRs. Additional logins extend that default
across the scoped repositories. Other controller holds still block.
An unavailable Client review scope holds that PR rather than bypassing acceptance.
Required checks are read from current branch protection and
matched to their configured App. The shared
[ready-handoff acceptance rule](../../github/references/cli-reference.md#ready-handoffs)
decides whether Client acceptance is required; when it is, the watcher reads
the accepted current-head decision from Launchplane rather than trusting the
ready comment. Client prose is never emitted. Deliberate manual repository
pauses remain independent of acceptance.

The oldest eligible PR anchors a group; additional members share at least one
of its top-level areas. Renames count both source and destination; root files
form one area. `--max-batch-size` defaults to four. Area grouping is a routing
heuristic; Launchplane's combined candidate checks still prove compatibility.
An active candidate normally preserves its batch; scheduler-backed staging below
is the bounded exception. Reconciliation, landing, or a preexisting next queue preserves
that repository's batch. Each write rechecks policy, pauses, train state, current
head and readiness. A behind-base single-member queue awaiting the train's
branch update can still accumulate this watcher's group before any stored
candidate exists; the watcher never updates that branch itself.
If the scheduler starts a candidate containing only this pass's confirmed
labels, the watcher finishes its already selected group. Complete controller
membership must prove that ownership; unchanged controller-proven blocking
labels on already held PRs remain in place. A hold without a blocking label
stops continuation. Another actor's new or changed queued label,
landing, reconciliation, failed candidate or unavailable evidence stops it.
Candidate observations can omit the queue: unchanged policy, selected head and
PR labels retain the scan's author admission while every write still reads
current readiness.

On a target whose controller scheduler is explicitly enabled and mutating,
while a published candidate is checking, a later pass can stage one capped group
for the next candidate. Targets driven locally keep their existing handoff.
Staging reads the full author-policy queue through the
existing Level 1 run-once service path with `mutate: false`; that path writes
only its normal diagnostic evidence, never a branch, label or merge. An
unavailable queue read before any write leaves that repository busy for this
pass and lets other repositories proceed; authorization denials still stop.
After a write begins, the existing no-replay stop remains. Every
staged label requires the same published candidate ref, SHA, base and complete
membership, a fresh author-policy queue and current-head readiness. Unknown
label authors, incomplete labeled-PR inventory, another enrolled base,
failed/uncertain evidence, a release hold, or a candidate transition stop
staging. A queued group outside the active candidate blocks further groups,
including on repeat passes, preserving the per-pass cap and existing routing custody.
If the running candidate later fails or changes, Launchplane may combine its
remaining members with that queue under its own policy cap and recovery rules.
The current candidate keeps its membership; Launchplane selects the queued
group only at its next candidate boundary. Staging emits `next_batch_queued`
without a local-driver handoff, since the active scheduler continues the train.

The watcher never extends an already queued next group;
clearing a previously labeled PR's hold can still change the train's membership.

The helper emits JSONL `batch`, `held`, `busy`, `skipped`, `enqueued`, `next_batch_queued`, and
`driver_handoff` events. Busy diagnostics include queue dispositions and labeled
PR numbers when available. Once the controller proves there is no reconciliation,
landing or eligible next queue, labeled PRs it reports
as ineligible do not hold the repository busy. Their labels stay in place;
the train still owns their holds. Dispositions cover all enrolled bases in
the repository. Labeled PRs absent from that inventory, or still reported as
missing their enqueue label or lacking label-author evidence, preserve the
batch until their state is known. Labeled stack children absent from the
inventory remain on the Supervisor's coordinated stack path.
Clearing a hold rejoins the current queue and may rebuild an active candidate.
Busy alone never means a healthy
candidate is progressing. Skipped events identify PRs whose head readiness or
file inventory was not proved. The handoff contains the maintained driver's argv and labeled member
numbers. Run that driver using the Supervisor's normal background monitoring,
one driver per train. Stop an earlier driver for that train before
starting the new handoff; verify every intended member's eventual landing. The
watcher does not merge, update branches, deploy or send Client invitations.

The local lock serializes this watcher's labels and pause updates. It does not
lock Launchplane's scheduler, another host, or manual queue writers. Service
state can change after a read and before a GitHub label: this interim helper is
not an atomic admission fence. When using an optional manual pause, coordinate
other drivers and account for already queued/active work. Do not claim that a
pause file halts an existing candidate. Client-product delivery still follows
the linked Client Products rules; those rules also link the decision retiring
the Launchplane landing hold. First-class service holds and atomic admission
belong to [launchplane#3327](https://github.com/cbusillo/launchplane/issues/3327)
service admission.

## Daily stranded-work service

`scripts/stranded_service.py` uses the persistent service runner's locks,
process cleanup and reconciliation markers for a separate daily launchd job.
It calls the existing direction audit in stranded Markdown mode and invokes a
configured sender. It contains no credentials. The Supervisor
installs and starts it after merge; preparing or rendering a job never starts it.
Use the installed runtime catalog, never a task worktree.

Keep this configuration private. Select the same inventory and trusted authors
as the Supervisor's existing question queue. Supply the existing Discord helper
as an absolute argument array; it receives Markdown on stdin, and `{report}` in
an argument expands to the retained report path for file-based senders. The
helper's documented success contract must be exit zero only after confirmed
delivery. Its stdout/stderr are retained as transport evidence; the runner does
not infer success from their wording. A session-only MCP reply tool cannot serve as this command.
If no standalone route exists, resolve that on the owning issue before activation;
do not invent a destination or provision credentials.

```json
{
  "uv": "/absolute/path/to/uv",
  "catalog": "/runtime/catalog",
  "state_dir": "/private/stranded-state",
  "scan_args": ["--stranded-owner", "OWNER", "--question-queue", "OWNER/CATALOG#QUEUE", "--director-name", "DIRECTOR", "--max-requests", "2000", "--deadline-seconds", "600"],
  "delivery_argv": ["/absolute/path/to/uv", "run", "--no-project", "--no-cache", "/runtime/catalog/skills/supervisor/scripts/discord_digest.py", "--file", "{report}"],
  "hour": 7,
  "minute": 30,
  "weekly": {"owner": "OWNER", "weekday": 1}
}
```

The optional `weekly` entry runs the
[weekly direction audit](../../direction/SKILL.md#weekly-audit),
`direction_audit.py --weekly --owner OWNER`, after the daily digest, whether or
not that digest succeeded. It runs on the first scheduled run on or after the
ISO `weekday` (1 is Monday) and, after a failure, on each later daily run until one
audit records the weekly stamp that ISO week. The receipt is saved as
`weekly/DATE.receipt.json` under `state_dir`. Each attempt that left the stamp
unwritten stays as its own `DATE-*.attempt.json`, never overwritten, with stderr
beside every attempt. Repository windows are committed together only after the
complete receipt is flushed, so a stopped collection or output failure leaves
the original windows for the next scheduled or manual audit.
An exit-1 attempt with complete JSON and `weekly_skipped` can advance the read
repositories' windows; the receiver must use its findings as well as the next
receipt. The audit is
bounded to 30 minutes and sends nothing. The plist does not change: add the entry
to the private configuration, and the next run picks it up.

`discord_digest.py` is the standalone digest sender. It reads Markdown from
`--file` or stdin and reads `DIRECTOR_DIGEST_DISCORD_WEBHOOK_URL` from the
process environment first, otherwise `local.env` under `CODE_HOME`, then
`CODEX_HOME`, then `~/.code`. It parses only that assignment, without sourcing
the file or expanding shell expressions. Keep the webhook in the private
mode-600 environment file; never put it in the configuration or arguments.
A missing key fails with `digest_webhook_missing`, without a value.

For stranded-work reports, the sender posts the full list once on the pinned
[digest archive](https://github.com/cbusillo/codex-skills/issues/2006), reusing an
identical bot-authored comment on a reconciled retry. `--tracking-issue
OWNER/REPO#NUMBER` selects another archive. It then sends one message below
2,000 UTF-16 units: date and coverage, counts by kind, all Director waits by
name, oldest five links for each other kind, and the full-list link. If that
cannot fit, the same single message carries counts, the full-list link, and the
complete original report as a `stranded-work.md` attachment. Every person wait
is preserved in that attachment. Archive errors prevent Discord delivery.
Input, summary, archive, and delivery failures have distinct value-free names
in retained sender stderr. The full-list archive still depends on GitHub
accepting the report as one comment; an archive rejection stops delivery with
`digest_archive_failed`, preserving the report for reconciliation.

The sender reads the guild and channel from the webhook metadata and posts with
`wait=true` and mentions disabled. Each confirmed message prints its
`https://discord.com/channels/GUILD/CHANNEL/MESSAGE` receipt. Other Markdown
input keeps the existing multipart behavior. Only all confirmed parts produce
exit zero. See the
[Discord webhook contract](https://docs.discord.com/developers/resources/webhook).
It never retries: on a partial failure the prior links remain in
`delivery.stdout`. Reconcile **all parts** at the receiver before clearing the
marker; a link for an early part alone does not prove the whole report arrived.
Named errors contain no URL or response body. Do not run the sender directly
as a connectivity test against the real destination. After merging and updating
the catalog through its supported path, the existing job needs no reconfiguration.
Its schedule, plist, private configuration and webhook remain the same.

The calendar uses the Mac's system timezone: verify it is US Eastern for a
7:30 AM ET schedule. launchd coalesces a missed calendar invocation after sleep;
this user LaunchAgent requires the user to be logged in, but no terminal or
agent session. `KeepAlive` and `RunAtLoad` are disabled. One successful delivery
per local calendar day suppresses duplicate kickstarts; failures do not become
successful deliveries. Exit 3 from the collector is delivered with coverage
warnings and retained as `scan_exit_code: 3` in the daily receipt. The runner
exits zero after the sender succeeds so launchd does not replay that delivery.
Other collector failures retain output without sending it. Scan and delivery
have 15-minute and two-minute bounds, respectively.

After filling the private configuration with verified existing routing, the
Supervisor renders, installs and starts only this job:

```sh
uv run /runtime/catalog/skills/supervisor/scripts/stranded_service.py render --config /private/stranded.json --output /private/stranded-jobs
install -m 600 /private/stranded-jobs/com.codex-skills.supervisor.stranded.plist ~/Library/LaunchAgents/com.codex-skills.supervisor.stranded.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.codex-skills.supervisor.stranded.plist
```

Trigger the first digest immediately, then inspect the logs and receiver:

```sh
launchctl kickstart "gui/$(id -u)/com.codex-skills.supervisor.stranded"
launchctl print "gui/$(id -u)/com.codex-skills.supervisor.stranded"
```

`state_dir` retains each attempt's Markdown, collector stderr and delivery
stdout/stderr in a separate dated directory, plus daily delivery status JSON and
runner logs. Retries preserve earlier failed attempts. A zero sender exit records transport
success; record the actual Discord message link and Chris's receiver evidence
on the owning issue before calling daily delivery active. A failed, timed out
or interrupted send leaves `daily.stranded.reconcile`. Later invocations refuse
to send until the Supervisor checks the retained outcome at the destination.
If delivered, record its verified message URL so the schedule resumes without
replaying that day's digest:

```sh
uv run /runtime/catalog/skills/supervisor/scripts/stranded_service.py clear --config /private/stranded.json --delivered-url https://discord.com/channels/GUILD/CHANNEL/MESSAGE
```

If confirmed unsent and the marker references today's completed scan, retry that
retained report without scanning again. This command is only for a
confirmed unsent delivery; an empty stdout alone does not prove that when a
network request could have succeeded. Input, webhook configuration, summary and
archive-stage failures precede Discord delivery. An HTTP or response failure,
`digest_delivery_failed`, a timeout, or an interrupted sender can follow an
accepted POST and require checking the receiver first. The runner uses its existing lock, reservation and daily
receipt, retains the original attempt, and records retry output separately:

```sh
uv run /runtime/catalog/skills/supervisor/scripts/stranded_service.py retry-unsent --config /private/stranded.json
```

If confirmed unsent and a fresh scan is needed, clear the marker and kickstart once:

```sh
uv run /runtime/catalog/skills/supervisor/scripts/stranded_service.py clear --config /private/stranded.json
```

To stop and remove the job:

```sh
launchctl bootout "gui/$(id -u)/com.codex-skills.supervisor.stranded"
rm ~/Library/LaunchAgents/com.codex-skills.supervisor.stranded.plist
```

Keep private configuration and evidence for recovery. Stop before updating or
rotating its files and verify children exited. Pull the landed runtime catalog
through the supported update path, render again, review and bootstrap the job.
To roll back, restore the previous reviewed configuration/catalog revision and
render/bootstrap it after reconciling any pending send. Other Supervisor jobs
and GitHub records need no change. Offline tests:
`uv run skills/supervisor/scripts/test_stranded_service.py`,
`uv run skills/supervisor/scripts/test_services.py` and
`uv run skills/supervisor/scripts/test_discord_digest.py` (fake HTTP only) and
`uv run skills/supervisor/scripts/test_digest_summary.py` (synthetic reports).

During every Supervisor check, inspect this job too: `launchctl print` above,
`runner.jsonl`, `runner.stderr.log`, `daily.stranded.reconcile`, and the latest
daily receipt. After the scheduled time, a missing receipt means the digest has
not been confirmed: read the attempt's scan/sender output, reconcile any marker,
and retry only a confirmed unsent delivery. A scan failure can retry without a
send marker; a sender failure stays stopped across future mornings until
reconciled. Never treat a loaded job alone as proof that digests are arriving.

## Persistent Supervisor services

On the Director's Mac, launchd owns enqueue, drive, foreign and throughput,
plus optional `idle-codex` and `idle-claude` jobs. They survive Supervisor session restarts and start again at login.
The Supervisor installs and starts them after review; an executing session
prepares and tests the helper without installing or starting these services.
Use the installed runtime catalog, not a task worktree, as their source.

Keep one private configuration at a stable location, reusing the same hold and
foreign-watch state files across takeovers. Each service takes the existing
bounded command as an argument array, an absolute working directory and a
delay between successful runs. The runner uses no shell, copies no credentials,
and preserves the command's output in its log. Use absolute executable and
script paths: launchd does not inherit a terminal's directory or shell setup.
Do not put secrets in this configuration; helpers use their existing private
credential sources. A configuration has this shape, with all four entries:

```json
{
  "uv": "/absolute/path/to/uv",
  "services": {
    "enqueue": {
      "argv": ["/absolute/path/to/uv", "run", "--no-cache", "/runtime/catalog/skills/supervisor/scripts/auto_enqueue.py", "--owner", "OWNER", "--hold-file", "/private/holds.json", "--apply"],
      "cwd": "/runtime/catalog",
      "restart_seconds": 60
    },
    "drive": {
      "argv": ["/absolute/path/to/uv", "run", "--no-cache", "/private/existing-bounded-drive-loop.py"],
      "cwd": "/runtime/catalog",
      "restart_seconds": 60
    },
    "foreign": {
      "argv": ["/absolute/path/to/uv", "run", "--no-cache", "/runtime/catalog/skills/supervisor/scripts/foreign_watch.py", "--owner", "OWNER", "--own-login", "OWNER", "--own-login", "shiny-code-app[bot]", "--launchplane-login", "DELIVERY-LOGIN", "--director-login", "OWNER", "--mention-approval", "APPROVAL-COMMENT-LINK", "--state", "/private/foreign-state.json", "--persistent"],
      "cwd": "/runtime/catalog",
      "restart_seconds": 60
    },
    "throughput": {
      "argv": ["/absolute/path/to/uv", "run", "/runtime/catalog/skills/supervisor/scripts/fleet_counts.py", "--owner", "OWNER", "--service-snapshot"],
      "cwd": "/runtime/catalog",
      "restart_seconds": 300
    }
  }
}
```

Existing four-entry configurations remain valid. To remove per-notice idle-watch
re-arming, add an optional entry to that same `services` object:

```json
"idle-codex": {
  "argv": ["/absolute/path/to/uv", "run", "/runtime/catalog/skills/supervisor/scripts/codex_idle_watch.py", "--ledger", "/private/existing-worker-ledger.json", "--delivered-state", "/private/codex-delivered.json", "--persistent", "--notify-config", "/private/supervisor-services.json"],
  "cwd": "/runtime/catalog",
  "restart_seconds": 1
}
```

`idle-claude` uses the same watcher with `--harness claude` and its own
`--delivered-state`. Reuse the existing worker ledger and delivered-state files.
The notification target is one current-Supervisor binding in that same service
configuration, never a second session registry or a Supervisor-owned worker row.
At initial setup, before bootstrapping either idle job, and at every takeover,
the current Supervisor runs:

```sh
uv run /runtime/catalog/skills/supervisor/scripts/services.py bind --config /private/supervisor-services.json
```

`bind` derives the caller's actual root native ID and account home, finds its
transcript only in that home, and validates its identity. It rejects Codex child
contexts. If its own source is ambiguous, pass the exact `--transcript` path.
Claude additionally needs its existing managed tmux pane; use `--tmux-socket`
when that existing route uses another socket. Unsupported routes refuse without
terminal input, account changes or automatic migration. No handwritten recipient
JSON is needed. The binding is explicitly the Supervisor, not worker ownership;
ordinary review notifications keep their existing ownership requirements.
Keep the Supervisor out of the watched worker ledger.

Loaded jobs reread this binding for every notice. Binding replacement and final
recipient selection through submission share one configuration lock, so after a
successful takeover future sends cannot use a cached predecessor. A busy lock
defers an unsent notice. A spent uncertain attempt stays bound to its original
recipient for reconciliation; changing the current binding never replays it. Reuse healthy loaded jobs without rerendering their plists at takeover.
A held or failed job still follows the existing reconciliation and restart steps;
`bind` changes only the current address and never clears a hold or starts a job.
Before a successor binds, the previous address remains current. A dead or
unavailable recipient can hold the job for reconciliation; bind the successor
and follow the existing recovery steps rather than deleting delivery state.

Codex delivery uses the existing native queue helper. Claude delivery uses the
existing guarded tmux route, including idle, empty-composer and identity checks.
Keep a Claude Supervisor at its idle empty prompt without non-scheduled background
tasks. While it is busy, notices defer until that proof returns; the normal
finished-session check still handles candidates during the wait.
Both idle watches serialize sends under the configuration lock. Native queue results distinguish a certainly unsent attempt from
a permission or RPC refusal. Unsupported routes and identity errors stop for
reconciliation. A certainly unsent busy recipient is rechecked on the next poll;
uncertain or denied sends are never automatically replayed.

Persistent mode immediately sends each new closeout request and authentication
failure at the next poll, then keeps watching. It rechecks closeout requests with
`finished_map.py`'s exact-session candidate reader and includes the supported
`finished_map.py --session-id` command in the notice. Do not reconstruct transcript
parsers. Notice delivery grants no exit authority: every existing handoff, input,
task, process-exit and claim verification still applies. The watcher keeps its
existing deadline; the service restarts clean deadline exits automatically.
Normal SIGTERM/SIGINT stops let an idle watch exit cleanly. A process-group stop during a
notification retains its attempted notice and recipient evidence; the existing runner
keeps its bounded child-exit and reconciliation checks.
Keep the idle service restart delay short, as in the example. If using capacity
retry, keep exactly one watcher for its existing state file; status and digest
readers retain their current read-only route. Logging notices
until the next scheduled check does not meet the immediate-handling rule.

The same delivered-state file marks each attempt spent before calling the
sender and retains its exact notice and recipient binding until success. The
watermark records an attempted notice, not proof of delivery. A certainly unsent,
deferrable attempt removes that reservation so the next poll can recheck it.
Other failures keep the reservation and evidence, exit nonzero and use the
existing service reconciliation hold. Read that evidence and the original
recipient's readback, then handle the candidate through normal finished-session
verification. Resolve any refusal on its supported route before using the
existing service `clear` command below. Clearing the service hold or changing
the binding never removes a spent notice or replays its uncertain delivery.
Never delete delivery state to make a send run again.

One lock per delivered-state file prevents two watches using it concurrently,
including bounded watches. At takeover, reuse a loaded service or verify the
prior bounded watch ended before re-arming; a lock refusal never authorizes a
duplicate watcher.

Replace the example paths and scope with the current Supervisor handoff's
commands. The drive entry runs the existing bounded loop calling the maintained
train driver, one driver per repository. It does not replace Launchplane's
controller or take over another driver. [launchplane#3419](https://github.com/cbusillo/launchplane/issues/3419)
owns moving that scheduling into Launchplane. The throughput entry runs `fleet_counts.py`; its JSON stdout accumulates in the
throughput log. Use only successful entries (`ok: true`) as count snapshots.
`--service-snapshot` keeps this read-only service sampling after failed reads by
returning zero with `ok: false` and no totals; the ordinary CLI exits nonzero.
Shared transport still owns retries and cooldowns. Check `diagnostics.actor`
and installation scope before interpreting these visible totals as the full fleet.
Keep the drive command at a durable private path. Preview enqueue first under the
existing authority described above before configuring its `--apply` command.

Before first startup, verify the previous deadline loops and their children
have ended, and reconcile their unread output. Before changing a running
configuration or rendering replacement jobs, stop these jobs and verify their
children have exited. A per-service lock prevents duplicate runners sharing
one configuration; the existing watcher and driver locks retain their own
scope. A second configuration is not a takeover route.

Render only (writes private plists and empty log directories, never installs):

```sh
uv run /runtime/catalog/skills/supervisor/scripts/services.py render --catalog /runtime/catalog --config /private/supervisor-services.json --output /private/supervisor-services
```

After reviewing those jobs, the Supervisor starts each with:

```sh
launchctl bootstrap "gui/$(id -u)" /private/supervisor-services/com.codex-skills.supervisor.enqueue.plist
```

Repeat for `drive`, `foreign`, `throughput` and each configured idle job. To start automatically after a
Mac login, the Supervisor places the reviewed plists in `~/Library/LaunchAgents/`
and bootstraps those paths instead. Session takeovers reuse the loaded jobs;
do not bootstrap another set or re-arm deadline loops. During each check:

```sh
launchctl print "gui/$(id -u)/com.codex-skills.supervisor.enqueue"
tail -n 40 /private/supervisor-services/logs/enqueue.jsonl
tail -n 40 /private/supervisor-services/logs/enqueue.stderr.log
```

Repeat for the other jobs. Their log names are `<service>.jsonl` and
`<service>.stderr.log` under the render output's `logs/`.
Rotate logs during a stopped maintenance window when needed.

Clean bounded exits restart after `restart_seconds`. Transient enqueue reads
already recover inside `auto_enqueue.py`, without an extra wrapper. Any nonzero
exit or launch/configuration failure emits `reconciliation_required` and stops
that service: launchd does not blindly replay uncertain writes or bypass quota
retry times. A private `<config>.<service>.reconcile` marker also preserves a
failed or interrupted command across login, reboot and fresh bootstrap. The
runner writes it before starting a child and removes it only after exit 0, so
a forced runner kill cannot silently resume a command. Read the helper output
and canonical records, respect any retry time, verify prior children have ended,
and resolve the cause. Then clear that service's marker under its lock with
`uv run /runtime/catalog/skills/supervisor/scripts/services.py clear --config /private/supervisor-services.json --service enqueue`
before restarting a stopped job with
`launchctl kickstart "gui/$(id -u)/com.codex-skills.supervisor.enqueue"`.
Do not use `kickstart -k` to displace a running service.

To stop, use `launchctl bootout "gui/$(id -u)/com.codex-skills.supervisor.enqueue"`
and repeat for the other jobs. SIGTERM reaches the runner, which terminates its
active child process group and waits for exit before returning. Verify the
job is absent and its children have ended. To keep it stopped after login,
remove only its reviewed plist from `~/Library/LaunchAgents/`; retain the
configuration, holds, watch state and logs for takeover or reconciliation.

## Catalog landing verification

Catalog verification runs independently through the existing service runner.
The Supervisor handles failure notices only; it does not inventory landings,
launch smoke sessions or keep a latest-success pointer. Add the optional
`catalog` entry to the same private service configuration and render/start it
through the [existing service setup](#persistent-supervisor-services). A configuration
may contain only this job; unrelated Supervisor services are not required:

```json
"catalog": {
  "argv": ["/absolute/path/to/uv", "run", "/runtime/catalog/skills/supervisor/scripts/catalog_landing.py", "--poll", "--config", "/private/catalog-landing.json", "--evidence", "/private/catalog-evidence"],
  "cwd": "/runtime/catalog",
  "restart_seconds": 60,
  "failure_argv": ["/absolute/path/to/uv", "run", "/runtime/catalog/skills/supervisor/scripts/discord_digest.py"]
}
```

Its private landing configuration keeps the stable launch pointers described
below, with `source_worktree` pointing to the durable runtime checkout. Omit
`pr` and `landing_sha`: polling resolves them from default-branch first-parent
history and exact merged-PR evidence. Set `notify_config` to the existing
service configuration so cleanup reads the current Supervisor identity.
The service entry's `failure_argv` uses the existing sender, accepting Markdown
on stdin, to the channel Chris and the Supervisor already use.
No new account, recipient registry or delivery service is created.

The first run checks the current landing and explicitly leaves earlier history
unverified. Later runs enumerate every first-parent landing since the saved
cursor and verify the latest runtime once for that batch. They reconcile through
the runtime helper, run both fresh harnesses, close smoke sessions normally,
and post verification on the configured owning issue, listing every covered
SHA. The comment records HEAD before and after smoke; neither observation
measures a session's loaded SHA. A newer runtime proves containment, not an
earlier runtime's behavior. Only a nonempty diff confined to `README.md` and Markdown under `docs/` may
skip live sessions; skill instructions still need smoke checks. Cursor advancement
requires verification, cleanup and the posted comment. Success needs no Supervisor action.

Verification failures post the failed gate. The existing runner notifies that
channel on any failed child or stale reconciliation marker, including an
interrupted run or a helper that cannot start, then keeps the
service stopped. Read private evidence and reconcile any
uncertain launch, request, comment or notification before recovery. Resume a
failed verification explicitly with the same command plus `--resume-failed`,
using the same evidence directory; this reuses launch and closeout journals.
A completed bad acknowledgement needs a fresh attempt. First get each preserved
session's normal handoff and run `session_closeout.py` with the exact native ID
and the original `<attempt>/<harness>-closeout` evidence directory. The saved
`<harness>_source` gives its native ID, original ledger and generation. Once all
old sessions have retired, run the poll command with `--supersede-failed`. It
revalidates those exact closeouts through the existing helper, refuses changed
generations or uncertain mutations, includes later landings, and preserves the
old attempt while starting fresh sessions. It can retry the same tip after an
environment repair. It never skips missing merged-PR proof or rewritten history.

After recovery succeeds, clear the runner's `catalog` marker and restart that
job through the existing service route. Never remove a journal or replay an
uncertain mutation to clear a failure. The runner reserves each notice before
sending and never replays an uncertain delivery. Before explicit `services.py
clear`, inspect its retained evidence, resolve uncertain delivery at the existing
channel, and ensure the failure is visible there. For a certainly unsent notice,
fix the sender and deliver once through that existing route. Clear archives the
original notice without relabeling it delivered or resending it; a later stopped
episode gets its own notice. Prelaunch read failures keep no saved failed
verification, so the existing clear/restart route retries those reads.

For one bounded manual verification or recovery:

Run `uv run skills/supervisor/scripts/catalog_landing.py --config <private-landing.json> --evidence <private-evidence>` with private identity pointers:

Set `repo` to `OWNER/codex-skills`, integer `pr`/`issue`, and the full merged
`landing_sha`. Supply absolute `source_worktree` (retained landed source),
`runtime_worktree` (bound checkout) and private `ledger` paths; the existing tmux
container's `window_id`; and the current native `supervisor_session`.
`command_files` maps `codex`/`claude` to their normal fresh launch-file paths.

An optional `account_config` selects existing private account mappings.
Resolve a retired source through the runtime reconciliation procedure, keeping
an available landed/runtime source until this check finishes. The helper requires
the exact merged PR landing SHA, synchronized/already-current runtime receipt,
matching bound checkout and landing ancestry. `not_applicable` never proves sync.
It launches fresh managed Codex and Claude sessions through normal account
selection and receipts on `tmux -L supervisor`, without browser access, using
the existing dedicated container. It never restarts a daemon or answers a trust
prompt. Each initial completed native acknowledgement must name the landing,
use no tools and arrive within two minutes; launcher return and echoed input
alone do not pass. It then sends one targeted handoff request to each idle empty
prompt, saves its exact native-session issue handoff, and delegates normal closeout.
A saved handoff reruns closeout directly, preserving its URL and native ID even
after retirement. Manual recovery reuses `<catalog-evidence>/<harness>-closeout`.
A Codex `register_ledger` failure preserves its launch object:
save that object as a private receipt, run `iterm_tab.py register --receipt-file
<retained-launch.json> --ledger <ledger.json>`, then rerun with the same evidence.
Registration reads the retained native receipt; it never relaunches the worker.
A delayed handoff or shutdown leaves a passed launch distinct from incomplete
cleanup. Failed or uncertain sessions remain preserved for the normal recovery.

The one-line result names the first failed gate or completion, landing/runtime
SHAs, smoke handoffs and private evidence. Polling posts its result automatically;
a bounded manual run leaves posting to its caller. A failed smoke does not undo
the confirmed merge. Reuse the same evidence directory on recovery; an uncertain
mutation is never retried with a fresh journal or another route.
For other rare terminal recovery, use [finished-session shutdown](#finished-session-shutdown-stages).

## Launching and nudging

### Opt-in Codex GUI work

On a worker's GUI relaunch request, complete
[finished-session shutdown](#finished-session-shutdown-stages), then relaunch
that same item through this route with its handoff. Follow
[Planning: Claim](../../github/references/cli-reference.md#planning-claim): use
a new task branch at the verified pushed SHA for no-PR work, or the original
claim ID with `--resume-from` for retained PRs.
Preserve actual permission refusals through the handoff.

For an authorized native-app task, put a single standalone declaration in its
brief's first nonblank line, using the exact app bundle ID, for example
`GUI task: com.apple.TestFlight`. Examples quoted later in a source-work brief
do not opt that task into GUI work.
Launch that brief alone with `--account-provider openai --computer-use required`. The launcher rejects terminal app targets and adds
the GUI safety instructions to the worker's prompt. GUI commands retain their
local invocation and client-side overrides; they do not use the managed RPC
ingress or acquire a managed-client exemption. A live local GUI client therefore
keeps idle-closure admission conservative. Other briefs create no GUI state.
With the catalog Chrome opt-in, local Codex launches share the marketplace
setup described below.

Only one Supervisor GUI worker may run at a time, across account homes and both
terminal backends. The launcher refuses a locked screen or unavailable lock
evidence, reserves the shared GUI slot before creating a terminal, and rechecks
the lock in the worker wrapper before starting Codex. The wrapper holds that
slot until Codex exits. This coordinates Supervisor launches, not manually
started GUI workers: check for those before briefing work too.

The worker may use only the named task app. Never target Chris's terminals or
other windows, synthesize global keys, or use a focus fallback
([#1753](https://github.com/cbusillo/codex-skills/issues/1753)). Check screen lock
before every GUI action and stop immediately if locked or unknown. No agent
unlocks the screen. Missing app approval, Accessibility or Screen Recording
permission is Chris's action; neither the launcher nor the worker grants it.

During GUI launches and enrolled local Codex launches, the launcher validates the default home's current
`~/.codex/.tmp/bundled-marketplaces/openai-bundled` directory and creates the
selected `CODEX_HOME`'s corresponding path as a symlink to it. A matching link
is reused; an occupied path, redirected parent or different link target refuses
without replacement. The link stays after the task ends and is validated again
on later local launches. It also passes `--no-daemon` and the account-relative
`-c marketplaces.openai-bundled.source="<account-home>/.tmp/bundled-marketplaces/openai-bundled"`
override to that Codex process. The link alone does not load the marketplace.
No shared `config.toml`, authentication, permissions or catalog installation is
changed. Tests use fake homes, console evidence and child processes only.

The paired private GUI launch receipt includes `gui`:
named app, unlocked preflight, marketplace source/link action, process override,
reservation token and shared state path. It uses the count receipt's filename
in `<storage root>/Supervisor GUI Receipts/`; Context Panel's existing
`Launch Receipts/` record stays count-only and keeps its existing size contract.
The launcher prunes its own day-old GUI receipts separately. Terminal output
gives the app, link action and reservation token without account paths. Keep
receipts private. The link
follows replacement at the default home's unversioned path; missing or relocated
targets refuse. CU-T2 established discovery and one Node/Sky state screenshot,
not arbitrary-directory input or remote app-server qualification. If runtime
startup fails, preserve the exact terminal outcome and report it; do not change
working directory, account, executable registration or grants to get around it.

A partial/uncertain terminal submission or killed wrapper retains the GUI
reservation and blocks another launch. Inspect the receipt, exact terminal,
native thread and child processes first; do not replay. After proving the worker
and any submitted descendants have ended, release that exact token:

```sh
uv run skills/supervisor/scripts/gui_launch.py release --token <receipt-token> --verified-worker-ended
```

The release refuses while a worker wrapper holds the lock or when the token
differs. This is recovery after verified shutdown, never a way to make room for
a second GUI worker. Keep a pending reservation's token from the launch failure
output too. The state lives at `~/.code/skill-data/supervisor-gui.json`, across
account homes. Failures known to occur before command submission release the
slot automatically, including receipt failures; a send whose outcome is unknown
retains it. The wrapper uses the launcher's resolved Python interpreter, without
creating a second environment for the worker. The full command and brief are
staged in a mode-0600 file beside the reservation, so nested terminal quoting
does not expand the brief into tmux's message limit. Normal exit or verified
release removes that file. A killed wrapper retains it for recovery. If a
launcher is interrupted before printing a token, inspect the shared state file
and exact terminal inventory before the same verified-shutdown release. At
wrapper startup, a short bounded lock wait tolerates a concurrent reservation
check; a pending or live worker still blocks a second GUI launch.

### Terminal launches

```sh
uv run skills/supervisor/scripts/iterm_tab.py window
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <dedicated-window-id> --command-file <private-launch-file> --brief-file <private-brief-file> --account-provider openai
uv run skills/supervisor/scripts/iterm_tab.py list
uv run skills/supervisor/scripts/iterm_tab.py read --session-id <iterm-session-id>
uv run skills/supervisor/scripts/iterm_tab.py send --session-id <iterm-session-id> --text-file <private-message-file> --verified-target
```

For existing sessions, `send` types the message, reads the exact target, and
uses Tab when the Codex composer visibly offers `tab to queue message` (or
the compact `tab to queue`). Otherwise it uses Return. This applies to both
terminal backends. `--no-submit` leaves the text typed. The returned `delivery`
is `queued` or `submitted` only when readback shows new message text in the
queue or transcript with an empty composer; `typed` means input remains, and
`unknown` means the screen does not prove delivery. Inspect the included
`readback` before any retry when `readback_required` is true. Long or truncated
messages can remain `unknown` even after acceptance. No raw terminal keys are
needed to queue a busy local Codex session.

Use one dedicated Supervisor window, never the window with the most tabs. The
`new` creates the tab in the background without selecting it or moving keyboard
focus. It waits up to 10 seconds for that exact tab's session, refreshing the
iTerm hierarchy before launching. A session-wait timeout names the tab for
inspection. If creation returns no tab identity, run `list` and inspect first;
the tab may still exist. Do not create another tab or replay the launch without
checking it. `window` still restores the previous tab after creation.
For a fresh launch, command files contain one direct agent invocation and its already authorized
model settings, without a prompt or the Discord channels flag. Put the exact
brief in a separate UTF-8 file and pass `--brief-file`; the launcher reads it
and appends it as one quoted prompt argument. For example, a launch file can
contain `cd /absolute/repository && exec codex --remote unix:// -C /absolute/repository`, with the task
text in the brief file. The supported form is a direct `codex`, `claude` or
`agy` invocation, optionally preceded by `cd PATH &&` and simple command or
environment prefixes. `agy` uses its native `--prompt-interactive` option;
Codex and Claude take one positional prompt after `--`. Keep prompt options
out of the command file. Shell wrappers, extra commands, trailing shell punctuation
and shell comments refuse because they may discard the prompt.
For fresh managed Codex `--remote` workers on either terminal backend, add
`--ledger /private/launch-ledger.json`. The existing ingress publishes the
first native thread ID and transcript into that launch's private receipt,
without changing the closed-session log or other launches. The launcher
matches account, process generation and terminal, checks the transcript identity
header, and atomically appends an owned row. Streaming transcript content does
not delay registration. The receipt includes `native_session_id`, `transcript`,
`native_receipt` and `ledger_recorded`; terminal `session_id` keeps its existing meaning.
First-screen verification and all shutdown checks still apply.
A registration failure after submission names the terminal and retains its
receipt. Inspect the terminal and save the reported `launch` object to a private
file, then use `iterm_tab.py register --receipt-file <file> --ledger <ledger>`
to finish registration without relaunching. Duplicate registration refuses.
`codex-session.py status --launch-receipt <native_receipt>` reads the same
identity, including after the client exits; no transcript globbing is needed.
Keep retained receipts while their workers need recovery, then remove them
with normal task-evidence cleanup.
Other fresh modes keep their launch procedure. Append their existing native
ledger entries through `iterm_tab.py register --entry-file <entry.json>
--ledger <ledger>` so every append shares the lock. Do existing-row maintenance
only after launches and registrations using that ledger have finished; never
write an old whole-ledger snapshot over a running batch. Launch-only rows are
preserved by appends and still need their normal identity binding before shutdown.
For an exact-source recovery without command/brief files, use the documented
[`new --resume-session-id` route](#release-a-verified-finished-session).
Empty, whitespace-only, missing or unreadable briefs
refuse before account selection, daemon startup, tabs or launch receipts.
So does a brief that neither links the [brief rules](brief-rules.md), as a
GitHub `blob/main` URL or an absolute path to a file holding the current
rules block, nor includes that block unchanged. A link pinned to a commit, a
relative path or an edited copy refuses; link the reference again. Local
paths with spaces, quotes or brackets are not recognized; use the URL.
Tabs in briefs are allowed and sent through bracketed paste. Other terminal
control characters refuse. For a batch, repeat both file options in matching launch order; every brief
is checked before the first launch. Do not put a shell text substitution in the
launch file to load the prompt.
After the catalog's [pinned Chrome server](https://github.com/cbusillo/codex-skills/blob/main/README.md#chrome-across-claude-account-homes)
is installed for the selected Claude home, omit `--chrome` from launch files:
the user MCP entry already supplies those tools regardless of the session account.
Every `new` launch requires
`--account-provider` as below; keep account variables out of launch files.
The direct invocation must match the selected provider: `openai` runs `codex`,
`anthropic` runs `claude`, and `google` runs `agy`. A mismatch anywhere in a
batch refuses before daemon startup, receipt preparation, terminal creation or
receipt writing on either backend. Correct the invocation or provider and retry;
no account configuration migration is needed.
Single-issue execution briefs include `Anchor: https://github.com/OWNER/REPO/issues/N`
(or `Anchor and claim:`) on its own line. Before accounts, daemons or terminals,
both backends run `gh-plan.py show <anchor> --full --launch-check`, reusing
`show`'s native status and the ownership facts shared with `next`, without its
source-ranking or delivery-history reads. Follow the
[brief admission format](../SKILL.md#the-pattern); its kind and exact evidence
URLs reach the claim's own admission and wait checks without writing planning
state. Active source or
operational work can launch; a blocker, structured wait, live
claim, retained delivery or incomplete read refuses. The existing claim lease
reader recognizes expiry as a changed ownership fact. No new launch-state record
is written. Repeated attempts against the same hold keep refusing; clearing one
hold still leaves the others in force. Read the existing issue and stopped
worker's handoff, then dispatch only after the relevant fact changes.
For read-only work that answers a wait, use `Read-only anchor: <issue URL>`;
that worker must not claim or implement the held work. Its ownership check
still runs, while waits and static PR/branch artifacts can be investigated.
A retained-delivery
successor adds `Resume from: <source claim comment ID or URL>` on its own line. The
source's authored release must exist. A qualified retained-PR handoff is
needed to exempt current delivery artifacts; no current artifacts need no
exemption. The worker's `claim --resume-from` still validates full lineage.
Neither this line nor a release alone clears other holds or claim holders, including
the cross-record source's current claims. Numeric IDs select claims on the
anchor itself; use the full issue-comment URL for another source record.
Target waits remain authoritative; a source's missing legacy wait record
does not add a launch hold beyond the worker's existing claim route. Follow
the [decision and wait reconciliation step](../SKILL.md#the-pattern) and
[wait-record contract](../../github/references/cli-reference.md#planning-claim).
Missing anchor lines keep existing PR-only, lead and other launch routes; the
automatic check covers explicit issue anchors. Workers claim independently after launch.
An execution anchor with stale, done or unstatused scope needs its existing
scope reconciliation before dispatch. Before giving a fresh assignment to an
idle session through `send` or `queue`, review its current anchor state under
the Supervisor procedure. Automatic enforcement here covers new launches;
resuming its current work does not create another assignment.
Use `--account` for an explicit override through the same mapping and receipt
path. Run one agent invocation, without restart loops or commands that continue
after it exits. Read the launched screen once for folder trust or another
blocking prompt. For Codex launches the startup observation watches up to 20
seconds, ending early once the brief's turn shows as running, and reports a
visible hook-review prompt as `launch_blocked` with
`startup.blocked_prompt: hook_trust`, the exact terminal identity and screen.
It does not answer the prompt or prove readiness; a later prompt still needs
first-screen/transcript verification. Do not launch
a duplicate after a blocked result. Record the new native thread and exact transcript in the
ledger. Use `codex-session.py queue` for Codex nudges with the verified thread id and
its configured home; never queue exit commands. The terminal helper suppresses
broadcast input, uses bracketed paste for multiline messages, and sends text
and Return separately. Verify input and prompt
before `--verified-target`, then read back once; do not replay an uncertain send.

Codex launch output includes `codex_home`, `pid`, `tty`, `launch_pid`,
`launch_started_at` and `launch_generation` when the managed proxy can be
identified on the exact launch TTY. Launch and reattachment share that proxy's
process and generation identity, including before the native client starts.
Preserve those fields when adding the native
thread ID and transcript to the ledger. If capture is unavailable, the launch
still works but cannot supply idle-admission proof. After reattachment, refresh
missing or mismatched ledger identity fields from `codex-session.py status`
before `finish`; status reads the recorded generation even after the client
exits. Already-running clients from an older catalog keep their in-memory
generation recorder until they exit and may need that same refresh.
Resume and queue mutations use the source account's
existing daemon and the same per-thread lease as closure and successor admission:

```sh
uv run skills/supervisor/scripts/codex-session.py queue --session-id <native-id> --codex-home <source-home> --message <text>
uv run skills/supervisor/scripts/codex-session.py resume --session-id <native-id> --codex-home <source-home>
uv run skills/supervisor/scripts/codex-session.py status --session-id <native-id>
```

Native resume and queue mutations are routed through these helpers by command policy.

Before it takes the lease, queue asks the `--codex-home` daemon to read the
thread and refuses, with nothing written, unless that daemon has it loaded.
The submission uses that same connection.
Account homes can share one sessions directory, so a transcript does not show
which home runs the thread; only the running home's daemon delivers queued
input. Take the home from the ledger's `codex_home`. Resume a stopped session
before queueing to it.

Queue submission reports success only after its acknowledgement and connection
and ownership cleanup complete. Once submission may have started, any failure,
including timeout or cancellation during cleanup, reports uncertainty with
no-replay guidance. Inspect fresh queue state before further action; never resend
the message automatically. Pre-send cancellation still propagates normally.

Use the canonical lowercase, hyphenated UUID from the native thread record.
The managed RPC ingress requires that same spelling for every action, so another
spelling cannot select a different source freeze or ownership lease. Process
observation recognizes all UUID spellings the native CLI accepts as the same thread.
The helper invalidates the old receipt before sending or launching. `status`
returns the resumed generation's metadata for the ledger; preserve the native
thread and verified handoff when updating it. A later `finish` accepts that new
generation, never the old receipt. Terminal `/resume` input names this route
instead for a single-line Codex command; Claude targets retain their own resume
command, and multiline pasted reference text is unchanged. When successor
admission uses a leased idle-closure receipt, that old generation stays frozen
even if the external claim write needs recovery. Retry the normal claim recovery
or start a new thread and claim available work; never resume that handed-off
source. Ordinary unloaded-thread admission keeps its existing recovery behavior.

### Browser and Computer Use preflight

Page checks stay headless. For a GUI brief explicitly naming the Agents Chrome
profile, add `--require-browser` to `iterm_tab.py new`. Ordinary
source briefs get warnings and keep working. `--without-browser` skips Claude's
bounded connected-browser listing; the two flags cannot be combined. Neither
flag permits an unbriefed browser or desktop action.

With `.local/chrome.toml` present, Claude launches reject `--chrome` and export
`CLAUDE_CODE_ENABLE_CFC=0`, keeping the built-in account-specific server off.
Use `mcp__catalog-chrome__*` on the pinned Agents route. Preflight checks the
selected home and project and calls only `list_connected_browsers`, once per
server in a batch. Empty, failed, malformed and timed-out replies cannot pass
a required-browser launch. Unrequested Computer Use settings are not a launch
warning; enabling it and using Claude Computer Use remain Chris's actions.
Codex is the default for screen work. Under Q475, run Claude Computer Use only
in a visible iTerm tab with Chris's approval for that session, never under the
launchd tmux server. Tool discovery alone does not authorize screen use.
Do not change his logins, Chrome profiles, app settings or privacy grants.

For Codex Computer Use, use `iterm_tab.py new --computer-use required` with the
[app declaration](#opt-in-codex-gui-work), or `--computer-use unused` for source-only mentions.
Local Codex plugin setup uses the account-relative marketplace;
`--require-browser` requires `chrome` and `browser`.
Remote launches keep
their endpoint; `--require-browser` makes `--remote unix://` check the selected
daemon's `plugin/list` for the launch's `-C` directory, refusing inventory
failure or missing plugins. Source launches skip this unrequested inventory;
daemon admission remains an independent check. Optional local discovery failure
warns but keeps the validated local route. An explicit `--no-daemon` is retained
once and never triggers daemon admission, including without the opt-in. Local
interactive clients keep idle-closure admission conservative while live:
they bypass the managed ingress and receive no managed-client exemption.
If marketplace setup itself fails, the warning names the fallback to the
selected-home daemon, or the retained local configuration when `--no-daemon`
was explicit. Other explicit remote endpoints are
unverified and cannot satisfy `--require-browser`. A managed daemon launch also
refuses an endpoint outside the selected account during preflight; select its
account or use a supported local `--no-daemon` launch. Use local `codex -C` for
the supported browser route. Nothing restarts an existing daemon or edits
shared config. Discovery does not prove a working extension, runtime or OS grant.

Session start makes static Chrome-route checks only and prints one loud line
on a conflict. Unrequested Computer Use availability is left to launch
preflight. Direct Claude sessions must omit `--chrome` and start with
`CLAUDE_CODE_ENABLE_CFC=0`; the hook reports a conflicting saved setting rather
than changing it. Local launcher Codex sessions carry their source override into the
hook's diagnostic environment, where it can check that override. Without this
marker, remote or direct Codex setup is left to launch preflight rather than
printing unverified marketplace uncertainty on every startup.
The hook never calls a GUI tool.

**Remote config research, Codex 0.163.0 (#2119):** `thread/start` accepts a
`config` map, and its
[processor](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/app-server/src/request_processors/thread_processor.rs)
loads it into the thread's config stack. A throwaway, unauthenticated app-server
loaded one fake bundled MCP server through that map while the same fixture
without the override loaded none. Both threads were ephemeral, unsubscribed
and the process exited; no model turn or real GUI server ran. This proves the
protocol path, not tool use in the stock remote TUI. That TUI's
[forwarding code](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/tui/src/app_server_session.rs)
filters out `marketplaces` for both CLI `-c` and a `-p` profile. A profile can
supply supported model choices remotely, but cannot repair marketplace loading
there. The native local flag avoids needing a custom remote thread client.

### tmux launches

The launcher stages each command and brief in a mode-0600 temporary shell file,
so tmux receives only its path. The shell removes that file as it starts and
still waits for the launch receipt before running the agent. A failure before
pane creation removes any remaining file; a created pane owns its deletion.
Only tmux's fixed `command too long` diagnostic
is included in errors; other stderr remains private.

Use the same helper and launch/brief files, adding global `--backend tmux`
before its command. The default dedicated server is `tmux -L supervisor`;
`--tmux-socket <name>` selects a separate test server. List, read, send and
close do not start or replace a missing server. Only `window` creates a
container. Each launch creates one background tmux window in that container,
without depending on a GUI connection. Launches never open a GUI view; the
explicit view command below is used on request.

```sh
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux window
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux new --window-id '<returned-window-id>' --command-file <launch-file> --brief-file <brief-file> --account-provider openai
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux list
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux read --session-id <returned-session-id>
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux send --session-id <returned-session-id> --text-file <message-file> --verified-target
```

The account preflight, daemon check, brief quoting, launch receipts and
partial-batch failure records are shared with iTerm. tmux creates a staged
process that waits for a unique start message. Its command cannot run before
the receipt is written. It then executes the selected invocation in zsh,
without sourcing a personal profile, and retains the pane when it ends. A
receipt or submit failure preserves that exact terminal for inspection; never
replay an uncertain launch. Messages use literal targeted input, bracketed
paste for multiline text, and the shared submission/readback route above.
Recheck and read back as for iTerm; `--verified-target` still attests prompt/input
inspection. Target checks ignore animated titles while preserving pane, process,
TTY and exit-state checks.

When adapting a private trial launcher, retain its token checks, command and
brief generation, account options and launch logging. Select this backend on
every terminal call for new tmux entries, and obtain a new container id with
`window`; never reuse an iTerm window id. Keep the legacy iTerm race-recovery
path only for iTerm launches. Record the returned tmux coordinates in the same
ledger and route close/read calls by each entry's backend. Never use an
iTerm-only `list` result to infer that a tmux task is gone.

On macOS, `window` starts a missing server through a transient user-login
launchd service using tmux's foreground `-D` mode, outside the terminal's
process tree. The job is registered in memory; no LaunchAgent file, login item,
saved tmux option or iTerm setting is installed or changed. The native `-D`
mode keeps an empty server alive. Each agent window explicitly receives the
launcher's PATH, Code-home, cache/tool roots, locale and SSH-agent socket from
`LAUNCH_ENVIRONMENT_KEYS`; account roots still come from the selected account.
Other launcher-only environment is not copied into the launchd server. Put
additional per-agent environment in its existing launch file. An existing server is reused untouched,
reported as `server_startup: existing`; this does not prove that a legacy
server was started independently of iTerm. Never restart or migrate an active
server just to change its parent. New containers on that server retain its
current lifecycle until the Supervisor's normal shutdown boundary. A failed
launchd start refuses without falling back to a terminal-owned server.
Workers need tmux 3.2 or later. Non-macOS hosts retain native detached startup.
Reads and attaches use `-N`,
so a missing server is never silently replaced.

**Opening the view is opt-in.** iTerm's native tmux integration can select tabs
as it restores them; the creation API's background flag does not govern that
later restoration. No launch tries to correct keyboard focus after the fact.
This uses the issue's allowed explicit-view fallback. The first successful
launch's `view` receipt reports `deferred` and the explicit command when no
control-mode client is attached, `attached` when one is already present, or
`skipped` on other sockets. An older/unknown server reports `unsupported`
without a retry command that would refuse. It never starts a GUI child. If a view is attached,
later tmux windows are surfaced by iTerm itself; detach before background
launches if its focus behavior interferes with typing.

Choose the Director's requested iTerm window once, by its current number
(currently 3 in the assigned brief):

```sh
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux view --window-id '<container-id>' --iterm-window-number 3
```

The helper resolves that number through iTerm's supported Python API and
remembers its stable `window_id` in private Code-home
`skill-data/supervisor-view.json`. Subsequent `view --window-id '<container-id>'`
uses that identity even when window numbers change. `--iterm-window-id` selects
an exact identity instead. A closed or missing remembered window refuses with
instructions to choose again; it never falls back to the current window or
another window that reused the number. To start a fresh dedicated window after the remembered one closes, use the
existing `iterm_tab.py window` command, then pass its returned ID to
`view --iterm-window-id`; deleting the memory file is unnecessary. With no
previous choice and no target, an explicit view creates and remembers one
dedicated **Supervised agents** window. These paths create only a new gateway session and never type into any
existing session. Tests use fake iTerm objects, never the real app.

For an existing chosen window the helper uses
[iTerm's tab creation API](https://iterm2.com/python-api/window.html#iterm2.Window.async_create_tab)
with `select=False` and the attach command supplied at creation. For the
initial dedicated window it uses `Window.async_create`. New-gateway profile
overrides clear inherited startup text, idle input and triggers; saved
profiles are untouched. A lock on the exact server and container serializes
creation; client TTY and pending-command checks prevent a duplicate gateway.
The receipt reports uncertain or failed creation without relaunching agents.

The explicit attach is `tmux -N -L supervisor -CC attach -r -t <container-id>`.
The running server must be tmux 3.7 or later to enforce read-only control input.
Older/unknown servers keep working headlessly, and the explicit view refuses
before connecting to iTerm. Updating just the client does not upgrade a live
server: wait for the Supervisor's normal server shutdown/update boundary, never
restart active workers to enable viewing. On supported servers the read-only
client rejects pane input, including control-mode `send-keys`;
messaging agents still uses the maintained `send` command. Attachment and
later native tabs may move focus; the view receipt says so. The helper reads
[iTerm's restore preference](https://iterm2.com/python-api/preferences.html#iterm2.preferences.PreferenceKey.OPEN_TMUX_WINDOWS_IN)
and warns once per created view unless it is **tabs in the attaching window**
(value 2). Only the Director changes that setting. Other preferences may
place integrated tabs in separate windows despite the chosen gateway window.

**Q308's safe closing route is Shell > tmux > Detach, or closing only the
gateway.** That disconnects the view while agents keep running. Ordinary
Cmd-W/Close on an integrated agent tab or window is explicitly excluded:
[it kills that tmux window](https://iterm2.com/documentation-tmux-integration.html),
even for a read-only client. Read-only is an input guard, not a substitute for
Detach. Never use `kill-server`, `kill-session`, or a raw pane/window kill to
disconnect or close workers. Existing plain iTerm sessions keep their original
backend; migrate by launching fresh tasks, never moving active work.

Tests use throwaway `tmux -L` names in private temporary `TMUX_TMPDIR`
directories. Teardown stops only its own server and launchd job and removes
its socket and lock files on normal or exception completion. Abruptly killing
the test runner can leave its isolated launchd job; retain that exact job/socket
identity for test-owned cleanup, never sweep a live Supervisor socket.
Tests never drive real iTerm or touch existing
Supervisor sockets, clients or lock files. The launchd test kills its own
simulated terminal process group and proves that its server and worker remain.

For shutdown, complete the Supervisor skill's transcript, issue handoff,
permission and input checks first. Read the exact pane, clear only verified
input if needed, then read back that the entire agent input is empty. Send
`/quit` for Codex or `/exit` for Claude with `send --verified-target`. An exact
exit command reports `delivery: session_ended` once the matching harness
processes observed on that TTY are gone, even if the pane disappears before
readback. This result does not release claims or close the terminal. Wait at
most 60 seconds, reading `read` for `dead: true` and checking that the matched
agent PID and processes belonging to that pane have exited. If its TTY was
reused, use the native close command's ownership proof below. Preserve a running or
uncertain pane. Record/release native session claims under the existing
finished-session procedure before removing the terminal.

```sh
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux close --session-id <tmux-session-id> --ledger <private-ledger.json>
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux list
```

Finish verifies the native transcript and handoff. Terminal closure
rechecks the owned launch nonce, container, window, server and TTY, rejects
splits or linked windows, and proves that both the recorded PID and the pane
process are absent. It checks twice, then asks tmux to remove only that dead
pane, conditional on its launch nonce and dead state still matching inside
the server. A dead pane has no input consumer; a live shell cannot qualify
from its screen appearance. Read back disappearance once. An uncertain close
is retained for reconciliation rather than retried.

If the dead pane's old TTY has been reused, close proceeds only when tmux
identifies a different live pane on that TTY and process ancestry proves every
current job there belongs to that new pane's PID tree. Unattributed jobs,
missing ancestry, and either recorded PID still present preserve the dead
pane. The live pane is never a close target, and no process is killed.

For the locked-screen acceptance proof, the Supervisor arranges a short test
with the Director. Prepare one harmless real Codex or Claude launch with an
exact brief asking for a response and an unqualified safe-exit verdict, and
private command, message and ledger files. Start the bounded proof from the
Supervisor's existing session after the Director agrees to lock the screen.
While it stays locked: create the container and launch, verify native thread,
transcript/account and terminal identity, read the response, send one harmless
message at a verified empty prompt, read the reply, and perform the full
shutdown above. Capture timestamps, launch/read/send/process-exit/close
readbacks privately. After unlock, demonstrate the tmux view and read one
existing iTerm session without changing it. Record the Director-observed lock
interval and outcomes on the owning issue; real-server tests alone do not
prove locked-screen behavior. The Supervisor runs the controls; the Director
only locks/unlocks and confirms what they saw. Leave preexisting test servers
and active sessions alone.

## Choosing the account

Launch with `--account-provider openai|anthropic|google`. Context Panel is the
only account-choice source, under the decisions on
[codex-skills#1270](https://github.com/cbusillo/codex-skills/issues/1270).
A single launch follows `answers.useNext`; repeat `--command-file` for a batch,
and the helper uses the first corresponding IDs in the provider's published
`launchOrder`. It never re-ranks, substitutes a configured account or applies
a private reserve. Account configuration only maps snapshot rows to launch
environments. An unmatched or ambiguous choice refuses before creating tabs.

Codex uses the [shared native renewal and hold path](#checks-and-morning-digest).
Only accounts actually considered for launch are checked; an explicit healthy
choice does not probe unrelated failed homes. Native `codex login` and the exact
home's `codex app-server daemon start/restart` remain available for recovery. An agent
starts a stopped account daemon after the Director signs in; this is the
existing native recovery command, not another renewer.

Read Context Panel's
[Use next ranking and launch receipts contract](https://github.com/cbusillo/context-panel/blob/main/docs/provider-usage-access.md#use-next-ranking-and-launch-receipts)
for ranking, stale-list eligibility, count-only receipt fields and retention.
The launcher checks the published stale list's observation time, refuses once
it reaches 30 minutes, and reports `nextCapacityAt` when nothing is rankable.
Reader failure refuses; for OpenAI and Anthropic, `--account <name>` is the explicit override, but still
needs a snapshot row so its launch receipt uses the correct opaque ID.
Configuration errors and Context Panel's plain reset prompt lines are reported
in launch output. Never apply a reset; Chris does that.

The helper exports the account's `env` ahead of the command, including an agent
started after `cd`. A command that sets or unsets account variables refuses, including through
common shell wrappers. Keep brief text in `--brief-file`, separate from the
shell settings in `--command-file`.
App Group launch receipts go through the installed snapshot executable's
`--record-launch`, never a direct worker write. Explicit storage roots instead
use a temporary write probe. Both routes preflight before terminal creation;
errors, timeout or unconfirmed receipt names refuse launch without new grants.
Receipt publication is atomic immediately before submission. Failed/uncertain
submission retains its receipt and exact terminal identities: list/read before
retrying, never replay the whole batch. Only this launcher's own expired receipt
files are pruned. Use `account_choice.py --provider PROVIDER [--count N]` for
read-only previews; normal `iterm_tab.py new` owns actual receipts.

Read the returned account, source and reason before recording the session.
Successful launch output reports environment variable names, without their
private path values. A daemon refusal includes the selected home in its recovery
diagnostic; keep that diagnostic out of public comments.

`remaining_fraction` is Context Panel's headline for whichever window limits
the account now, named by `limiting_window_label`; it is not a weekly figure.
When reporting capacity, read `window_capacity.five_hour` and
`window_capacity.weekly` instead: each lists that period's windows with their
own remaining fraction, reset and burn. For weekly pacing or banked-reset burn,
use the weekly entries' `burn_units_per_hour`, never changes in the headline.
Snapshots older than
[context-panel#961](https://github.com/cbusillo/context-panel/pull/961) carry
no periods, so both are `null` (unknown) and the label is `null`.

For source work use `codex --remote unix:// -C /absolute/repository` with the
per-session settings. The empty endpoint selects that account's daemon; fixed
endpoints can attach to another account, and removing remote can select embedded
mode. Every batch command needs exactly one existing literal absolute `-C`/`--cd`,
without variables or tilde; shell `cd` alone does not set a remote worker's folder.
Preflight precedes daemons, terminals and receipts. Selected-home project trust
checks canonical, original and Git-root entries in order; explicit directory
entries take precedence even with no trust level. Worktree trust roots are the
main checkout; unrelated parent-folder trust never qualifies. The first match
must be trusted and the config readable/valid. The launcher uses no other trust
overrides and never edits config. Follow its exact-target interactive review
or explicit-entry remedy yourself; it may not show a new prompt for an existing
untrusted entry. See the native [directory option](https://developers.openai.com/codex/cli/reference/)
and [project trust](https://developers.openai.com/codex/config-reference/).

Claude needs literal `cd /absolute/repository && exec claude ...`.
Under [the existing #2100 decision](https://github.com/cbusillo/codex-skills/issues/2100),
launcher trust covers roots under `~/Developer` and registered configured-root
worktrees whose main checkout is there. It resolves symlinks first and records
both worktree and main roots in every configured Claude home and an existing
default config; parent-folder trust never qualifies. It changes only the trust
key, atomically preserving concurrent saves and all other keys/login data.
Missing configured configs refuse; missing default configs are not created.
Other roots must already be trusted in the selected home or refuse for the
Director's interactive review. The launcher never broadens that scope.

Before remote Codex launch, native `daemon start` on each selected home reuses
healthy daemons or brings back stopped ones with a bounded command/socket check.
It never uses bootstrap or reads tokens/changes accounts. Success proves a
connection now, not continued daemon health. Native WebSocket admission probes
an invalid UUID through `thread/resume`, without creating a thread, hooks or
inference. Initialization, admission, optional browser plugin check and draining
inventory share the protocol deadline. Unknown responses/timeouts refuse;
healthy admission never waits on unrelated loaded threads.

Never restart/stop a draining daemon. Complete loaded-thread metadata inventory
is required to report its active count; failed/partial inventory reports unknown
and the method/error, never permission to restart. Wait or explicitly select
another configured Context Panel account. Any separately authorized manual
restart still requires fresh complete inactive-thread proof; dead terminal/pane
state does not prove a remote worker ended. An unfamiliar admission response
needs diagnosis, never a socket-only healthy fallback.

Preparation bounds include worktree trust and resume source reads. A stalled
read reports `preparation_unavailable` with preserved paths/target; inspect it
and any macOS privacy prompt without relaunching, changing permissions or
restarting a server. Actual pending-macOS-prompt interruption remains unqualified.
First-screen observation catches early exit and visible hook-review prompts,
not complete readiness. Failed/unknown observations preserve identities,
receipts and available cause/output; never infer a missing cause or exit proof.
Inspect before retrying. iTerm uses scoped zsh/native termination monitoring;
tmux reads pane state without driving iTerm. Echoed briefs are not startup proof.
Both use noninteractive zsh: use executable paths/explicit exports, not aliases
or interactive directory hooks. Keep raw screens/briefs/account-home diagnostics
private and redact public reports.

Accounts live only in private config, the first `[accounts]` table in
`$CODE_HOME`, then `$CODEX_HOME`, then `~/.code`, under
`skill-data/supervisor.toml`, or `--account-config`. List each provider's
accounts in any order:

```toml
[accounts]
snapshot_command = ["/Applications/Context Panel.app/Contents/MacOS/Context Panel", "--account-snapshot"]

[[accounts.account]]
name = "<local nickname>"
provider = "anthropic"
context_panel_configuration_id = "<configurationID from the snapshot>"  # or context_panel_label
env = { CLAUDE_CONFIG_DIR = "<account config dir>" }

[[accounts.account]]
name = "<existing AGY login nickname>"
provider = "google"
context_panel_label = "<Google label from the snapshot>"  # or context_panel_configuration_id
single_login = true
env = {}
```

Point `snapshot_command` at the installed app executable, with its full path
as one array element and `--account-snapshot` as the next. Invoke it directly;
`open -a` cannot return the reader's JSON and exit status. The reader updates
with the installed app; an old separately built `context-panel-account-snapshot`
may omit rankings. The Supervisor replaces that standalone command in its private config,
then previews each provider through `account_choice.py` before launching. Use the
installed app's actual path if it differs from the example. See Context Panel's
[installed-reader contract](https://github.com/cbusillo/context-panel/blob/main/docs/provider-usage-access.md#agent-readable-account-snapshot).

Private-config migration: delete the `reserve =` lines under `[accounts]`
and in each `[[accounts.account]]`, including any per-account reserve.
Keep the `[accounts]` table, snapshot command and account environment mappings;
remove obsolete comments that prescribe a fallback order.
They are ignored by the launcher for compatibility with existing files; account
order now only helps people read the mapping. Configure Use last and other
capacity choices in Context Panel. The Supervisor owns this private config;
executing agents propose the exact migration on the owning issue and the
Supervisor applies it after source landing. Rewrite any earlier Google
placeholder row using the explicit single-login form above; invalid rows
refuse configuration loading for every provider.

Codex accounts must set `CODEX_HOME` to the account's home. Claude Code
accounts set `CLAUDE_CONFIG_DIR` for an alternate profile or use `env = {}`
for the default profile. A nonempty env must include the provider's variable.
The launcher unsets an inherited
`CLAUDE_CONFIG_DIR` for the default profile; setting it to `~/.claude` selects
an alternate profile, not the default. The helper never reads credentials and
never changes a
login, including the desktop app's.

Google requires the explicit `single_login = true` and `env = {}` form. AGY 1.3.4's
`--help` (checked October 13, 2026) and its [authentication docs](https://antigravity.google/docs/cli/install/)
provide no account/home override; this row identifies the existing AGY login,
without switching or independently verifying it. Configure exactly one Google
row matching the existing login, and confirm that login in the launched AGY
session. Multiple configured Google rows or multiple Google snapshot rows refuse,
as does `--account` for Google. Google follows Context Panel's Use next or
eligible published launch order and writes the same count-only receipts as the
other providers. Output includes `account_binding: existing-agy-login` and empty
`env_keys`, so it does not claim an account was pinned by an environment variable.
Google launch files must invoke `agy` directly and keep `HOME` unchanged; remove
`HOME`, `CODEX_HOME` and `CLAUDE_CONFIG_DIR` settings from the file to use the mapped existing login. A launch
file invoking another harness refuses before tabs or receipts.
If the AGY login changes, reconcile the mapping and Context Panel row before
launching again. No credentials or login settings are read or changed by this
selection path. The Supervisor applies private-config migrations after the
source PR lands, then records the real launch's account and transcript on the
owning issue.

## Finished-session shutdown stages

The shared parser accepts only a final unindented unfenced verdict after fully
closed blocks. Open, mismatched or indented fences/verdicts never qualify.
Recognition supplies a candidate, not authority; use a standalone final
`Safe to exit: yes` and keep every shutdown gate below.

### Native shell-input proof

`iterm_tab.py close --session-id <exact-iterm-id> --ledger <private-ledger.json>`
requires one explicitly Supervisor-owned unsplit launch, exact terminal/TTY,
and matching window/tab coordinates when recorded. Never add Director sessions.
It requires native shell integration to report an editing prompt with an empty
whole command; appearance never substitutes. After final `exec`, first list:
an absent exact terminal needs no surviving-shell proof. Preserve an ended
terminal with no TTY jobs and report the profile setting without changing it.

For a surviving idle Supervisor zsh shell on Chris-Studio, the integration is
`. /Applications/iTerm.app/Contents/Resources/iterm2_shell_integration.zsh`.
Use the installed app/shell path on other hosts. Manually inspect/preserve input
under [shutdown](../SKILL.md#close-finished-sessions-on-every-notice-and-check)
before sending only that command with `send --verified-target`; read back its
native empty prompt. This authorizes bootstrap only, not closure, and changes
neither a personal profile nor `.zshrc`. Uncertain input preserves the tab.
Claude final `exec` uses `exec env` for account-move opt-in; returning launches
contain it in a subshell. The Director's profile governs automatic tab closure.

### Recovery after a host or terminal crash

A killed worker cannot supply its own verdict. A Supervisor or direction session
on its original source host uses termination recovery; a worker that actually
finished with a verdict uses `finish`. Preserve worktrees, branches and dirty files.

Build a private JSON object from the original launch ledger, never another
transcript's token: native `session_id`, `harness`, `pid`, `tty`, `transcript`,
source `host` matching the hostname, original `tmux_socket` and nonce-bearing
`tmux_session_id`, actual timezone-qualified `ended_at`, and `cause`.
`retained_artifacts` names every preserved `worktree` and `branch`, including
other repositories. Supply all known source-account `transcript_roots`, including
resumed/archived copies. Use `github_plan_termination.py --help` for its contract.
Copy original `supervisor_owned`, `window_id`, `tab_id` and `launch_started_at`
when available. Never invent them; missing/mismatched pane identity uses the
stricter path without a native TTY cutoff. Unknown identity/host/transcripts or
terminal evidence refuses. A nonce is identity, not a credential; inspect the
publication scope because attestations/releases include source and retained
paths, while private transcript paths/roots stay local.

The verifier preserves an unterminated final JSONL line byte for byte; it can
ignore it only after complete identity matching and proof activity predates
termination. Interior corruption, complete malformed records or missing identity
refuse. A detached worktree needs rebase/bisect metadata naming its branch or a
HEAD descending from it; original-ledger custody still applies, and a shared
ancestor proves no ownership. A branch without a worktree uses `worktree: null`;
an empty artifacts array is valid only when nothing was created. Fresh inventory
must confirm no claimed-source artifacts were omitted.

Release and readback recheck exact native identity/activity, recorded PID/TTY,
resumed processes, dead pane or absent original server, retained roots/branches
and unchanged record. A replacement server qualifies only if newer than
termination with the original nonce absent; pane-number reuse proves nothing.
A running process, activity within five minutes or after termination, live or
uncertain terminal, changed record or unproved PID reuse refuses. TTY reuse alone
never proves exit. The helper stops no process and removes no terminal. Release
preserves waits, native blockers and retained-work ownership. Keep the original
termination time on retries.

Verified kernel boot time can independently bound crash exit when every account
copy's activity and mtime strictly predate boot and snapshots stay unchanged.
Resumed-client, replacement occupant/harness, terminal, quiet-period and artifact
guards still apply. Missing/earlier boot evidence uses the strict route; ordinary
finish and historical-exit rules are unchanged.
A pending opaque action requires two native passes over every authenticated copy
under source ownership, even without TTY reuse: NotLoaded, empty queue, unchanged
files/generation/action and repeated physical exit checks. Preserve the barrier.
Loaded/queued/unavailable/changed copies or an action at/after boot refuse; stop
no client. Only a verified stopped daemon may use its configured exact-home
`codex app-server daemon start`; never restart running/draining daemons.
The standalone proof has one ten-minute read deadline across scans/native passes;
claim checks pass their remaining deadline through lineage/rechecks, and short
RPC bounds and the three-second idle file budget remain unchanged. An original
retained account may authenticate its canonical rollout under another established
root; read-only proof moves no files and changes no queue-deletion origin rule.
Check native tasks and remote peers yourself: an idle desktop or ID-less picker
may have no transcript. `--confirm-session-ended` attests those unavailable peers.

```sh
uv run skills/github/scripts/github_plan_termination.py --record <private-termination.json> > <attestation.md>
skills/github/scripts/gh-comment issue <canonical-issue> --repo OWNER/REPO < <attestation.md>
uv run skills/github/scripts/gh-plan.py --repo OWNER/REPO release-claim <canonical-issue> --claim-comment <original-claim-comment-id> --evidence-comment <posted-attestation-url> --termination-record <private-termination.json> --role supervisor --session <current-native-id> --confirm-session-ended --dry-run
```

Inspect the generated attestation before posting through automation identity;
use its actual comment URL in the dry run, then repeat without `--dry-run`.
PR-only claims use their canonical PR and its attestation. Include every verified
retained same-repository PR with repeatable `--retained-pr`; normal link,
ownership and commit-activity guards remain.

Successors use [Planning: Claim](../../github/references/cli-reference.md#planning-claim),
including preservation/conflict refresh. `--resume-from` takes the released
original claim comment ID, never branch/release ID, to derive its qualified
authored retained-PR handoff. Claim a new branch before creating its linked
worktree; retained/source branches stay preserved and siblings remain unowned.
Without a retained PR, verify authored release, clean state and live remote head,
record that pushed SHA on the issue/brief, then use an ordinary new branch/claim
and worktree at that SHA, with no `Resume from:`. Preserve dirty/unpushed crash
state through the linked contract first. Recheck artifacts before adoption or
retirement; never automatically delete remote branches. Admission and wait
resolution still apply; release grants no product, runtime or merge authority.

### Release a verified finished session

Normal shutdown uses [Routine check](routine-check.md); exceptional input,
reviews and handoffs follow [shutdown](../SKILL.md#close-finished-sessions-on-every-notice-and-check).
Preserve the original journal on recovery:
definite refusals can recover after prerequisites change, while uncertain
sends/launches/closes are never replayed under another directory. Completed retirement
can recover from the canonical closure with the same generation; every current
claim, handoff, source and disappearance gate still runs. A genuinely
new launch generation has its own evidence.

The pre-exit claim check must be clear with complete coverage. Only after
independently verified actual exit, the maintained finish helper owns closure
and canonical release discovery. Manual recovery previews before apply:

```sh
uv run skills/supervisor/scripts/release_session_claims.py finish --ledger <private-ledger.json> --session-id <native-id> --supervisor-session <supervisor-id> --owner OWNER --confirm-session-ended
```

`--apply` records closure and reports existing releases; finish posts no new
attestation/release and changes no wait, blocker, status or retained artifact.
Require `ok: true` and `needs_reconciliation: false` before guarded close and
confirmed disappearance. Only then remove the unchanged active row under source
ownership; finish retains its full source identity in the canonical log.
Unknown claims/writes stay in the ledger. The canonical
user-wide log/account inventory under `~/.code/skill-data` is independent of
provider homes; alternate logs are private evidence, not successor authority.
Preserve original source, transcript, process/terminal generation, handoff and
actual ended/verdict times. Never invent metadata, move a cutoff forward,
link aliases by worker token or infer exit from log age, silence or a merged PR.

[Release](../scripts/release_session_claims.py) and
[native proof](../../github/scripts/github_plan_native.py) own all process,
queue, execution, schedule/goal, account-copy and chronology checks. Missing,
malformed, changing or newer source facts stay unknown. Use existing mappings,
`--account-config` or explicit transcript roots. Unknown hosts, ID-less clients
and peers still need actual Supervisor verification; the confirm flag attests
current exit. Other owners/Apps never authorize identity switching. Disposable
`--repo` scope never becomes a fleet all-clear. Read-only metadata cannot clear
an action barrier/freeze; never delete leases or restart running/draining daemons.
A stopped daemon may use its exact configured-home native `daemon start` route.

An exited Codex source resumes through guarded `new --resume-session-id` on
its original account. After retirement, verify its exact canonical closed-log
source row and export that unchanged row to private JSON, preserving absolute
native paths and original custody. Re-register it with `iterm_tab.py register
--entry-file <verified-closed-source.json> --ledger <original-ledger.json>`.
Then use the original owned ledger, with no command/brief/browser files:

```sh
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux new --window-id <container-id> --resume-session-id <native-id> --ledger <original-ledger.json> --account-provider openai
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux read --session-id <returned-terminal-id>
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux send --session-id <returned-terminal-id> --text-file <fresh-safe-turn.txt> --verified-target
uv run skills/supervisor/scripts/codex-session.py status --session-id <native-id>
```

Preserve input and inspect the idle prompt before a fresh safe turn. Keep native
`session_id`, transcript and handoff; store the terminal ID only in
`tmux_session_id`/`iterm_session_id`, with this recovery's verified PID/TTY and
generation. Missing launch identity needs `status` and PID/TTY verification,
never old metadata. A partially completed native resume retains its connection
and invalidation; retry that exact resume for metadata, preserving the source.
Keep actual recovery exit times and earlier evidence; uncertain launches keep
their exact pane without replay, account switching or daemon restart.
Retained direct-binding Codex sessions without native daemon proof use the full
manual shutdown procedure, never a skipped native gate. A frozen
predecessor needs a new claimed thread. Stale queued input uses `queue-cancel`
preview on the original owned ledger/source-host termination record, then one
observed ID/full digest apply to **NotLoaded** only. Never unload/archive/drain
or resume to qualify. Cancellation preserves unrelated entries/freezes, proves
no completion/release, and uncertainty requires readback without replay.

#### Finish claim reconciliation

Use [Planning: Claim](../../github/references/cli-reference.md#planning-claim)'s
abandoned-claim path: exact canonical source/related IDs, verified author/native
identity, original ended time, genuine handoff or honestly labeled permitted
ownership context; preview/apply release-claim and rerun finish. After an attempt,
follow `release_recovery_action`, resolve unknown writes and preserve selected
IDs before new evidence. Never rebuild handoffs from shell commands/caches.
Aliases, foreign authors, status-only ownership and failed reads stay unresolved.

Historical backfill preserves every original verified source row and retained
PR, uses existing matching checkouts, and previews before applying the canonical
attestation/release path. Missing historical PID/TTY is an explicit Supervisor
attestation, never invented evidence. Conflicting URLs/times or bad identity
preserve the whole session; malformed whole-ledger structure/duplicate IDs refuse
globally. Only confirmed issue-scoped refusals can leave independent verified
claims proceeding; source uncertainty, unknown writes or retained/source-worktree
peers stop that source. Use backfill's `--closed-log`, `--checkout-root`,
`--confirm-session-ended`, targeted `--session-id` and bounded deadline selectors.
Input inventories are never rewritten; PR handoffs stay owned until their route
succeeds. Original records and timestamps, never output attestations, bind recovery.

If disappearance fails, preserve the journal and reconcile its original close.
Only after proving a definite no-op, use the guarded terminal-close procedure
and rerun closeout with that same evidence; never replay an uncertain close.
Normal finish precedes close; termination recovery precedes interrupted cleanup.
Native close owns nonce/location, PID/TTY, unsplit and shell/dead-pane proof.
Physical pane cleanup does not prove a resumed source ended. Preserve uncertain
closure without force/replay. See [shell-input proof](#native-shell-input-proof).
Service/cache changes wait for their owning Supervisor's normal restart boundary.

## Pending reviews of record

Read this when workers ask the Supervisor to run a review from its terminal.
Use `pending_reviews.py` there to discover and dispatch requests across the
explicit Supervisor-owned repositories in the run's ledger and retained review
journals. It does not alter
the ledger, transcripts, launch files or account configuration. Run it once
without `--apply` to see pending requests, refused bindings and blocked prior launches:

```sh
uv run skills/supervisor/scripts/pending_reviews.py \
  --ledger /absolute/private/run/ledger.json \
  --checkout OWNER/REPO=/absolute/repository \
  --checkout OWNER/OTHER=/absolute/other-repository \
  --evidence-dir /absolute/private/review-evidence
```

Add `--apply` to run the pending reviews serially and post completed nonempty
results.

### Dispatcher timer

While workers post Supervisor review requests, use the Supervisor harness's
built-in recurring timer every three minutes. Each tick launches the command
above with `--apply` as a background job of the Supervisor's own terminal
session, then reads its JSON when the job finishes.
This leaves the Supervisor free to handle notices and checks while reviews run.
Keep the same ledger, checkout mappings
and durable evidence directory on every tick. This is the maintained route;
no private fallback dispatcher is needed.

Reviews run serially and may outlast a tick. A held sweep lock is expected:
skip that tick and let the active pass finish. Reconcile `results[].status`
values `refused` and `blocked`, plus top-level `ledger_errors`, `attention`
and `error`, against the diagnostics below; a timer tick never authorizes
replay of a failed, denied or uncertain action. For `notification_needed`,
check [completion notifications](#completion-notifications) and the dispatcher's
retained notification outcome for that report version.

Record the timer's identity, command, state paths and routed-notification
history in the Supervisor handoff
and recreate it on takeover. Stop it when this run ends.

### Dispatcher state and recovery

Use the same evidence directory for every invocation for this run:
its launch journal prevents replay after interruption, failure or an uncertain
comment write. This directory is durable operational state, not disposable
task evidence. Keep it in a private run-state directory without automatic
expiry, or an appropriate private directory on Retained-Artifacts. Never put
it under the auto-pruned Developer-Artifacts/task-evidence root, a temporary
directory or any cleanup policy that can remove journals while requests remain
open. Keep journals and results through handoffs and until every request is
resolved; a denied/spent request needs its journal for as long as that request
could be discovered again. If state is lost, reconstruct it from retained
evidence and reconcile remote delivery before apply; absence of a journal is
not permission to replay a known failure or denial. The helper never changes
storage roots or silently restores state.

The ledger and evidence directories must be on a local POSIX filesystem that
supports hard links, flock and directory fsync. exFAT and some network mounts
cannot supply these semantics. Unsupported-operation errors refuse before
continuing and name this requirement. Select a supported location before use;
when relocating existing state, preserve its journals/results and reconcile
spent launches first. A permission or authorization denial remains a denial,
not a filesystem fallback. No alternate directory is tried automatically.

A read-only advisory lock on the ledger's containing directory
also serializes passes using that private run directory, even after an atomic
ledger replacement or with different evidence directories. It changes no ledger
or directory contents. Only `--apply` takes these locks; read-only discovery
remains available during an active review and creates no evidence files. A held
apply lock means wait for that sweep, never select another ledger directory to
evade it. Distinct runs use their own private run directories. The evidence lock
also prevents concurrent writes through different ledger paths. During a
Supervisor handoff, pass the same `--evidence-dir` to the successor even if its
ledger moves to another run directory. Keep that directory and its journals;
a new evidence path cannot deduplicate concurrent copies of one session in
different run directories. One evidence directory serves one logical run,
including its Supervisor handoffs. Unrelated runs use distinct private paths,
for example `--evidence-dir /private/run-a/reviews` and
`--evidence-dir /private/run-b/reviews`. Retained history establishes that run's
historical ownership, even after an author leaves its ledger; the lock does
not partition history between unrelated runs.

If unrelated runs already shared a directory, stop their dispatchers before
separating state. Preserve the original private directory unchanged as inactive
recovery evidence. Use the existing ledgers, launch receipts and handoffs to
assign each paid-review family and completion comment to its original run.
Copy that family's journals, prompt/response/result files and every notification
version byte-for-byte into its run's private evidence directory; keep the
original archive so recorded absolute evidence paths remain readable. Verify
that every existing attempted, failed and denied state is present before using
the new `--evidence-dir`. Each family has one active owning run. If ownership is
ambiguous, keep dispatch stopped until the existing records resolve it; an empty
directory is never a recovery route. Record both active and archive paths in the
handoff. No session identity, account, lease or daemon is changed.

The shared GitHub reader owns pagination and
transport retries; the comment helper owns write reconciliation. This helper never retries inference or an uncertain write.
Journals retain `review_failed` for failed/empty inference, `post_failed` for
confirmed non-applied writes, and `post_unknown` for uncertain delivery or
invalid transport metadata, including their errors and structured post outcome
when available. A failed model-review call also retains its structured
`review_failure` metadata; discovery exposes its declared `review_phase` and
`review_outcome`. A selection failure remains spent and does not authorize
retry, a new provider or another identity. Inspect the error before deciding
whether a new request is permitted; a policy/permission denial is never evaded. `post_attempted` records entry into the write call; an interrupted
call is uncertain, never permission to retry. Read a refused outcome and its
journal; a real permission or authorization refusal stops that action. Do not
renew a request to evade a denial. A completed review whose final input check
fails keeps its result and records `post_blocked` with the diagnostic. No write
was attempted. Discovery reads existing journals and reports `blocked`, their
phase and error instead of promising another pending launch. It writes nothing.
Completion and acknowledgement reports also expose an existing `journal`,
`journal_phase`, canonical `evidence`, `response_evidence` and `prompt_evidence`
paths. A weighed note or explicit queue acknowledgement cannot hide an
uncertain write or a paid result retained locally; reconcile it before
deciding how to settle the queue item. These diagnostics do not themselves
prove publication, provider completion or readiness.

For a retained `review_completed` or `post_blocked` result, revalidate the exact
head, clean worktree and original request/prompt scope before manually posting
that result through the existing GitHub helper, with the manual report heading
and head described below. If a newer request exists at the same head, do not
post the superseded scope as an unbound manual report: retain it as historical
evidence and resolve the current scope with the author. Preserve the original
raw receipt when manually reconciling a generated report; its request id and
head cannot settle a different request. Keep the original evidence directory. Its successful
inference metadata and nonempty report can confirm the requested provider on
that manual post without buying another review. Changed inputs require the
author to resolve the scope; never post against guessed bindings. Uncertain
writes still require remote readback and the GitHub helper's existing manual
reconciliation, never automatic replay. Remote receipts let a later sweep skip
confirmed posts without replaying them.

For a finished repository, verify that every retained request is resolved and
its PR is closed before retiring that repository's state from active discovery.
Stop the dispatcher between passes, preserve all of its journal/result families
in a private archive outside the active evidence directory, and record their
original paths and resolution evidence in the run handoff. Restore the complete
families to those original paths before any resumed dispatch of that scope.
Never archive state to retry a spent request. An unavailable repository read
does not prove resolution or authorize archival: keep its exact refusal visible
and resolve coverage through the existing operational/authorization route.

### Worker review request

Read this when writing a worker brief or requesting a Supervisor-run review.
The author posts the request as a PR comment through `gh-pr.py comment
--body-file`, with the full current 40-character head SHA and the exact native
session ID from its branch claim. Keep each field on its own line with no
extra annotations. Link the claimed implementation issue in the PR body so the
dispatcher can bind the author. Include the change's purpose and paths to read
in the prompt; the reviewer reads files itself and is not asked to run tests.

A request comes from a configured automation identity and uses the shared
Supervisor-specific delegation predicate. Generic worker-run review requests
and readiness status notes do not delegate, trigger or supersede this work.
Quoted, fenced and indented examples and HTML comments are not delegations.
Head-bound weighed dispositions are outcomes even when their text repeats the
Supervisor directive; they cannot supersede a real delegation. The dispatcher
also accepts matching abbreviated heads in existing requests. Quoted Supervisor
phrases and incidental prose do not create requests.
Requests are ordered by creation time; editing an older request does not
supersede a newer one. A served or failed request id/head never launches again
because it was edited. Post a new request comment for a new review or scope;
never use that to evade a denial. Use this format for new requests. Copy the
contents without the example's outer fence; a fully quoted or fenced request
is an example and does not delegate work or hold readiness:

```text
Supervisor runs the review.
Head: FULL_CURRENT_SHA
Author session: NATIVE_SESSION_ID

Prompt:
Review the change and the surrounding files. Give a realistic trigger and
severity for each finding, or none. Does this PR add a test that pins a literal,
version, wording or config text? Your access is read-only; read whatever files
you need, and do not modify anything.
```

The author keeps its session and claim open until the completed report is
posted, weighs the findings under [reviews by another model](../../references/model-review.md),
and addresses reproduced defects before calling `gh-pr.py ready` with green
current-head CI. A posted request or raw report is not a weighed review.

A fenced or inline `Prompt:`, `Review prompt:` heading or quoted prompt is
also accepted. Prompt fences close only with the same character and at least
the opening length, so a longer outer fence preserves nested code examples.
An empty or unclosed fence refuses; supply a scope and matching close, or use
a plain Prompt section. Plain heading text may start on the same line after
the colon. Existing direct read-only requests that explicitly say what
to read and check are prompts themselves, regardless of capitalization. A request without an
inline prompt may link one same-repository GitHub issue comment; the helper
follows the chain and refuses missing, untrusted, ambiguous or cyclic links.
A Prompt section containing only comment links also follows the linked prompt;
it never sends a bare URL as the review scope.
It rechecks every prompt comment, the latest request, PR state/head and worktree
before posting. After inference it also rereads the trusted author claims,
bypassing and refreshing the per-sweep claim cache. A released or changed
session/provider binding blocks posting, keeps the completed report and
requires resolving the current author before manual reconciliation. Prompt identity compares the comment id, authored body, edit time
and author login; reactions and endpoint-specific fields do not invalidate it.
Every completed report and its metadata are retained before that final check,
even when a moved head or edited prompt prevents posting. The existing model
review helper owns the review timeout from standing policy; dispatch adds no
outer time limit that could interrupt its cleanup. An unserved obsolete request
still refuses: renew it with the current head and prompt when another review is
needed. A served request instead reports `completed_stale` after the head moves,
with `reviewed_head`, `current_head`, its original completion status/link and
notification fields. Abbreviations resolve only through unique full heads in
trusted evidence. Weigh the completed review at its actual head through the
existing disposition helper to acknowledge it without buying another review.
That historical result cannot satisfy a new request or readiness at the new
head. Never run against a guessed commit or missing prompt.

The authoring session must match a trusted structured branch claim on an
issue recorded for that repository in the ledger or referenced by the PR.
This supports briefs that name a parent before the worker creates its own issue.
Claim history uses the shared effective-release and exact-source release checks;
only a valid same-author, later release ends the original claim. Handoffs do not
leave released sessions competing as live authors. Untrusted, premature or invalid
release evidence preserves ownership. A missing current issue/claim binding still
refuses, rather than silently treating a released historical session as another run.
`Author session:` resolves multiple recorded authors on the same branch;
otherwise exactly one must match. A trusted claim matching the PR branch but
belonging only to sessions outside this ledger reports `not_owned` without
requiring a local checkout or failing the sweep. When a checkout is supplied,
registered branches at this exact head are also checked for this run's claims
before skipping: an owned successor requires explicit `Author session:` and
normal worktree binding, rather than silently becoming `not_owned`. A branch
at another head supplies no ownership evidence. An explicit author matching an
outside-run claim remains `not_owned` despite unrelated same-head branches. Missing or ambiguous ownership
still refuses; it cannot prove another run owns the item. The supported
correction is to link the issue containing the trusted branch claim in the
PR body, and supply `Author session:` when needed to distinguish authors.
A verified outside-run claim then reports `not_owned`, including without a
checkout. For owned work, also pass the repository's `--checkout` mapping.
Do not mark unresolved ownership as covered or guess it is foreign. Overlapping claims
from this and other runs require an explicit matching `Author session:`.
Malformed claims are ignored only when a parseable branch explicitly differs
from the reviewed branches, whether their issue is in the ledger or referenced
by the PR. An unrelated session's malformed record cannot block other branches. A matching/unknown malformed claim or an unavailable
claim read still blocks pending inference. When the selected worktree is on a successor
branch rather than the PR branch, `Author session:` is required. A branch that
happens to share the commit must not become the inferred author or provider. The ledger's `author_provider`, when present, records the actual
provider; otherwise the native `codex`/`claude` harness family records
OpenAI/Anthropic, as enforced by the launcher. The existing review helper
checks the standing policy and selects the other provider.
Worktree matching requires the exact same-repository head, a matching origin
and a clean checkout. Tracked changes and untracked project files refuse;
the same shared predicate as `review_with_model` permits only its untracked
`.model-review-*` scratch files. Existing helper scratch does not trigger a
false dirty refusal, and no worktree contents are deleted or cleaned. It prefers the
PR branch, otherwise requires one candidate or an explicit absolute `Worktree:`
path among the registered candidates. A successor branch at the same commit
is valid; its branch claim can identify the authoring session too.
It never fetches, creates, cleans or changes a worktree.

Confirmed writes, including the GitHub helper's `reconciled_applied` outcome,
report `status: posted`. Later sweeps return `already_completed`, including the
recorded author identity even if posting ended before updating its journal.
`requested_provider_confirmed` requires the matching local journal's successful
other-provider metadata and retained nonempty report at completion or a post
phase. A copied receipt from the shared automation login alone cannot attest
inference. Missing local evidence leaves provider completion unconfirmed.

### Completion notifications

An edit to a served request sets `request_edited_after_review`; the completion
link is preserved and inference never reruns. Weigh the original scope and
record whether a separate request is needed for changed scope. The dispatcher
uses the repository, PR, `completion_id` and `completion_updated_at` to notify each
report version once on `--apply`, including recovered posts. It reads the posted
comment back to bind its version before notifying.
A corrected edit to the same comment can need a fresh
notification; a disposition must follow its latest edit.
`notification_needed` means the result still lacks a later trusted weighed
disposition, not a history of terminal input. The dispatcher records delivery
separately as `notification_status: sent`, `pending` or `blocked`, with
`notification_journal` in the same durable evidence directory. Discovery reads
that history without sending or writing. Older receipts first recover original
author identity from the matching paid journal and all versions of that completion
comment, then attempt the trusted branch-claim binding only without retained
identity. Conflicting or unreadable history blocks delivery rather than rebinding
to a successor. Malformed claims or failed GitHub reads
become `notification_error`, preserving the already-posted completion link.
Delivery history belongs to that posted version. A retained sent or spent outcome
is read before resolving a live recipient, including after ledger retirement or
claim handoff. Historical author/provider metadata remains available from the
receipt and retained journals; it is never replaced by a successor. A delivered
notice neither confirms provider completion nor weighs findings. Paid journals
also keep posts observable after retirement when readback failed before a
notification could be reserved. Sweeps discover repositories from retained
notification filenames and verified paid-journal bindings, even after the last
ledger entry is retired. Legacy paid journals use their retained prompt-source
repository and unchanged request/head journal key. Unreadable paid-journal
scope reports attention instead of silently disappearing. Automatic discovery
still covers open PRs; retain journals for closed items through normal recovery.
Foreign-run completions with no local history retain the ownership exclusion.

Only a unique, Supervisor-owned author entry in this repository's ledger may
receive a notification. Native transcript identity must agree with that entry.
Codex delivery uses `codex-session.py queue` with the ledger's absolute
`codex_home` and native session UUID; its existing managed-source and ownership
checks remain in force. The transcript must be inside that home's live sessions.
Claude delivery uses the existing tmux `Terminal.send` route with the ledger's
launch nonce, coordinates and matched process PID/ancestry. It requires an ended
turn without background tasks and two stable reads of the supported empty
composer: an unindented `❯` line containing only whitespace, with unindented
horizontal rules immediately above and below. Its closing rule must be the last
such rule on screen. The current Claude renderer puts continuation text two
columns inward; a quote or typed marker in a draft cannot become that prompt.
The dispatcher validates only the shared parser's selected line and never
searches for an earlier empty candidate. Cursor spaces and styled whitespace
padding count as empty. Visible typed, suggested, wrapped or unknown input,
missing/indented borders and unsupported composer modes refuse without keys or
clearing. A legacy iTerm Claude
entry reports the unsupported route; there is no automatic terminal migration.
Ledger-relative transcript paths resolve beside the ledger, as for other helpers.

The dispatcher reserves each delivery durably before calling the send helper.
Checks that stop before delivery record `not_sent` with their diagnostic.
Busy turns, background tasks, nonempty prompts and verified transcript read races
report `pending` without adding attention. Unsupported routes, missing home or
terminal metadata and identity/coordinate mismatches report `blocked` with
attention. Both remain certainly unsent: after the underlying condition is
corrected, later apply passes recheck the ledger, native identity and empty prompt.
Independent review warnings remain visible. Discovery never retries.
A permission refusal, a queue call, a possibly sent terminal message or an
interrupted attempt stays spent. Those failures expose `notification_error`,
set `needs_attention` and make the sweep nonzero, preserving the review link.
Read the exact session and reconcile a spent attempt before any manual
notification; never delete its journal to retry or use another route after a
denial. Once sent, ledger maintenance does not require another notice. Corrected
report edits get their own version and retain the original author; an edit cannot
replay or conceal a spent earlier version. A later weighed disposition ends
notification need.
The helper weighs no findings and never calls `ready`.
Posting removes the inherited per-command own-user
opt-in before invoking the GitHub helper. An earlier personal-identity approval
cannot carry into this write; the existing automation App or its refusal remains
the supported route. Its raw receipt cannot serve as the author's
head-bound weighed disposition required by readiness.

A manual report explicitly headed `Review of record, run by the Supervisor`,
bound to this head after the request, is returned as `reported_review`. Its
heading alone does not attest provider completion. Pending/awaiting/waiting/not-run
headings remain status prose. Echoing the original request inside a report cannot
launch another paid review. The latest raw or manual report is the result to
route; an earlier disposition cannot acknowledge a newer corrected report.

Provider-unconfirmed reports, served-request edits and notification binding
failures set `needs_attention`, appear in top-level `attention`, and make the
sweep exit nonzero until a later trusted head-bound weighed disposition
acknowledges that report. It must also follow the request's last edit. Verify
the report and provenance, then post that disposition through
`gh-pr.py comment --reviewed-head FULL_SHA`. Attention clears without creating
provider proof. A weighed disposition after a manual report is reported as
`already_weighed`; the author remains responsible for the provider-specific
review of record and readiness gates.

Without a Supervisor report, weighing an unrelated review is instead
`weighed_without_review` and needs attention. It neither silently drops the
request nor automatically purchases another review. If the Supervisor confirms
this delegation was intentionally already handled, include the exact line
`Supervisor request acknowledged: REQUEST_COMMENT_ID` in a trusted disposition
at that request's head through the same `--reviewed-head` helper. It must follow
the request's last edit. This clears only the queue warning as
`request_acknowledged`, without provider proof, new readiness evidence or
inference. Quoted/fenced examples, another request ID and a different head do
not acknowledge the request. The same rules apply to historical heads;
acknowledging a request at A never satisfies a new request at B.

Shared paths keep their existing authority: the account reader's policy,
capacity snapshot and launch-receipt store, the model review policy and the
GitHub transport's private caches. A refusal there still stops the affected
review.
Its own writable state lives only in the caller's private evidence directory.
Apply requires that directory to have no group/other permissions (0700); a new
directory is created privately. If an existing directory is too open, restrict
that exact directory to 0700 while retaining its journals, then rerun. The
helper never chooses a fresh location to evade a spent launch or denial.
Persistent prompt, report and journal files are created with private permissions
(0600). Journal updates and retained artifacts use synced temporary files and
atomic publication; the first journal reservation remains exclusive. A crash
during replacement preserves the previous complete record.

The exact resolved prompt remains in `<key>.prompt.txt`. Model-review writes
directly to a private `<key>.response.txt`, retained even if metadata parsing
fails or the caller is interrupted during helper exit. This raw candidate is
not completion/provider proof and is never posted without successful metadata
validation; the validated canonical report remains `<key>.md`. A response file
alone cannot authorize replay or publication. Each journal keeps
its source comment bindings (repository, id, authored body, edit time and login).
Use these originals to resolve scope when a linked prompt is edited after
inference. Confirmed/applied transport metadata combined with a failed process
exit is `post_unknown`, never described as confirmed non-applied. An unavailable
evidence directory or held lock blocks apply; no alternate path, account,
credential or terminal is chosen automatically.

Malformed owned ledger entries are excluded from authorization and reported
in `ledger_errors`; every copy of a duplicate session identity is excluded.
Discovery continues for the remaining valid entries, with a nonzero exit to
signal partial coverage. The Supervisor corrects those registry records before
claiming complete coverage. Unparseable JSON or a malformed root collection
cannot establish run scope and stops the sweep; supply a corrected ledger or
a labeled valid derivative in the same private run directory. The helper never
rewrites its input. Repository names and checkout mapping keys compare without
case, and duplicate mappings differing only in case refuse.
