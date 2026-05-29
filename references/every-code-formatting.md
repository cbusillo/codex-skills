# Every Code Formatting Guidance

Use this reference when a skill generates chat output, issue or PR comments,
handoffs, reviews, readiness reports, closeout summaries, or docs snippets.
Keep skill-specific instructions short and link here instead of repeating broad
formatting rules in every `SKILL.md`.

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
- Prefer point-in-time evidence for handoffs and closeout: merge commits,
  workflow run URLs, PR numbers, issue comments, landing-plan or deploy record
  ids, and exact dates when relative timing could become stale.
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
  credentials in environment variables, private config, or signed-in operator
  surfaces.

## Tone And Density

- Be direct and human. Keep summaries high signal, with enough context to resume
  work without replaying the whole session.
- Do not duplicate the harness prompt or broad style rules inside skill docs.
  Reference this guide when the skill needs formatting behavior.
- Avoid filler status text in durable comments. Future agents need facts,
  evidence, decisions, blockers, and next actions.
