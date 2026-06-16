# Exec Harness Scenarios

These public-safe fixtures exercise observable skill context shape and selected
fake-GitHub workflows with the Every Code exec harness and fake Responses API.
They do not spend model tokens.

Files prefixed with `local-llm-` are the exception: they use the local LM Studio
provider embedded in each scenario's `config_toml`, spend only trusted local
model time, and keep raw outputs under the harness artifact directory.

Run them from the repository root:

```bash
scripts/validate-exec-harness-skills.sh skill-creator/evaluations/exec-harness/*.json
```

If the sibling Every Code checkout is not at `../code`, set `CODE_EXEC_HARNESS`
to the harness script path.

CI-promotion decisions and public-safe not-run reasons for harness, local LLM,
and performance checks are recorded in
`skill-creator/references/skill-scorecard.yaml`.

The scenario assertions inspect captured provider requests under
`expect.responses`. Keep fixtures synthetic, avoid private paths or secrets, and
record not-run evidence when the harness is unavailable.

For command-policy scenarios, assert both sides of the contract when practical:
the provider request should contain the relevant structured
`policy.command_policies` entries, and the captured command should use the
helper-backed path. These scenarios are regression coverage for the skill
catalog and routing context; they are not a substitute for Codex Lab runtime
command-blocker enforcement.
