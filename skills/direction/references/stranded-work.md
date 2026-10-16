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
Repository order rotates by UTC date without storing a second inventory. All
repositories' open work precedes trigger discovery and recent closed history, and zero-comment records skip
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
- Current-head Ready handoffs without the base branch's enqueue view from the
  existing active train policy helper. Unavailable policy or an absent target
  is routing unknown, never proof that a PR is unrouted or enqueued.
  An aged Ready remains a candidate when its enqueue view is set: a label alone
  cannot prove that a receiver is running or the train is making progress.
- Open issues with a merged linked PR, aggregated to one candidate per issue
  with the newest explicit completing merge preferred over incidental Refs.
  Explicit closing scope is distinguished
  from Refs/partial scope; neither authorizes automatic closure. Real waits
  remain in the evidence for the landing reconciler's review.
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
  and is never machine-checked. The close option maintains the discovery label;
  use it on an already closed issue to index an existing manual record. A
  labeled issue without a trusted record is a coverage gap. Fired triggers bypass the aged-work threshold
  and remain in each daily digest while the issue is closed. The sweep is
  report-only: the repository agent/Supervisor reviews and reopens. A patch in
  the advisory database does not prove an installed dependency is fixed.
  Missing event facts, unread comments, malformed records, repository failures,
  page caps and exhausted read budgets remain explicit coverage gaps. Archived
  repositories are read too; reopening them would first require unarchiving,
  which this sweep does not do.
- Existing train blocked views, aged from their label event, plus structured
  waits naming the Supervisor, Director, Director login or supplied aliases.
  Body-only waits have unknown age because unrelated issue activity must not
  reset their clock. `--owner` identifies a Director whose login differs from
  the repository account.

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

Use the existing Supervisor's daily/morning scheduler and existing Director
Discord destination/helper to run this command and deliver its Markdown once
per day. Deliver an incomplete report too; preserve the nonzero scan status and
coverage gaps instead of suppressing the digest. Keep routing, repository
lists, helper invocation and scheduler configuration private. Do not build a
second Discord client, copy credentials, or use a session-bound sleep loop.
Record the actual scheduler registration and receiver receipt on the owning
issue before describing daily delivery as active. Source and fixture success
alone do not establish scheduled delivery.

Update by pulling the landed catalog through its supported path; a digest
uses the audit and question helpers from the same catalog. On a delivery
failure, retain the report and helper outcome for the Supervisor; reconcile an
uncertain send before replay. To stop, disable only the existing scheduler's
stranded-work entry. Restore its previous command/configuration to roll back;
GitHub state needs no rollback because the sweep changes none.

Offline replay: `uv run skills/direction/scripts/test_direction_stranded.py`.
The reduced fixture retains pre-recovery facts from the October 13–15 incidents
and ages them to a replay clock. It is not a current backlog snapshot. The
operational-register fixture is an anonymized register, kept as plain text in
the register's own wording, as it stood when two due entries had gone unraised.
