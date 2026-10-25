# Daily stranded-work sweep

The direction audit's stranded mode is a read-only backstop for work awaiting
its next receiver. It reuses the existing repository inventory, paged audit
reads, structured wait parser, question view and current-head Ready parser.
It never edits GitHub, records an audit turn, clears ownership, closes an issue
or applies a label. Existing enqueue and blocked labels are views to inspect;
no new Supervisor label or hand-maintained state is required.

```sh
uv run skills/direction/scripts/direction_audit.py \
  --stranded-work-only --stranded-owner example-owner \
  --question-queue example-owner/catalog#123 \
  --director-name 'Example Director' --age-hours 2 \
  --max-requests 2000 --deadline-seconds 600 --stranded-format markdown
```

Use `--repo OWNER/REPO` and repeated `--stranded-repo OWNER/REPO` for a narrow
scope. Repository discovery includes archived repositories for closed-trigger checking.
Open records have no age cutoff; closed issues and their full discussions are
selected by the close helper's `plan:reopen-trigger` discovery label without a
history cutoff. The JSON record, not the label, determines the event. Closed promises retain the
recent-history window (30 days by default). Full discussion pages are read, including
questions with no Q number. The request budget and deadline cover the whole
run, reserving the larger of 250 core requests and the shared transport's
bulk-read reserve for other work. Shared transport handles
quota, retries and conditional response caching. Use `GITHUB_READ_CACHE_DIR`
for a private task cache when runtime/account directories must stay untouched.
Repository order rotates by UTC date without storing a second inventory. Cheap
trigger discovery precedes open discussions so that their read budget cannot
starve the event checks. All repositories' open work then precedes recent closed
history, and zero-comment records skip
comment reads. Budget, quota-reserve and deadline stops count unread records
together instead of flooding the digest. Repeated limited runs on one day keep
the same order. The GitHub budget counts logical read calls; shared transport
owns bounded HTTP retry attempts. Scoped Launchplane policy reads are counted
separately and use the same deadline and existing private admin configuration;
no new grant is requested.

Findings include the original evidence link, recorded event age (or explicit
unknown), next actor and review requirement:

- Questions without a matching Director reply or trusted recorded decision. The
  shared `oq.questions` view supplies classification; the shared question
  delivery inventory supplies registration, acknowledgement and resurfacing
  evidence when `--question-queue` is selected and available.
  The digest also omits questions with a later trusted comment headed
  `Q<number> withdrawn by the Supervisor` or `Supervisor answer to Q<number>`.
  These existing records settle the exact question for digest reporting, including
  recovered registrations; they do not become Director decisions. Quoted text,
  ordinary mentions, untrusted authors and replies predating the question do
  not settle it.
