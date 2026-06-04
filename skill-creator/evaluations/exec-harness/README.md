# Exec Harness Scenarios

These public-safe fixtures exercise observable skill context shape with the
Every Code exec harness and fake Responses API. They do not spend model tokens.

Files prefixed with `local-llm-` are the exception: they use the local LM Studio
provider embedded in each scenario's `config_toml`, spend only trusted local
model time, and keep raw outputs under the harness artifact directory.

Run them from the repository root:

```bash
scripts/validate-exec-harness-skills.sh skill-creator/evaluations/exec-harness/*.json
```

If the sibling Every Code checkout is not at `../code`, set `CODE_EXEC_HARNESS`
to the harness script path.

The scenario assertions inspect captured provider requests under
`expect.responses`. Keep fixtures synthetic, avoid private paths or secrets, and
record not-run evidence when the harness is unavailable.
