# Historical Every Code Harness Fixtures

These synthetic scenarios were written for the retired Every Code exec
harness. They are retained as historical fixtures, not a supported validation
suite. Do not run the old wrapper or restore Every Code for current skill work.
Use [current validation guidance](../../references/validation.md) for Codex and
Codex Lab.

The fake Responses API scenarios inspected captured context without model
tokens; fake GitHub scenarios exercised selected helper calls. Files prefixed
with `local-llm-` used a trusted local provider and incurred local model work.
Neither mode establishes current Codex or Codex Lab command interception.

The harness-dependent entries and CI-promotion decisions in
[the scorecard](../../references/skill-scorecard.yaml) describe historical
coverage. Their commands, models, gates, and not-run reasons do not schedule or
recommend new runs. The scorecard's active static and helper checks still apply.

A future port must explicitly select the current host and model, verify the
fixture assumptions, and establish fresh evidence. Preserve synthetic inputs
and useful behavioral assertions without treating old passes as current ones.
