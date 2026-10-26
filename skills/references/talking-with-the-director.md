# Talking With The Director

For the role words Director, Client, and admin, see
[role words](role-words.md).

Use this reference when a skill generates chat output, issue or PR comments,
handoffs, reviews, readiness reports, closeout summaries, or docs snippets.
Keep skill-specific instructions short and link here instead of repeating broad
formatting rules in every `SKILL.md`.

## Plain Language

Write `DIRECTION.md`, direction pull requests, and every question to the
Director or a Client in everyday words: no coined terms, and no architecture
vocabulary where a plain word exists. Every question says in plain words what
changes on yes.

## Carry Context

- Assume the Director has not read the issue, file, or earlier session you just
  read. Introduce an issue, PR, person, or catalog term by what it is and where
  it came from. A number or name never replaces the description.
- Write every numbered issue and PR mention in chat and Markdown GitHub
  bodies/comments for the Director as
  `[repo#N](URL) short title`, for example:
  `[catalog#753](https://github.com/OWNER/catalog/pull/753) runtime checkout`.
  Use the correct `/issues/N` or `/pull/N` URL and put a few words of title
  after the link, including in lists and tables. Repeat the linked form when
  naming the item again, even in notes after a draft; use "it" or "this PR"
  when a full reference is unnecessary.
- For the Director's own repositories, the label may omit the `OWNER/`
  prefix; always keep the repository name. For a repository under another
  account, use
  `[OWNER/repo#N](URL) short title`. Bare `#N` and `PR #N` labels lose context.
- In plain-text titles and commit subjects, use full `OWNER/REPO#N` IDs and
  a short description; put clickable links in the associated Markdown body.
- Every step that sends the Director to a page includes its full clickable
  URL, in questions and handoffs as well as other instructions. This applies
  to Launchplane, GitHub, Context Panel, App Store Connect, and other sites.
  A bare path, a code-formatted route, or "open X in the UI" is insufficient.
  Resolve the address from the configured service or authoritative page link;
  never guess a host. Keep private URLs in destinations allowed to know them.
  If a public thread cannot carry the URL, deliver the linked step through the
  authorized private handoff and point the public thread to that handoff.
- Preserve exact-output requests, runnable commands, machine-readable fields,
  helper-checked decision lines, and `Refs`/closing syntax verbatim;
  apply the linked format to the surrounding prose.
- In chat, report findings in plain words first. Keep skill vocabulary such as turn,
  audit, escalation, deviation, and marker in files unless the Director used it
  first.

## Decisions, Holds, And Work State

- Record a `Director decision:` as the link to the question it answers, when
  one exists, and the Director's exact words, with nothing after the quote.
  When acting on a decision, quote it with its link, not its Q ID alone.
- When a decision belongs to the Director, ask a direct question that says what
  is being decided, your recommendation, and what each choice changes. Separate
  what the Director must do by hand from what you will do after the answer.
