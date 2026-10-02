# Command Policy Contract

Command policies are portable skill declarations that a runtime command blocker
can consume. They are not runtime configuration and they are not, by themselves,
an execution guard. The split is deliberately narrow:

| Layer                          | Owns                                                                    |
| ------------------------------ | ----------------------------------------------------------------------- |
| Skill frontmatter              | Portable command-policy declarations and preferred routes               |
| Repo validators                | Catalog shape, path resolution, and coverage checks                     |
| Compatible execution test      | Model routing behavior when policy context is present                   |
| Host with an implemented command-policy consumer | Command interruption, blocking, and conflict handling |
| Runtime/admin config           | Identity, enforcement mode, fallback, hosts, overrides, and trust roots |
| Helper scripts                 | Credential mechanics, safe execution, output shape, and cleanup         |
| Skill prose                    | Judgment, sequencing, exceptions, and human explanation                 |

These are this catalog's extension contracts. Codex or Codex Lab support must
be verified in the specific host version; neither the host's name nor a passing
catalog validator proves that it implements this command-policy consumer.
Codex's `agents/openai.yaml` invocation policy is a separate control.

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
- `exceptions`: optional list of `{repository: owner/repo, argv_prefix: [...]}`
  pairs for source-only commands in their owning GitHub repository. Repository
  names are lowercase. A consumer skips only that matching policy when both the
  argv prefix and verified command working directory match. Without repository
  evidence or exception support, the original policy still applies.

Frontmatter must not encode installation-specific runtime state such as concrete
bot logins, token names beyond helper documentation, enterprise host allowlists,
fallback permissions, enforcement modes, or per-install overrides. A source-only
exception may identify the command's owning repository; this is distinct from
an installation's runtime override. Its prefix must strictly extend the policy's
`argv_prefix`; exact and regex policies do not accept exceptions.
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

## Repository Evidence

The simulator's `--cwd` and the shared command hook verify a Git checkout or
linked worktree by reading its Git top-level and exact GitHub `origin` identity.
They accept HTTPS, SCP-style SSH, and SSH URLs without contacting GitHub;
non-Git directories, missing origins, other hosts, and failed reads supply no
exception. Ambient `GIT_*` overrides are removed for these local reads. This is
checkout identity evidence for the habit guardrail, not proof of trusted source
or a security boundary.

The hook uses the event's `cwd` (or its process cwd when absent), or one leading
literal absolute `cd <directory> &&` prefix whose existing target is independently
verified by Git. It never executes the shell to infer context. Other directory
or uv project switches, environment assignments, explicit Launchplane
executable paths, `$()` substitutions, and backticks retain the block. Unquoted
newlines separate commands, including after comments. Heredoc scripts retain the
existing matcher behavior tracked in #671 and cannot use repository exceptions.
Use the tool's working-directory option or
that single prefix and `uv run [--extra dev] launchplane` for the declared
source-only exports and offline gates. A simple
shell wrapper uses the same checks on its enclosed command. The simulator
receives already normalized argv and the consumer's verified command directory;
`--cwd` describes that directory, not a shell `cd` instruction.

## Runtime Configuration

Runtime/admin config decides how a portable policy is enforced. The same
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

## Execution Evidence Boundary

Agent execution cases can show that the tested context exposes command-policy
metadata and that the tested model chooses a helper-backed route. They do not
prove that the runtime intercepted a raw command before execution. Runtime
command-blocker tests must live with the host that implements the blocker.
Historical Every Code harness results are not evidence of current Codex or
Codex Lab enforcement. Select current evidence using
[validation guidance](validation.md).
