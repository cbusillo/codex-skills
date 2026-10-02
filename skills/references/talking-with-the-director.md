# Talking With The Director

For the role words Director, Client, and admin, see
[role words](role-words.md).

Use this reference when a skill generates chat output, issue or PR comments,
handoffs, reviews, readiness reports, closeout summaries, or docs snippets.
Keep skill-specific instructions short and link here instead of repeating broad
formatting rules in every `SKILL.md`.

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
- Preserve exact-output requests, runnable commands, machine-readable fields,
  helper-checked decision lines, and `Refs`/closing syntax verbatim;
  apply the linked format to the surrounding prose.
- In chat, report findings in plain words first. Keep skill vocabulary such as turn,
  audit, escalation, deviation, and marker in files unless the Director used it
  first.

## Decisions, Holds, And Work State

- When a decision belongs to the Director, ask a direct question that says what
  is being decided, your recommendation, and what each choice changes. Separate
  what the Director must do by hand from what you will do after the answer.
- Reuse decisions already made. When the Director says a discussed proposal is
  basically approved, act within that approval instead of asking again.
- Describe a hold by the action it stops and the condition that ends it. If
  the Director has not named an end condition, keep that action on hold until
  the Director explicitly lifts it; do not invent a deadline or treat silence as
  approval. Continue independent authorized work. "Don't land yet" holds
  merging, not preparation or validation.
- When parallel research or several agents report during one turn, hold their
  findings and questions and send one consolidated message after they finish
  or need the Director. Before that, give only brief "still running" notes. When
  that message carries many decisions, offer to take them one at a time; once
  the Director agrees, ask one question per message and act on each answer before
  asking the next. That pacing does not replace a complete handoff: a final
  response still lists every decision that remains open.
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

## Links And Evidence

- In chat, use clickable local file links for real local files:
  `[label](/absolute/path/file.ext:line)`. Include a line number when it helps.
- In GitHub comments, avoid local absolute paths. Use repo-relative paths,
  commit URLs, PR URLs, issue URLs, workflow run URLs, or dated comments.
- For images stored in private repositories, use
  `https://github.com/OWNER/REPO/blob/<sha>/<path>?raw=true` for readers with
  repository access, or upload an attachment; bare `raw.githubusercontent.com`
  URLs do not carry the viewer's GitHub session and can return 404.
- Prefer point-in-time evidence for handoffs and closeout: merge commits,
  workflow run URLs, PR numbers, issue comments, landing-plan or deploy record
  ids, and exact dates when relative timing could become stale.
- For Background Review lifecycle wording, use
  `background-review-reporting.md`. Record the target and observation time;
  never turn absence into a terminal claim. If later terminal evidence appears,
  preserve the original statement and add a follow-up.
- Do not paste large logs or long copied source text. Link to the run, file, or
  artifact and quote only the useful lines.

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
