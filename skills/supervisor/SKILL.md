---
name: supervisor
description: Use to hand implementation work to a separate agent process on any provider, including headless CLI handoffs, through the supported launcher; or when the Director asks a session to supervise other sessions for a stretch of work or take over a Supervisor handoff. Full supervision groups related work, checks sessions about every 23 minutes, records Director decisions, routes landings through the merge train, and hands off what needs the Director. Not for in-harness subagents, model reviews (model-review), Computer Use calls, doing an item's work (executing loop), judging supervised runs (direction, on another model), or watching one PR (babysit-pr).
metadata:
  short-description: Brief, watch, nudge, and record agent sessions
resources:
  - path: references/routine-check.md
    kind: reference
    description: Short entry for recurring checks without skill reloads.
  - path: scripts/compact_check.py
    kind: script
    description: Read each ledger session once and emit changed pointers or outstanding actions and coverage gaps.
  - path: scripts/session_closeout.py
    kind: script
    description: Run normal finished-session gates with a compact receipt and no uncertain mutation replay.
  - path: scripts/catalog_landing.py
    kind: script
    description: Verify a catalog landing through runtime reconciliation and fresh native sessions on both harnesses.
  - path: scripts/heavy_run.py
    kind: script
    description: Queue capped local Docker runs through cleanup with private evidence.
  - path: references/heavy-runs.md
    kind: reference
    description: Read before the next local heavy run, changing slots, or moving to a dedicated runner.
  - path: scripts/services.py
    kind: script
    description: Render persistent launchd jobs without installing them, and supervise bounded commands.
  - path: scripts/iterm_tab.py
    kind: script
    description: List, read, launch and reach exact iTerm or tmux sessions.
  - path: scripts/account_choice.py
    kind: script
    description: Follow Context Panel's ranked choices and write count-only launch receipts.
  - path: scripts/claude_account.py
    kind: script
    description: Opt-in rate-limit move requests and pass-through Claude relaunch wrapper.
  - path: references/claude-account-moves.md
    kind: reference
    description: Read when enrolling or moving a rate-limited Supervisor Claude session.
  - path: scripts/goal_board.py
    kind: script
    description: Rebuild the goal board from every open Track issue on OWNER/direction.
  - path: scripts/status.py
    kind: script
    description: Read context and activity for explicit ledger sessions.
  - path: scripts/codex_idle_watch.py
    kind: script
    description: Emit bounded Codex idle notices without deciding completion.
  - path: scripts/foreign_watch.py
    kind: script
    description: Emit bounded untrusted foreign-poster and Launchplane review notices.
  - path: scripts/auto_enqueue.py
    kind: script
    description: Enqueue current-head ready handoffs in capped groups between candidates, respecting the Supervisor hold record.
  - path: scripts/fleet_counts.py
    kind: script
    description: Read open issue and PR counts per visible non-archived repository in the selected account and fleet totals with one paginated GraphQL query.
  - path: scripts/oq.py
    kind: script
    description: Read all Director questions and the acknowledged queue; explicit register/ack writes use the GitHub helper.
  - path: scripts/q_number_guard.py
    kind: script
    description: Stop hook that sends back, once, a reply asking the Director to decide without a bold Q number; report prints its logged counts.
  - path: scripts/finished_map.py
    kind: script
    description: Find transcript close-out candidates for Supervisor verification.
  - path: scripts/release_session_claims.py
    kind: script
    description: Record verified closure and report canonical claim releases across Director repositories; includes explicit closed-log release backfill.
  - path: scripts/codex-session.py
    kind: script
    description: Register a fresh managed native launch, resume or queue an exact thread under the closure/admission ownership lease, read its new launch metadata, or preview and cancel one guarded NotLoaded queue entry.
  - path: scripts/codex_session_proxy.py
    kind: script
    description: Private per-client native RPC ingress that holds source ownership for attachments and actions; imported by codex-session.py.
  - path: scripts/handoff_record.py
    kind: script
    description: Print this Director's own handoff record from private config, defaulting to OWNER/direction.
  - path: references/field-reports.md
    kind: reference
    description: Where every Director's Supervisor sends field reports about shared skills.
  - path: references/helpers.md
    kind: reference
    description: Local state, session registry schema, goal board, helper usage and shutdown stages.
  - path: references/brief-rules.md
    kind: reference
    description: Rules every Supervisor brief carries by link, and the milestone lead brief template.
