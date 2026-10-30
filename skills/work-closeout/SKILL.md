---
name: work-closeout
description: Use when the user requests a native reminder, including plain repeating reminders or reminders to return to an exact Codex or Claude Code session or start a repository check-in, or asks to wrap up, clean up, close out, pause, park, hand off, determine what remains before stopping, preserve plan direction for the next session, update or remove stale plans/handoffs, reconcile issue graph state, remove transient artifacts, or asks whether they can exit. Coordinates GitHub plan cleanup, safe git/worktree hygiene, artifact cleanup, and final state summaries. Safe-to-exit and closeout final answers must include a Love Gate section with explicit `Love:` and `Do not love:` entries before the safe-to-exit verdict.
metadata:
  short-description: Close out workstreams cleanly
resources:
  - path: scripts/check_claims.py
    kind: script
    description: Reads a session's claim releases and retained PR handoffs on GitHub before a safe-exit verdict.
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
    example_argv: ["uv", "run", "scripts/check_claims.py", "--owner", "OWNER", "--session", "<native-id>", "--transcript", "/absolute/native-transcript.jsonl"]
    purpose: Reads only the session's journaled and transcript-recorded claim targets, then verifies exact releases and retained PR handoffs; never writes.
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
   all-clear under the [radar rule](../github-work-rollup/SKILL.md#external-comment-radar).
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
   for this session on either harness. Updating status or preserving a
   PR does not release ownership. Keep the claim while a required review is
   pending and this session is expected to resume; end that turn without a
   safe-exit all-clear.
9. Report final state concisely.

## Claim Disposition

Before reporting `Safe to exit: yes`, account for every claim this session took,
including earlier turns, other repositories and PR-only work. Ownership lives
on GitHub: the claim comment, and its exact release.

For a held claim, first finish or durably hand off its work and update Current
Status with the next actor, action and retained artifacts. Then release it with
the `release_command` the claim printed (`gh-plan.py release`), naming every
retained PR's full URL in the handoff. An exact release frees the work for a
successor at once. It releases ownership only. Never release another session's
claim. Read the [claim contract](../github/references/cli-reference.md#planning-claim)
before releasing or repairing a retained-PR handoff.

Include `Next actor:` in the handoff, or pass `--next-actor` to release;
updating Current Status alone does not supply the required release actor.

Then read the record back:

```bash
uv run <skill-dir>/scripts/check_claims.py \
  --session NATIVE_ID --transcript /absolute/native-transcript.jsonl
```

The check takes its targets from the session's private host-local claim journal
and recorded claim results in its own native transcript. `gh-plan.py claim`
records the exact repository and issue or PR number after target preflight and
before posting. Failed or interrupted attempts stay targets until GitHub
readback accounts for them. Children claiming under the root session ID enter
the same journal even when their output is absent from the root transcript.

For a session that began after the journal shipped (`JOURNAL_SHIPPED` in
`check_claims.py`), the check decides journal coverage before running the
reconstruction below. Every recorded claim result must have a matching row;
a result naming another expanded session must match that session's own journal,
which is checked independently. Recorded results include advertised native saved
and background output. Missing artifacts or invocations retain reconstruction.
A modern session or native child with no claim
rows and no unmatched result needs no reconstruction. Every claim attempt must
also have settled: before and after the
GitHub reads, no process outside the check's own callers is a `gh-plan` command
naming the session or a comment POST to one of its journal targets; no journal
row was written in the last 10 minutes; and the journal is unchanged from the
one snapshot the check read its targets from. The GitHub search below still runs, and each claim it finds is
checked. Other sessions keep the reconstruction.

For older helpers and mixed sessions, the check reads linked local native child
transcripts recursively: Claude's own `subagents` directory, Codex's existing
native child index and non-interaction `SubAgentActivity.agent_thread_id` events. Peer
interaction and native-indexed sibling completion events do not establish parent
edges; each sibling stays covered through its verified native parent. Contradictory parent edges refuse. It never
scans rollout directories. A missing indexed or advertised child refuses;
restore its transcript on the claiming host or pass that child's native
`--session` and `--transcript`. Supplied child paths can restore moved evidence.
Keep the journal with its session; another host also needs the complete native
lineage and recorded claim results.

For older helpers, the reader reconstructs bounded literal invocations and
native outputs without executing recorded programs. A shell command it cannot
decode, such as one that names the helper through a shell variable, can be
settled by GitHub's own record instead: every claim comment shows its session
ID, so one search of the App's account for that ID finds the claims made under
that session ID, and each one found is checked like any other target. The
search settles a command only when the reader shows it finished in the
foreground, with no `&` launch, at least 10 minutes before the check; only when
the search returns complete results that include every claim the check already
knows; and only when the App configured now has a single installation and no
client App. A code-mode cell settles only when the reader proves it finished,
which it cannot for cells that leave a process running.

For a Codex source killed by a host reboot, add `--termination-record /absolute/original-record.json
--confirm-session-ended` to the check after independently verifying native task
and remote peer closure. Use the original source-host record from Supervisor's
[crash recovery](../supervisor/references/helpers.md), with the original native
identity, terminal, transcript roots and retained artifacts. Pass one original
session per recovery check. The existing termination verifier must prove that
the source and every discovered native child ended before the current boot.
It rechecks their transcripts, resumed processes, terminal and retained artifacts
before and after canonical claim reads. The boot time supplies the latest possible
completion bound for interrupted historical calls; complete GitHub search after
the same indexing delay and exact release checks still apply. Missing reboot
proof, a resumed child, changed lineage, or incomplete search keeps coverage
conditional. This reads evidence only and never releases claims or edits the ledger.
Run this recovery before retiring retained worktrees: the original record's
artifacts remain part of termination proof. Missing or changed artifacts keep
the result conditional and must be reconciled through their owning handoff.
For large historical lineages, `--read-timeout SECONDS` selects a positive finite
aggregate read budget (ten minutes by default). The same deadline covers
canonical reads and all source proofs/rechecks; it never resets per child or
pass. Idle-observation and native RPC limits remain unchanged.

The search does not prove absence. It relies on GitHub's index catching up
within those 10 minutes, and it cannot see a claim an undecodable command made
under another session ID, through another App or account, or from a host with
other configuration. The claim journal still records every claim the current
helper makes on this host. A command that detaches work another way, such as
through `tmux` or `launchd`, is background work under the rule below.

A command that cannot be settled stays in `unresolved_invocations` and the
verdict stays conditional while known targets are checked. When a search ran
and could not settle it, the reason is in `coverage.search_errors`. When
`unresolved_invocations` remains, or when recovering older/remote-host evidence,
read [recorded claim discovery](references/claim-discovery.md) for supported
program/data boundaries and recovery. Use an explicit literal `--repo OWNER/REPO`
or qualified record reference in claim commands.

The check reads those issues and PRs, including closed records, plus the one
search and the App's installation list when a command needs settling. It never
scans repositories. It verifies every
claim on each target against its exact authored release or validated Supervisor
release, and checks each retained PR's handoff. Repeat `--session` and
`--transcript` for any other session to include. A transcript belonging to
another session, malformed claim evidence or an incomplete GitHub read refuses.
Unrelated truncated transcript noise is ignored; a malformed recorded claim
is not.
`--repository OWNER/REPO` and `--owner OWNER` are compatibility context flags;
they cannot narrow the recorded targets or request a fleet scan. Only exit 0
with `claims_clear: true` and complete coverage permits an all-clear. The result
reports its GitHub reader `request_count`, including session searches.
Keep the verdict conditional/no on a held claim, unverified retained PR or
incomplete read. A later-deleted record or removed App access remains unreadable
evidence, never a release. Preserve the journal and owning issue; restore the
authorized read path, or record an irrecoverable-evidence decision for the
Director. Do not silently retire the target or manufacture an all-clear.
The Supervisor runs the same check for any session.

An open PR need not merge before a session exits. Its exact release names the
retained PR, and the check verifies the same repository, source identity and
independent issue link under the successor's handoff contract. Retained work is
a PR on the claim's branch, unless it was opened after the release and does not
link the issue, or an open PR by the automation identity that links
the issue and is named in the release or opened during the claim, the same rule
`gh-plan.py release` uses. A PR the release mentions only as context is not
retained work. Explicit `--exclude-pr` classifications are recorded by the
release helper and honored by closeout for other branches. They never hide
the claim's own branch or subject PR. For an older release that lacks this
attribution, rerun its exact release command with the reviewed handoff and
`--exclude-pr`; the helper checks current PRs and appends an authored source
handoff without editing the original release. An unknown or malformed
classification keeps closeout blocked. Merge-train batch PRs (head under
`launchplane/train/` with Launchplane's batch marker) opened during the claim
are excluded unless the release names them or they use the claim's own branch. A PR-only claim
uses the PR itself as its work record. Preserve the branch, worktree and the
next actor's custody.

The check does not stop processes. Stop or hand off any background work you
started before the verdict; leaving it running is a closeout problem, not
something the check proves.

A session that crashes cannot release. Its claim frees itself when its lease
expires: the catalog's hooks renew a host-local lease on the session's activity
(see [Planning: Claim](../github/references/cli-reference.md#planning-claim)),
and after 30 minutes without activity a successor on the same host may take
over, recording a `Lease takeover:` line in its claim. Set
`GH_PLAN_CLAIM_LEASE_MINUTES` when claiming to change the period the claim
records. A host with no lease for
the session keeps the claim; a Supervisor or direction session releases an
ended session's claim there with `gh-plan.py release-claim`. The lease is crash
recovery, not a substitute for releasing.

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
- Current-target review findings are accounted for under
  `../references/model-review.md`.
- PR, issue, and GitHub plan state is current.
- This session's claims are released, with complete readback evidence
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
are fixed or tracked, or the not-run reason is explicit. Keep an `UNKNOWN`
verdict and its follow-up visible in closeout; an authorized merge does not
make the inspection clean. If the inspection config
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
  that branch is this task's, even after compaction or a handoff. Once work is
  complete or handed off, release and verify its claim, then follow
  [qualified retirement](../github/references/worktrees.md#retirement) from
  outside the tree. When the task's
  record does not show that it created a registered worktree, the owning session is
  unknown: leave it, and mention it when it bears on this closeout.
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