- Current-head Ready handoffs still open past the age threshold, with or
  without the enqueue label. Since
  [launchplane#3834](https://github.com/cbusillo/launchplane/pull/3834) the
  train admits a current-head Ready itself, so the label proves neither
  routing nor progress; the finding notes it as evidence. A Ready whose PR now
  conflicts with its base is reported at once, because the train cannot admit
  it until the branch is refreshed and handed off again. Unavailable policy or
  an absent target for the base branch is routing unknown.
- Open `Track:` issues in `OWNER/direction` whose Current Status `Last verified`
  is older than a later milestone-relevant change: a native child closing, or
  a Director comment or recorded `Director decision` on the Track or a direct
  child. A clock time in `Last verified` is read as US Eastern; a bare date
  counts as the end of that day. A Track with no readable `Last verified` is
  reported once any such change exists. The sweep reuses collected child
  discussions and reads an uncollected child's comments only when it changed
  after `Last verified`. Ageing starts at the oldest unrecorded change.
- Open issues with a merged linked PR, aggregated to one candidate per issue
  with the newest explicit completing merge preferred over incidental Refs.
  Completing keywords flag immediately, even after a later status review.
  Incidental Refs flag immediately if status is absent, unknown or no newer
  than the merge. A trusted Current Status or structured wait record updated
  after the merge suppresses that issue for 14 days, including the exact
  boundary: at 14 days it is reported again if still open with no newer
  trusted discussion activity. Later trusted discussion renews the quiet
  period only after a qualifying post-merge status review. Issue activity
  timestamps and labels alone cannot establish a review. Body status is
  dated from its body edit time (creation time when never edited), read
  separately from REST issue activity; unreadable edit evidence is a coverage
  gap and cannot suppress a finding. These findings bypass `--age-hours`.
  Explicit closing scope is distinguished from Refs/partial scope; neither
  authorizes automatic closure. Real waits remain in the evidence for the
  landing reconciler's review.
- A blocked view whose native prerequisites all completed and whose structured
  wait is clear; real open blockers and person waits are preserved.
- Trusted, explicitly dated `Due: <ISO timestamp with timezone>` promises, and
  the existing recorded “Measured after one day” comparison promise. Closure
  of the research issue does not settle its promise. A later linked completion
  from a trusted receiver does.
- Open issues whose own dated wait has passed: a Current Status
  `Parked until:` or `Waiting for:` line, or a structured `ends_when`, that
  starts with a date or whose date follows `until`, `after`, `by`, `before` or
  `due` (a structured `waiting_for` counts only the latter). A structured wait
  record supersedes Current Status prose, and a clear record reports nothing.
  Dates after `since`, or cited records such as "the 2026-10-12 comment", are
  history, not triggers. A date without a time is reached at its start in UTC.
- Operational-register entries, for repositories whose `.github/github.json`
  names one under `docs.openOperationalIssues`. Each `### ` entry with
  `- **Field**:` bullets is read from the default branch. An entry is reported
  when a date in the first clause of its `Next check` has passed, or when the
  first clause of its `Current action` starts with, or says it waits on,
  `owner`, `Director`, the Director's login or a supplied alias. Undated and
  relative triggers ("after about a week") are not ageable and stay quiet. An
  unreadable or missing declared register, or an entry without `Next check`,
  is a coverage gap.
- Closed issues with an authoritative structured reopen trigger whose event has
  fired. The [record and close option](../../github/references/cli-reference.md#structured-reopen-triggers)
  reuse the wait parser's authorship, precedence and example filtering. Dates,
  issue closure, PR merge, published stable releases and GHSA patch information
  are checked; `recurrence` retains exact symptom text for search-before-filing
  and is never machine-checked or indexed. The close option maintains the discovery label;
  use it on an already closed issue to re-publish and index an existing manual
  record and reconcile its normal close metadata. A
  labeled issue without a trusted record is a coverage gap. Fired triggers bypass the aged-work threshold
  and remain in each daily digest while the issue is closed until the receiver retires the handled event with
  `close ISSUE --clear-reopen-trigger "<resolution evidence>"`; this also removes
  the discovery label. Use it after the reopened work closes, or when review
  decides not to reopen. The sweep is
  report-only: the repository agent/Supervisor reviews and reopens. A patch in
  the advisory database does not prove an installed dependency is fixed.
  Missing event facts, unread comments, malformed records, repository failures,
  page caps and exhausted read budgets remain explicit coverage gaps. Archived
  repositories are read too; reopening them would first require unarchiving,
  which this sweep does not do.
- Finished task worktrees left on this host, one finding per repository with
  every worktree in its evidence. A worktree counts when its lock names a
  retire command and its branch is already in the default branch's tracking
  ref, or every PR for it is merged or closed and contains its local head. The
  local check needs no GitHub read; other branches use one PR read each from the
  sweep's budget, and a full page of PRs for one branch is a coverage gap. Only
  worktrees of the sweep's selected repositories or account are scanned. A worktree with no commits of its own is aged from its
  creation, so a session that has only just started stays under the age
  threshold. The root comes from `--worktree-root`, or else from the host's
  configured `hostHelper` root; without one the check is skipped. Each worktree
  is retired with its lock's command, never from the report.
- Open issues whose body Current Status `Next action:` routes the work to the
  Supervisor: the action opens with the capitalized role, makes it the subject
  of a verb, or waits on or hands off to it. A path, skill name or description
  that only mentions it does not count, and an empty field never reads the next
  line. Each issue is aged from the body's edit time, which is read once per such issue from the
  sweep's budget. An unreadable edit time is a coverage gap and the issue is
  still listed with unknown age. The result's `supervisor_next_action_by_repo`
  and a Markdown line give the count per repository. Status kept only in a
  comment is not read for this check.
- Existing train blocked views, aged from their label event, plus structured
  waits naming the Supervisor. Body-only waits have unknown age because
  unrelated issue activity must not reset their clock.
- Structured waits on the Director that no open Director question links, as
  `unasked_director_wait`, oldest first. The comment radar's Director-wait
  classifier decides whether a wait is on the Director now; a wait worded as a
  hold is listed until the Director has spoken on the issue. A question links a
  wait when it is unanswered and on the same open issue, or registered, or on
  another open issue whose comment link is in the wait record. A wait record
  alone never asks the Director. `--owner` identifies a Director whose login
  differs from the repository account.

The collector emits JSON by default. Markdown is a delivery payload, including
coverage warnings even with zero candidates. Exit 0 means complete within the
selected scope, 3 means incomplete coverage; neither means work is approved or
completed. Page caps, budget exhaustion, unreadable records and unavailable
question-delivery views are explicit gaps. Decisions outside selected
repositories/history, promises without a parseable due time, and service-only
train custody are not an all-clear. No production/deploy conclusion is drawn.
Full delivery coverage requires `--question-queue` and the question-delivery
helper from the sibling change; an omitted queue or an older catalog remains
explicitly incomplete. A malformed delivery contract has its own error reason,
distinct from a missing helper. JSON retains registration and receiver receipts.
The selected queue's trusted registration markers recover original discussions
even when outside the repository/history selection. Missing/deleted sources and
untrusted records retain their exact URLs in coverage evidence; budget limits
remain explicit rather than becoming permanent unexplained queue failures.
Queue registration/source recovery runs before the general inventory and closed
history. Only trusted in-scope questions and registered URLs enter its delivery
view. Registered unresolved questions outside the open inventory remain visible;
unread sources are distinguished from missing sources. Records with unread
comments are not evaluated from an older body marker. Unread repositories and
states are grouped in one coverage entry.

Closed promises are limited to `--history-days`: an outstanding promise
on an older, untouched closed issue can be missed. Structured reopen triggers
have no history cutoff, within the same explicit page/request/quota budget.
Repository discovery can only enumerate the selected reader's visible owned
repositories; undiscoverable private repositories are outside that inventory. This is outside selected
coverage, even when that selected scope is complete. Explicit closing keywords
are a review heuristic; the sweep does not replace GitHub's native closing-link
authority or prove that a linked PR completed an issue.

## Daily delivery and maintenance

Use the launchd [daily service](../../supervisor/references/helpers.md#daily-stranded-work-service)
and configured Director Discord destination with `discord_digest.py` to deliver
one short message per morning without a running Supervisor session. The message
shows date and coverage, counts by kind, every Director-waiting item by name,
and the oldest five links for each other kind. It links to the full Markdown
list, posted once through the existing bot comment helper on the pinned digest
tracking issue ([this catalog's archive](https://github.com/cbusillo/codex-skills/issues/2006)).
The sender's `--tracking-issue OWNER/REPO#NUMBER` selects another archive.
The existing installed command needs no reconfiguration after the catalog
update; its default archive is the pinned issue above. No plist, schedule or
webhook change is needed. Deliver an incomplete report
too; preserve the nonzero scan status and coverage gaps instead of suppressing
the digest. Keep routing, repository lists, helper invocation and scheduler
configuration private. Use the catalog sender with the privately configured
digest webhook; do not copy credentials or use a session-bound sleep loop.
Record the actual scheduler registration and receiver receipt on the owning
issue before describing daily delivery as active. Source and fixture success
alone do not establish scheduled delivery.

Update by pulling the landed catalog through its supported path; a digest
uses the audit and question helpers from the same catalog. On a delivery
failure, retain the report and helper outcome for the Supervisor; reconcile an
uncertain send before replay. An identical full-list comment is reused on a
reconciled retry through the bot helper's body deduplication. An archive failure
prevents the Discord send. If the required items cannot fit below 2,000 UTF-16
units even with shortened names, retain the failure for reconciliation rather
than omitting waits or sending several messages.
To stop, boot out only the stranded-work launchd
job and remove its LaunchAgents plist. Restore the previous reviewed configuration to roll back;
The sweep changes no findings state; archived full lists remain as history.

Offline replay: `uv run skills/direction/scripts/test_direction_stranded.py`.
The reduced fixture retains pre-recovery facts from the October 13–15 incidents
and ages them to a replay clock. It is not a current backlog snapshot. The
operational-register fixture is an anonymized register, kept as plain text in
the register's own wording, as it stood when two due entries had gone unraised.
