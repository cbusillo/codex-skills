# Model-Aware Skill Authoring

Read this reference when adapting a skill for a named model or investigating a
model-specific behavior change. Ordinary skill edits do not require a model
comparison or another documentation pass.

## Select the relevant guidance

Keep the model separate from the host: Astra and Sol are models; Codex and Codex
Lab provide tools, skill discovery, and execution controls. Verify the target
host's capabilities when the change depends on them.

For OpenAI models, use the [OpenAI Docs skill](../../openai-docs/SKILL.md) to
fetch current guidance for the exact requested model and prompting topic.
That skill owns source selection, model-specific guide retrieval, and disclosed
offline fallbacks. Do not duplicate complete model guides here or load every
model's advice into each authoring task. For another provider, consult its
current official guidance for the named model through `docs-lookup` when needed.

## Apply Astra guidance to skill design

The following authoring implications are our application of
[OpenAI's Astra prompting guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra#prompting-best-practices),
reviewed September 12, 2026. Verify current advice through OpenAI Docs when doing
Astra-specific work; these are audit priorities, not measured improvements.

| Documented tendency | Authoring implication |
| --- | --- |
| Greater sensitivity to skill and AGENTS instructions | Remove conflicting defaults and distinguish invariants from judgment calls. |
| More clarification and approval pauses | State authorized preparation and follow-through; reuse existing approval within its scope. |
| Broad verification on small tasks | Identify required gates and when to broaden checks; reuse applicable passing evidence. |
| Less delegation than a workflow may expect | State the workflow's intended delegation criteria and available tools. |
| Detailed, recurring output formats | Keep required result fields clear and apply the user's writing preferences. |

Use [execution scope](../../references/execution-scope.md) and the relevant
policy-owning skill for the actual rules. Preserve intentional merge,
production, review, validation, and output requirements unless the user changes
them. Model advice helps express those policies; it does not grant authority to
replace them.

## Keep other-model advice conditional

Preserve an explicitly requested model, including GPT-5.6 Sol. Fetch its own
current guidance instead of substituting Astra's. Keep an effort comparison,
prompt recipe, or local-model workaround scoped to the model and experiment
that justified it. Do not install an old same-effort/one-level-lower experiment
as a prerequisite for every skill edit.

When a model-specific exception is still useful, put it in a conditionally read
reference with its target, source, observed problem, and applicable evidence.
Keep the skill's shared outcome, authority, and completion contract in the
entrypoint. Add another model section only when supported work needs it.

## Validate the claimed change

Use [validation guidance](validation.md) to select source review or execution
evidence. Preserve representative cases and record the host, model, skill
revision, relevant settings, and observed outcome. Use matched before/after
tasks for claims about fewer interruptions, lower cost, or faster completion.
A model migration or API benchmark is not required to repair conflicting text.