commands:
  - name: supervisor-check
    source: skill
    resource_path: scripts/compact_check.py
    example_argv: ["uv", "run", "scripts/compact_check.py", "--ledger", "<ledger.json>", "--state", "<check-state.json>"]
    purpose: Print changed session pointers, outstanding actions and current coverage without response contents.
  - name: supervisor-closeout
    source: skill
    resource_path: scripts/session_closeout.py
    example_argv: ["uv", "run", "scripts/session_closeout.py", "--ledger", "<ledger.json>", "--session-id", "<native-id>", "--supervisor-session", "<supervisor-id>", "--owner", "OWNER", "--evidence", "<private-evidence>"]
    purpose: Close one normal finished session or return its first failed gate with private evidence.
  - name: supervisor-verify-landing
    source: skill
    resource_path: scripts/catalog_landing.py
    example_argv: ["uv", "run", "scripts/catalog_landing.py", "--config", "<private-landing.json>", "--evidence", "<private-evidence>"]
    purpose: Reconcile and verify a catalog landing through both fresh native harnesses.
  - name: supervisor-heavy-run
    source: skill
    resource_path: scripts/heavy_run.py
    example_argv: ["uv", "run", "scripts/heavy_run.py", "--manifest", "<private-run.json>", "--evidence", "<new-private-evidence>"]
    purpose: Run one queued Docker job with CPU and memory caps and verified lifetime cleanup.
  - name: supervisor-services-render
    source: skill
    resource_path: scripts/services.py
    example_argv: ["uv", "run", "scripts/services.py", "render", "--catalog", "<runtime-catalog>", "--config", "<private-services.json>", "--output", "<private-jobs-directory>"]
    purpose: Prepare reviewed launchd jobs; only the Supervisor installs and starts them.
  - name: supervisor-iterm-tab
    source: skill
    resource_path: scripts/iterm_tab.py
    example_argv: ["uv", "run", "scripts/iterm_tab.py", "list"]
    purpose: List exact window, tab and session identities.
  - name: supervisor-account-choice
    source: skill
    resource_path: scripts/account_choice.py
    example_argv: ["uv", "run", "scripts/account_choice.py", "--provider", "anthropic"]
    purpose: Show which configured account a launch would use and why.
  - name: supervisor-goal-board
    source: skill
    resource_path: scripts/goal_board.py
    example_argv: ["uv", "run", "scripts/goal_board.py", "--owner", "OWNER", "--format", "markdown"]
    purpose: Rebuild the goal board from GitHub on every check, after compaction and at takeover.
  - name: supervisor-status
    source: skill
    resource_path: scripts/status.py
    example_argv: ["uv", "run", "scripts/status.py", "--ledger", "<ledger.json>"]
    purpose: Report ledger session context and transcript activity.
  - name: supervisor-codex-queue-cancel-preview
    source: skill
    resource_path: scripts/codex-session.py
    example_argv: ["uv", "run", "scripts/codex-session.py", "queue-cancel", "--session-id", "<native-id>", "--codex-home", "<target-home>", "--ledger", "<original-ledger.json>"]
    purpose: Observe opaque queued-entry identities without creating ownership or changing source state.
  - name: supervisor-codex-queue-cancel-apply
    source: skill
    resource_path: scripts/codex-session.py
    example_argv: ["uv", "run", "scripts/codex-session.py", "queue-cancel", "--session-id", "<native-id>", "--codex-home", "<target-home>", "--ledger", "<original-ledger.json>", "--queued-submission-id", "<selected-id>", "--entry-sha256", "<observed-full-entry-sha256>", "--apply"]
    purpose: Delete one selected entry only from a verified NotLoaded target, preserving other queues and barriers.
  - name: supervisor-codex-idle-watch
    source: skill
    resource_path: scripts/codex_idle_watch.py
    example_argv: ["uv", "run", "scripts/codex_idle_watch.py", "--ledger", "<ledger.json>", "--once"]
    purpose: Observe idle turn ends for verification.
  - name: supervisor-oq
    source: skill
    resource_path: scripts/oq.py
    example_argv: ["uv", "run", "scripts/oq.py", "OWNER/REPO#NUMBER", "--owner", "<owner-login>", "--decision-author", "<recording-automation-login>"]
    purpose: Gather Director questions from the whole discussion; see helpers for explicit queue registration and presentation acknowledgement.
  - name: supervisor-q-number-report
    source: skill
    resource_path: scripts/q_number_guard.py
    example_argv: ["uv", "run", "scripts/q_number_guard.py", "report", "--since", "YYYY-MM-DD", "--until", "YYYY-MM-DD"]
    purpose: Count the Q-number Stop hook's pass, caught and second-pass verdicts for a date range.
  - name: supervisor-foreign-watch
    source: skill
    resource_path: scripts/foreign_watch.py
    example_argv: ["uv", "run", "--no-cache", "scripts/foreign_watch.py", "--owner", "OWNER", "--own-login", "OWN-LOGIN", "--state", "<private-state.json>", "--deadline-seconds", "1800"]
    purpose: Wake on untrusted foreign comments and Launchplane product-review records without starting work.
  - name: supervisor-finished-map
    source: skill
    resource_path: scripts/finished_map.py
    example_argv: ["uv", "run", "scripts/finished_map.py", "--ledger", "<ledger.json>"]
    purpose: Find close-out candidates without authorizing shutdown.
  - name: supervisor-auto-enqueue
    source: skill
    resource_path: scripts/auto_enqueue.py
    example_argv: ["uv", "run", "scripts/auto_enqueue.py", "--owner", "OWNER", "--hold-file", "<private-holds.json>", "--handoff-login", "<agent-login>", "--once"]
    purpose: Preview stable batches from active Launchplane policy; apply only under the Supervisor's existing enqueue authority.
  - name: supervisor-enqueue-holds
    source: skill
    resource_path: scripts/auto_enqueue.py
    example_argv: ["uv", "run", "scripts/auto_enqueue.py", "holds", "--hold-file", "<private-holds.json>", "--pause", "OWNER/REPO"]
    purpose: Serialize repository pauses and optional manual release pauses with local enqueue passes.
  - name: supervisor-release-session-claims
    source: skill
    resource_path: scripts/release_session_claims.py
    example_argv: ["uv", "run", "scripts/release_session_claims.py", "finish", "--ledger", "<ledger.json>", "--session-id", "<native-id>", "--supervisor-session", "<native-id>", "--owner", "OWNER", "--confirm-session-ended"]
    purpose: Report consumed releases and unresolved claim IDs after transcript and actual process exit verification.
  - name: supervisor-codex-session-queue
    source: skill
    resource_path: scripts/codex-session.py
    example_argv: ["uv", "run", "scripts/codex-session.py", "queue", "--session-id", "<native-id>", "--codex-home", "<source-home>", "--message", "<text>"]
    purpose: Queue exact-thread input after invalidating its closure under the ownership lease.
  - name: supervisor-codex-session-launch
    source: skill
    resource_path: scripts/codex-session.py
    example_argv: ["uv", "run", "scripts/codex-session.py", "launch", "--codex-home", "<selected-home>", "--", "codex", "-C", "<worktree>", "--", "<brief>"]
    purpose: Register a fresh managed client before executing it on the selected account's observed daemon.
  - name: supervisor-codex-session-resume
    source: skill
    resource_path: scripts/codex-session.py
    example_argv: ["uv", "run", "scripts/codex-session.py", "resume", "--session-id", "<native-id>", "--codex-home", "<source-home>"]
    purpose: Resume a native thread through the same lease as closure and successor admission.
  - name: supervisor-codex-session-status
    source: skill
    resource_path: scripts/codex-session.py
    example_argv: ["uv", "run", "scripts/codex-session.py", "status", "--session-id", "<native-id>"]
    purpose: Read the resumed generation's launch metadata for the native ledger.
  - name: supervisor-iterm-close
    source: skill
    resource_path: scripts/iterm_tab.py
    example_argv: ["uv", "run", "scripts/iterm_tab.py", "close", "--session-id", "<exact-iterm-id>", "--ledger", "<private-ledger.json>"]
    purpose: Close one Supervisor-owned tab after current process and empty-input proof.
