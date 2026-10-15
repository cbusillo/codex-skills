# Supervisor helpers

Read this before rebuilding a ledger, watcher or digest, or sending terminal
input. Run every Python helper with `uv run` from any directory using its catalog
path. The status, question and candidate helpers are read-only by default;
the question helper's explicit `--register` and `--ack` write delivery records.
iTerm controls
require macOS, iTerm2's Python API enabled and the session's existing authority;
API connection failure is a stop, not a reason to change personal settings.

The shared launcher passes Claude Code `--settings '{"promptSuggestionEnabled": false}'`
before its brief on either backend, including when the command file has other
settings. A launch-file inline `--settings` object is merged with this override,
preserving its other keys. For `--settings file.json`, set
`"promptSuggestionEnabled": false` in that file first: the launcher checks it
before launch and preserves the file argument, private values and relative
permission-rule base. It never rewrites that file or inlines its contents.
Use literal JSON or a readable file path; relative files resolve under the command's `cd` directory.
Use an absolute path in place of shell-expanded settings paths.
Relative settings files require an absolute `cd` directory; otherwise use an absolute file path.
Claude launches also scope `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false` to the
launch so an inherited override cannot enable suggestions. Remove explicit
assignments of that variable from command files; the launcher supplies it.
Existing sessions may still draw dim suggestions. `read` retains the
plain `screen` and adds `input_line` with the last visible `❯`, `›` or `>`
prompt's text, line index and style (`dim`, `non_dim`, `mixed`, `empty`, or
`unknown`). tmux reads SGR faint attributes with `capture-pane -e`; iTerm reads
cell faint attributes. A single leading inverse cursor cell followed by dim
characters is classified as dim; other mixed styles stay mixed. Missing
prompt/style evidence is unknown. This is a
single visible line, not a full multiline draft or idle-prompt proof; inspect
the full screen and transcript too. `director_input_verified` is always false:
no terminal style establishes authorship or approval. Follow the skill's
Director-input and Ctrl-U checks before clearing or closing.

A send failure after typing reports JSON on stdout, an explicit failure on
stderr, and exits nonzero. `text_sent: true` (or `null` for uncertain text delivery), `input_may_contain_text: true`
and `submission: not_attempted|unknown` preserve the partial outcome. Read
both output and exit status; do not redirect away a refusal and send again.
After verified non-submission, `clear --verified-target` sends Ctrl-U; read
back empty input before another send. An unknown submission needs readback,
not a retry.

## Private ledger

Create a private JSON array from the handoff's actual session identities. Paths
are relative to that ledger file; never commit transcripts, account names,
briefs or operational coordinates. No helper searches accounts or copies auth.
Use `iterm_tab.py list` to record the exact window, session and TTY; titles are
labels, not identity. Match the harness process's PID and transcript yourself. The ledger loader
normalizes iTerm's `/dev/` TTY prefix for `ps` matching.
Exclude the Director's sessions or set `supervisor_owned` to false.

```json
[
  {
    "session_id": "native-thread-or-session-id",
    "harness": "codex",
    "transcript": "sessions/task.jsonl",
    "supervisor_owned": true,
    "repository": "OWNER/REPO",
    "issue": 123,
    "brief": "brief-task.md",
    "iterm_session_id": "exact-iterm-session-id",
    "pid": 12345,
    "tty": "ttys123",
    "handoff_url": "https://github.com/OWNER/REPO/issues/123#issuecomment-456"
  }
]
```

Each native session id is unique. Codex UUIDs have no date-prefix restriction.
Missing/malformed or changing transcripts are errors, never idle/finished
proof. The watcher reports an error for that session and keeps watching the
other sessions; a malformed ledger itself ends the watch. Transcript errors
include malformed nested payload, message, content and identity fields; they
supply no completion proof for that session.
Status output includes the latest response and can be private; keep it
out of public comments. Context counts may be absent and are not a reason to
hand off. For Claude, the count includes cached input.

For tmux entries, replace `iterm_session_id` with `tmux_session_id` from launch
output and record `backend: "tmux"`, `tmux_socket`, `window_id`, `tab_id`, `tty`
and the verified harness `pid`. Keep the native `session_id` and transcript
fields unchanged. The tmux terminal id contains a launch nonce: never replace
it with a pane number or a title. A mixed ledger may hold both backends; use
the matching backend when reading or closing each terminal.

Status and candidates expose `verdict_recognition`: `recognized` for a valid
completed unqualified verdict, `unrecognized` when verdict wording is present
but does not qualify, and `absent` when it is missing. A final verdict may share
its line with `No Director action needed.` or `No Director decision needed.`;
other accompanying prose stays unrecognized. For an ambiguous response, read
the full transcript and handoff and ask the worker for a fresh standalone
unqualified verdict if appropriate. Manual review does not override a refused
parser result or any shutdown verification.

## Checks and morning digest

```sh
uv run skills/supervisor/scripts/fleet_counts.py --owner OWNER
uv run skills/supervisor/scripts/status.py --ledger <private-ledger.json>
uv run skills/supervisor/scripts/codex_idle_watch.py --ledger <private-ledger.json> --deadline-seconds 1800
uv run skills/supervisor/scripts/oq.py OWNER/REPO#123 --owner <Director-login> --decision-author <recording-automation-login>
uv run skills/supervisor/scripts/finished_map.py --ledger <private-ledger.json>
```

Rebuild the ~23-minute check and morning digest using the harness's scheduler.
For each check, read statuses, verify turn-end notices, nudge stalls with exact
facts, gather Director questions and perform the skill's finished-session step.
The watcher exits at its wall-clock bound; re-arm it with the harness's built-in
monitor/scheduler. A new watcher may repeat a notice. `clean: null` means a turn
ended, not that work succeeded; an abort is also a notice. Use Claude Code's
`notify_when_idle` when available, not another watcher.

### Codex provider capacity stops

