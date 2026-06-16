# Command Policy Contract

Command policies are portable skill declarations that a runtime command blocker
can consume. They are not runtime configuration and they are not, by themselves,
an execution guard. The split is deliberately narrow:

| Layer                          | Owns                                                                    |
| ------------------------------ | ----------------------------------------------------------------------- |
| Skill frontmatter              | Portable command-policy declarations and preferred routes               |
| Repo validators                | Catalog shape, path resolution, and coverage checks                     |
| Exec harness                   | Model routing behavior when policy context is present                   |
| Every Code / Codex Lab runtime | Command interruption, blocking, and conflict handling                   |
| Runtime/operator config        | Identity, enforcement mode, fallback, hosts, overrides, and trust roots |
| Helper scripts                 | Credential mechanics, safe execution, output shape, and cleanup         |
| Skill prose                    | Judgment, sequencing, exceptions, and human explanation                 |

## Frontmatter Fields

`policy.command_policies` entries must remain portable across installations.
They may describe stable command ownership, risk, and safe routes:

- `id`: stable skill-local policy identifier. Runtimes should report policies as
  `{skill}:{id}`.
- `match`: exactly one matcher: `argv_exact`, `argv_prefix`, or `shell_regex`.
- `action`: portable handling class, currently `require_preferred`,
  `require_confirm`, or `reject`.
- `message`: short risk explanation suitable for surfacing to the agent.
- `preferred`: replacement routes, usually helper scripts or delegated skills.

Frontmatter must not encode installation-specific runtime state such as concrete
bot logins, token names beyond helper documentation, enterprise host allowlists,
fallback permissions, enforcement modes, or per-repo/per-install overrides.
Prefer role language such as "configured automation identity" over a concrete
account name in portable policy messages and preferred-route purposes.

## Path Resolution

Preferred script paths are resolved relative to the skill directory that owns the
policy. Sibling references may use `../<skill>/...` when the helper is owned by a
neighboring skill in the same catalog. Preferred skill names are resolved through
the installed skill catalog.

Examples:

```yaml
preferred:
  - kind: script
    path: scripts/gh-pr.py
    example_argv: ["scripts/gh-pr.py", "merge", "<pr>"]
  - kind: skill
    name: babysit-pr
```

## Matching And Precedence

Token matchers run against parsed argv. `shell_regex` runs against the shell text
the runtime is about to execute. If the runtime only has argv tokens, it may use
`shlex.join(argv)` as a compatibility representation; the simulator must mirror
the runtime's documented normalization.

When multiple policies match, the primary policy is selected deterministically:

1. `argv_exact`
2. Longer `argv_prefix`
3. `shell_regex`
4. Stable skill load order
5. Policy declaration order within the skill

Diagnostics should include all matching policies even when one primary policy is
selected. That keeps sibling-skill ownership disputes visible.

## Runtime Configuration

Runtime/operator config decides how a portable policy is enforced. The same
`require_preferred` declaration might be audit-only, warning, interrupting,
approval-gated, or blocked depending on the trusted runtime policy pack.

Runtime config owns:

- configured automation identity
- enforcement mode
- active-auth fallback permission
- enterprise host allowlists and host-specific token routing
- per-install or per-repo overrides
- trusted skill catalog roots
- shell/pty interception details

## Harness Boundary

Exec-harness scenarios can prove that skill context exposes command-policy
metadata and that a model chooses a helper-backed route. They do not prove that
the runtime intercepted a raw command before execution. Runtime command-blocker
tests must live with Every Code / Codex Lab.