- Every new question has a shared-counter Q ID in chat and on GitHub.
  GitHub's `gh-comment`, `gh-pr.py comment`, and `gh-issue` comment paths number
  bodies starting with `Owner question:` or `Director question:` automatically;
  an existing heading ID stays unchanged, including on edits and repeat delivery.
  Write the heading ID as `Q300.` or `Q300 —` so it is distinct from question
  content such as `Q4 budget`. A Q ID mentioned only in the context is not the
  question's own ID.
  A question names people without notifying them: the same helpers refuse a
  question with any `@login` other than the repository owner's unless it is
  written as `<code>@login</code>`, so a quoted draft reply pings no one until
  its approved text is posted on its own.
  The helpers allocate only after GitHub confirms the comment was saved, then
  add the ID through an exact-comment edit. The locked counter stores the
  comment URL and its assignment together. Failed creation consumes no number;
  interrupted numbering reports the saved URL and any assigned ID. Read that
  comment, then repair through `gh-comment ... --edit-comment ID` with its full
  body, rather than creating another comment or allocating another ID. If it
  already has a chat ID, include that same ID in the original heading when
  posting or repairing it; do not allocate another. Delivery refuses to register
  or acknowledge an unnumbered original. Its read view keeps numbered peers
  usable and marks the original as needing repair, without presenting its text.
  For an exact-text personal-identity write, include the ID before asking for
  approval. If this is a new question, allocate it with the chat command below,
  show that numbered text for approval, then post it unchanged. The personal
  write path refuses an unnumbered heading before publishing anything.
  Briefs link this reference instead of repeating numbering instructions.
  In chat, get each new question's short ID from the
  [shared question counter](../direction/scripts/question_id.py):
  `uv run <direction-skill-dir>/scripts/question_id.py next`.
  Both harnesses use the counter in `CODE_HOME` or `~/.code`. For multiple
  machines, use the same existing `[questions].queue` in each machine's
  private `skill-data/supervisor.toml`, in the Director's personal repository
  namespace. Queue configuration alone keeps legacy local numbering; explicit
  `seed` after the migration below activates shared allocation. Cross-machine
  safety begins only after that bootstrap and every machine's join, not on pull. The queue's repository holds an isolated
  `state/director-questions` branch containing only counter state, with no
  workflows or source files. GitHub's file-SHA update rejects competing writes;
  each allocation preserves its request identity for uncertain-write recovery.
  Only numeric IDs and request hashes are published, never cleartext question
  URLs, the local counter file, seed discussions or question text. Hashes provide
  request identity, not secrecy. Keep that runtime state branch
  when cleaning source branches. A recorded shared counter never falls back
  to local numbering after a configuration loss.
  Without a queue, the existing local counter remains available for one machine.
  On first setup, run `seed --seed-from OWNER/REPO#NUMBER`, repeating
  `--seed-from` for current question discussions belonging to this Director.
  To migrate, stop prior allocators and collect this Director's retained counter
  snapshots. Run `seed --include-counter PATH` for each other machine, with
  `--confirm-prior-counters-complete` attesting that all earlier allocators are
  stopped and the current counter plus those snapshots cover every retained
  counter. That explicit bootstrap alone can create shared state, importing the
  highest ID and existing assignments before it is visible. Each joining machine
  runs `seed` to publish its floor before `next`; a catalog pull cannot initialize
  shared state automatically. No machine registry or manual numeric seed is needed.
  Shared allocation requires GitHub and the existing Contents automation access;
  local callers serialize while that request is in progress.
  Missing shared state never resets from a machine's local value. The local
  `counter_commit` records verified Git history. After reconciling pending writes,
  restore only a missing branch with `seed --restore-counter-from COMMIT` and
  `--confirm-prior-counters-complete`, using the verified full counter commit
  and all retained counter snapshots. Restoration never overwrites an existing
  branch. An unknown or incomplete history remains a recovery hold. An uncertain
  seed retains shared intent locally and blocks new IDs until seed is reconciled;
  it cannot silently resume local allocation.
  Only that Director's and its configured automation's question headings and
  explicit historical `Reserved Qx–Qy` lines count, including in organization repos;
  incidental references, quoted IDs and other authors do not advance the seed.
  Existing local IDs stay unchanged. Allocation does not reread old seed sources.
  An ID never changes and is never reused: when you ask the same
  question again, even reworded, keep its ID. Restate the question each time
  you list it, so the Director can answer "Q2 yes" without scrolling back.
  Keep the original GitHub comment link alongside its Q ID.
- When the Director's answer to a multi-option question can be read two ways,
  say back the chosen and rejected options in one line before recording the
  `Director decision:`. Before asking again, or saying no decision exists,
  search `repo:OWNER/direction` and the item's newest Current Status and
  linked issues for a Director decision.
- Once the Director agrees to something, carry it out and land it. Wait for the Director only when they asked you to, or when a stop boundary applies. Never add a "show the Director first" step they didn't ask for. When the Director does ask you to wait, say what is waiting and what ends the wait.
- For a wait the Director asked for, do not invent a deadline or treat silence
  as approval. Continue independent authorized work. "Don't land yet" holds
  merging, not preparation or validation.