`status.py` and idle notices report `state: provider_capacity` only for the
recorded Codex `task_complete` capacity error, including a first-turn failure
with no response text. A quote of that error, another provider error, an abort
or new task activity is not a capacity stop.

The Supervisor may enable bounded retries after landing. Add each watched
Codex session's existing absolute `codex_home` to its ledger entry and verify
its native UUID, transcript and `supervisor_owned: true`. The transcript must
be inside that home's live `sessions` directory and its existing daemon socket
must be available. No helper starts a daemon or changes login settings.

```sh
uv run skills/supervisor/scripts/codex_idle_watch.py --ledger <private-ledger.json> --capacity-state <private-capacity-state.json> --resume-capacity --deadline-seconds 1800
uv run skills/supervisor/scripts/status.py --ledger <private-ledger.json> --capacity-state <private-capacity-state.json>
```

Use the same retry-state path in every watcher restart, check and morning
digest; retain it through takeover. Only one watcher may operate on it at a
time. Without `--resume-capacity`, the watcher remains read-only. The exact
thread nudge uses the existing `codex queue` path described under
[launching and nudging](#launching-and-nudging), with the verified home's
explicit Unix endpoint. It queues only `continue` after 1, 3 and 10 minutes
of backoff, at most three attempts per native session across restarts. The
budget never resets automatically after a successful turn or a later capacity
error. Do not rotate/delete the state to retry an exhausted session.

The attempt is durably reserved before sending; transcript readback must show
a new `continue` user message. A crash, command failure or absent readback
reports `capacity_retry.state: reconciliation_required` and permits no more
automatic sends. Inspect the exact thread and queued messages before any
manual intervention; never replay an uncertain send. A changed home or
transcript reports `target_changed` and also stops automatic sends.
After the Supervisor verifies queued messages and the current native thread,
manual nudges use the existing [exact-thread queue procedure](#launching-and-nudging).
Keep automatic retries halted for an uncertain native session and retain its
budget; retire the session from the active ledger through normal closeout when
its work finishes. There is no automatic clearing of uncertain-send evidence.
Checks and the morning digest use the state-aware status command above and
include every `provider_capacity` session, even without automatic retries,
and every `exhausted`, `blocked`, `reconciliation_required` or `target_changed`
retry record. `blocked` names unavailable ownership/identity/home/socket
verification, so a missing ledger field or daemon cannot look like ordinary
backoff forever. Eligible sessions expose `retry_at`; `retry_due` means that
time has arrived. If it persists, verify the bounded watcher is still running
and re-arm it with the same state file under the existing check procedure.
`exhausted` means the session remains at capacity after using its
three attempts; it needs Supervisor attention. These states establish no
completion or safe-to-exit verdict.

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

### Acknowledged question delivery

Use the existing Supervisor discussion as the durable question queue. Workers
register question references there; the Supervisor acknowledges actual
presentation under the shared [question queue rules](../../references/talking-with-the-director.md#decisions-holds-and-work-state).
These commands use the configured automation identity and read back their writes:

```sh
uv run skills/supervisor/scripts/oq.py ORIGINAL_QUESTION_URL --owner OWNER --decision-author TRUSTED_BOT --queue OWNER/CATALOG#QUEUE --register
uv run skills/supervisor/scripts/oq.py --owner OWNER --decision-author TRUSTED_BOT --queue OWNER/CATALOG#QUEUE
uv run skills/supervisor/scripts/oq.py --owner OWNER --decision-author TRUSTED_BOT --queue OWNER/CATALOG#QUEUE --ack ORIGINAL_QUESTION_URL --delivery-url PRESENTATION_RECEIPT_URL
```

Pass each trusted decision/registration author's login with `--decision-author`.
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
registration identity; `question_id` preserves
its existing heading's Q ID from the shared counter, if present. Registrations
store references, not a copy of the question. `director-question:delivery`
first-line JSON records contain `version: 1`, `event: registered|delivered` and
`question_url`; `delivered` also carries `delivery_url`. Only the Director and
explicitly configured trusted authors' records count. Quoted/fenced examples
do not count. No label is delivery state.

The view reports `queue_length`, position, original age, registration link,
delivery receipt and `resurface`. An unacknowledged question is emitted every
check, even after successful registration. A delivery receipt leaves it queued
until an explicit linked Owner/Director decision from the Director or trusted
recorder; `candidate_answers` preserves plain replies for Supervisor reconciliation and
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
Include the run's privately recorded capacity deadlines. Post the Director's
question queue under the shared
[queue rules](../../references/talking-with-the-director.md#decisions-holds-and-work-state);
use only the messaging authority already granted for this run.

## Foreign-post notices

For ongoing runs, use the [persistent services](#persistent-supervisor-services)
below and read their logs during checks. The bounded invocation here remains
available for a one-off check.

```sh
uv run --no-cache skills/supervisor/scripts/foreign_watch.py --owner OWNER --repo OWNER/REPO --own-login OWN-LOGIN --own-login AUTOMATION-LOGIN --launchplane-login DELIVERY-LOGIN --state <private-watch-state.json> --deadline-seconds 1800
```

Repeat `--repo` and login flags for the run's configured scope. Initial startup
begins at the current time; `--since` supplies an explicit first watermark for
a historical catch-up; on an existing state it rewinds the selected repositories
only when earlier than their stored watermark. Unlisted repository state stays
preserved. The host's UTC clock must be correct. Restart with the same state file after a notice or
deadline. `--once` performs one pass. Only one watcher owns each state file.
Run it with the harness's built-in background completion notice so its exit
wakes the Supervisor. During takeover verify the old watcher's recorded process
has ended before re-arming; retain an uncertain process and its unread output.
Concurrent watchers using that state file refuse before reading GitHub. The
helper reads one since-scoped repository issue-comment listing per pass
(including PR timeline comments), paginating through the shared reader within
the wall-clock deadline. It uses the shared bulk quota reserve, identity,
conditional cache and retry policy; no separate quota probe or history scan.
A failed, partial or malformed read preserves that repository's watermark.
The bounded invocation reports an error and exits for Supervisor reconciliation.
With `--persistent`, notices remain in the log and retryable reads recover after
the normal interval or the reported quota retry time, whichever is later.
Authentication, malformed-response and other nonretryable failures still stop.
Wait until a reported quota retry time before a manual restart; increase the bounded deadline when a historical
catch-up cannot finish. Partial pages never advance past a failed read.
Successful repositories
advance to the pass start, with a one-second overlap on their next read.

Foreign authors (including deleted/unknown authors) and configured Launchplane
authors or first-line product-review markers
produce metadata notices marked `untrusted`; comment bodies are omitted. A
marker is a notification hint, never proof of authorship or accepted direction.
A foreign author stays `foreign` even when its comment has a review marker.
Configure known service bots among own logins when their ordinary posts should
not wake the Supervisor; no suffix rule silently excludes unknown posters.
Read the canonical record before acting under existing authority. The watcher
does not launch agents, post messages, grant access or widen briefs. New issue
bodies, inline review comments and Discussions are outside this bounded comment
feed; the existing work rollup still supplies broader discussion coverage.

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
use the same message policy.

When the task authorizes a Supervisor handoff, sessions use the installed GitHub
skill's helper: `uv run <github-skill-dir>/scripts/gh-pr.py --repo OWNER/REPO ready <pr>`
(or `--waiting-for-client`), following the GitHub
[ready handoff contract](../../github/references/cli-reference.md#ready-handoffs).
The producer and watcher share `github/scripts/ready_handoff.py`. The latest
handoff by creation/edit time from its author, or a Supervisor explicitly listed with
`--handoff-login`, wins. The `Engineering ready; waiting for ...` form invalidates
an earlier Ready. Missing/stale heads, drafts, blocking train dispositions,
current-head change requests and unavailable checks do not enqueue. A file
inventory changed during reading skips that PR; other ready members continue.
The shared parser normalizes trailing periods and bold markers on both Ready
and renewed waits, accepts a bold full head SHA, and recognizes current and
legacy nothing-to-test markers at the start of the test-notes section without
requiring particular punctuation.
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

## Persistent Supervisor services

On the Director's Mac, launchd owns four jobs: enqueue, drive, foreign and
throughput. They survive Supervisor session restarts and start again at login.
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
      "argv": ["/absolute/path/to/uv", "run", "--no-cache", "/runtime/catalog/skills/supervisor/scripts/foreign_watch.py", "--owner", "OWNER", "--repo", "OWNER/REPO", "--own-login", "OWNER", "--own-login", "shiny-code-app[bot]", "--launchplane-login", "DELIVERY-LOGIN", "--state", "/private/foreign-state.json", "--persistent"],
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

Repeat for `drive`, `foreign` and `throughput`. To start automatically after a
Mac login, the Supervisor places the reviewed plists in `~/Library/LaunchAgents/`
and bootstraps those paths instead. Session takeovers reuse the loaded jobs;
do not bootstrap another set or re-arm deadline loops. During each check:

```sh
launchctl print "gui/$(id -u)/com.codex-skills.supervisor.enqueue"
tail -n 40 /private/supervisor-services/logs/enqueue.jsonl
tail -n 40 /private/supervisor-services/logs/enqueue.stderr.log
```

Repeat for the other jobs. Their log names are `<service>.jsonl` and
`<service>.stderr.log` under the render output's `logs/`. Read foreign notices
there during checks; `--persistent` keeps watching after notices and retains its watermark.
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

## Launching and nudging

### Opt-in Codex GUI work

For an authorized native-app task, put a single standalone declaration in its
brief, using the exact app bundle ID, for example `GUI task: com.apple.TestFlight`.
Launch that brief alone with `--account-provider openai` and a local `codex -C`
command, without `--remote`. The launcher rejects terminal app targets and adds
the GUI safety instructions to the worker's prompt. Other briefs keep the
existing launch path and create no marketplace links or GUI state.

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

During a real GUI launch only, the launcher validates the default home's current
`~/.codex/.tmp/bundled-marketplaces/openai-bundled` directory and creates the
selected `CODEX_HOME`'s corresponding path as a symlink to it. A matching link
is reused; an occupied path, redirected parent or different link target refuses
without replacement. It also passes the account-relative
`-c marketplaces.openai-bundled.source="<account-home>/.tmp/bundled-marketplaces/openai-bundled"`
override to that Codex process. The link alone does not load the marketplace.
No shared `config.toml`, authentication, permissions or catalog installation is
changed. Tests use fake homes, console evidence and child processes only.

The account's private Launch Receipt and terminal launch output include `gui`:
named app, unlocked preflight, marketplace source/link action, process override,
reservation token and shared state path. Keep these receipts private. The link
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
uv run skills/supervisor/scripts/gui_launch.py release --state <receipt-state-path> --token <receipt-token> --verified-worker-ended
```

The release refuses while a worker wrapper holds the lock or when the token
differs. This is recovery after verified shutdown, never a way to make room for
a second GUI worker. Keep a pending reservation's token from the launch failure
output too; before-terminal failures during preparation release it automatically.

### Terminal launches

```sh
uv run skills/supervisor/scripts/iterm_tab.py window
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <dedicated-window-id> --command-file <private-launch-file> --brief-file <private-brief-file> --account-provider openai
uv run skills/supervisor/scripts/iterm_tab.py list
uv run skills/supervisor/scripts/iterm_tab.py read --session-id <iterm-session-id>
uv run skills/supervisor/scripts/iterm_tab.py send --session-id <iterm-session-id> --text-file <private-message-file> --verified-target
```

Use one dedicated Supervisor window, never the window with the most tabs. The
`new` creates the tab in the background without selecting it or moving keyboard
focus. It waits up to 10 seconds for that exact tab's session, refreshing the
iTerm hierarchy before launching. A session-wait timeout names the tab for
inspection. If creation returns no tab identity, run `list` and inspect first;
the tab may still exist. Do not create another tab or replay the launch without
checking it. `window` still restores the previous tab after creation.
Launch files contain one direct agent invocation and its already authorized
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
Empty, whitespace-only, missing or unreadable briefs
refuse before account selection, daemon startup, tabs or launch receipts.
Tabs in briefs are allowed and sent through bracketed paste. Other terminal
control characters refuse. For a batch, repeat both file options in matching launch order; every brief
is checked before the first launch. Do not put a shell text substitution in the
launch file to load the prompt.
After the catalog's [pinned Chrome server](https://github.com/cbusillo/codex-skills/blob/main/README.md#chrome-across-claude-account-homes)
is installed for the selected Claude home, omit `--chrome` from launch files:
the user MCP entry already supplies those tools regardless of the session account.
Every `new` launch requires
`--account-provider` as below; keep account variables out of launch files.
Use `--account` for an explicit override through the same mapping and receipt
path. Run one agent invocation, without restart loops or commands that continue
after it exits. Read the launched screen once for folder trust or another
blocking prompt. Record the new native thread and exact transcript in the
ledger. Prefer `codex queue` for Codex nudges with the verified thread id and
its configured home; never queue exit commands. The terminal helper suppresses
broadcast input, uses bracketed paste for multiline messages, and sends text
and Return separately. Verify input and prompt
before `--verified-target`, then read back once; do not replay an uncertain send.

### tmux launches

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
paste for multiline text, and a separate Return. Recheck and read back as for
iTerm; `--verified-target` still attests prompt/input inspection.

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
`/quit` for Codex or `/exit` for Claude with `send --verified-target`. Wait at
most 60 seconds, reading `read` for `dead: true` and checking that the matched
agent PID and all processes on its TTY have exited. Preserve a running or
uncertain pane. Record/release native session claims under the existing
finished-session procedure before removing the terminal.

```sh
uv run skills/supervisor/scripts/close_ttys.py --backend tmux --ledger <private-ledger.json> --session-id <native-id> --verified-handoff --verified-input
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux close --session-id <tmux-session-id> --ledger <private-ledger.json>
uv run skills/supervisor/scripts/iterm_tab.py --backend tmux list
```

The preview verifies the native transcript and handoff. Terminal closure
rechecks the owned launch nonce, container, window, server and TTY, rejects
splits or linked windows, and proves that both the recorded PID and the pane
process are absent. It checks twice, then asks tmux to remove only that dead
pane, conditional on its launch nonce and dead state still matching inside
the server. A dead pane has no input consumer; a live shell cannot qualify
from its screen appearance. Read back disappearance once. An uncertain close
is retained for reconciliation rather than retried.

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
A temporary write probe checks receipt storage before any tab is created; it
is removed and never counted as a launch. A probe failure names the error type
and asks for write access to the reader's storage root. Each receipt is written
atomically immediately before the launch command is
submitted, after its tab has a session. A failed or uncertain submit retains its
receipt; inspect the tab before retrying. Receipts use the snapshot command's
`--storage-root`, or the reader's documented App Group default. The launcher
prunes only its own day-old files (identified by the `supervisor-` filename
suffix). A batch failure reports the completed launch identities and the failed tab,
including whether its receipt was written and whether submission was attempted.
List and read these tabs before retrying, rather than replaying the whole batch.

```sh
# Read-only previews; these do not write receipts.
uv run skills/supervisor/scripts/account_choice.py --provider anthropic
uv run skills/supervisor/scripts/account_choice.py --provider openai --count 3
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <launch-file> --brief-file <brief-file> --account-provider anthropic
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <first-launch> --brief-file <first-brief> --command-file <second-launch> --brief-file <second-brief> --account-provider openai
```

Read the returned account, source and reason before recording the session.
Successful launch output reports environment variable names, without their
private path values. A daemon refusal includes the selected home in its recovery
diagnostic; keep that diagnostic out of public comments.

For Codex's shared app server, launch with `codex --remote unix:// -C /absolute/repository`: the empty
Unix endpoint resolves through the selected `CODEX_HOME`, so each account uses
its own daemon. A fixed socket or WebSocket endpoint attaches to that server's
account regardless of the exported home. Keep the per-session `-c` settings:
removing `--remote` while retaining the reasoning override can select embedded
mode instead of the shared daemon (see [the Q152 validation](https://github.com/cbusillo/codex-skills/issues/1613#issuecomment-6102593628)).
`-C` (or `--cd`) must name the brief's repository explicitly: a shell `cd` alone
leaves a remote worker in the daemon's working folder. The launcher requires
exactly one existing literal absolute directory (no `~` or shell variables)
and checks every launch in a batch
before starting any daemon, creating terminals or preparing receipts.
The preflight reads each selected account's `config.toml` `[projects]` entries.
It checks the canonical directory, then its original spelling, then the Git
trust root in the same order. For a registered linked worktree that root is
the main checkout; its trust also covers repository subdirectories. An explicit
directory entry takes precedence even when it has no trust level. The first
matching entry must have `trust_level = "trusted"`. Trusting an unrelated parent
such as `/Users/example/Developer` does not cover a repository root.
An untrusted folder refuses with the selected home and an interactive Codex
command to review its folder-trust prompt, naming the actual trust target.
When an explicit entry already exists with an untrusted or absent trust level,
the refusal instead asks you to review that entry and deliberately change it
yourself if appropriate; launching Codex may not show another prompt.
Review trust yourself before retrying;
the launcher never edits config or supplies trust overrides. An unreadable or
malformed config also refuses without printing its contents. The preflight uses
only these selected-home project entries; it does not use managed, cloud, profile
or CLI trust overrides as evidence. See the official
[directory option](https://developers.openai.com/codex/cli/reference/) and
[project trust setting](https://developers.openai.com/codex/config-reference/).
Before creating any tab or launch receipt, `iterm_tab.py new --account-provider openai`
runs `CODEX_HOME=<selected-home> codex app-server daemon start` for each selected
home, with a 30-second command timeout. Native `start` reuses a healthy running
daemon; it also brings one back after a reboot. It does not use `bootstrap`,
which can stop existing work. After a successful exit, the launcher checks that home's
`app-server-control/app-server-control.sock` with a one-second connection timeout.
If startup fails or the socket is still unavailable, the whole launch refuses,
naming that home, the failure, and its shell-quoted native command for diagnosis.
The launcher does not read tokens or change account configuration. The check
proves the socket accepts connections; it cannot
guarantee that the daemon stays running until the launch command executes.

The launcher then connects through the native Unix WebSocket transport, reads
all loaded threads with `thread/loaded/list` and metadata-only `thread/read`,
and counts active threads, including workers with no terminal. It checks
admission with `thread/resume` using an intentionally invalid native UUID;
the daemon rejects draining admission before validating that UUID. The normal
invalid-session-id response proves admission is open without creating a thread,
running hooks or starting inference. Unknown responses or incomplete reads refuse.
This protocol check is version-sensitive; an unfamiliar response needs diagnosis,
not a restart or a fallback that treats the socket as healthy.

Never restart or stop a draining daemon from the launcher. A refusal reports
the active-thread count when available. Wait for the daemon to finish, or use
Context Panel's account reader and `--account <name>` to choose another configured
account explicitly. Before any separately authorized manual restart, freshly
inspect the daemon's active threads and refuse while one is running or their
state is unknown. Terminal clients and dead tmux panes cannot prove a worker
has ended; a remote Codex thread can continue after its terminal disappears.

Both terminal backends observe the first two seconds after submission. An early
exit, including a failed Codex bootstrap, returns `launch_failed`, retaining the exact
terminal identity, any already-written count-only receipt, and available last
output (tmux also reports available exit status or termination signal). An iTerm profile that immediately closes
the exited session can make its final screen unavailable. Inspect those terminals before retrying;
the launcher never replays a batch automatically. This observation catches early
exit, not full agent readiness or a later failure. iTerm replaces the interactive
shell with the launch command in a scoped zsh process, so an agent exit ends
that exact session even if the launch file omitted `exec`. It uses the native
session termination monitor; tmux reads pane state without driving iTerm. Brief
text and echoed commands are never treated as startup-error evidence.
Both backends use non-interactive zsh. iTerm inherits the exported environment;
tmux forwards the launcher variables documented in [tmux launches](#tmux-launches).
Use executable paths and explicit exports in launch files for tools that need
them; interactive-only aliases, functions and directory-change hooks are not
loaded by that scoped shell. A failed observation returns an unknown dead state,
never proof that the submitted worker exited.
Some tmux servers leave both cause fields empty even for a confirmed dead pane;
the launcher still reports failure with its output, rather than inventing a cause.
Failure screens can include echoed account-home exports and the private brief;
keep the raw launch diagnostic private and redact it before public reporting.

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
name = "<default-profile nickname>"
provider = "anthropic"
context_panel_label = "<default profile label in the snapshot>"
env = {}

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

A final unfenced exit verdict may follow completed backtick or tilde fenced
blocks. A verdict inside an open block does not qualify. Shorter, mismatched,
trailing-text or over-indented closing fences do not close it; list-item
fences close relative to the list's content column. Recognition still supplies
only a candidate; the shutdown procedure owns every verification before terminal input or closure.
A final verdict indented four columns, including a nested verdict bullet, stays
unrecognized. Use a standalone unindented final `Safe to exit: yes` instead.


### Native shell-input proof

`iterm_tab.py close --session-id <exact-iterm-id> --ledger <private-ledger.json>`
requires one matching `supervisor_owned: true` launch-ledger entry with its
`iterm_session_id` and `tty`. Include the `window_id` and `tab_id` from launch
output when available; they are checked too. Director-owned or ambiguous entries
and split tabs refuse. Never add a Director's tab to the Supervisor ledger.

The close command requires iTerm's native shell integration to report an
editing prompt whose entire command is empty. Missing/unknown input refuses;
screen appearance and `--verified-input` are not substitutes for the close
command's native proof. A successful final `exec` normally ends the terminal;
after the exit wait, use `list` and record closure if the exact session is gone.
Native prompt proof is needed only for a surviving idle outer shell, such as
an older returning launch or a failed `exec`. On Chris-Studio's zsh shells,
the standalone integration source command is:

```sh
. /Applications/iTerm.app/Contents/Resources/iterm2_shell_integration.zsh
```

Use the installed integration for the launch shell and actual app location on
other hosts. This changes only that Supervisor shell, never `.zshrc` or an
iTerm profile. Before bootstrapping an already idle Supervisor shell, perform
the shutdown procedure's manual input inspection and Director-text preservation
steps, then send just this source command with `send --verified-target`.
This manual check authorizes only bootstrap, not closure; read back the new
native empty prompt before closing. Preserve the tab if input is uncertain.
If an ended session is retained with no TTY processes, preserve it and report
the profile setting instead of force-closing or changing the Director's setting.

Claude launches with a final simple `exec` use `exec env` to pass the account-move
opt-in only to the agent that replaces the launch shell. Returning
launches keep the opt-in contained in a subshell. The Director's iTerm
profile still decides whether an ended terminal closes automatically.

### Verify and close

`finished_map.py` offers candidates only. Its conservative last-line parser
rejects quoted, negated, conditional and aborted verdicts. The shared parser used
by status, candidates and terminal closure ignores one complete trailing
`<oai-mem-citation>` block when evaluating the verdict, while retaining the full
response in status output. Text after the block or an incomplete block remains
unrecognized. A complete Claude local `/exit` record sequence preserves
the preceding verdict; other commands or new work invalidate it. Read the transcript
and issue handoff yourself under the skill's full shutdown procedure; an
unrecognized format remains manual verification, never permission to close.

Resolve verified close-out prompts one time by the owning skill, preserving
Director text privately. Once the session is idle at its prompt, use
`iterm_tab.py clear --session-id <id> --verified-target` for Ctrl-U, then `read`
to verify the entire input is empty. Send `/exit` or `/quit` with `send --text`
only after that check, then wait a bounded time for the matched process to end.
Update the ledger's handoff URL from the actual issue record.

### Recovery after a host or terminal crash

A session killed without closeout cannot supply its own safe-exit verdict.
For a tmux-hosted worker, use the terminated-session route below, from the original source host, as a
Supervisor or direction session. Leave its worktrees, branches and uncommitted
files in place. `release_session_claims.py finish` remains the route for a
session that actually finished and wrote its verdict.

Copy the exact original session identity from the private launch ledger into
a private JSON object. Preserve its native `session_id`, `harness`, recorded
`pid`, `tty`, and `transcript`; do not infer identity from a token mentioned in
someone else's transcript. Add the recorded source `host` (matching that
host's hostname), original `tmux_socket` name and nonce-bearing
`tmux_session_id` from launch output, actual
timezone-qualified `ended_at` and `cause`. Name every preserved worktree and
branch, including work in other repositories, under `retained_artifacts`.
The record has this shape (values are illustrative):

```json
{
  "session_id": "exact-native-session-id",
  "harness": "codex",
  "pid": 12345,
  "tty": "ttys123",
  "host": "original-host",
  "transcript": "/private/sessions/exact-native-session-id.jsonl",
  "transcript_roots": ["/private/sessions", "/private/archived_sessions"],
  "tmux_socket": "supervisor",
  "tmux_session_id": "tmux:supervisor:0123456789abcdef0123456789abcdef",
  "ended_at": "2026-01-01T20:16:00-05:00",
  "cause": "terminal force-quit killed the original tmux server",
  "retained_artifacts": [
    {"worktree": "/artifacts/worktrees/repo/task", "branch": "work/task"}
  ]
}
```

Supply all known source-account transcript directories on that host, including
resumed and archived transcripts. Unavailable original identity, host,
transcripts, or terminal evidence is a refusal, not proof of termination.
An unterminated partial final JSONL line is retained byte for byte and may be
ignored only after matching the complete native identity records and proving
its file activity predates termination. Interior corruption, complete malformed
records or missing identity still refuse. A detached worktree must have rebase/bisect metadata naming its recorded
branch or a HEAD descending from that branch. This corroborates Git state;
use the original ledger to identify the correct worktree. A shared ancestor
alone cannot prove which session owned a detached checkout. For a current-repository branch with no local
worktree, record `{"branch": "work/task", "worktree": null}`; release verifies
the branch in its fresh repository inventory. Use an empty `retained_artifacts`
array if the worker crashed before creating any branch or worktree; release
checks that no claimed-source artifacts were omitted.

The private input's transcript paths/roots stay local. Generated attestations
and releases contain the requested source identity and retained paths; inspect
that publication scope before posting them. A tmux launch nonce is identity,
not a credential. A busy reused TTY still refuses when current process identity
cannot establish that the source is ended. Resolve that identity rather than
assuming terminal reuse proves inactivity.

Verify native task status and any remote peers yourself too: an idle desktop
thread or an ID-less picker may not yet write a new transcript. The existing
`--confirm-session-ended` attestation includes those unavailable peers.

```sh
uv run skills/github/scripts/github_plan_termination.py --record <private-termination.json> > <attestation.md>
skills/github/scripts/gh-comment issue <canonical-issue> --repo OWNER/REPO < <attestation.md>
uv run skills/github/scripts/gh-plan.py --repo OWNER/REPO release-claim <canonical-issue> --claim-comment <original-claim-comment-id> --evidence-comment <posted-attestation-url> --termination-record <private-termination.json> --role supervisor --session <current-native-id> --confirm-session-ended --dry-run
```

Generate the attestation, inspect it, and post it through the configured
automation identity. Use its exact returned comment URL in `release-claim`.
After inspecting the dry run, repeat without `--dry-run`. For a PR-only claim,
use its canonical PR URL and post the attestation on that PR. Add every
verified retained same-repository PR with repeatable `--retained-pr`; the
ordinary PR link, ownership and commit-activity guards still apply.

The helper freshly checks the recorded PID/TTY and resumed processes, exact
native transcript identity/activity, and the original dead pane or missing
server before release and again on readback. A replacement server qualifies
only when its start time is after termination and the original launch nonce
is absent; reused pane numbers supply no identity evidence. Retained paths
must still be worktree roots with their recorded branches preserved. It refuses a running process,
transcript activity within five minutes, activity after the actual termination,
a live/uncertain terminal, or a changed record. PID reuse refuses too. It never
stops a process or removes a terminal. The release records the retained
artifacts; claim release does not clear waits, native blockers or retained-work
ownership gates. Keep the original termination time across retries.

For the successor, **`--resume-from` takes the original claim comment's numeric
ID after that claim has been released; it never takes a branch**. It is also
distinct from the release comment's ID. When retained PRs need a handoff, pass
the new release comment's numeric ID as `--handoff-comment`. Inspect the
supported route with the helper's help before putting it in a brief:

Without a retained PR, continue the verified preserved branch using only the
released original claim ID:

```sh
uv run skills/github/scripts/gh-plan.py --repo OWNER/REPO claim <canonical-issue> --worker <new-token> --session <successor-native-id> --branch <verified-preserved-branch> --resume-from <original-claim-comment-id> --next-action "Resume the preserved branch after checking its current state"
```

With a retained-PR handoff, claim a **new task branch**, separate from every
retained PR/source branch. The existing retained-handoff route preserves the
original worktrees and PRs; it does not authorize editing their branches.

```sh
uv run skills/github/scripts/gh-plan.py --repo OWNER/REPO claim <canonical-issue> --worker <new-token> --session <successor-native-id> --branch work/<new-task-slug> --resume-from <original-claim-comment-id> --handoff-comment <release-comment-id> --next-action "Continue the prerequisite while preserving the retained PR work"
```

Add the actual admission and existing wait-resolution evidence when required. Read
[Planning: Claim](../../github/references/cli-reference.md#planning-claim)
for retained PR and conflict-refresh rules; a release grants no new product,
runtime, or merge authority. Recheck all preserved artifacts before adopting
them, and preserve uncommitted work. Listing an extra artifact preserves it;
it does not transfer another claim or authorize editing a sibling branch.

### Release a verified finished session

Before closing the verified-finished tab, discover and release its remaining
claims. Read [Planning: Claim](../../github/references/cli-reference.md#planning-claim)
and its Abandoned automation claims section first. The current Supervisor must
verify the transcript handoff and actual process exit, including hosts/peers
the helpers cannot inventory. The helper rechecks the exact native transcript,
unqualified safe verdict and native activity before every release; normal
finish and full closure ledgers also recheck their recorded PID/TTY. It also
checks exact-ID resumed harness processes and matching native transcripts in
all account homes from the existing Supervisor account configuration. Newer
native records, including a resumed session's metadata, invalidate closure.
No credentials or account logins are read or changed. `--account-config` selects
that existing mapping; without a mapping, repeat `--transcript-root` for every
known source-harness transcript directory on this host (Codex sessions and
archived_sessions; Claude projects). Unknown hosts/peers remain the caller's
verified-closure responsibility, never an inference from missing inventory.
Check current native task status as well: ID-less resume/picker commands and an
idle desktop thread may not expose an ID in process arguments or write a new
transcript yet. `--confirm-session-ended` attests that the source is still ended
now, including those cases, not merely that the historical log once said so.
It does not
send exit commands or change issue status, waits, blockers, branches or worktrees.

```sh
uv run skills/supervisor/scripts/release_session_claims.py finish --ledger <private-ledger.json> --closed-log <private-closed.json> --session-id <finished-native-id> --supervisor-session <current-native-id> --owner OWNER --checkout-root <existing-checkout-directory> --confirm-session-ended --verified-handoff
```

Default is a read-only inventory preview of claim IDs, retained PRs and attestation bodies.
PR-only claims appear in `pr_claim_handoffs`, with exact claim IDs, the verified
closure's `attestation_body`, the checkout, and dry-run/apply argv. They stay owned until that
route succeeds; an empty automatic `claims` list does not mean all ownership
was released. Post the supplied attestation on the PR through `gh-comment`,
put its returned URL in the command's evidence slot, then run the supported
`release-claim` dry run and apply from that repository's checkout. The same
source-branch activity, author, closure and peer checks apply. An outstanding PR handoff keeps `needs_reconciliation` true and the command
nonzero. Resolve these handoffs before calling ownership cleanup complete.

It does not assert release preflight success: that check needs a posted
attestation, and apply previews the maintained release helper before releasing.
Repeat with `--apply` after inspecting it. Apply records the newly observed exit
time in a private JSON log, posts/readbacks the required `Closed session` /
`Ended at:` / `Safe to exit: yes` attestation on each canonical issue, then runs
the maintained `release-claim` preview and apply. The original observed exit
time survives retries. The log has the ledger's array schema plus `ended_at`
(timezone-qualified ISO timestamp) and `process_exit_verified: true`; retain
the native transcript path, PID/TTY and exact issue-comment `handoff_url`.
Repeat finish previews use the existing log's original closure time. If a
session truly resumes and closes again, verify its new PID/TTY, transcript and
handoff, then use a fresh `--closed-log` file for that newly observed closure;
preserve the prior log unchanged. A corrected handoff can likewise use a fresh
verified log rather than silently rewriting earlier evidence.
No transcript text or process arguments are posted. Checkouts must already
exist as `<checkout-root>/<repository-name>` with the matching origin; the
release command runs there for that repository's ownership inventory and never
edits the checkout. A missing or mismatched checkout leaves claims untouched.

Discovery pages the automation installation's Director repositories and their
issue comments and issue bodies since the earliest timestamp in the source transcripts,
including closed issues. It reads each matching canonical discussion, honors
existing valid authored releases through the shared ownership classifier, and
names source-branch or handoff-retained PRs. Partial inventory is an error, not
an empty result. Unstructured source claims need an authored exact-ID release;
structured claims and their related ownership follow-ups use `release-claim`.
Hand-written and status-only ownership are explicitly reported instead of
silently dropped. Planning refusals are isolated by issue and session, so
verified independent claims still proceed; `issue_refusals` prevents an all-clear.
Automatic follow-up selection requires an exact native `Session:` field and
ordering after the structured source; a reused worker token alone is not evidence.
The shared bot login alone never identifies a session. Native session coverage
remains explicit in the release receipts; missing peer APIs are covered only
by the Supervisor's actual transcript/closure verification.

For the one-time STALE-CLAIMS backfill, supply either the closed-session ledger
above or the Supervisor's verified inventory directly as `--closed-log`.
Inventory rows name `repo`, `issue`, `claim_comment_id`, `session_id`, original
`ended_at`, `safe_exit_yes: true`, `still_running: false`, and optional
`handoff_url` / `retained_pr_url`. Repeated native sessions are grouped; a bad
row or conflicting end time preserves that session's entire group. Exact
source claim IDs bound the inventory scope, and already-valid releases are
reported. The existing canonical route also includes that source's verified
same-author, exact-session ownership follow-ups; preview lists those related
IDs explicitly. Unlisted structured sources are never added. An active
follow-up without a selected structured source is reported for reconciliation.
The input file is never rewritten. Use the default read-only preview first.

Historical inventory records lack original PID/TTY data. This is an explicit
Supervisor closure attestation, not invented process metadata: the caller must
verify actual inactivity, including ID-less GUI/picker sessions and unknown
hosts, before using `--confirm-session-ended`. The helper locates native
transcripts in configured account homes and archives, verifies exact native
identity and the current safe verdict, and rechecks resume arguments and all
transcript activity against the original end time. Normal finish still requires
the matched PID/TTY and Supervisor-owned ledger entry.

Missing handoffs are discovered from the native final response or recorded
comment-write receipts, then verified against live comment author and dates.
A fallback source-issue or summary comment must name the exact native ID;
shared-bot recency alone never qualifies. The summary thread defaults to
`OWNER/codex-skills#884`; override it with `--handoff-thread OWNER/REPO#NUMBER`.
An interrupted session with no public closeout can use the inventory's original
structured ownership comment as recovery context only when its exact native ID,
author and original timestamp still match. The attestation labels it as an
ownership record with closeout verified separately; it never invents a source
summary or publishes the private transcript. Missing/changed source records
remain reported for reconciliation. Retained PRs in
the inventory are named alongside source-branch and exact handoff PRs. Preview
checks the target checkout remote, native peers, ownership activity, retained
PR identity/links and head dates using the shared canonical guards. It posts
no attestation/release and cannot prove the later write readback; apply must
still run canonical `release-claim` preview/apply from each target checkout.

Use the original actual closure timestamps; neither conversion time nor
attestation posting time substitutes for them. Backfill still requires the
recorded PID to be absent and the transcript unchanged; another job on a reused
historical TTY does not identify the source session. Normal finish checks all
jobs on the matched current TTY. Backfill refuses a harness on that TTY unless
its resume arguments identify a different session; an unidentified harness
needs native-identity verification through the documented manual release route.
Invalid closure records are reported per session while
independent verified sessions proceed. If a record lacks a matched
transcript, unqualified safe verdict, original closure proof or handoff URL,
report it for reconciliation instead of inventing evidence.
Malformed ledger schema or duplicate native IDs refuse the whole log; correct
its format against original evidence before retrying.

```sh
uv run skills/supervisor/scripts/release_session_claims.py backfill --closed-log <private-closed.json> --supervisor-session <current-native-id> --owner OWNER --checkout-root <existing-checkout-directory> --confirm-session-ended --verified-handoff
```

The discovery read deadline defaults to ten minutes; `--deadline-seconds` sets a bounded
longer window for a larger historical inventory. Each repository's open PR list
is reused within a session; exact handoff URLs also identify merged retained PRs.
Per-issue read windows restart after discovery, so release subprocess time does
not consume another session's read budget. PR conversation comments are filtered
out; deleted or inaccessible attestations invalidate their historical releases.
Superseded unmerged or foreign handoff PRs are not retained candidates, while
source-branch PRs still undergo the maintained release safety checks.
Possible PID reuse remains a conservative refusal. Verify the current native
identity, then use the documented evidence-backed `release-claim` route for that
record; never invent a replacement PID or change the original closure timestamp.
The command uses the currently configured automation identity only; claims by
another App or a human are reported and preserved, with no identity fallback.
Use `--session-id` to reconcile one logged session; inspect the default preview,
then add `--apply`. For disposable live trials only, repeat `--repo OWNER/REPO`
to scope discovery; that result reports explicit scope rather than all-repository
coverage. Test only self-created throwaway issues/claims, never real stale claims.
On a refusal or uncertain write, preserve posted attestations/releases and
their returned diagnostics. Only a confirmed issue-scoped dry-run refusal may
continue to independent claims after fresh closure verification. Source-session
uncertainty and uncertain writes stop that session's remaining release steps.
Visible peers in source or retained worktrees also stop the session's releases,
even with a different native ID: a continuation may have acquired a new ID.
The shared release helper owns this classification; it is not inferred from
error wording.
Inspect their readback before recovery. Already valid releases are skipped;
unchanged attestations and releases use the shared transport's body deduplication.
Keep unfinished reconciliation in the ledger. `retained_status_claims` explicitly names Current Status markers that still
need reconciliation or the supported `--resume-from` handoff route. The helper
does not rewrite those fields.
`needs_reconciliation` distinguishes successful ownership releases from a
completely reconciled planning record. Empty repository scope refuses. Output
also explicitly excludes other `--owner` values and automation App identities;
use those repositories' configured evidence-backed release route under existing
posting authority, without changing identity settings or using a personal fallback.
Then perform the guarded tab-close
step below.

```sh
uv run skills/supervisor/scripts/close_ttys.py --ledger <private-ledger.json> --session-id <native-id> --verified-handoff --verified-input
uv run skills/supervisor/scripts/iterm_tab.py close --session-id <exact-iterm-id> --ledger <private-ledger.json>
```

The dry-run helper's two flags attest the Supervisor checked the issue handoff and empty input
(or recorded the Director's text). Default is a dry run. This helper never
answers prompts or sends exit commands: it checks transcript, ownership, exact
iTerm id and TTY, then proves the recorded PID is absent and only `login` and idle login/interactive shell processes
remain on that TTY. Other jobs, including prompt helpers and shell scripts, preserve the tab.
Process arguments are inspected privately and never emitted. Its existing
`--apply` adapter remains available and rechecks transcript activity and processes.
The procedure above uses that dry run for the transcript/handoff gate, then
`iterm_tab.py close` for current process and native input proof; neither command
alone replaces the full shutdown procedure. The close command performs one
non-force close request and has no dry-run mode. An uncertain inventory or remaining/reused PID preserves
the tab. Read back `iterm_tab.py list` to confirm closure; never force or retry
an uncertain close. Other terminal applications use the same skill procedure
through their supported controls, not this iTerm adapter.

Rendered persistent service runners use temporary uv caches. The configured uv
executable also receives `--no-cache` when it launches a long-running service
child. This flag is local to that invocation: short nested helper calls retain
normal caching and avoid repeated dependency downloads. Direct service commands
must use the documented temporary-cache watch launcher when they run a watch.
Apply updated service definitions only at the owning Supervisor's normal
restart boundary; do not interrupt another session to make cache maintenance run.
