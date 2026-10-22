# Merge-Train Execution

Read before advancing or diagnosing a train. The entrypoint retains the runtime
authority, mutation gate, and exact landing-SHA checkout handoff requirements.

- **Before Enqueueing**: Run the controller without `--mutate` and inspect the
  intended PRs in `result.dry_run_result.queue`. The current GitHub adapter
  checks both the PR author and the actor who last applied the enqueue label
  from active Launchplane policy,
  using the label's latest `labeled` event. Enqueue with the trusted automation
  identity's installation token. The labeler must be trusted automation or have
  a role in `allowed_actor_roles`. A label applied by a GitHub App acting with
  a user's token is always refused, regardless of the user's role.
  An unauthorized labeler is reported as
  `<enqueue_label> label ignored: applied by <login> ...`; the same prefix
  also covers unreadable labeler evidence
  and an App acting for a user. Report those refusals;
  do not work around it by relabeling under another identity. Check
  `actor_role` (the author's role), `eligible`, `ineligible_reasons`, checks, and head SHA before
  adding ready labels. If the trusted automation identity is refused, use
  **Capability Scope** below to diagnose the policy grant; relabeling cannot
  supply it. A missing ready label alone is the expected state before
  enqueueing; an unauthorized author remains ineligible after labeling. When an
  active candidate or landing is being reported instead of a fresh queue, use
  the current policy and GitHub author and label-event evidence. A queue covers the PRs examined
  in that phase; stacked-PR phases can show only the root. Missing queue evidence
  does not mean that the queue is empty or that the intended PRs are eligible.
- **Capability Scope**: GitHub App installation permissions, Launchplane's
  repository/base author allowlist, and the train's merge identity are separate.
  An installation-wide Actions grant can enable dispatch/rerun across covered
  repositories without admitting their PR authors to a train. Resolve the actual
  App IDs used by the agent and each train; do not assume they are different.
  An installation permission change affects every consumer of that installation.
  Compare their service-enforced credential contracts before expanding it;
  a consumer can reject permissions beyond its ceiling. Before requesting a
  missing grant, inspect the remaining capabilities needed by the intended
  workflow and collect verified gaps into one concrete decision. When asked for
  cross-repository coverage, or when the same setup gap recurs in another
  repository, compare the live authorized repository inventory with active
  train targets and intended automation authors. The
  configured targets are not the whole fleet: report unenrolled repositories
  separately, and keep archived or intentionally excluded repositories visible.
  Repository metadata can indicate intended integration but cannot establish
  runtime enrollment. Preserve existing authorization for unchanged
  scope; additional identities or repositories remain explicit grant decisions.
  Keep real target lists and numeric identities in runtime records or reviewed
  admin input. This preflight uses reads only and adds no approval gate.
- **Automation Roles**: Inventory PR authors separately from check publishers,
  reviewers, and merge actors. A bot that only reports checks or reviews needs no
  enqueue-author grant. A shared provider identity such as `github-actions[bot]`
  does not identify one specific controlled workflow. When reviewing an author
  grant, state whether the policy can constrain the intended repository and
  workflow provenance; an author-ID allowlist alone cannot express the latter.
- **Controller Semantics**: Each call advances one safe phase at a time:
  same-repo linear stack-collapse planning/execution when needed,
  collapsed-root admission, candidate plan/build/observe, landing-plan
  creation, PR-native landing, and child PR disposition.
- **Landing Contract**: Read Launchplane's canonical
  [PR-Native Landing](https://github.com/cbusillo/launchplane/blob/main/docs/merge-train-policy.md#pr-native-landing)
  section for the provider target, completion evidence, and single-entry,
  multi-entry merge-method, legacy, squash/rebase and stack exceptions. Use
  [Batch And Recovery](https://github.com/cbusillo/launchplane/blob/main/docs/merge-admission.md#batch-and-recovery)
  for admission/outcome reconciliation. Let the controller perform that path;
  constituent completion is not an instruction to merge constituents by hand.
- **Stacked PRs**: For a same-repo linear stack, label the root PR that
  targets the protected base branch and every child that is ready to land,
  since collapsing merges each child into the root. Launchplane collapses a child only when it is
  itself ready to land: open, not a draft, labeled by an allowed enqueue actor
  or admitted as an allowed dependency update, and from an allowed author.
  Otherwise the controller reports `stack_unsupported` with a
  `blocking_reason` naming the child. To land the root while a child stays
  held, retarget the held child to the protected base branch; never ready or
  label a held child just to unblock its parent. Let Launchplane collapse child branches
  into that root, wait for the root head SHA to satisfy checks, admit only the
  root to the flat train, and resolve child PRs after the root lands according
  to policy. Treat forked, ambiguous, sibling, cyclic, stale-head, or
  permission-limited stacks as blocked/unsupported instead of mutating by hand.
- **Retry Model**: Repeated controller calls are expected; let
  `scripts/launchplane-train-drive.py` make them. The CLI holds an OS file lock
  per repository/base train in the user's shared cache, across worktrees.
  A block selecting another PR reports `needs_owner` with both PR numbers,
  rather than failing the tracked PR. When the controller confirms that other
  PR's block was applied and its policy permits continuation, the driver proceeds.
  Blocks on the tracked PR, unsupported stacks and blocks without a selected PR
  remain failures.
  A second local driver exits with `needs_owner`, naming the running PR and
  start time and suggesting enqueueing the PR for that driver. If that driver
  exits before the queued PR lands, rerun the refused driver; a driver stops
  when its own PR lands and does not promise to drain unrelated work. Process exit
  releases the lock; stale metadata never keeps a train locked. Drivers on
  other hosts still obey Launchplane's controller lease.
  Three consecutive refusals with the same code and this driver's PR number
  stop with `needs_owner`, preserving the code, HTTP status and trace ID.
  Progress, a different refusal, and lease contention reset that streak;
  unassigned refusals keep the existing helper-failure budget.
  It pauses on every non-terminal
  state, treats a candidate made stale by a queue change as rebuildable, waits
  out a block raised only because the batch candidate's checks are still
  running or another driver holds the controller lease, treats a branch the
  controller refreshed as progress, and stops early with `needs_owner` when
  the dry run's `intended_next_action` is `update_branch` and the driver may
  not refresh that PR, even if the controller reports `wait_for_root_checks`.
  `--allow-branch-update` permits refreshing only the driver's own selected PR.
  It stops on other blocks, failed,
  ineligible, closed, deadline, no response, or repeated controller refusals,
  with each refusal's error code, HTTP status and trace ID. A candidate failure reports the
  failing checks and run URLs on the candidate commit. A failed candidate whose
  checks are still running, such as a rerun of a failed job, keeps waiting under
  the deadline; one with no failing and no running check stops after repeated passes.
  Snapshots retain the response's `reason_code` and, when present, the service's
  bounded `last_controller_result`. That canonical pass evidence includes its
  source record, policy/time binding, observed checks, retry lineage and next
  actor/observation; missing evidence remains null. The driver does not
  reconstruct historical causes or use diagnostic metadata to change outcomes.
  Launchplane owns the [last-result contract](https://github.com/cbusillo/launchplane/blob/main/docs/merge-train-policy.md).
  Snapshots also retain the write-action helper's validated public-safe
  `records` and `result`, including candidate and landing-plan identities and
  run links. A change to that evidence emits a snapshot even within the same
  controller phase; a new request trace alone does not. Polling backoff still
  follows phase changes. Every terminal receipt carries the emitted snapshots
  in order as `controller_evidence`, so later idle, refused or unavailable
  responses do not erase earlier plan or landing evidence. These are observed
  controller responses; the terminal `landing_sha` and `prs` remain GitHub
  read-back evidence, and missing controller evidence is not reconstructed.
  Driver GitHub reads use the shared conditional cache and retry deadline.
  The controller helper projects validated candidate PR numbers. While the
  controller observes a candidate containing this driver's PR, it leaves the batch PRs
  to that observation and reads them again on a phase change or final landing.
  Every controller call makes Launchplane re-read the whole repository train
  from GitHub, and Launchplane also runs its own passes. Unchanged controller
  phases and a lease held by another holder therefore poll less often, doubling
  up to `--max-wait-seconds` (fifteen minutes by default), and respect the
  shared low-budget floor; any phase change returns to `--poll-seconds`. A
  newly reported action Launchplane says to mutate once and call again
  (`admit_collapsed_root`, `plan_candidate`, `build_candidate`, `plan_landing`,
  `land_batch`) is followed by a shorter `--progress-seconds` pause (15 by
  default, never above `--poll-seconds`), still under the low-budget floor and
  the deadline; the same action repeated backs off like any unchanged phase.
  The terminal receipt's `cadence` counts controller requests, read-only
  observations and GitHub read calls (a paged check read counts once), and
  splits elapsed time into controller requests, sleep, reads, recovery,
  terminal read-back and an unattributed remainder such as a branch update.
  A recovery run is one measured span, not a counted controller request.
  `passes` lists each controller pass with its action, whether it was new
  progress, its request time and why it paused. These are local measurements,
  not Launchplane's own timing. A generic `github_request_failed` refusal probes
  the quota-free `/rate_limit` endpoint through the target repository's
  installation. Zero local core quota permits one wait to reset per refusal
  streak, within the driver's deadline, without spending its helper-failure budget.
  This is evidence about the local App; the controller can use another identity.
  Further refusals retain the failure budget until the controller makes progress.
  Other refusals retain their existing failure budget.
  A timeout, absent/malformed reply, unverified success or unknown write stops
  immediately with `controller_recovery`; an open PR does not settle a
  candidate/admission mutation. Before sending a mutating request, the driver
  saves its exact scope and key in an owner-only request file beside its local
  train lock. The stop event names that file and the key fingerprint, never the
  raw key. The pending file survives process exit and prevents a restarted
  driver from starting a fresh pass, including stack-finish passes. Keep it
  until recovery completes; deleting it discards the reconciliation barrier.
  To recover, rerun the same driver command with `--recover-controller`.
  It reads the saved PR and runs a read-only controller pass, then replays the
  saved mutating request with its original key. Read-only evidence alone does
  not clear the pending request. Only an accepted replay clears it;
  an unavailable read, lease refusal or denial keeps the file and stops.
  When Launchplane accepted the pass but the helper cannot project its reply
  (`controller_reply_unreadable`, with the helper's `projection_error_code`),
  the pass happened, so the driver releases the request file and stops. Read
  the PR and controller state for its effect, and compare the pinned reply
  schema with Launchplane's before relying on that reply shape.
  A verified PR landing is still reported as landed with unresolved controller
  recovery attached, and recovery works after that PR has landed.
  At the deadline, final read-back has one fixed 15-second grace window;
  controller mutations and stack-finish observations keep the original deadline.
  Once the tracked root lands, the driver makes no more mutating controller
  calls. It first reads the tracked children; a completed stack needs no
  controller observation. For unfinished children it observes dry-run passes
  from the service's own controller, bounded by the existing finish-pass limit
  and deadline. Only the same collapse ID and root can supply child evidence;
  unrelated queue selection or unavailable observation stops finishing without
  adopting that queue. The terminal `stack_finish` records completion, changed
  selection, unavailable observation, pass limit or deadline. Confirmed root
  landing remains `landed`; open or unknown children remain visible in `prs`.
  The helper contract has no stack-bound post-landing mutation, so a matching
  dry run never authorizes a generic mutating pass that can race queue changes.
  Authentication and permission failures emit an `error` stop event.
- **Evidence**: For stack runs, report the stack-collapse plan record id, any
  batch candidate record id, the landing-plan record id, workflow run URLs, and
  the final root merge commit. Include child disposition evidence when the root
  lands.
  The driver's terminal `prs` rows distinguish `landed`, `closed`, `open` and
  `unknown`. A closed child is `superseded` only when the projected controller
  disposition confirms it was closed after its root landed and its expected
  head matches GitHub; `carried_by` retains that collapse, root and head evidence.
  Only a verified GitHub merge supplies a child's `merge_commit_sha`; a carried
  child's value stays empty. Failed reads keep prior verified terminal results,
  while unavailable nonterminal members are `unknown`.
  Candidate and landing-plan membership come from the service projection.
  GitHub history can recover omitted companions sharing the target's exact
  landing commit, regardless of their current enqueue labels. The terminal
  `companion_inventory` reports whether that bounded history read completed;
  a cap or unavailable read leaves companion coverage incomplete even when the
  target's landing is verified.
- **Batch Evidence**: For flat batch runs, report the dry-run/admission reason,
  candidate record id and candidate SHA, required-check status on the candidate
  commit, landing-plan record id, each landed PR number and merge commit, managed
  feedback delivery status, and post-merge checks on the target repository's
  default branch.
- **Recovery Evidence**: If Launchplane patches are needed during rollout,
  verify their PR checks, post-merge CI/Security/CodeQL, and Deploy Launchplane
  before retrying mutation. Record the failing workflow run id and trace id that
  motivated the patch.
- **Troubleshooting**: Treat phase-specific merge-train endpoints as detail or
  recovery surfaces. They are not the default skill workflow.
