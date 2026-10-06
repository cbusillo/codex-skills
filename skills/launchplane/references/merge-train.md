# Merge-Train Execution

Read before advancing or diagnosing a train. The entrypoint retains the runtime
authority, mutation gate, and exact landing-SHA checkout handoff requirements.

- **Before Enqueueing**: Run the controller without `--mutate` and inspect the
  intended PRs in `result.dry_run_result.queue`. The current GitHub adapter
  checks both the PR author and the actor who last applied `ready-to-merge`,
  using the label's latest `labeled` event. Enqueue with the trusted automation
  identity's installation token. The labeler must be trusted automation or have
  a role in `allowed_actor_roles`. A label applied by a GitHub App acting with
  a user's token is always refused, regardless of the user's role.
  An unauthorized labeler is reported as
  `ready-to-merge label ignored: applied by <login> ...`; the same
  `<enqueue_label> label ignored:` prefix also covers unreadable labeler evidence
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
- **Proven Batch Flow**: The controller has been proven against a live
  multi-PR batch train. It can reflow a failed candidate when the eligible queue
  changes, build and observe a replacement candidate, create a landing plan,
  land the original PRs through GitHub's PR merge API in train order, and post
  managed feedback to each PR. Treat this as the normal rollout path, not an
  experimental one-off.
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
  Blocks on the tracked PR and blocks without a selected PR remain failures.
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
  failing checks and run URLs on the candidate commit.
  Driver GitHub reads use the shared conditional cache and retry deadline.
  The controller helper projects validated candidate PR numbers. While the
  controller observes a candidate containing this driver's PR, it leaves the batch PRs
  to that observation and reads them again on a phase change or final landing.
  Unchanged controller phases poll less often, up to five minutes, and respect
  the shared low-budget floor. A generic `github_request_failed` refusal probes
  the quota-free `/rate_limit` endpoint through the target repository's
  installation. Zero local core quota permits one wait to reset per refusal
  streak, within the driver's deadline, without spending its helper-failure budget.
  This is evidence about the local App; the controller can use another identity.
  Further refusals retain the failure budget until the controller makes progress.
  Other refusals retain their existing failure budget.
  At the deadline, final read-back has one fixed 15-second grace window;
  controller mutations and stack-finish passes keep the original deadline.
  Authentication and permission failures emit an `error` stop event.
- **Evidence**: For stack runs, report the stack-collapse plan record id, any
  batch candidate record id, the landing-plan record id, workflow run URLs, and
  the final root merge commit. Include child disposition evidence when the root
  lands.
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
