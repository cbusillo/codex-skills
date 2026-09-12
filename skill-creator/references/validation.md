# Skill Validation

Read this reference when a skill change affects routing, workflow, policy, or
host compatibility. Start with the affected behavior and the owning repository's
required checks. Keep the scope proportional and reuse current passing evidence.

## Choose evidence for the claim

| Evidence | What it establishes | What remains unproven |
| --- | --- | --- |
| Structure/reference/schema validators | Files, metadata, links, and declared contracts satisfy the catalog checks | Host discovery, policy enforcement, and model behavior |
| Helper tests using representative inputs | The exercised script behavior and failure handling | Whether an agent chooses the helper |
| Independent source review or manual cases | Instruction consistency and a reviewer's proposed decisions | Actual tool execution or a measured model improvement |
| Direct local-model calls | That model's responses to the supplied context | Codex/Astra behavior or host tool and approval handling |
| Codex or Codex Lab execution | Observed actions and outputs for the tested host, model, configuration, and task | Untested hosts, models, tasks, or runtime enforcement paths |

Complete required checks even when another row supplies useful evidence. A
missing optional harness does not create a new approval gate; a required check
that cannot run remains missing evidence and must be reported.

## Static and helper checks

Use the maintained validator, normally `uv run scripts/quick_validate.py
<skill-directory>` from the creator directory. In this catalog, instruction
changes also use its existing structure/reference, behavior, and command-policy
validators. Use its public-safety validator before publishing.

Do not add tests that merely duplicate instruction wording. For script changes,
exercise actual inputs and failure modes. For metadata changes, check both the
catalog schema and the target host's documented loader contract. In Codex,
explicit-only invocation uses `policy.allow_implicit_invocation: false` in
`agents/openai.yaml`; catalog acceptance of the same field elsewhere is not
proof that Codex enforces it.

## Current Codex and Codex Lab execution

Identify the intended host and model before selecting a command. Inspect the
available executable's version and help; do not assume that `codex`, `codex-lab`,
and legacy `code` have interchangeable flags, discovery rules, or harnesses.
Use the existing authorized access path and the host's supported execution
interface. An instruction audit alone does not require changing runtime or
model configuration.

The host and model are separate parts of the evidence. To test Astra behavior,
run a compatible Codex or Codex Lab host with Astra and record the model actually
used; another model's run cannot establish Astra behavior.

Use an isolated task workspace or fixtures under the repository's worktree and
artifact rules. Point the test at the exact maintained skill source and verify
that the tested host loaded that revision. Do not edit an installed runtime or
generated cache to make the test select a skill. Preserve real production and
external-message approval boundaries in synthetic tasks.

For substantial or fragile changes, forward-test representative tasks with fresh
agents; read [forward-testing guidance](forward-testing.md). Normally cover the
intended task, an adjacent task that should not activate the skill, and a boundary
or ambiguity case. Judge actual tool calls, outputs, and artifacts. When checking
implicit discovery, use a real host discovery/invocation test; explicitly handing
the model `SKILL.md` cannot establish that behavior.

## Direct local-model and manual paths

Use the `local-llm` skill's current inventory and direct LM Studio chat helpers
when a local model is requested or selected for a supported review role. Choose
from current available models and user constraints; do not inherit model names
from legacy harness examples. Preserve that skill's endpoint, data-retention,
and lifecycle rules. Do not substitute a cloud provider for private local work.

Give source reviewers the relevant artifacts and task constraints. Give an
execution case the task and necessary inputs without the expected answer or
another reviewer's conclusions. A local model can help triage wording and
propose decisions, but its answer is not an Astra execution test.

## Model guidance and reporting

For model-specific work, fetch the named model's current official guidance using
`openai-docs`. Keep effort and migration recipes scoped to that model. Review
instruction priority, permitted follow-through, material decision points,
delegation, output requirements, and validation breadth without changing the
user's intentional policies.

Record the source revision, host/model, relevant configuration and scope, cases
run, observed result, and any missing evidence. Reuse an applicable baseline;
collect matched before/after cases when claiming a behavior or performance
improvement. A cleaner instruction file or a passing static check alone does
not establish fewer pauses, lower cost, or faster completion.