policy:
  command_policies:
    - id: prefer-coordinated-codex-resume
      match:
        shell_regex: >-
          (?x)^\s*(?:(?:\S*/)?codex|"[^"]*/codex"|'[^']*/codex')\s+
          (?:(?:exec|e|--(?!help\b)[\w-]+
          |(?:--(?!help\b)[\w-]+|-[cCpmsaio])(?:=|\s+(?!-))(?:"[^"]*"|'[^']*'|[^\s"'])+
          |(?=['"](?:--[\w-]+=|-[cCpmsaio])|-[cCpmsaio][^=\s])(?:"[^"]*"|'[^']*'|[^\s"'])+)\s+)*
          (?:resume\b|queue\b(?=[^\n]*\s['"]?--message(?:[ =-]|$)))
          (?!(?:\s+(?!--(?:\s|$))(?:"[^"]*"|'[^']*'|[^\s"'])+)*?\s+(?:-h|--help)(?:\s|$))
      action: require_preferred
      message: Native resume and queue mutations must invalidate closure evidence under the same ownership lease as successor admission. Use the exact-thread helper with the source account home.
      preferred:
        - kind: script
          path: scripts/codex-session.py
          example_argv: ["uv", "run", "scripts/codex-session.py", "queue", "--session-id", "<native-id>", "--codex-home", "<source-home>", "--message", "<text>"]
          purpose: Queue input through the supported ownership-coordinated route.
        - kind: script
          path: scripts/codex-session.py
          example_argv: ["uv", "run", "scripts/codex-session.py", "resume", "--session-id", "<native-id>", "--codex-home", "<source-home>"]
          purpose: Resume an exact native thread through the supported ownership-coordinated route.
    - id: reject-system-events-keyboard-synthesis
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?osascript\b(?=[\s\S]*\bSystem\s*Events\b)[\s\S]*
          (?:\bkeystroke(?:\s+|\s*\()|\bkey\s+(?:code|down|up)\b|\.\s*key(?:Code|Down|Up)\s*\()
      action: reject
      message: &keyboard_refusal Global keyboard synthesis can type into another application or agent session even when System Events names a process. Use the browser tools' file-upload action or a page/DevTools API scoped to the exact tab for browser input; use iterm_tab.py send --verified-target for terminal input.
      preferred: &keyboard_alternatives
        - kind: script
          path: scripts/iterm_tab.py
          example_argv: ["uv", "run", "scripts/iterm_tab.py", "send", "--session-id", "<exact-iterm-id>", "--text", "<text>", "--verified-target"]
          purpose: Send terminal input only after verifying the exact session target.
    - id: reject-keyboard-synthesis-tools
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?(?:cliclick\b[\s\S]*(?:\s|'|")(?:t|kp|kd|ku):
          |(?:xdotool|ydotool)\b[\s\S]*(?:\s|'|")(?:type|key|keydown|keyup)\b
          |(?:wtype|dotool|dotoolc)\b|xte\b[\s\S]*\b(?:str|key|keydown|keyup)\b)
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: reject-keyboard-synthesis-apis
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?(?:python[\d.]*|node|osascript|swift)\b[\s\S]*(?:
          \bpyautogui\s*\.\s*(?:write|typewrite|press|hotkey|hold|keyDown|keyUp)\b
          |\b(?:pynput\s*\.\s*keyboard|from\s+pynput\s+import\s+keyboard)\b[\s\S]*\bController\b[\s\S]*\.(?:type|press|release|tap|touch|pressed)\b
          |\b(?:CGEventCreateKeyboardEvent|CGEvent\s*\(\s*keyboardEventSource\s*:))
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: reject-python-keyboard-synthesis
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?python[\d.]*\b[\s\S]*\bimport\s+keyboard\b[\s\S]*(?<![\w.])keyboard\s*\.\s*
          (?:write|press|release|send|press_and_release|play|replay|stash_state|restore_state|restore_modifiers)\b
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: reject-python-keyboard-replay-module
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?python[\d.]*\b[\s\S]*\s-m\s+keyboard\b[\s\S]*(?:\s|'|")<
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: reject-imported-keyboard-synthesis-apis
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?python[\d.]*\b[\s\S]*\bfrom\s+(?:pyautogui|keyboard)\s+import\b
          [^\n;]*\b(?P<keyop>write|typewrite|press|release|send|press_and_release|play|replay|stash_state|restore_state|restore_modifiers|hotkey|hold|keyDown|keyUp)\b
          (?:\s+as\s+(?P<keyalias>\w+))?[\s\S]*(?<![\w.])(?(keyalias)(?P=keyalias)|(?P=keyop))\s*\(
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: reject-aliased-keyboard-synthesis-apis
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?python[\d.]*\b[\s\S]*\bimport\s+(?:pyautogui|keyboard)\s+as\s+(?P<keymodule>\w+)\b
          [\s\S]*\b(?P=keymodule)\s*\.\s*(?:write|typewrite|press|release|send|press_and_release|play|replay|stash_state|restore_state|restore_modifiers|hotkey|hold|keyDown|keyUp)\b
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: reject-robotjs-keyboard-synthesis
      match:
        shell_regex: >-
          (?ix)(?:\S*/)?node\b[\s\S]*\b(?:require\s*\(\s*["']+robotjs["']+\s*\)|from\s*["']+robotjs["']+)
          [\s\S]*\b(?:typeString|typeStringDelayed|keyTap|keyToggle|unicodeTap)\s*\(
      action: reject
      message: *keyboard_refusal
      preferred: *keyboard_alternatives
    - id: prefer-session-helper-for-iterm-typing
      match:
        shell_regex: '(?i)\bosascript\b[^\n]*(?:write text|iTerm)'
      action: require_preferred
      message: Use exact session identities and separate text and Return sends through the maintained helper.
      preferred:
        - kind: script
          path: scripts/iterm_tab.py
          example_argv: ["uv", "run", "scripts/iterm_tab.py", "list"]
          purpose: Inspect exact identities before sending or launching in the dedicated window.
---

# Supervisor

Follow [task scope](../references/execution-scope.md), the Director's overall
and repository direction, and [talking with the Director](../references/talking-with-the-director.md).
Full supervision is a direction session the Director starts directly in a host,
not a subordinate call. The Supervisor briefs, watches, nudges and records;
it never decides for the Director, widens a running brief, or judges a run it
launched. Another model's direction session judges that run.

Load this skill at takeover. Scheduled checks use only
[Routine check](references/routine-check.md), which names the private ledger,
recovery note and compact readers. Read a rare event's linked section when it
occurs; do not reload this skill or the whole helpers reference each check.
For a separate worker under existing delegation authority, read
[terminal launches](references/helpers.md#terminal-launches),
[account choice](references/helpers.md#choosing-the-account),
[the registry](references/helpers.md#local-state-and-the-session-registry), and
[shutdown](#close-finished-sessions-on-every-notice-and-check) before launching.
That role does not start a full Supervisor run.

## Before Taking Over: Permission Mode

Check the current permission mode. If it blocks a needed step, name that step
in the first message, before a denial. Claude auto mode is not launch or train
authority; its README exception covers only an authorized direct `gh-pr.py merge`.
The brief decides scope. Never change the Director's settings or work around a denial.

## Pilot Status

Use this Director's [handoff record](references/helpers.md#handoff-record),
not another person's repository. Shared lessons go to
[field reports](references/field-reports.md). The pilot ends after two consecutive
handoffs with no new lesson or widened brief, takeover from a handoff alone
including checks and digest, and Director questions delivered in one place
under the shared queue rules. Say whether each handoff contains a new lesson.

## Harnesses

Use the [tmux launch procedure](references/helpers.md#tmux-launches) for new
workers on `tmux -L supervisor`; existing iTerm workers retain their backend.
Detaching a view leaves workers running. Never kill the server to disconnect.
A read-only view cannot accept the Director's answers: say at launch that you
will relay them, then use exact-session sends at a verified idle prompt.
For Codex, use the managed daemon ingress and `codex-session.py queue` with the
source home; it invalidates closure under the ownership lease. For stale queued
input, read [recovery](references/helpers.md#release-a-verified-finished-session)
and use explicit queue-cancel preview and ID/digest apply. Cancellation does
not prove completion or release claims. A refusal never permits switching accounts.
Codex as Supervisor is expected to work; claim verification only after a run is
recorded on the handoff record. Keep the brief, issue and current step through compaction.

## Accounts, Pace, And Launch Effort

Follow Context Panel's account choice and capacity. Never apply a banked reset.
Ask the Director to apply one only when every account of that provider,
including use-last, has 0% remaining. Relay reset-expiry notices within 24 hours;
they do not authorize an early reset. Fill productive capacity with soonest
expiries first. Medium is the default launch effort; use high for authorization,
credentials, security, policy, guidance and hard debugging.

## The Pattern

1. Before briefing product work or asking about it, run `product_summary.py`
   against a current default checkout. Group one repository and related
   subsystem work per brief and PR. Use `github-plan` to prepare a canonical
   parent, recheck live ownership, and claim the parent and each included issue
   on the same branch before moving native children or creating a worktree.
   On refusal, reconcile and release only newly made group claims. Verify both
   parents after a move; restore a missing original link after a failed move.
2. Every worker has an anchor in its repository, including sweeps and checks.
   Use `Anchor and claim:` or `Read-only anchor:` and one `Admission:` line
   with the kind and exact evidence URLs. A held issue stays held until its
   actual blocker changes; release, a new timestamp or a split does not clear it.
   For retained work, supply `Resume from:` with the source claim ID or URL;
   require the authored release and qualified handoff. Read-only work answering
   a wait may proceed unclaimed with its ownership check.
3. State a goal and observable finish line, not a mechanism. Include
   [brief rules](references/brief-rules.md) by link, and use its milestone-lead
   or Cloud-pass template when applicable. A named mechanism is a suggestion
   unless it records a Director decision. Check which visual thing a request
   means before briefing it. New scope needs a Director decision and a fresh
   brief and session; the running brief remains unchanged. Brief unsupported
   operations as designing a supported path. Never append an extra Supervisor
   qualification step after delivery; use the completing PR's tests and normal
   deploy health checks, preserving real event waits and remaining engineering.
4. Pass the prompt through `iterm_tab.py new --brief-file` and keep settings
   and invocation in the command file. Follow the launcher preflight and read
   the first screen. A missing transcript after one minute or trust prompt is
   blocked; preserve its exact terminal, never relaunch or grant trust to pass.
   Use headless page checks. Explicit GUI briefs use the
   [GUI and browser preflight](references/helpers.md#browser-and-computer-use-preflight)
   and `--require-browser`; Codex is the default. Claude Computer Use needs the
   Director's approval for that visible iTerm session.
5. Before a batch, compare opened and merged PRs over recent hours. When merges
   lag and no merge-path repair is available, launch finishing work only.
   Follow the brief rules' PR cap and state any repair exemption. Workers post
   on child issues; parents hold in-place Current Status and decisions.
   Local heavy Docker runs use [one locked entrypoint](references/heavy-runs.md).
6. Director questions go on their item's issue, then the shared queue. Record
   actual decisions under [question rules](../references/talking-with-the-director.md#decisions-holds-and-work-state)
   and [status labels](../github-plan/SKILL.md#status-labels), preserving every
   unresolved wait. Point the worker at the recorded decision. A Supervisor or
   direction-recorded decision counts; leave escalation labels for the weekly
   direction audit. Link the question queue in every check and handoff.
7. Run [Routine check](references/routine-check.md) about every 23 minutes and
   close finished sessions immediately on notices. Read `[staffing]`
   `openai_workers` and `load1_limit` from the host's private Supervisor config;
   report missing values and continue available work. Refill below the target
   in the same turn a worker closes. At high load add CPU-light work. When
   `next` is thin, read actionable unclaimed `plan:active` issues and Cloud-pass
   findings under the overall Order. Require complete paginated staffing
   coverage; a first page or bounded board is insufficient. Never add a
   staffing label or parallel manual inventory.
8. Nudge stalls with exact facts. Continue an actionable lead at its verified
   idle prompt. Allow automatic compaction; routine idleness alone is no reason
   to compact. Manual Claude `/compact` needs a safe idle prompt and Keep
   instructions. Never queue a Codex request to compact itself. On failed
   compaction or account departure, obtain a durable handoff, recheck ownership,
   close safely, then relaunch the same goal with its retained-work source.
9. Land through the assigned train with the maintained driver, one per repo,
   or the repository's authorized protected-branch route where assigned.
   Load `launchplane` before train actions. After about 45 minutes with only
   `lease held`, read candidate CI directly. Use the
   [auto-enqueue procedure](references/helpers.md#interim-auto-enqueue-watcher)
   for existing interim routing. After landing load `github`, perform
   [catalog verification](references/helpers.md#catalog-landing-verification)
   for catalog landings, then complete After A Merge and planning reconciliation.

## Client Products

Follow overall direction for release timing, Client identity and acceptance,
including `director_standing`; Launchplane owns the gated production path.
Read Launchplane's Client release review documentation for the mechanics.
Open Client-visible PRs when ready so previews reach the Client. Other-Client
products need their own Accept on the current change before landing and a
separate release Accept; hand off `--waiting-for-client` until then. Unchanged
base updates retain acceptance under Launchplane's carried-acceptance rule.
Director-standing or no-Client repositories need no extra acceptance unless held.
Classify each PR from its own Client test notes under the
[ready-handoff contract](../github/references/cli-reference.md#ready-handoffs);
bring only judgment calls to the Director. Every Client-release repository's
PR carries `## Client test notes`. Discussions are drafts; issues are requests.

## Milestone State

Rebuild the board from GitHub at takeover, each check and after compaction;
use `goal_board.py --owner OWNER --changes-state <private-board-state.json>`
for routine output and the full board for takeover. Coverage gaps stay unknown.
Open the linked decision and Current Status before repeating a hold or briefing
work. PR handoffs stay open until merged; read current head, mergeability and
train reason. Correct conflicting Track summaries under `github-plan`, naming
the governing decision and reason. Assess each Track's `closure_review` under
the [milestone contract](../github-plan/references/milestones.md); the pointer
reader is not proof that a Track's own exit criteria passed.

## Local State

Resolve private paths absolutely. Keep only the session registry, helper
watermarks/deadlines, enqueue holds, capacity-retry state, closed-session log,
and one recovery note overwritten when a pending local action changes.
Record exact session/terminal/transcript identities, repository, issue and brief.
Keep pointers, not status prose, copied decisions, board lines or rule lists.
GitHub owns those facts. Historical logs are not authority or takeover input.

## Procedure

At takeover, check permission mode, load `direction` for its daily-turn reads,
resolve this Director's handoff record and read the latest handoff and later
comments. Read original Director words before repeating them. Rebuild the full
goal board and registry, read each named thread or screen once, verify recovery
note actions at their sources, and list actual watcher/driver processes.
Recreate the ~23-minute check and morning digest with the harness scheduler.
Bind existing [persistent services](references/helpers.md#persistent-supervisor-services)
with `services.py bind`; otherwise start bounded Codex and Claude idle watches
with separate delivered-state files and the scoped foreign watcher. Process
closeout notices immediately, re-arm bounded watches after delivery/error/deadline,
and preserve coverage gaps. Watch live Client production-release events too.
Schedule the [pending-review dispatcher](references/helpers.md#dispatcher-timer)
while worker review requests exist. Never relaunch existing services blindly.
Read `direction_audit.py --stale-waits-only` for repositories discovered by
`gh-plan.py next`, with quota and landing priority considered. Review full
finish lines, post-merge comments, selection exclusions and coverage; correct
obsolete qualification-only waits instead of staffing completed work. Real
engineering and people's waits remain. Post findings and needs-Director links
on the handoff record. A retire/stop/redirect finding goes to `direction`, not
an executing change or a Supervisor verdict.
Keep working through compaction. Close out only when done, compaction fails,
or the account needs departure, with durable handoff and verified safe closure.

## Move Rate-Limited Claude Sessions

Use [the account-move procedure](references/claude-account-moves.md) only for
its opted-in rate-limit route. Preserve the source on uncertainty; never switch
identity to carry a refused action.

## Close Finished Sessions On Every Notice And Check

For normal sessions use `session_closeout.py` from
[Routine check](references/routine-check.md). It runs the gates below and
returns one receipt or the first failed gate. Its journal never permits an
uncertain mutation to be replayed. Use the procedure below for input preservation,
pending prompts, interrupted sessions and other exceptional recovery.

Handle turn-end notices as they arrive. For a watch notice with
`closeout_request`, start this procedure immediately and re-arm that
harness's watch with the same `--delivered-state` file. For a Claude
`notify_when_idle` notice, resolve its exact native session in the ledger and
run `finished_map.py --ledger <private-ledger.json> --session-id <native-id>`;
its `closeout_request` enters the same procedure. Re-read the transcript even
when the notice supplies a request. A request is a verification task, not
permission to send an exit command. An absent request leaves the session open
for normal stall or capacity handling. Every scheduled check still scans all
sessions, recovering missed notices. Re-arm an expired or failed watch; do not
let loss of the watch look like a fleet with no finished sessions.

Match a notice's terminal id to the ledger's `iterm_session_id` or
`tmux_session_id`, then verify the backend's `read` TTY and the native identity
in that entry's transcript. An agent name or tab title alone is not identity;
use its original launch receipt to find the exact terminal and native ids, or
report the ambiguity and keep it open. A malformed ledger is a fleet-wide
read blocker: report the invalid entry and reconcile it against its original
launch receipt before retrying; never guess or skip identity checks.

Before exit, when `closeout_blockers` lists background tasks or scheduled jobs,
report their ids and keep the session open. Nudge that exact worker to check
each task's completion condition and stop an obsolete task through its native
TaskStop or CronDelete route, then give a fresh close-out verdict. For an unknown
schedule id or a job that is already absent, have the worker run native CronList
to reconcile the list before deciding what to delete. A still-needed
task remains a wait. A missing terminal
notice is uncertainty, not proof that a task ended; never send exit keys or
stop another session's tasks to clear the list.
Once process exit is independently proven, use the established final-verdict
and process closure checks. Scheduled jobs alone do not suppress a review
notification to an idle empty Claude prompt; an active turn or Bash/Monitor
task still does.

Aim to finish verification and shutdown within two minutes of the worker's
verdict. If a check fails, keep the session open and report the exact missing
proof and next action immediately. Do not bypass a check to meet that target.
Run each session's verification and claim discovery separately from global
work selection, train polling and other finished sessions; a slow GitHub read,
uncertain release or pending permission on one session must not hold up the
others. A missed target remains visible in the next check and handoff.

1. Match the ledger's session to its transcript, process, and terminal tab.
   Leave the Director's own sessions alone. Keep thread-only sessions without
   a matched terminal open and record them in the ledger. Read the session's
   latest response in its transcript: it must give the session's unqualified
   close-out verdict `Safe to exit: yes`, and its work must be handed off on
   the item's issue. A quotation, negation, or verdict for one check does not
   qualify. Never infer completion from silence,
   idleness, or the screen; Claude Code redraws its screen and can hide the
   close-out message. If later transcript activity resumes work, keep it open.
   Use `safe_verdict`, then run the session's
   [claim check](references/helpers.md#claim-checks-at-close-out); keep the
   terminal open until it clears. A required review wait still takes
   precedence; do not close that worker out.
2. Inspect the input line before sending keys. `read` reports its visible
   style as `dim`, `non_dim`, `mixed`, `empty` or `unknown`; this is style
   evidence, never authorship or approval. Never record dim suggestions as
   Director text or decisions. Ctrl-U clears typed input; a suggestion survives
   Ctrl-U. Use that known check only after preserving any confirmed
   Director-typed draft in private handoff files with its session/tab identity.
   Before clearing other nonempty input, preserve it privately as unverified
   text, including dim text; that backup never counts as a Director decision.
   Keep the tab open when text cannot be preserved or its author is uncertain.
3. A prompt for unfinished item work means the session is not finished; keep
   it open. Resolve a pending close-out permission prompt only after verifying
   the full pending tool call in the transcript matches the prompt and its
   exact action meets the brief and the owning skill's existing rules.
   If the action cannot be verified, keep the session open and report the prompt.
   Use one-time approval only; never select an option that changes persistent
   permissions. Never approve new scope or access to make a session exit.
   Immediately before each approval, verify the visible prompt still matches
   the checked tool call. Send one response, then read back the prompt state;
   never retry an uncertain send or answer a subsequent prompt without checking it.
   If the prompt needs the Director, leave the session open and include it in the next
   "needs the Director" list. After resolving a prompt, recheck the transcript
   and handoff before continuing, because the session may have resumed work.
4. Immediately before sending keys, recheck the transcript, handoff, input
   line, and that the session is at its prompt with no turn running. Clear the
   input with Ctrl-U and verify the entire input is empty before sending
   `/exit` for Claude Code or `/quit` for Codex into that terminal's input,
   never through `codex queue`. A fully dim suggestion is not typed input;
   if typed, mixed or unknown input remains, keep the tab open.
   Check every send's output and exit status. A failed send can leave text
   typed with Return not attempted or its submission unknown. Read the exact
   session; after proving it was not submitted, use verified-target `clear`
   and verify empty input before another send. Never append a second command
   or retry an uncertain submission.
   Use a bounded wait for that session's process to end. Verify it ended before closing
   the tab. If it is still running or its identity is uncertain, keep the tab
   open and record the next action in the ledger.
5. After verified process exit, run `release_session_claims.py finish` as
   [finished-session shutdown stages](references/helpers.md#finished-session-shutdown-stages)
   describes, before closing the tab. It records the observed exit in the
   shared host-local closed-session log, discovers the session's claims across accessible
   repositories under `--owner` and the configured automation identity, and
   reports their existing exact worker or validated Supervisor releases.
   It reports unresolved claim IDs without posting another release. Use the
   existing evidence-backed `gh-plan.py release-claim` route to reconcile those
   claims, preserving retained PRs, then rerun finish.
   Load `github` and `github-plan` before that step. Keep incomplete discovery,
   unreleasable claims and uncertain writes in the ledger for reconciliation;
   never report claims released without confirmed readback. A running session,
   foreign identity or missing unqualified verdict is reported, never released.
   Use the helper's `backfill` mode for already-ended sessions with original
   verified closure times and transcript/issue handoffs in that log. It never
   treats log age, a merged PR or an idle tab as closure evidence. Preserve
   status, waits, blockers and retained artifacts; release is ownership only.
   Name retained PRs and worktrees in the handoff and record any incomplete
   reconciliation before reporting shutdown complete. The closed-session log
   records the transcript's verified verdict and observed process exit; never
   write a fixed "safe-exit yes" just because a close command was attempted.

6. After the process-exit wait, read `list`. If the exact session is gone,
   record that the tab already closed. For a surviving idle shell, use `iterm_tab.py close --session-id <exact-iterm-id>
   --ledger <private-ledger.json>`. It checks the launch ledger, rejects split
   tabs, and rechecks the TTY processes and native shell prompt twice before
   one non-force close. Read `list` once to confirm disappearance; preserve an
   uncertain close rather than retrying. Missing shell input proof preserves
   the tab; read the [native shell-input setup](references/helpers.md#native-shell-input-proof)
   for the supported launch-shell setup, without changing the Director's profile.
   If a retained ended session has no TTY processes, preserve it and report the
   profile setting; do not force-close it or change the Director's setting.
   For tmux, follow the linked backend procedure: an ended pane is retained
   for readback and closes only after its owned ledger identity and actual
   process exit are proved. A live shell or split window stays open.

## Handoff Shape

Post one full handoff comment on this Director's own handoff record under
[Agent Comments](../references/talking-with-the-director.md#agent-comments).
Link the freshly rebuilt board and coverage, governing decisions, open ready
PRs, running session identities and anchors, briefs and next actors, verified
watchers/drivers with scopes/state/deadlines, private file pointers and Director
sessions to leave alone. Link new landed lessons or their tracking issues,
the latest catalog-verification receipt, pending recovery actions and the
Director's question queue. Say whether any lesson is new. A handoff is incomplete
until each discovered Track is read or named unknown. Send new public-safe
shared lessons to field reports too; keep operational contents on their sources.

## Lessons From The Pilot

Use fresh exact source evidence. Session titles, silence, successful launch,
physical job completion and earlier summaries do not prove present workflow
success. Preserve active or uncertain work and exact identities. A real launch,
claim, review or cleanup defect belongs on its owning issue; do not maintain
private rule copies. Long watches use `uv run --no-cache` so they do not hold
shared cache-use locks; do not interrupt another session to reclaim a cache.
