# Supervisor brief rules

These are the rules every Supervisor brief carries, and the milestone lead
brief template. They live here so they are reviewed and keep history, not in
a local copy that drifts
([Q587](https://github.com/cbusillo/codex-skills/issues/1043#issuecomment-6300994805)).
Change them only through a PR to this file.

A brief links this file, either as
`https://github.com/OWNER/REPO/blob/main/skills/supervisor/references/brief-rules.md`
or by its absolute path in the catalog checkout, or includes the
[rules block](#rules-every-brief-carries) below unchanged. The launcher
refuses a brief that does neither; see
[terminal launches](helpers.md#terminal-launches). Prefer the link: links
inside an included copy resolve only from this file.

A session reading this from its brief: read this file in full before starting,
and keep it with the brief through compaction.

## Rules every brief carries

<!-- brief-rules:start -->
Rules for this brief (every Supervisor brief carries them):

- On Claude Code, use the Read tool for your own session's `tool-results` files and pass this instruction to subagents you start to avoid sensitive-path prompts from shell reads.
- A command-policy "Run instead" message is the supported route, not a
  refusal: follow it. Correct your own command mistakes and retry. Only a real
  denial (a permission, sandbox or authorization refusal of the action itself)
  means stop that action, report its exact text, and continue other work;
  never redo it another way ([task scope](../../references/execution-scope.md)).
- Keep working through compaction
  ([codex-skills#1064](https://github.com/cbusillo/codex-skills/issues/1064)),
  keeping the brief, the item and your current step.
- OpenAI capacity: if a turn stops on "Selected model is at capacity", the
  Supervisor sends "continue"; just resume
  ([capacity stops](helpers.md#codex-provider-capacity-stops)).
- At a repository's PR cap: count open agent PRs first, excluding train batch
  PRs (titled `Launchplane batch: #...`) and drafts parked on purpose; at about
  8, do not open another ([the Pattern](../SKILL.md#the-pattern), step 1).
  Merge-path and CI-repair fixes are exempt from this cap, and their briefs
  say so.
- Pace (Director, October 18, 2026): never open PRs faster than they can land.
  Finish and hand off what you open before starting anything new.
- Never use Chris's own Chrome (the ChatGPT for Chrome extension, Claude in
  Chrome, or any automation of his browser) unless the brief is a GUI task
  that names it. Browser and page checks run headless (Playwright or the
  repository's own browser harness). Close anything you open
  ([browser preflight](helpers.md#browser-and-computer-use-preflight)).
- Read the item in full first, every comment and linked issue, then re-check
  it against current code before starting (overall DIRECTION.md Issue trial,
  [direction#96](https://github.com/cbusillo/direction/issues/96)). If the
  code or a later comment already settles it, say so on the item and stop.
  Follow the repository's
  [executing loop](../../references/executing-loop.md), opening each step's
  owning SKILL.md before the step.
- Recheck ownership, then claim with `gh-plan.py --repo OWNER/REPO claim` (admission: the kind
  named at the top of the brief) before any worktree. The installed
  `gh-plan.py claim --help` and
  [Planning: Claim](../../github/references/cli-reference.md#planning-claim)
  decide the flags; flags a brief suggests are hints. If the item is claimed,
  already done, or needs a Director decision before any action, say so on the
  item and stop.
- Work in your own linked worktree from
  `/Users/cbusillo/.local/bin/dev-worktree /Users/cbusillo/Developer/<repo> <slug> origin/main`
  ([task worktrees](../../github/references/worktrees.md)). Never edit a
  primary checkout. Commit and push with
  `/Users/cbusillo/Developer/codex-skills/skills/github/scripts/git-commit-as-bot`
  and `git-push-as-bot`, with `--worktree /absolute/task/path` first on both.
  Name your intended task path, never the result of `pwd` or Git discovery
  ([bot helpers](../../github/SKILL.md#helpers-and-identity)). Never
  force-push or use `git stash`.
- PRs land only through the merge train. When a PR is green and reviewed,
  hand off with `uv run /Users/cbusillo/Developer/codex-skills/skills/github/scripts/gh-pr.py --repo OWNER/REPO ready <pr>`
  under the [ready handoff contract](../../github/references/cli-reference.md#ready-handoffs);
  a later push needs a fresh `ready`. A PR that needs the Client's acceptance
  hands off with `--waiting-for-client` ("Engineering ready; waiting for the
  Client's acceptance") until the Client accepts its current head. When the
  brief holds a PR for anyone else before the train (Chris, the Supervisor, a
  landing decision), never post plain `ready`: the train enqueues it within
  minutes ([codex-skills#2428](https://github.com/cbusillo/codex-skills/issues/2428)).
  Post the waiting handoff instead, a `gh-pr.py comment <pr> --body-file <file>`
  whose first line is `Engineering ready; waiting for <named reason>`, and run
  `ready` once the hold ends. A brief that holds a PR names that wait in one
  line. Every PR in a repository that feeds a Client release carries
  a `## Client test notes` section. Launchplane and codex-skills have no
  Client.
- Scope ([approved scope rule](https://github.com/cbusillo/codex-skills/issues/1043#issuecomment-6091977127)):
  small fixes in the same file or area that share the change's reason go in
  the same PR; different behavior, its own risk, security, access or
  authority, Client-visible, or large work gets its own issue (search first).
- New issues
  ([Q586](https://github.com/cbusillo/codex-skills/issues/1043#issuecomment-6300583500)):
  before opening one, search open and closed issues across Chris's
  repositories, and add to the existing issue when one fits. Open a new issue
  only when nothing fits; link it to its parent, and name the next actor and
  the finish line. A slice of ongoing work goes on its parent. Close the issue
  with the PR that finishes it (`Closes #N`).
- Never grant, copy or invent access or credentials, and never switch
  identity or use a workflow to carry a call Launchplane denied. Record a
  denial with its trace on the item and the owning authorization issue.
  Reading is never a stop ([launchplane](../../launchplane/SKILL.md),
  [task scope](../../references/execution-scope.md)).
- Never copy, export or move any license or credential file
  ([codex-skills#2232](https://github.com/cbusillo/codex-skills/issues/2232)).
- Reviews: run your own review of record through the
  [model-review](../../model-review/SKILL.md) skill
  (`review_with_model.py run`), from the provider other than your own (Q267),
  then weigh it and post the disposition with
  `gh-pr.py comment --reviewed-head`. Do not ask the Supervisor to run it. Do
  not run a Google (Gemini) review unless the brief asks for one: its quota is
  scarce (Director, October 16, 2026). If the helper refuses with EPERM or
  another receipt error, stop that action, report its exact text, and only
  then fall back to the [worker review request](helpers.md#worker-review-request)
  ([codex-skills#2069](https://github.com/cbusillo/codex-skills/issues/2069)).
- Weigh findings under
  [Weighing Findings](../../references/model-review.md#weighing-findings)
  (Q486, [codex-skills#2219](https://github.com/cbusillo/codex-skills/issues/2219)):
  fix every reproduced defect, proven by the cheapest check that would catch
  it again; decline only non-defects, with one line each. After two review
  rounds on the same PR, stop patching and redesign with a fresh look.
- Style ([codex-skills#2455](https://github.com/cbusillo/codex-skills/issues/2455)):
  before writing Python, Odoo XML or JavaScript, read
  [python-odoo-style](../../references/python-odoo-style.md) in full, and
  check your diff against it before handoff. CI fails on new comments and
  docstrings outside its exceptions.
- Tests ([Q375](https://github.com/cbusillo/codex-skills/issues/1884#issuecomment-6215129557)):
  run focused tests plus the full test modules for the safety areas you touch
  locally ([gate selection](../../repo-readiness/SKILL.md#gate-selection));
  never run the complete multi-shard suite locally. CI runs it and stays the
  required gate before `ready` and merge.
- Inspection: run it through the
  [jetbrains-inspection](../../jetbrains-inspection/SKILL.md) skill's
  supported route for the repository; the skill owns which backend applies.
  Never open Chris's desktop IDE or his normal PyCharm profile. If no
  supported route can run, record UNKNOWN with the helper's exact reason
  ([gate selection](../../repo-readiness/SKILL.md#gate-selection)); never
  report it as passed.
- When you need Chris, post a comment on the item starting with
  `Owner question:` quoting the exact stop-boundary line from the overall or
  repository DIRECTION.md, then continue with what you can. Only a reply from
  cbusillo or a recorded "Director decision" counts
  ([talking with the Director](../../references/talking-with-the-director.md)).
- Keep the item's Current Status current. When the item is done or handed
  off, close out with [work-closeout](../../work-closeout/SKILL.md) and end
  with a standalone, unqualified final line: Safe to exit: yes
<!-- brief-rules:end -->

## Milestone lead briefs

A milestone lead brief
([Milestone lead trial, codex-skills#2277](https://github.com/cbusillo/codex-skills/issues/2277);
Q515) fills the placeholders in double underscores, then carries the rules
above by link or inclusion. A lead ends its own run with the safe-exit line
from step 7, not after each item.

```text
Brief from the Supervisor (__HANDOFF_RECORD__), __DATE__. Trial token: __TOKEN__. Milestone lead brief (Milestone lead trial, https://github.com/cbusillo/codex-skills/issues/2277; Director decision Q515).

You are the long-running lead for: __TRACK__. Admission for every item you take: milestone (__MILESTONE__), linked into that Track's graph.

How a lead works:
1. Own the Track's progress, not one item. Read the Track issue in full, its native children and their Current Status, and the overall and repository DIRECTION.md. Do not claim the Track issue itself.
2. Take the Track's next actionable item in order: not waiting on a person or event, not natively blocked, not owned by another running session. Currently owned by other sessions, leave alone: __OWNED__. Recheck ownership before each claim. Hold exactly one claim at a time.
3. Flow rule: land or close your open PR before opening another. A PR waiting only on the merge train is landing; you may start reading and planning the next item while it lands, but not open a second PR.
4. Use subagents (the Agent tool) for parallel work under your claim: reading and diagnosis, running tests, your own review of record, drafting comments. Only one subagent or you edits files at a time, in your one task worktree.
5. After each item: post one short progress line on the Track issue (finished, blocked and on whom, next), keep the item's Current Status current, release the claim, then take the next item.
6. People waits: when an item needs a person (Chris, a Client, an outside party), record the wait on the item, post an "Owner question:" there if it is Chris's, and move to the next item. Never wait idle on a person.
7. End a turn at least every two hours with a short progress note, without a safe-exit line, and wait for the Supervisor's steering or continuation. Stop working only when no item on the Track is actionable by an agent or you need the Director; then post a lead handoff on the Track issue (what moved, what waits on whom, next item), close out with work-closeout, and end with a standalone final line: Safe to exit: yes

For work subject to the shared rules' PR cap, instead of opening another PR:
1. Help land what is already open there: fix a failing check, resolve a conflict, or answer review findings on the oldest stuck PR.
2. If nothing open there is agent-fixable, finish your change on its branch, leave it unopened, and move to work in a repository under the cap. Note the held branch in your Track handoff.
3. Report "nothing actionable" only when no repository you may work in has actionable work.

Waiting on CI or the train: do not wait on a background polling loop for CI or a landing. Do other Track work and check `gh-pr.py checks <pr>` yourself between steps; run `ready` the moment checks settle green with a weighed review for the current head.

__SPECIFIC__

Rules for this brief: https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/brief-rules.md
```

On October 19 four leads sat idle for 20 to 40 minutes because their
background polling loops never fired (one had a case-sensitive status match);
that is why leads check CI directly.
