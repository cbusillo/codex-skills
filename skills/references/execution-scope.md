# Task scope and authorization

Apply these rules when using the repository execution skills that link here.
For chat and durable reports, read [talking with the owner](talking-with-the-owner.md).

- Follow the user's explicit instructions over skill guidelines. Infer routine
  implementation choices within the requested task and carry authorized work
  through its applicable checks.
- Before changing files in another repository, read its `AGENTS.md` (and
  `CLAUDE.md`, if present), including any instructions specific to the paths
  being changed.
- Also read its `DIRECTION.md` first when it has one; when it has none, keep
  working and say in the final message that it has no `DIRECTION.md` and that
  the owner's overall direction in `OWNER/direction` applies, if one exists,
  without suggesting that the repository adopt its own.
- Reuse authorization already given for the same action and scope. A requirement
  for explicit approval does not imply a fresh question when that approval is
  already present. Fresh readiness checks do not by themselves require renewed
  user approval. Preserve narrower requirements such as approval of an exact
  comment, production target, release, plan digest, or immediate device restart.
- Merge authorization attaches to a change and its destination, not to a pull
  request number. It comes from an instruction to merge or land the work,
  approval of a plan that says it ends in a merge, or a grant the user stated as
  standing. Choosing between approaches, or telling the agent to continue,
  approves building, not landing. When proposing work that should end in a
  merge, say so in the proposal, so that one approval covers both.
- Reuse merge authorization for a replacement, split, or follow-up pull request
  that delivers the approved change to the approved destination; a new number
  or a routine revision needs no new question. A standing grant covers later
  pull requests within the scope the user gave it. If its wording is no longer
  in context, confirm it instead of relying on a summary.
- Before merging, confirm the pull request lands only the approved change, and
  find out whether merging releases, deploys, or changes production. Reuse
  authorization that already covers those effects; ask for what is missing,
  including when the effect cannot be determined. A failing, degraded, or
  unavailable gate is a readiness problem, not a permission problem: keep
  diagnosing, fixing, and retrying within existing authorization, and report a
  blocker when the user's help is needed.
- A skill match is not authorization to expand the task. A read-only assessment
  remains read-only; a readiness check does not itself authorize a merge, release,
  deployment, cleanup, or message to another person.
- If a skill requires a pause, identify and link the exact source, quote the
  relevant instruction, and explain which information or authorization is still
  missing. Distinguish a requirement from an interpretation. Complete independent
  authorized work while the dependent action waits.
- Ask for missing information early and continue independent authorized work.
  Before requesting approval, complete the authorized preparation needed to make
  the proposed action concrete and reviewable. For user decisions, explain the
  choice, a recommendation when evidence supports one, and practical consequences
  in plain language. Include technical detail when it affects the choice; resolve
  routine engineering decisions within existing authorization.
- Before asking the user to approve a deploy, promotion, recovery, onboarding,
  or permission-gated path, check the whole path read-only first. For each
  step, find the identity it runs as and that identity's effective access, the
  environment, ruleset, and branch-protection gates it must pass, any refusal
  in the code it calls, whether its dry run validates what its apply will, and
  past failures of the same step in run history. Report every blocker found in
  one message and ask once. When a step cannot be checked read-only, name it
  as unverified in that same message instead of discovering it after approval.
- Before proposing a new issue, helper, or tooling, search open and closed
  issues in the affected repository and the user's related repositories for
  the same need. Build on what exists: reopen, extend, or adopt it, and say
  which issue you found. Propose something new only when the search finds
  nothing that covers it, and say what you searched.
- Preserve unrelated changes and isolate implementation when necessary. Ask when
  edits overlap or ownership cannot be established, rather than treating every
  dirty checkout as a blocker. Isolation does not relax protected-branch or
  runtime-checkout rules.
- When a task depends on a local service or app on this machine that is not
  running, start it without asking and record what you started and why in the
  task's PR or issue when posting is authorized, otherwise in the task report.
  Ask first if startup affects another person, a remote or production system,
  or paid resources, or if those effects cannot be determined; reuse approval
  that already covers those effects. Use a start action that leaves login
  startup behavior, machine settings, and service configuration unchanged. If
  startup fails, report the failure and any workaround. Leave fixes for why it
  was not running to follow-up issues under existing planning and posting
  authority; when no issue write is authorized, include the proposed follow-up
  in the task report.
- Run checks required by the repository and affected behavior. Reuse passing
  evidence for the same revision and environment; broaden or repeat checks when
  changes, failures, or unresolved risk justify it. Report missing required
  evidence honestly without inventing extra gates.
- Retain configured delegation, review, approval, and output-format policies.
  These rules clarify their application; they do not change their defaults.
