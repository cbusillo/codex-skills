---
name: work-closeout
description: Use when the user requests a native reminder, including plain repeating reminders or reminders to return to an exact Codex or Claude Code session or start a repository check-in, or asks to wrap up, clean up, close out, pause, park, hand off, determine what remains before stopping, preserve plan direction for the next session, update or remove stale plans/handoffs, reconcile issue graph state, remove transient artifacts, or asks whether they can exit. Coordinates GitHub plan cleanup, safe git/worktree hygiene, artifact cleanup, and final state summaries. Safe-to-exit and closeout final answers must include a Love Gate section with explicit `Love:` and `Do not love:` entries before the safe-to-exit verdict.
metadata:
  short-description: Close out workstreams cleanly
resources:
  - path: scripts/check_claims.py
    kind: script
    description: Reads native-session claim releases and retained PR handoffs before a safe-exit verdict.
  - path: scripts/repo_cleanup.py
    kind: script
    description: Read-only cleanup inventory, fork dispositions, private snapshot revalidation, and post-action verification.
  - path: scripts/reminder_link.py
    kind: script
    description: Prepares a native terminal URL and manual fallback for an exact or fresh session without writing reminders.
  - path: scripts/reminder_list.swift
    kind: script
    description: Performs read-only exact Reminders-list lookup with existing macOS access.
  - path: scripts/reminder_save.py
    kind: script
    description: Shared plain/session reminder writer with optional links, repeat schedules, readback, explicit existing-item IDs and dry runs.
  - path: scripts/reminder_save.swift
    kind: script
    description: Native scheduling and visible Reminders URL attachment adapter called by the shared writer.
  - path: references/session-reminders.md
    kind: reference
    description: Read when the Director requests a native reminder, whether plain or for a session or repository.
commands:
  - name: closeout-check-claims
    source: skill
    resource_path: scripts/check_claims.py
    example_argv: ["uv", "run", "scripts/check_claims.py", "--owner", "OWNER", "--harness", "codex", "--session", "<native-id>", "--transcript", "/absolute/native-transcript.jsonl"]
    purpose: Inventories all configured writer App installations and refuses an all-clear until exact releases and retained PR handoffs are verified; never writes.
  - name: session-reminder-link
    source: skill
    resource_path: scripts/reminder_link.py
    example_argv: ["uv", "run", "scripts/reminder_link.py", "--harness", "codex", "--directory", "/absolute/repository", "--session-id", "<uuid>", "--verify-list"]
    purpose: Prepares a URL and verifies a unique private destination list without creating or changing reminders.
  - name: repo-cleanup-inventory
    source: skill
    resource_path: scripts/repo_cleanup.py
    example_argv: ["uv", "run", "work-closeout/scripts/repo_cleanup.py", "inventory", "--repo", "/path/to/repo", "--json"]
    purpose: Reports scoped cleanup evidence without authorizing or performing deletion.
  - name: session-reminder-save
    source: skill
    resource_path: scripts/reminder_save.py
    example_argv: ["uv", "run", "scripts/reminder_save.py", "--harness", "codex", "--directory", "/absolute/repository", "--session-id", "<uuid>", "--due", "2026-10-20T09:00", "--timezone", "America/New_York"]
    purpose: Prepares a dry run; add --apply only under existing reminder-write authority to save and verify the visible URL.
  - name: repo-cleanup-forks
    source: skill
    resource_path: scripts/repo_cleanup.py
    example_argv: ["uv", "run", "work-closeout/scripts/repo_cleanup.py", "forks", "--owner", "OWNER"]
    purpose: Reports the cleanup policy's keep, archive, or delete disposition for each fork without changing any repository.
