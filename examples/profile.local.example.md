# Local Profile Example

Optional: copy this file to `.local/profile.md` only when you want private,
machine-specific context. The copied file is ignored by git, and skills continue
normally when it is absent.

Use this for preferences such as:

- default GitHub owner or organization
- preferred automation identity
- private repository aliases
- local filesystem paths
- durable cross-repo workflow preferences that are not public-safe

Do not store tokens, passwords, or private keys here. Reference environment
variable names or credential helpers instead.

Review and prune the copied profile during memory distillation or
rollout-friction closeout. If a local note becomes broadly useful, promote only
the public-safe behavior into a skill or repo doc and leave private values in
`.local/profile.md`.