- A wait on the Director is asked only by a numbered `Director question:`
  whose link is in the wait's `--evidence` and that is registered in the
  question queue below. `gh-plan.py wait`, `create --waiting-for` and
  `gh-pr.py ready` refuse an unasked one and name the fix
  ([wait records](../github/references/cli-reference.md#planning-claim)). A
  hold the Director set or agreed to links the Director's own comment or its
  `Director decision:` record instead; record a decision made in chat that way
  first. A wait record or a plain comment alone never reaches the Director; the
  Supervisor raises an unlinked wait at its next check.
- When your own question proposes a different method or scope, everything that
  depends on the answer waits, including closing the issue. Continue only
  independent work.
- **Filter by whose decision it is, never by how many there are.** A question
  reaches the Director only when it hits a stop boundary, is a product choice,
  or changes something the Director already agreed. Otherwise the agent decides
  it and records why.
- **What is the Director's goes in a queue, one at a time.** Each item is
  checked against the record first: its premise, and the real incidents it
  cites. It is brought with its context and one recommendation.
- **The Director always sees the queue length** ("1 of 4"). Never tell the
  Director to ignore questions. "Later" keeps an item queued; nothing is
  dropped silently. A handoff links the remaining queued questions and names
  who brings the next one; it does not ask them all at once.
- **Posting is not delivery.** After posting an Owner/Director question, register
  its original link in the existing Supervisor question discussion with
  `uv run <supervisor-skill-dir>/scripts/oq.py ORIGINAL_QUESTION_URL --owner OWNER --decision-author TRUSTED_BOT --queue OWNER/REPO#QUEUE --register`.
  Use the queue discussion named by the brief or current Supervisor handoff;
  when none is recorded, retain the source link in the handoff and explicitly
  name who must receive it. Do not silently claim it reached the Director.
  The helper reads back one registration per source link. Keep the original
  shared-counter Q ID on repeat presentations; registering does not allocate
  another number. The original comment owns the question text.
- At every Supervisor check and takeover, read
  `oq.py --queue OWNER/REPO#QUEUE --owner OWNER --decision-author TRUSTED_BOT`, including source items from
  worker handoffs to catch missed registrations. Bring the oldest applicable
  question under the queue rules above. Only after actually presenting it,
  acknowledge delivery with `--ack ORIGINAL_QUESTION_URL --delivery-url RECEIPT_URL`;
  the receipt is the Director-facing Discord message or a durable GitHub record
  of the presentation. Queue registration itself is not that receipt. A failed
  or unknown send remains unacknowledged and resurfaces at the next check.
  Acknowledgement is delivery, never approval: unanswered questions stay queued,
  including "later". A linked reply is a candidate for the Supervisor to
  reconcile; the queue resolves only an explicit linked Owner/Director decision,
  authored by the Director or a trusted recorder. Read the entire oldest-first
  unresolved queue, including acknowledged items; `resurface` only identifies
  missing delivery receipts. Queue records, not hand-applied labels, own delivery state.
- Before asking the Director to approve an admin path (deploy, promotion,
  recovery, onboarding, or a permission-gated workflow), walk the whole path
  read-only: each step, the identity that runs it, the permission, environment,
  or ruleset it needs, whether that identity holds it today, code-level
  refusals, and earlier failures of the same step. Search existing issues too.
  Report every blocker together, then ask once.
- For a live test that needs the Director's hands (a device tap or a physical
  switch), end the turn with one to three plain numbered actions and say how
  long you will watch. Start watching after the Director replies, and check whether
  a stale reading predates the request before calling the step failed.
- In the final message, say plainly whether you are done, waiting on a named
  person for a named thing, or still working. Say "still working" only when
  work is actually running and you retain responsibility for its follow-through;
  an intention to resume later is not running work. When anything remains,
  state the next action and who takes it.

## Claims And Evidence

- Treat what the Director says, and your own earlier statements, as claims to
  check against GitHub, logs, docs, or provider sources before building on
  them. Say when a repeated claim was never verified.
- Disagree plainly when the evidence contradicts the Director or a reviewer. Do
  not agree just to agree.

## Gaps And Defects

- Handing the Director a script or console snippet to paste into an admin panel
  is a missing-capability signal, not a routine path. If it is the only way,
  say so and file or point to the issue for the missing capability.
- When you hit a defect in a tool, helper, or skill from a repository the
  Director controls, route it to that owning repository rather than only noting
  it where it was hit: with posting authority for that repository, search for
  an existing issue and add to it or file one; without that authority, put the
  proposed issue in your report. Ask before filing in a repository the
  Director does not control. A local workaround note is temporary: link the tracking
  issue once one exists, re-check it at the start of later work, and remove
  the note once the fix lands.

## Shape

- Match the format to the task. Tiny answers can be one paragraph; readiness,
  review, and closeout work can use short labeled sections.
- Lead with what the user needs next: findings for reviews, status for
  readiness, done/remaining/checks for closeout, and decision points for plans.
- Prefer concise Markdown over rigid templates. Avoid boilerplate headings when
  they do not add scanability.
- Keep generated issue and PR text durable: include intent, evidence, current
  state, and the next concrete action when one remains.

## Agent Comments

1. **First line:** what happened, in plain words.
2. **Then:** who acts next and on what, or "nothing needed from you."
3. **Link, don't copy:** anything recorded elsewhere (commits, CI and checks,
   reviews, merge state, other issues) is a link, not a restatement.
