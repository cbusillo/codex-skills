# Task scope and authorization

Apply these rules when using the repository execution skills that link here.

- Follow the user's explicit instructions over skill guidelines. Infer routine
  implementation choices within the requested task and carry authorized work
  through its applicable checks.
- Reuse authorization already given for the same action and scope. A requirement
  for explicit approval does not imply a fresh question when that approval is
  already present. Fresh readiness checks do not by themselves require renewed
  user approval. Preserve narrower requirements such as approval of an exact
  comment, production target, release, plan digest, or immediate device restart.
- For a merge, the scope is the change the user approved, not a pull request
  number. Approval of an approach, or an instruction to carry work through,
  covers merging the pull request that delivers that change once its required
  checks, review state, and human-comment gate are clear. A standing grant, such
  as merge approval for the session, covers later pull requests in the same
  workstream until the user narrows it. Ask again when the delivered change
  materially exceeds what was approved, a required gate is failing or
  unavailable, or the merge would also release, deploy, or change production.
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
- Preserve unrelated changes and isolate implementation when necessary. Ask when
  edits overlap or ownership cannot be established, rather than treating every
  dirty checkout as a blocker. Isolation does not relax protected-branch or
  runtime-checkout rules.
- Run checks required by the repository and affected behavior. Reuse passing
  evidence for the same revision and environment; broaden or repeat checks when
  changes, failures, or unresolved risk justify it. Report missing required
  evidence honestly without inventing extra gates.
- Retain configured delegation, review, approval, and output-format policies.
  These rules clarify their application; they do not change their defaults.
