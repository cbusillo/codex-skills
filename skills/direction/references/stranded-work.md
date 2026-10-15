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
scope. Owner discovery excludes archived repositories. Open records have no
age cutoff; recently updated closed records are also read for outstanding
promises (30 days by default). Full discussion pages are read, including
questions with no Q number. The request budget and deadline cover the whole
run, with 250 core requests reserved for other work. Shared transport handles
quota, retries and conditional response caching. Use `GITHUB_READ_CACHE_DIR`
for a private task cache when runtime/account directories must stay untouched.
Repository order rotates by UTC date without storing a second inventory; when
the budget ends, unread records are counted together instead of flooding the
digest. Repeated limited runs on one day keep the same order. The budget counts
logical read calls; shared transport owns bounded HTTP retry attempts.

Findings include the original evidence link, recorded event age (or explicit
unknown), next actor and review requirement:

- Questions without a matching owner reply or trusted recorded decision. The
  shared `oq.questions` view supplies classification; the shared question
  delivery inventory supplies registration, acknowledgement and resurfacing
  evidence when `--question-queue` is selected and available.
- Current-head Ready handoffs without the base branch's enqueue view from the
  existing active train policy helper. Unavailable policy or an absent target
  is routing unknown, never proof that a PR is unrouted or enqueued.
- Open issues with a merged linked PR. Explicit closing scope is distinguished
  from Refs/partial scope; neither authorizes automatic closure. Real waits
  remain in the evidence for the landing reconciler's review.
- A blocked view whose native prerequisites all completed and whose structured
  wait is clear; real open blockers and person waits are preserved.
- Trusted, explicitly dated `Due: <ISO timestamp with timezone>` promises, and
  the existing recorded “Measured after one day” comparison promise. Closure
  of the research issue does not settle its promise. A later linked completion
  from a trusted receiver does.
- Existing train blocked views, aged from their label event, plus structured
  waits naming the Supervisor, Director, owner login or supplied aliases.
  Body-only waits have unknown age because unrelated issue activity must not
  reset their clock. `--owner` identifies a Director whose login differs from
  the repository owner.

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
and ages them to a replay clock. It is not a current backlog snapshot.