4. **Write out only what exists nowhere else, briefly:** results of manual
   checks, the reason for a choice, and a Director or Client decision quoted
   exactly.

Use this shape for handoff, readiness, and closeout comments too. Do not keep
second copies in collapsed "Details" sections. Preserve required machine-readable
markers and exact-output contracts as described under Carry Context.

## Links And Evidence

- In chat, use clickable local file links for real local files:
  `[label](/absolute/path/file.ext:line)`. Include a line number when it helps.
- In GitHub comments, avoid local absolute paths. Use repo-relative paths,
  commit URLs, PR URLs, issue URLs, workflow run URLs, or dated comments.
- For images stored in private repositories, use
  `https://github.com/OWNER/REPO/blob/<sha>/<path>?raw=true` for readers with
  repository access, or upload an attachment; bare `raw.githubusercontent.com`
  URLs do not carry the viewer's GitHub session and can return 404.
- Link point-in-time evidence for handoffs and closeout: commits, workflow
  runs, issue and PR comments, landing plans, and deploy records.
- Link logs and source text at their owning run, file, or artifact.

## GitHub Markdown Writes

- Use helper-backed body-file or stdin paths for multiline Markdown. Avoid
  shell-quoted `\n` strings and unquoted heredocs for GitHub bodies.
- Use the GitHub skill helper guidance for PR bodies, PR comments, issue bodies,
  issue close comments, and review feedback. Those helpers preserve literal
  Markdown and centralize auth/retry behavior.
- For closeout, put recovery-critical handoff content in the owning GitHub issue
  or PR comment. Local handoff files are temporary scratch unless the user asks
  for an offline/private handoff or the file is intentionally committed docs.

## Public Safety

- Before writing public issues, PRs, docs, or summaries, remove private service
  URLs, credential paths, tokens, copied provider payloads, private operational
  context, and machine-specific local paths unless they are explicitly safe and
  necessary.
- Use public-safe placeholders for examples. Keep concrete service URLs and
  credentials in environment variables, private config, or signed-in admin
  surfaces.

## Tone And Density

- Be direct and human. Keep summaries high signal, with enough context to resume
  work without replaying the whole session.
- Do not duplicate the harness prompt or broad style rules inside skill docs.
  Reference this guide when the skill needs formatting behavior.
- Avoid filler status text in durable comments. Future agents need facts,
  evidence, decisions, blockers, and next actions.
- Before sending, scan the whole reply for numbered issue/PR mentions. Link
  each prose mention with its short title, or use a pronoun instead; leave
  literal commands and helper-consumed text unchanged.
