# Merge-Train Execution

Read before advancing or diagnosing a train. The entrypoint retains the runtime
authority, mutation gate, and exact landing-SHA checkout handoff requirements.

- **Before Enqueueing**: Run the controller without `--mutate` and inspect the
  intended PRs in `result.dry_run_result.queue`. The current GitHub adapter
  classifies the PR author, not the person or App adding the label. Check
  `actor_role`, `eligible`, `ineligible_reasons`, checks, and head SHA before
  adding ready labels. A missing ready label alone is the expected state before
  enqueueing; an unauthorized author remains ineligible after labeling. When an
  active candidate or landing is being reported instead of a fresh queue, use
  the current policy and GitHub author evidence; missing queue evidence does
  not mean that the queue is empty or that the intended PRs are eligible.
- **Capability Scope**: GitHub App installation permissions, Launchplane's
  repository/base author allowlist, and the train's merge identity are separate.
  An installation-wide Actions grant can enable dispatch/rerun across covered
  repositories without admitting their PR authors to a train. Before requesting
  a missing grant, inspect the remaining capabilities needed by the intended
  workflow and collect verified gaps into one concrete decision. For a recurring
  setup failure, inspect the active policy's configured targets and intended
  automation authors together. Preserve existing authorization for unchanged
  scope; additional identities or repositories remain explicit grant decisions.
  Keep real target lists and numeric identities in runtime records or reviewed
  operator input. This preflight uses existing evidence and adds no approval gate.
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
- **Stacked PRs**: For a same-repo linear stack, label only the root PR that
  targets the protected base branch. Let Launchplane collapse child branches
  into that root, wait for the root head SHA to satisfy checks, admit only the
  root to the flat train, and resolve child PRs after the root lands according
  to policy. Treat forked, ambiguous, sibling, cyclic, stale-head, or
  permission-limited stacks as blocked/unsupported instead of mutating by hand.
- **Retry Model**: Repeated controller calls are expected. Stop and report
  blocked, stale, denied, or failed states with compact evidence and trace IDs.
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
