# Supervisor helpers

Read this before rebuilding a ledger, watcher or digest, or sending terminal
input. Run every Python helper with `uv run` from any directory using its catalog
path. The status, question and candidate helpers are read-only. iTerm controls
require macOS, iTerm2's Python API enabled and the session's existing authority;
API connection failure is a stop, not a reason to change personal settings.

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
For the digest, also use `github-work-rollup` for human comments, `github` for
Dependabot/security signals and `infra-ops` for authorized live-site checks.
Include the run's privately recorded capacity deadlines. Post the Director's
questions in one batch with recommendations; use only the messaging authority
already granted for this run.

## Foreign-post notices

```sh
uv run skills/supervisor/scripts/foreign_watch.py --owner OWNER --repo OWNER/REPO --own-login OWN-LOGIN --own-login AUTOMATION-LOGIN --launchplane-login DELIVERY-LOGIN --state <private-watch-state.json> --deadline-seconds 1800
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
A failed, partial or malformed read preserves that repository's watermark,
reports an error and exits for Supervisor reconciliation. Wait until a reported
quota retry time before re-arming; increase the bounded deadline when a historical
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
pause update. Before initialization, identify Client products and shared release
inputs from Launchplane's product records and the current Supervisor handoff.
Mark those repositories coordinated: they stay on the existing product landing
path, including engineering-only work. This list expresses the Supervisor's
delivery holds, never enrollment, branch names or enqueue labels.

```sh
uv run skills/supervisor/scripts/auto_enqueue.py holds --hold-file <private-holds.json> --init --coordinated-repo OWNER/CLIENT-REPO --coordinated-repo OWNER/SHARED-REPO
uv run skills/supervisor/scripts/auto_enqueue.py --owner OWNER --hold-file <private-holds.json> --once
```

Initialization refuses an existing record. Missing or malformed hold state stops
the watcher. Its fields are `paused_repositories` and `coordinated_repositories`
(repository lists), plus `client_releases` (objects with `repository` and a
`ready` or `open` status). Reuse the same record through takeover; reconcile it
with the handoff before rearming. Do not maintain another per-session copy.

Client-product batching remains coordinated as the
[Client Products rules](../SKILL.md#client-products) require. The watcher's
`client_releases` entries are an optional manual pause control, not a required
step for Client releases; leave them empty for normal routing. A ready/open
entry pauses only the named product's own landings, never Launchplane or any
other repository. A coordinated product is already held, so ordinary product
coordination needs no such entry.
During takeover, use the handoff to distinguish deliberate manual pauses from
entries created only for the retired Launchplane hold. Clear only the latter
before rearming, keeping the product coordinated in the same command:
`--release-finished OWNER/CLIENT-REPO --coordinated-repo OWNER/CLIENT-REPO`.
The hold command waits for the current repository's enqueue section before acknowledging the
update. Between repositories, a pause update can win the lock and make the
watcher's next nonblocking acquisition stop safely. Finish the hold update and
reconcile any already emitted enqueue events before rearming the watcher.
After the release finishes, clear that entry; ordinary pauses remain independent.

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
`--once`, `--interval`, and `--deadline-seconds` configure it. Re-arm through the
harness's scheduler, or choose a longer explicit deadline for continuous use.
One failed repository/train read or uncertain write stops the run for reconciliation,
preserving already queued work. Reads use the shared GitHub cache, quota reserve
and retry deadline. No separate retry loop or human-auth fallback is added.

A ready handoff starts with the standalone `Ready for the merge train` line and
includes one `Head: <full SHA>` field matching the PR's current head. The latest
handoff by creation/edit time from its author, or a Supervisor explicitly listed with
`--handoff-login`, wins. The `Engineering ready; waiting for ...` form invalidates
an earlier Ready. Missing/stale heads, drafts, blocking train dispositions,
current-head change requests and unavailable checks do not enqueue. A file
inventory changed during reading skips that PR; other ready members continue.
An unavailable Client review scope holds that PR rather than bypassing acceptance.
Required checks are read from current branch protection and
matched to their configured App. A Client review label, request check, or real
Client test notes (including legacy `## Owner test notes`) additionally requires an accepted current-head decision
read from Launchplane, even when a ready comment claims acceptance. Client prose
is never emitted. Coordinated repositories remain held regardless of acceptance.

The oldest eligible PR anchors a group; additional members share at least one
of its top-level areas. Renames count both source and destination; root files
form one area. `--max-batch-size` defaults to four. Area grouping is a routing
heuristic; Launchplane's combined candidate checks still prove compatibility.
An active candidate, reconciliation, landing, or preexisting queue preserves
that repository's batch. Each write rechecks policy, pauses, train state, current
head and readiness. A behind-base single-member queue awaiting the train's
branch update can still accumulate this watcher's group before any stored
candidate exists; the watcher never updates that branch itself.
If a candidate starts during the group, remaining PRs wait
for another pass. Repeated passes never extend an already queued group.