policy:
  command_policies:
    - id: reject-pkill
      match:
        argv_prefix: ["pkill"]
      action: reject
      message: &process_stop_route Pattern kills can stop other sessions on a shared host. Stop your own background task through the harness's task stop (TaskStop or Codex's task stop), or use kill <pid> with the literal positive numeric PID you recorded when you started it, or the shell's own recorded last-background PID $!. Read a recorded pidfile first, then pass its numeric PID directly; do not use substitutions, other variables, process groups or piped targets.
    - id: reject-killall
      match:
        argv_prefix: ["killall"]
      action: reject
      message: *process_stop_route
    - id: reject-nonliteral-kill-targets
      match:
        shell_regex: >-
          (?x)\A(?:builtin\s+)?(?:\S*/)?kill(?=\s|$)
          (?!\s+(?:(?:-[sn]\s+[\w+-]+|--signal(?:=|\s+)[\w+-]+|-[\w+-]+)\s+)?
          (?:--\s+)?(?:[1-9]\d*|'\$!')(?:\s+(?:[1-9]\d*|'\$!'))*
          (?:\s+(?:[1-9]\d*\s+)?['"]?(?:[<>]{1,2}|[<>]&|&>+|>\|)['"]?\s+(?:'[^']*'|"[^"]*"|\S+))*\Z
          |\s+(?:-[lL]|--(?:list|table|help|version))(?:\s|$))
      action: reject
      message: *process_stop_route
    - id: reject-wrapped-process-kills
      match:
        shell_regex: >-
          (?x)\A((\S*/)?(
          xargs(\s+(?>-[0prtx]+|'?-i\S*|['"]?-[IEnPLsadJ] ([^\s'"]+['"]?|\s+\S+)|--(arg-file|delimiter|max-(args|procs|lines|chars)|replace|eof)(=|\s+)\S+|--[\w-]+))*
          |sudo(\s+(?>-[ughpDT]\s+\S+|-\S+))*
          |timeout(\s+(?>-[ks]\s+\S+|-\S+))*\s+\S+
          |nice(\s+(-n\s*-?\d+|--\w+(=|\s+)-?\d+|-\d+))*
          |caffeinate(\s+(-[dimsu]+|-[tw]\s+\d+))*
          |doas(\s+(-[uC]\s+\S+|-[Lnns]+))*
          |stdbuf(\s+(-[ioe]\s*\S+|--\w+=\S+))*
          |command|exec|nohup|time|env(\s+-\S+)*(\s+[A-Za-z_]\w*=\S+)*
          )\s+)+(\S*/)?(kill|pkill|killall)(\s|$)
      action: reject
      message: *process_stop_route
---

# Work Closeout

Apply [task scope and authorization](../references/execution-scope.md) when
using this workflow; it defines how existing approval and task boundaries apply.

Use this skill to leave a workstream tidy and understandable. It is about
cleanup and handoff, not proving readiness; use `repo-readiness` when the main
question is whether checks pass or a PR can ship.

For task cleanup, a bulk audit, or repository retirement, read [repository
cleanup and preservation](../references/repo-cleanup.md), which owns shared
evidence, disposition, preservation, and reporting. Its helper remains read-only.

When the user asks whether work is done, ready to hand off, or safe to exit,
compose the skills in order: use `repo-readiness` first for gates and evidence,
then use this skill for final hygiene, cleanup, and parking state. Do not force
a single skill when both readiness and closeout are required. This skill owns
the final safe-to-exit answer, and that final answer must include both
`Love Gate` and `Safe to exit`. Do not reduce Love Gate to `passed`, `ready`,
or a generic approval sentence; write the two entries explicitly as `Love:` and
`Do not love:`.

## Requested reminders

When the Director requests a native reminder, read [native reminders](references/session-reminders.md).
This skill owns plain reminders and session/check-in reminders for both harnesses.
Use `scripts/reminder_save.py` for agent-created reminders; do not write one-off
EventKit or AppleScript replacements. Plain reminders use `--plain` with a title,
notes, optional web link and standard repeats. Session reminders use
`reminder_link.py`'s URL, keep notes to provider/session ID and issue/PR links,
and preserve the terminal confirmation dialog. Save only within existing
reminder-write authority. If native access is unavailable, use the writer's
dry-run fields and the reference's visible manual workflow; report that save
is pending.

## Outcome

For repositories with `DIRECTION.md`, follow the shared
[executing loop](../references/executing-loop.md) during closeout.

Closeout succeeds when the owning durable surface is current or explicitly not
applicable, readiness evidence is preserved, safe cleanup is complete or
clearly deferred, remaining work has a blocker and next action, and the final
answer gives a truthful `Safe to exit: yes`, `conditional`, or `no` verdict.
The result must also say what changed, what remains, and what was removed,
preserved, or intentionally left in place.

## Workflow

1. Identify the repo, branch, active task, and whether a PR/issue/plan is in
   play. Before saying "safe to exit," name the owning durable surface in the
   closeout answer: the PR, issue, GitHub plan, approved private recovery
   location, or explicit "none" when no owning surface exists. Also state whether that surface was
   closed, updated, left open or parked with the current blocker and next action,
   or confirmed not applicable, with evidence.
   If release, runtime, deploy, browser, or device evidence is decisive, ensure
   the owning PR, issue, plan, or doc names the exact environment checked, or
   names the substitute environment and the remaining gap.
2. If `.github/github.json` exists, read it and check metadata
   freshness. Compare the current work with
   `metadataFreshness.updateWhen` and
   with common triggers: docs routing, validation gates, primary commands,
   important workflows, health endpoints, repo relationships, JetBrains
   expectations, cleanup policy, and ownership boundaries. If metadata should
   change, maintain in-scope metadata under existing task or standing authority,
   following [task scope](../references/execution-scope.md) for stop boundaries
   or expanded scope. A read-only audit stays read-only; record any remaining
   metadata work before saying it is safe to exit.

   Also read `cleanup`. During closeout, run or report repo-configured commands
   with `when: "routine"` as closeout evidence. Commands marked `explicit`,
   `cold`, `aggressive`, or any unrecognized value are report-only unless the
   user explicitly asks for that cleanup. Never run a cleanup command whose
   effect is unclear; record the not-run reason instead.

3. Inspect local state:

   ```bash
   git status --short --branch
   git ls-files --others --exclude-standard -z
   git worktree list
   ```

   Before refreshing any checkout, read
   [local checkout refresh](../github/references/post-merge-checkouts.md). It
   owns the active-branch refresh, the post-merge default-checkout refresh, and
   the explicitly requested untracked-only exception. Its essential gates, in
   order:

   - A runtime-bound checkout moves only through the landed
     `skills/github/scripts/reconcile-runtime-checkout.py`, with the confirmed
     final landing SHA; when you only have the PR head, read the PR for its
     merge commit with `../github/scripts/gh-pr.py view <pr>`. Do not fetch,
     pull, or merge there. If reconciliation is blocked, the merge still
     succeeded, but do not claim the installed runtime is current.
   - Tracked changes or an active Git operation make any checkout report-only.
   - Untracked-only dirt is eligible only when the user explicitly asks for
     that fast-forward. Use the `ls-files` listing above, not `git status`,
     follow the reference's exception steps, including its operation-state
     checks, and hash every listed file before the merge and again after it.

   Fast-forward only to a pinned fetched commit, never a moving ref, and never
   switch, reset, stash, clean, or overwrite a checkout. A closeout refresh
   without a merge has no landing SHA and must not invent one. After merged work
   from a task branch, also check the repository's local default checkout; if it
   cannot be refreshed safely, report: `Local default checkout remains stale;
   fast-forward it before default-branch work or audits.` The active task
   worktree remains the authoritative agent source.

   Consume the landing route from readiness or the GitHub skill's
   [Merging](../github/SKILL.md#merging) procedure. When that procedure selects
   the protected-branch route for a Director without Launchplane, skip context,
   policy and train steps; local `not_configured` alone cannot override a known
   Director setup or train requirement.
   In that case, close out the protected-branch work without a Launchplane
   failure, setup suggestion or train handoff. Configured-but-unreadable
   enrollment stays unknown and is preserved with its routing next action.

   If Launchplane is configured and the shared context helper is present, call it
   once as optional closeout context for the repo/workstream:

   ```bash
   uv run <skill-dir>/../launchplane/scripts/launchplane-context.py --repo OWNER/REPO
   ```

   Use `available` context to notice pending work, preview readiness,
   deploy/product evidence, or source-of-truth links that should be reflected in
   the closeout. Treat `no_context`, `unavailable`, `unauthorized`, `invalid`,
   or helper failure as normal absence. Do not block safe-to-exit only because
   Launchplane context is unavailable, and do not copy raw helper payloads into
   issues, PRs, or final summaries.

4. If GitHub state matters for closeout, use `github` for PR,
   Actions, labels, merge state, post-merge verification, GitHub
   security/quality signals, and safe branch/worktree cleanup.
   Use `github-plan` for planning issue indexes, Project state, blocker graphs,
   and planning issue closure. This skill owns closeout judgment and hygiene;
   the GitHub skills own helper-backed GitHub writes and planning mutations.
   Before declaring an owning GitHub issue or PR settled, run
   `uv run ../github-work-rollup/scripts/github_unanswered_comments.py --thread OWNER/REPO#NUMBER`.
   Any attention result
   or degraded coverage requires a response or explicit handoff before an
   all-clear; a bot response never proves Director acknowledgement.
   When an open PR still needs ongoing CI, review, mergeability, or
   merged/closed follow-through, transfer that loop to `babysit-pr` before
   declaring the work parked or safe to exit. For an already merged or closed PR,
   a `babysit-pr --once` snapshot can be closeout evidence.
5. Use `github-plan` for durable plan state, blockers, stale/duplicate plan
   cleanup, and Project planning state. Before recording a follow-up or handoff,
   read its [Status Labels](../github-plan/SKILL.md#status-labels).
   If source-of-truth docs, runbooks, deployment notes, or another durable record
   contain the decisive completion or blocker evidence, compare that state with
   the owning issue/plan before saying safe to exit. If they disagree, reconcile
   the issue/plan/docs state or report safe-to-exit as conditional/no with the
   mismatch named.
6. Classify this task's artifacts under the shared repository cleanup policy.
   For processes, follow this skill's command policies: use the harness's task stop for your own task, or kill only an exact PID recorded when you started it; never kill by pattern.
   Clean only task-owned disposable output whose purpose is complete: transient logs,
   screenshots, temp scripts, generated scratch files, generated caches, stopped
   test containers, or other consumed temporary files.
   If the repo still has legacy `handoff*.md` files or files matching
   `cleanup.handoffArtifacts.temporaryGlobs`, delete or migrate them once their
   content is captured in the owning GitHub issue or PR comment unless they are
   intentionally preserved as committed docs.
   IDE paths follow `../references/ide-configuration-policy.md`: preserve ignored
   IDE state in retained checkouts; inspect mixed tracked files hunk by hunk and
   retain the canonical shared form plus safe hunks. Never blanket-revert, clean,
   stash, or overwrite unrelated IDE changes. Whole-worktree retirement requires
   the shared cleanup policy's protected-file, preservation, ownership, and
   authorization evidence.
7. Do not remove user artifacts, broad system caches, unrelated untracked files
   or unrelated remote resources without explicit approval. Use `github` for
   task-owned branch cleanup. Before deleting task-owned testing or preview
   resources, establish disposability under current overall and repository
   direction, including their in-use, only-copy, production-isolation and
   live-data safeguards. For Launchplane-managed lanes, use the
   [disposable-lane observations](../launchplane/references/write-action-helper-contract.md#disposable-lane-observations)
   for that evidence. For Launchplane-managed resources, use
   [contract-backed lifecycle routing](../launchplane/SKILL.md#contract-backed-lifecycle-and-repair-routing).
   For other resources, use [infra-ops](../infra-ops/SKILL.md#workflow) for
   documented provider lifecycle operations under existing task authority.
8. Before a safe-exit all-clear, complete [claim disposition](#claim-disposition)
   for this native session on either harness. Updating status or preserving a
   PR does not release ownership. Keep the claim while a required review is
   pending and this session is expected to resume; end that turn without a
   safe-exit all-clear.
9. Report final state concisely.

## Claim Disposition

Before reporting `Safe to exit: yes`, account for every claim this session took,
including earlier turns, other repositories and PR-only work. Run:

```bash
uv run <skill-dir>/scripts/check_claims.py --harness codex \
  --session NATIVE_ID --transcript /absolute/native-transcript.jsonl
```

Use `--harness claude` on Claude Code. Both use the same procedure. The native
transcript must belong to that exact session and retain its timestamped history.
The check inventories every installation of the existing configured automation
writer Apps, including closed issues and PR-only claims. The read-only App does
not define source ownership. Each repository uses its existing configured writer
route; no credentials, grants or identities are changed. Incomplete installation,
repository or discussion reads keep coverage incomplete and are rechecked live.
Native receipts cross-check exact older sources; repeated `--repository OWNER/REPO`
adds known context, and `--owner OWNER` asserts account coverage. Neither option
narrows the complete installation inventory. No refused request is retried with
another identity.
Claude's native session subagent directory is included because its delegated
tools may inherit the parent's session ID. Codex's native read-only parent/child
index supplies child thread IDs and rollouts, including descendants and closed
edges. Child-owned claims and claims using the inherited root session ID are
both included. Missing native graph/schema/rollout evidence refuses coverage.
Full-history forks use their native child boundary after ancestry validation;
copied parent activity is evaluated in its parent, and coverage binds the raw
child artifact. A user-started fork with only its own native identity follows
the ordinary identity check; it does not need a spawned-child edge.
Missing native identity or a different running harness refuses an all-clear.
Preserve its coverage
and released-claim links as closeout evidence; it proves claim disposition only,
not readiness or permission to discard artifacts.

For a held own claim, first finish or durably hand off its work and update Current
Status with the next actor, action and retained artifacts. Use GitHub's maintained
comment helper to post `Released claim ID` as the first line, with the exact
native session ID, handoff evidence and every retained PR's full URL in the body.
This is the existing source-authored release, not `release-claim`'s Supervisor
attestation of an ended session. It releases this ownership only. Read the
[claim contract](../github/references/cli-reference.md#planning-claim) before
releasing or repairing a retained-PR handoff. Never release another session's
claim. Run the check after the write; only exit 0 with `claims_clear: true`
and complete coverage permits an all-clear. Leave its JSON visible in the native
tool output; use `tee` if a saved copy is needed. The Supervisor's safe-verdict
reader requires that fresh invocation result to cover every native source claim.
Missing or redirected claim output does not exempt ownership. Canonical session
inventory finds claims independently of command spelling or generated source.
Never repeat a write to manufacture evidence for an earlier invocation.
Histories with no shell execution or ownership activity keep the N/A path
without GitHub access. Any shell execution requires this check, including
ordinary reads. Claimed children require all native IDs in the
parent's check; later child activity requires a fresh check.

The checker does not interpret generated programs or prove covered writes.
Uncertain native activity is pending, rechecked against current native execution,
queue and process evidence on every closeout, claim and next invocation. It
clears when the native session is verifiably gone; absence from a registry alone is not
proof. This clears uncertainty only. Every actual claim still requires its exact
release or verified recovery release. Historical pending output is never a
permanent refusal before live readback. An active check may exclude only its
own exactly identified native execution; other running work keeps it pending.
Shared history roots do not select a native home: the active check reads every
matching authenticated home, requires one active home with its bound execution and
stable quiet copies elsewhere. Unavailable or conflicting facts stay pending.

Run the checker as its own native shell invocation, optionally piped to `tee`,
with one checker envelope visible. A saved report, final prose or an earlier
check before later native activity cannot attest current disposition. In Codex
use one literal `text(await tools.exec_command({cmd: "CHECK COMMAND"}));`
cell. Poll with one literal `text(await tools.write_stdin({session_id: PROCESS_ID}));`
cell using its returned handle. Keep saved output and other polls separate.
For long checks allow at least 600 seconds on Claude Bash, or collect Codex's
foreground process handle with its isolated polls. Do not background the check.
Claude's records sharing one API message ID have one launch boundary: finish
ownership before launching a separate check. Check one at a time.

A slow startup can snapshot after the checker's own first poll; the proof still
binds its original isolated launch and every empty poll of that same handle.
Native coverage reads standard, account and retained source homes through one
observation scope. Required copies must answer; a stopped socket is not an exit.
Process coverage requires native PID, executable and incarnation evidence.
Kernel-confirmed PID absence also requires a subsequent native queue and runtime
fence before it can support completion. A live, reused or unreadable PID stays
unknown. Reported `native_coverage` describes the inventory observation; it does
not attest that the later physical proof passed. An exhausted native-proof
deadline is reported separately. Status-only reads authenticate their own
history without reconstructing descendant claim history; the claim checker
still verifies every native child, raw digest and exact claim disposition.
For an unavailable home, the [managed launcher's diagnostic](../supervisor/scripts/codex_session_proxy.py)
keeps the source account and names its existing stopped-daemon remedy.
Unbound clients keep the refusal; use the maintained managed launcher for new
work. The checker never restarts daemons or terminates processes.

Native receipts use literal, command-bound outputs; arbitrary stdout and nested
saved JSON cannot supply execution completion or a fresh checker result.
Computed or detached code is not reconstructed. Current native evidence decides
whether its uncertainty remains pending. If that evidence is unavailable, record
the exact blocker and keep conditional/no while continuing independent work.

An open PR need not merge before a session exits. Its exact authored release
must name the retained PR, and the check verifies the same repository, source
identity and independent issue link under the successor's existing handoff
contract. A PR-only claim uses the PR itself as its work record. Preserve the
branch/worktree and the next actor's custody. A merely prose handoff, an
unreleased claim, missing transcript, inaccessible repository, incomplete
inventory or unverified retained PR keeps the verdict conditional/no. Record the
reason and continue independent work; do not turn a failed read into no claims.
If scope outside the reported installation coverage remains uncertain, do not
claim a complete inventory. The Supervisor's post-process release is recovery,
not a substitute for this executing-session check. Recovery after verified process
exit still reads the original unqualified declaration and checks live activity,
so historical ended sessions without a pre-exit receipt can release through the
existing Supervisor attestation route.
Physical native observation uses independently verified inactivity without
requiring claim disposition. A live Claude task clears through its bound terminal notice or
successful task stop. Recovery release still
requires the original declaration and its existing attestation contract. Normal Supervisor
close-out candidates and guarded terminal cleanup consume the accepted verdict.
After recovery, resume the source session and run this check: it accepts validated
Supervisor/Direction releases as well as exact source-authored releases, rereading
their closure evidence and retained PR state. A bare exact release supplied by
the claim helper remains valid; an explicit conflicting session field refuses.
For missing native identity, ambiguous resume/fork histories or inaccessible
scope, preserve the active process and terminal, record conditional/no and the
blocker on the item, and continue independent work. Post-process recovery applies
only to a session that already exited; it is not a way to force an unaccepted
session to exit. Follow the Supervisor's
[claim-gated verdict procedure](../supervisor/references/helpers.md#claim-gated-verdicts)
when it is overseeing the worker. Do not edit native transcripts or substitute
another session ID.

## Consuming Readiness Evidence

When `repo-readiness` has just run, consume its handoff instead of rerunning the
same gate discovery by default. Verify the evidence is fresh for the current
branch, PR, and commit, then use it to decide whether closeout can proceed:

- Status: ready, not ready, partially ready, or blocked.
- Required gates: checks inferred from `.github/github.json`, repo docs, CI, and
  the changed surface. Keep focused local test evidence separate from the full
  CI gate under [repo-readiness](../repo-readiness/SKILL.md#gate-selection) and
  the [test command contract](../github/references/config-schema.md#workflow-metadata).
  Under the split, `ready` needs green CI for the current head. Reuse passing evidence;
  closeout alone does not require another local test run. Preserve focused-test
  and affected safety-module evidence and explicit repository requirements.
- Passed, failed, pending, and not-run evidence with concrete reasons.
- Metadata/docs impact: whether `.github/github.json` or docs changed, were
  checked, are stale, or were intentionally not updated.
- Background review: consume the target, observation time or head SHA, and the
  canonical point-in-time state from the readiness handoff without re-inferring
  a terminal outcome from absence.
- Next action: the smallest step that would change readiness.

If the readiness handoff is missing, stale, tied to a different commit/PR, or
does not cover the current closeout question, run `repo-readiness` first or
record the gap as a conditional/no safe-to-exit reason. Do not treat closeout
cleanup as proof that gates passed.

Both this skill and `repo-readiness` read `.github/github.json` with the same
schema expectations: `qualityGate`, `docs`, `metadataFreshness`, `cleanup`,
`importantWorkflows`, repo relationships, health signals, and ownership or
Launchplane routing when present. Readiness uses those fields to decide what
must be verified; closeout uses the same fields to decide what final evidence,
metadata updates, and cleanup remain.

## Background Review State

Use `../references/background-review-reporting.md` for target matching,
point-in-time vocabulary, and durable wording. Never infer `skipped`, `not
emitted`, `did not run`, or another terminal state merely because no lifecycle
evidence is visible before the final response.

Take a point-in-time read when available, but do not poll or delay the final
response solely for a review that can only start after that response. `Not yet
observable` is non-terminal and does not block `Safe to exit: yes` when every
other gate is satisfied. A matching review that is already `in flight` remains
pending evidence and keeps the verdict conditional. Preserve positive terminal
evidence exactly, including cancellation, supersession, failure, skip reason,
and completed findings or no-findings results. `Observation unavailable` keeps
the verdict conditional unless the owning policy explicitly says that review
surface is not required for the current task.

Never wait indefinitely for a matching review. Use a bounded observation when
it is useful; if the review remains `in flight`, report the verdict as
conditional. Cancellation and supersession are terminal but are not clean
reviews. The never-started exception applies only when a later observation can
read the lifecycle surface and still finds no matching evidence because no
review ever started; then no corrective follow-up is required. If matching
terminal evidence appears after an earlier `not yet observable` summary, always
preserve that original point-in-time statement and add a follow-up; the
never-started exception cannot apply.

When writing final summaries, closeout comments, or migrated handoff content,
follow [Agent Comments](../references/talking-with-the-director.md#agent-comments)
for comments: link point-in-time evidence and keep only otherwise unrecorded
recovery-critical facts in GitHub for GitHub-backed work.

## Safe To Exit

Treat "safe to exit" as strict hygiene, not merely context preservation. Safe
to exit means work is ready and hygiene is complete, or unfinished work is
intentionally parked with durable state.

Any final answer to a safe-to-exit, wrap-up, closeout, pause, or handoff prompt
is incomplete unless it includes a `Love Gate` section.

Safe to exit: yes

- Work is complete or explicitly out of scope.
- Gates, inspections, docs checks, metadata checks, and post-merge checks are
  done or explicitly not applicable.
- The owning durable surface was named as closed/updated with evidence, or no
  owning PR, issue, GitHub plan, or approved private recovery location was in play.
- Background review state follows the point-in-time contract above.
  Current-target findings are accounted for under
  `../references/model-review.md`;
  no matching review is `in flight`. `Not yet observable` is permitted when a
  post-turn trigger may still occur and the answer says so explicitly. Activity
  in detached `auto-review-<hex>` worktrees for older targets is not a gate.
- PR, issue, and GitHub plan state is current.
- This native session's claims are released, with complete readback evidence
  under Claim Disposition and verified retained-PR handoffs where needed, or
  GitHub claim disposition is explicitly not applicable. Held or unread claims
  prevent an all-clear even when every readiness and artifact check passes.
- The branches and worktrees this task created or adopted are accounted for:
  eligible completed ones are removed under the shared cleanup policy, and each
  one that stays is named with its reason and what will remove it. Work from the
  task's own record of what it created, checked against `git worktree list`; a
  path or branch name that looks like this task's is not evidence. A merged task
  checkout left unaccounted for is unfinished closeout; one retained for a
  stated reason is not.
- No important untracked artifacts, transient processes, or hidden follow-up
  remain.

Safe to exit: conditional

- Work is unfinished but intentionally parked.
- Blockers and next steps are recorded in a PR, issue, or GitHub plan, or, when
  the user explicitly asks for private/offline parking, an approved reconstructable
  location verified with the
  [local-only preservation checks](references/parking-and-handoff.md#local-only-preservation-checks).
- The owning durable surface is named, current, and contains the blocker or next
  action needed to resume.
- Failing and not-run checks are recorded with reasons.
- Current auto-review blockers, if any, are recorded in the PR, issue, or plan
  that owns the unfinished work.
- Remaining docs, metadata, security, or quality follow-up is tracked durably.
- No transient local state is required to resume.

Safe to exit: no

- Uncommitted or unexplained work remains.
- Expected gates/readiness checks have not been run and no reason is recorded.
- An owning issue, PR, GitHub plan, or approved private recovery location remains
  stale, incorrectly
  blocked, or missing the completed/remaining work state.
- Source-of-truth docs or runbooks disagree with the owning issue/plan and that
  disagreement has not been reconciled or explicitly parked.
- Failing checks, docs/metadata/security follow-up, PR/CI/review state, or
  cleanup work is unresolved and untracked.
- Temporary artifacts or processes could confuse the next session.

For code changes, broad practical lint/static analysis and `jetbrains-inspection`
state must be included in closeout, or there must be a documented not-run reason
or intentional parking decision. When `.github/github.json` defines
`qualityGate.inspection`, missing JetBrains evidence for code changes prevents
`Safe to exit: yes`; use conditional/no until the inspection is clean, findings
are fixed or tracked, or the not-run reason is explicit. If the inspection
was a recorded `UNKNOWN` under the `repo-readiness` milestone exception, keep
that verdict and its out-of-milestone follow-up visible in closeout; the
authorized merge does not make the inspection clean. If the inspection config
is blank or feels wrong, ask the user before changing durable policy or
trusting a suspicious value; for a one-off local check, prefer the helper's safe
inferred route with `changed_files` scope and report the assumption.

## Parking Work and Handoff Surfaces

Before parking unfinished work or migrating a local handoff, read
[parking and handoff procedures](references/parking-and-handoff.md). Preserve
one current durable home with its blocker, next action, and verification state.
Use an authorized owning issue or PR when available; valuable local work may use
a known, approved, reconstructable durable location. Apply the plan hygiene
checks below as relevant.

## Plan Hygiene

- Use `github-plan` plus the sibling `github` helpers to update the active
  issue's `Current Status`, finish line, labels, and blockers before parking
  work. Projects display that record; the close flow sets automatic Status.
- For issue-backed work that is resolved without a merged PR, close the owning
  issue with a concise evidence-backed comment, or leave it open with the current
  blocker and next action. Do not rely on a local summary as the only record.
- Before parking or closing a workstream, run a Plan Direction Checkpoint:
  identify the next action, how it fits the current plan, whether the plan or
  issue graph changed, and where that durable state was updated.
- If the work revealed a new blocker, dependency, sub-workstream, or stale
  assumption, update the GitHub issue graph before relying on a handoff summary.
- Treat an accurate issue graph as closeout evidence. A handoff that describes
  work not represented in the owning issue, PR, or related issue graph is
  incomplete unless the user explicitly asked for private/offline parking and it
  meets the [local-only preservation checks](references/parking-and-handoff.md#local-only-preservation-checks).
- After a landing, follow github-plan's
  [Close Or Hand Off](../github-plan/SKILL.md#close-or-hand-off) reconciliation.
- Mark completed checklist items, record blockers, and remove or rewrite stale
  assumptions in the GitHub plan issue.
- Before declaring safe to exit after closing or merging implementation work,
  inspect remaining open plan issues through `gh-plan.py show`'s derived status.
  Reconcile native dependencies with `link`/`unlink` and person/event waits with
  `wait`, using existing evidence. Preserve deliberate scope holds and unresolved
  wait entries. Verify automatic Project Status reflects closure, re-read after
  supported updates, and leave manual Project fields untouched. The main LLM
  owns evidence review and verification; read-only agents may summarize mismatches.
- If workflow metadata changes are deferred, record the exact `.github/github.json`
  follow-up in the GitHub plan or closeout remaining items.

## Git And Worktree Hygiene

- Preserve unrelated user changes.
- Do not use destructive Git commands to bypass required evidence or authorization.
- For task cleanup, bulk audits, or repository retirement, apply the shared
  [repository cleanup and preservation](../references/repo-cleanup.md). Preserve
  unique or ambiguous state while investigating; reuse same-action/scope
  authorization and ask only for a missing choice or expanded scope.
- Before saying a checkout is tidy, refresh or report it under step 3.
- When asked to remove a worktree, establish uncommitted changes' disposition
  under the shared policy and retain unresolved or unpreserved state.
  Removing a worktree is not approval to lose its branch or local edits.
- Use `github` for PR-backed branch/worktree cleanup and GitHub
  state.
- Removing an eligible completed checkout this task created needs no new
  question. Eligible still means the shared policy's ownership, live-use, and
  contents checks; dirty or unpreserved state is retained. This covers the
  worktree and its merged local branch, not a remote branch. The task's record
  includes its claim on the owning issue, which names the branch. A worktree on
  that branch is this task's, even after compaction or a handoff. When its lock
  reason names a retire command, use that command. When the task's
  record does not show that it created a registered worktree, the owning session is
  unknown: leave it, and mention it when it bears on this closeout. Detached
  auto-review worktrees stay excluded as described below.
  [Task worktrees](../github/references/worktrees.md) documents portable Git
  removal and per-host helper retirement. Creation through the catalog helper
  does not replace a host's lock or guarded retirement policy.
- After merged PRs, include relevant post-merge Actions and GitHub
  security/quality signal outcomes when GitHub data is available. Report signals
  as clean, findings, unavailable, or not enabled; do not treat unavailable or
  not-enabled signals as clean.
- Concrete reproducible broad-gate findings that are not fixed now should be
  tracked after a duplicate search. Group speculative or huge-baseline findings
  into a cleanup plan/report instead of opening many issues.
- If cleanup safety remains ambiguous after the available investigation,
  preserve the candidate and name the exact missing evidence or choice.

## Auto Review Worktrees

Host auto-review flows may leave detached review worktrees, such as those the
retired Every Code harness created under:

```text
~/.code/working/<repo>/branches/auto-review*
```

Treat these as external review context, not the active workstream.

- Ignore them for normal safe-to-exit and dirty-worktree decisions.
- Do not treat their files as blocking the current repo closeout.
- Exclude them from ordinary cleanup. In an authorized bulk or retirement scope,
  preserve them unless the shared policy establishes owner/job completion, lock
  and runtime state, valuable contents, and exact disposition.
- Mention them only when relevant, for example: "Ignored detached auto-review
  worktrees."
- If the user asks about a review result or review worktree specifically, switch
  context deliberately and inspect that worktree as the task target.

## Required Love Gate

Before finalizing the closeout, perform a "Love Gate" check. This is an
emotional and qualitative alignment step where the agent evaluates the session's
output against the user's ultimate satisfaction and the agent's own engineering
standards.

- **Check if you "love" the work**: Does the implementation feel clean, idiomatic,
  and complete? Is the solution robust, or does it feel like a "just-in-case"
  patch?
- **Identify what you do not love**: Are there any compromises, technical debt,
  missing edge cases, or "smells" that remain? Be honest about shortcuts taken
  due to context limits or task complexity.
- **Report findings**: Always include a brief "Love Gate" section in closeout
  and safe-to-exit final answers. The section must include two explicit labels:
  `Love:` for what you love about the result, and `Do not love:` for concerns,
  compromises, or "nothing material" when no meaningful concern remains.

This gate ensures that the session ends not just with technical passing, but
with a shared understanding of the work's quality and "soul."

If you think the user's name is Justin, end with one short dad joke about the
session after the safe-to-exit answer.

## Output Format

Use a compact closeout report in chat. For a GitHub comment, follow Agent
Comments above and link the owning records instead of copying this report:

- Done: what changed or was handled.
- Remaining: concrete blockers or follow-up work, including how the next action
  fits the active plan.
- Checks: gates, inspections, docs, metadata, CI/Actions, and GitHub
  security/quality signals that passed, failed, were pending, or were not run.
- Background review: target, point-in-time state, observation time or head SHA,
  and terminal evidence when one was positively observed.
- Love Gate: include `Love:` and `Do not love:` entries. Use `Do not love:
  nothing material` when no meaningful concern remains.
- Cleanup: artifacts, plans, handoffs, branches, or worktrees removed or left
  intentionally.
- State: dirty files, PR status, CI status, or plan status when relevant.
- Owning surface: PR, issue, GitHub plan, approved private recovery location, or
  none; say whether
  it was closed, updated, left open or parked with current blocker and next
  action, or not applicable.
- Safe to exit: yes/no/conditional, with the condition if needed.

If there are no remaining items, say so plainly. Do not over-explain command
output unless the user asks for it.
