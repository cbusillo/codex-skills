# Task scope and authorization

Apply these rules when using the repository execution skills that link here.

- Follow the user's explicit instructions over skill guidelines. Infer routine
  implementation choices within the requested task and carry authorized work
  through its applicable checks.
- Reuse authorization already given for the same action and scope. A requirement
  for explicit approval does not imply a fresh question when that approval is
  already present. Preserve narrower requirements such as approval of an exact
  comment, production target, release, plan digest, or immediate device restart.
- A skill match is not authorization to expand the task. A read-only assessment
  remains read-only; a readiness check does not itself authorize a merge, release,
  deployment, cleanup, or message to another person.
- If a skill requires a pause, identify and link the exact source, quote the
  relevant instruction, and explain which information or authorization is still
  missing. Distinguish a requirement from an interpretation. Complete independent
  authorized work while the dependent action waits.
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