The helper emits JSONL `batch`, `held`, `busy`, `skipped`, `enqueued`, and
`driver_handoff` events. Busy diagnostics include queue dispositions and labeled
PR numbers when available. A blocked labeled PR deliberately holds the
repository until the Supervisor reconciles it; busy alone never means a healthy
candidate is progressing. Skipped events identify PRs whose head readiness or
file inventory was not proved. The handoff contains the maintained driver's argv and labeled member
numbers. Run that driver using the Supervisor's normal background monitoring,
one driver per train; verify every intended member's eventual landing. The
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

## Launching and nudging

```sh
uv run skills/supervisor/scripts/iterm_tab.py window
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <dedicated-window-id> --command-file <private-launch-file> --account-provider openai
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
Launch files contain the exact brief and model settings already authorized,
without the Discord channels flag.
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
Reader failure refuses; `--account <name>` is the explicit override, but still
needs a snapshot row so its launch receipt uses the correct opaque ID.
Configuration errors and Context Panel's plain reset prompt lines are reported
in launch output. Never apply a reset; Chris does that.

The helper exports the account's `env` ahead of the command, including an agent
started after `cd`. A command that sets or unsets account variables refuses, including through
common shell wrappers. Keep brief text separate from shell settings: quote it
with `shlex.quote`, or use a private brief file and a quoted `$(cat <brief-file>)`
argument for complex text rather than shell-specific ANSI-C quoting.
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
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <launch-file> --account-provider anthropic
uv run skills/supervisor/scripts/iterm_tab.py new --window-id <id> --command-file <first-launch> --command-file <second-launch> --account-provider openai
```

Read the returned account, source and reason before recording the session.
Successful launch output reports environment variable names, without their
private path values. A daemon refusal includes the selected home in its recovery
diagnostic; keep that diagnostic out of public comments.

For Codex's shared app server, launch with `codex --remote unix://`: the empty
Unix endpoint resolves through the selected `CODEX_HOME`, so each account uses
its own daemon. A fixed socket or WebSocket endpoint attaches to that server's
account regardless of the exported home. Keep the per-session `-c` settings:
removing `--remote` while retaining the reasoning override can select embedded
mode instead of the shared daemon (see [the Q152 validation](https://github.com/cbusillo/codex-skills/issues/1613#issuecomment-6102593628)).
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

Accounts live only in private config, the first `[accounts]` table in
`$CODE_HOME`, then `$CODEX_HOME`, then `~/.code`, under
`skill-data/supervisor.toml`, or `--account-config`. List each provider's
accounts in any order:

```toml
[accounts]
snapshot_command = ["<path to ContextPanelAccountSnapshot>"]

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
```

Private-config migration: delete the `reserve =` lines under `[accounts]`
and in each `[[accounts.account]]`, including any per-account reserve.
Keep the `[accounts]` table, snapshot command and account environment mappings;
remove obsolete comments that prescribe a fallback order.
They are ignored by the launcher for compatibility with existing files; account
order now only helps people read the mapping. Configure Use last and other
capacity choices in Context Panel. Document this change for the Director;
do not edit their private config yourself.

Codex accounts must set `CODEX_HOME` to the account's home. Claude Code
accounts set `CLAUDE_CONFIG_DIR` for an alternate profile or use `env = {}`
for the default profile. A nonempty env must include the provider's variable.
The launcher unsets an inherited
`CLAUDE_CONFIG_DIR` for the default profile; setting it to `~/.claude` selects
an alternate profile, not the default. The helper never reads credentials and
never changes a
login, including the desktop app's.

## Finished-session shutdown stages

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
step below (the verified exact-tab route from #1627/#1629 applies once landed).

```sh
uv run skills/supervisor/scripts/close_ttys.py --ledger <private-ledger.json> --session-id <native-id> --verified-handoff --verified-input
uv run skills/supervisor/scripts/close_ttys.py --ledger <private-ledger.json> --session-id <native-id> --verified-handoff --verified-input --apply
```

The two flags attest the Supervisor checked the issue handoff and empty input
(or recorded the Director's text). Default is a dry run. This helper never
answers prompts or sends exit commands: it checks transcript, ownership, exact
iTerm id and TTY, then proves the recorded PID is absent and only `login` and idle login/interactive shell processes
remain on that TTY. Other jobs, including prompt helpers and shell scripts, preserve the tab.
Process arguments are inspected privately and never emitted. Apply rechecks activity and processes and requests
one non-force close. An uncertain inventory or remaining/reused PID preserves
the tab. Read back `iterm_tab.py list` to confirm closure; never force or retry
an uncertain close. Other terminal applications use the same skill procedure
through their supported controls, not this iTerm adapter.
