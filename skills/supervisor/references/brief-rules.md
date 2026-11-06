# Supervisor brief rules

These are the rules every Supervisor brief carries, and the milestone lead
and Codex Cloud pass templates. They live here so they are reviewed and keep
history, not in a local copy that drifts
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
  8, do not open another. Fixes that unblock landing or repair failing CI are
  exempt from this cap; their briefs name the repair and exemption.
- Pace (Director, October 18, 2026): never open PRs faster than they can land.
  Finish and hand off what you open before starting anything new.
- Never use the Director's own Chrome (the ChatGPT for Chrome extension, Claude in
  Chrome, or any automation of his browser) unless the brief is a GUI task
  that names it. Browser and page checks run headless (Playwright or the
  repository's own browser harness). Close anything you open
  ([browser preflight](helpers.md#browser-and-computer-use-preflight)).
- If a named-app step needs Computer Use and you were not launched through
  [Opt-in Codex GUI work](helpers.md#opt-in-codex-gui-work), or its tools are
  missing, stop that step and post a Supervisor GUI relaunch request on
  your item, naming the app's bundle ID and the action needed. Do not ask the
  Director to enable or install a plugin. Continue independent work, then
  hand off and close out safely for the replacement worker. App approval,
  Accessibility and Screen Recording permissions remain the Director's.
  If already on that GUI route, report the exact missing-tool state to the
  Supervisor for diagnosis instead of repeating the relaunch.
- Read the item in full first, every comment and linked issue, then re-check
  it against current code before starting (overall DIRECTION.md Issue trial,
  [direction#96](https://github.com/cbusillo/direction/issues/96)). If the
  code or a later comment already settles it, record why and continue independent items.
  At your next review of the item, triage new reports on it or routed to you under
  [planning intake](../../github-plan/SKILL.md#triage-incoming-reports); keep
  acknowledged requests open and surfaced until their next action is done.
  Follow the repository's
  [executing loop](../../references/executing-loop.md), opening each step's
  owning SKILL.md before the step.
- Recheck ownership, then claim with `gh-plan.py --repo OWNER/REPO claim` (admission: the kind
  named in the brief's [admission line](../SKILL.md#the-pattern)) before any worktree. The installed
  `gh-plan.py claim --help` and
  [Planning: Claim](../../github/references/cli-reference.md#planning-claim)
  decide the flags; flags a brief suggests are hints. If the item is claimed,
  already done, or waiting, record why, skip it and continue independent items. A wait
  does not block read-only work that answers it.
- Work in your own linked worktree from
  `<catalog>/skills/github/scripts/dev-worktree /absolute/repository <slug> origin/main`
  ([task worktrees](../../github/references/worktrees.md)), allowing at least
  the documented command time where the harness sets a timeout. Never edit a
  primary checkout. Commit and push with
  `<catalog>/skills/github/scripts/git-commit-as-bot`
  and `git-push-as-bot`, with `--worktree /absolute/task/path` first on both.
  Name your intended task path, never the result of `pwd` or Git discovery
  ([bot helpers](../../github/SKILL.md#helpers-and-identity)). Never
  force-push or use `git stash`.
- Keep task output (review prompts/results, checks, logs and handoffs) under the
  task-evidence path printed by worktree creation, including shell redirections.
  Do not write it inside a worktree or beside worktree folders. Temporary
  model-review input scratch is helper-owned and removed when the run ends.
- Land through the GitHub skill's Merging procedure. Launchplane is optional
  unless this Director's direction or active policy requires its train. A missing
  or unreadable configured route stays unknown. For train-routed PRs, when green and reviewed,
  hand off with `uv run <catalog>/skills/github/scripts/gh-pr.py --repo OWNER/REPO ready <pr>`
  under the [ready handoff contract](../../github/references/cli-reference.md#ready-handoffs);
  a later push needs a fresh `ready`. A PR that needs the Client's acceptance
  hands off with `--waiting-for-client` ("Engineering ready; waiting for the
  Client's acceptance") until the Client accepts its current head. When the
  brief holds a PR for anyone else before the train (the Director, the Supervisor, a
  landing decision), never post plain `ready`: the train enqueues it within
  minutes ([codex-skills#2428](https://github.com/cbusillo/codex-skills/issues/2428)).
  Post the waiting handoff instead, a `gh-pr.py comment <pr> --body-file <file>`
  whose first line is `Engineering ready; waiting for <named reason>`, and run
  `ready` once the hold ends. A brief that holds a PR names that wait in one
  line. Every PR in a repository that feeds a Client release carries
  a `## Client test notes` section. Launchplane and codex-skills have no
  Client.
- Briefs state the goal and observable finish line, not a mechanism to build.
  A named mechanism is a suggestion unless it records a Director decision.
- Scope ([approved scope rule](https://github.com/cbusillo/codex-skills/issues/1043#issuecomment-6091977127)):
  small fixes in the same file or area that share the change's reason go in
  the same PR; different behavior, its own risk, security, access or
  authority, Client-visible, or large work gets its own issue (search first).
- New issues
  ([Q586](https://github.com/cbusillo/codex-skills/issues/1043#issuecomment-6300583500)):
  before opening one, search open and closed issues across the Director's
  repositories, and add to the existing issue when one fits. Open a new issue
  only when nothing fits; link it to its parent, and name the next actor and
  the finish line. A slice of ongoing work goes on its parent. Close the issue
  with the PR that finishes it (`Closes #N`); a partial PR keeps the linked
  issue open with its next actor and action
  ([ready handoff](../../github/references/cli-reference.md#ready-handoffs)).
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
  `gh-pr.py comment --reviewed-head`. Do not ask the Supervisor to run it. If
  the helper refuses with EPERM or
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
  check your diff against it before handoff. CI fails on new comments outside
  its exceptions; review whether a docstring is rendered by a tool.
- Tests ([Q375](https://github.com/cbusillo/codex-skills/issues/1884#issuecomment-6215129557)):
  run focused tests plus the full test modules for the safety areas you touch
  locally ([gate selection](../../repo-readiness/SKILL.md#gate-selection));
  never run the complete multi-shard suite locally. CI runs it; Ready and merge
  evidence follow the [test command contract](../../github/references/config-schema.md#workflow-metadata).
- Inspection: run it through the
  [jetbrains-inspection](../../jetbrains-inspection/SKILL.md) skill's
  supported route for the repository; the skill owns which backend applies.
  Never open the Director's desktop IDE or their normal PyCharm profile. If no
  supported route can run, record UNKNOWN with the helper's exact reason
  ([gate selection](../../repo-readiness/SKILL.md#gate-selection)); never
  report it as passed.
- Reply to the product's own Client, team and agents under the
  [Client-reply rule](../../references/talking-with-the-director.md#client-replies),
  using its guarded comment flow. While owning a PR, read new comments at
  each work check and before handoff through the existing watcher or radar.
- When you need the Director, create every new Director question on the item with
  [`oq.py create`](../../references/talking-with-the-director.md#director-questions),
  rather than authoring a prose question comment. Re-present existing questions
  with their original Q and source link; do not create them again. Leave presentation
  to the Supervisor; never use a
  native question UI (`request_user_input`, `request_user_input_async` or
  `AskUserQuestion`) in a Supervisor-launched worker. Use one short line in
  `basis` saying why this needs the Director, without pasting the stop boundary,
  then continue with what you can. Write no Q number
  yourself: the comment helpers number it from the
  [shared counter](../../references/talking-with-the-director.md#decisions-holds-and-work-state)
  and keep any number already there. Only a reply from
  this Director or a recorded "Director decision" counts
  ([talking with the Director](../../references/talking-with-the-director.md)).
- Post progress and run results only on your own child issue. Track parents hold
  proof, decisions and their finish line; do not copy per-slice progress,
  acknowledgements or heavy-window requests there. Keep private logs and data
  in local evidence, linking only a public-safe result on the child.
- For a local heavy Docker run, use the single
  [heavy-run entrypoint](heavy-runs.md). It queues the run and owns capped
  containers through verified cleanup; do not request or grant a window in
  comments. Existing data, review and production boundaries still apply.
- Keep the item's Current Status current. When the item is done or handed
  off, close out with [work-closeout](../../work-closeout/SKILL.md) and end
  with a standalone, unqualified final line: Safe to exit: yes
- Unless your brief delegates worker launches as a milestone lead, request a
  parallel session through the child's Current Status, updated
  with `gh-plan.py update-section`: `Next actor: agent:<launching-lead-native-id>`
  when a lead launched you (its id is in your brief), otherwise
  `Next actor: agent:supervisor`, its concrete launch action, and `Brief:`
  followed by the bounded brief in a fenced `text`
  block. Use `###` or plain labels inside it, since `##` starts a plan section,
  and a longer fence if it contains fences. That lead or the Supervisor launches it
  [verbatim under the standard header](helpers.md#terminal-launches).
  When a lead launched you, notify it through its verified native delivery route
  with the issue URL; an automation-authored status edit does not wake the foreign watch.
  Never post staffing lists or STAFF NOW on the Track board.
<!-- brief-rules:end -->

## Milestone lead briefs

A lead is a mini Supervisor for one of the Director's listed `DIRECTION.md`
milestones: it holds decisions, status and worker assignments, and does no
slices itself. This is the
[all-leads pilot](https://github.com/cbusillo/direction/issues/163#issuecomment-6635104014);
the first weekly direction audit at least one full week after this brief lands
judges it, and the Director keeps, changes or drops it. Either harness works;
Claude leads use tmux for guarded notifications, and the pilot starts on Codex.
A Track and its goal board are optional views.
When several leads share a repository, spread work within its shared PR cap.

Fill the placeholders below and carry the shared rules by link or inclusion.

```text
Brief from the Supervisor (__HANDOFF_RECORD__), __DATE__. Trial token: __TOKEN__. Milestone lead brief.

You are the long-running lead for: __MILESTONE__.
Scope: __REPOSITORIES__
Admission: milestone (__MILESTONE__)
Worker ledger: __SHARED_LEDGER__
Worker cap: __WORKER_CAP__ concurrent workers, recorded on your lead ledger row.
Allowed accounts: __ACCOUNTS__

How a lead works:
1. Read the Director's overall and repository DIRECTION.md, the milestone's finish line, and gh-plan.py next. If a Track is supplied, read it and its native children too; do not claim the Track itself. Take only work belonging to this milestone. Keep a current forecast date in the milestone's due date field once the Director has seen it, with each date and its reasons on your Track when supplied, and leave the field blank while the milestone waits on a person or event.
2. Brief workers for the next actionable items in that order: not waiting on a person or event, not natively blocked, not owned by another running session. Recheck ownership before dispatch; workers claim their own issues and use their own task worktrees. Choose the tier each slice needs under Q1199, rather than giving every slice the strongest tier.
3. Launch through the existing iterm_tab.py launcher on either terminal backend, into the shared ledger, with --lead-session-id <your-native-session-id> and an explicit --account from the allowed accounts. The launcher enforces your assigned cap across repositories and batches. Never raise the cap or use another account yourself. Put your native id and the native delivery pointers from the shared launch reference in each worker brief and use the shared brief rules. Handle worker-originated requests with --brief-issue on their child issues; do not send staffing requests to the Supervisor for launches within your assignment.
4. Watch and review workers through handoff comments, issue status and the optional board; leave diffs, CI logs, implementation, tests and the review of record to workers. Nudge stalls, record decisions and keep each issue's Current Status current. Close your workers with session_closeout.py on the same ledger, passing your native id as --supervisor-session; all existing claim, handoff, process and terminal checks apply. Recover a partial launch before dispatching more work. The Supervisor adopts your rows if you cannot continue.
5. Idle at the native prompt between events so messages reach you. Reuse worker-finish notices, the existing foreign watch narrowed to the milestone's repositories, and native message delivery; use a rare backstop check every few hours, not a continuous turn or cache-warming schedule. On each wake, handle finished workers and new messages, scan child statuses naming your native id, then refill available capacity. Preserve coverage gaps as UNKNOWN. Reach the Supervisor only by setting the item's Current Status to `Next actor: agent:supervisor` with the action, which its routine check surfaces; never send text into the Supervisor's terminal, where it reads as the Director's own input.
6. When an item needs a person, record its wait, follow the shared brief rules for Director questions and Asking a Client for Client questions, and move to independent milestone work. When nothing is actionable, keep waits and the next actor current and remain idle for events. When the assignment is done or handed off, close out with work-closeout and end with a standalone final line: Safe to exit: yes

At the shared PR cap, brief finishing work first. Workers with finished branches record unopened branches on their issues. Use other repositories in scope below the cap; report nothing actionable only when none has available work.

Workers land through the GitHub skill's Merging procedure and run ready for train-routed work once checks are green with a weighed review for the current head. While they wait on CI or landing, coordinate other milestone work.

__SPECIFIC__

Rules for this brief: https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/brief-rules.md
```

Use [account selection](helpers.md#choosing-the-account) for optional Context Panel
integration and the CLI-login baseline.
Launchplane uses the optional route above. Private staffing targets, host worktree
settings, persistent services and question queues are optional additions to the
catalog defaults, subject to the Director's own stricter instructions. Use the
brief's handoff anchor when no private handoff setting exists, and the existing
unconfigured-queue handoff when no question queue exists. Neither GUI tooling,
Docker nor a desktop IDE is a lead prerequisite; use the shared rules and
owning skills only when an item calls for them, preserving required checks and UNKNOWN.

## Codex Cloud pass briefs

Fill the placeholders for the local agent running the pass. Keep each Director's
Cloud environment IDs in private configuration or the private handoff record,
never in this catalog. The linked shared rules own claims, worktrees, checks,
review and PR handoff; this template adds only the Cloud pass specifics.

For each launch, resolve `cloud_environment` privately and set `task_prompt` to the bounded task.
Keep the scratch path with the task ID. Run every later Cloud command, including
status and diff, from that directory: the CLI writes `error.log` there.

```sh
task_scratch=$(mktemp -d "${TMPDIR:-/tmp}/codex-cloud.XXXXXX") &&
printf '%s\n' "$task_scratch" &&
cd "$task_scratch" && codex cloud exec --env "$cloud_environment" "$task_prompt"
```

```text
Brief from the Supervisor (__HANDOFF_RECORD__), __DATE__. Trial token: __TOKEN__. Codex Cloud pass brief.

Read-only anchor: __ANCHOR_URL__
Anchor and claim: __PASS_ISSUE_URL__
Admission: __KIND__; __EVIDENCE_URLS__
Goal and finish line: __BOUNDED_PASS_GOAL_AND_OBSERVABLE_FINISH__
Cloud environment: resolve __REPOSITORIES__ through __PRIVATE_CONFIG_OR_HANDOFF_REFERENCE__.

1. Read the anchor and pass issue in full. Leave the anchor unclaimed; claim the pass issue with the admission and evidence above under the shared rules before launching work.
2. Select independent tasks with an exact reproduction or bounded question, relevant paths, reachable inputs and a focused verification command suited to the runtime and dependencies; skip unsuitable work. Ask for implementation diffs for code work and disposable evidence for analysis or test runs; preserve justified no-change outcomes in corrective prompts too. Launch with the scratch example above. Send only public-safe source and instructions, never private data, credentials or local evidence. Keep private environment mappings out of Cloud prompts and public results.
3. Check each result against current code and the pass goal. Discard wrong proposals with a reason; record useful diagnosis and its next action on its owning claimed issue without applying evidence as maintained source. For accepted source, claim its linked owning issue before creating its task worktree under the shared rules, then use git -C <owning-task-worktree> apply; do not use codex cloud apply from a checkout.
4. For accepted source, run affected tests locally, plus the shared rules' required safety modules, lint, type checks and inspection. Get and weigh another provider's review through model-review. Within the shared PR cap and pace, hand each PR off through its repository's merge path. Close result issues with their completing PRs; link the pass as context, without closing it from a result PR.
5. Post a short tally on __TRIAL_RECORD_URL__: tasks launched, source accepted/adapted/discarded (with reasons), useful reports, environment/transport failures and PR links. Prefer removal; report Removed: and Net lines: per result, explaining growth. Keep details and Current Status on the pass issue. Close it when its finish line is met; otherwise name the remaining actor or event. Close out and release result and pass claims under the shared rules.

Keep working through compaction. Keep: this brief, the anchor, the pass issue and current step.
Rules for this brief: https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/brief-rules.md
```
