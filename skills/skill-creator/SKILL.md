---
name: skill-creator
description: Guide for creating effective skills. This skill should be used when users want to create a new skill (or update an existing skill) that extends Codex's capabilities with specialized knowledge, workflows, or tool integrations.
metadata:
  short-description: Create or update a skill
resources:
  - path: scripts/init_skill.py
    kind: script
    description: Scaffold a new skill directory with SKILL.md, optional resource folders, and agent UI metadata.
  - path: scripts/quick_validate.py
    kind: script
    description: Validate a single skill's frontmatter, naming, command policies, and structured metadata.
  - path: scripts/generate_openai_yaml.py
    kind: script
    description: Generate initial agents/openai.yaml UI metadata for a skill.
  - path: scripts/validate-skill-behavior.py
    kind: script
    description: Run skill helpers and declared commands and check what they do.
  - path: scripts/validate-skill-repo.py
    kind: script
    description: Validate all active skills in this repository.
  - path: references/openai_yaml.md
    kind: reference
    description: Field definitions and examples for agents/openai.yaml.
  - path: references/forward-testing.md
    kind: reference
    description: Guidance for subagent forward-testing of complex skill revisions.
  - path: references/validation.md
    kind: reference
    description: Select proportional static, source-review, and execution evidence for the current host and model.
  - path: references/model-aware-authoring.md
    kind: reference
    description: Apply current named-model prompting guidance to skill authoring without changing shared policies or defaults.
  - path: references/command-policy-contract.md
    kind: reference
    description: Contract for portable command-policy metadata, runtime enforcement boundaries, and simulator evidence.
  - path: references/skill-design-details.md
    kind: reference
    description: Detailed structured metadata, resource, and progressive-disclosure patterns for skill authors.
commands:
  - name: init-skill
    source: skill
    resource_path: scripts/init_skill.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/init_skill.py",
        "<skill-name>",
        "--path",
        "<output-directory>",
      ]
    purpose: Scaffolds a new skill directory from the maintained template.
  - name: quick-validate-skill
    source: skill
    resource_path: scripts/quick_validate.py
    example_argv:
      ["uv", "run", "scripts/quick_validate.py", "<path-to-skill-folder>"]
    purpose: Performs focused validation for one skill folder.
  - name: generate-openai-yaml
    source: skill
    resource_path: scripts/generate_openai_yaml.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/generate_openai_yaml.py",
        "<path-to-skill-folder>",
        "--interface",
        "short_description=<text>",
      ]
    purpose: Generates initial UI metadata; edit existing metadata files in place.
  - name: validate-skill-behavior
    source: skill
    resource_path: scripts/validate-skill-behavior.py
    example_argv: ["uv", "run", "scripts/validate-skill-behavior.py"]
    purpose: Runs skill helpers and declared commands and checks their behavior.
  - name: validate-skill-repo
    source: skill
    resource_path: scripts/validate-skill-repo.py
    example_argv: ["uv", "run", "scripts/validate-skill-repo.py"]
    purpose: Runs repository-wide validation across active skills.
---

# Skill Creator

Create or update skills that provide specialized workflows, tools, domain
knowledge and reusable resources. Verify current capabilities before relying on
Codex Lab or another host's specific behavior. Apply
[task scope and authorization](../references/execution-scope.md), preserving
existing authorization and intentional quality/approval policies. Install this
maintained override with the catalog's shared `../references/` directory;
copying only this folder omits its dependencies.

## Authoring Contracts

Assume the agent is capable: add only useful, non-obvious context, and prefer
concise examples. Match specificity to fragility: use judgment rules when many
approaches work, parameterized patterns when variation is acceptable, and exact
helpers/sequences for fragile operations.

Describe the destination before the process. A substantial skill makes its
goal, success evidence, autonomy/approval boundary, primary tool routing and
final result contract easy to find. Preserve hard safety, permission, evidence
and output constraints; remove repetition that changes no decision. Reserve
absolute wording for invariants and use decision rules for judgment calls.

Keep essential routing, judgment, safety and workflow in `SKILL.md`, under 500
lines. Metadata is always visible; the body loads on invocation; resources
load as needed, and scripts can execute without loading their source. Put
variant details, examples, configuration, schemas and exhaustive commands in
one-level-deep references linked from the entry point with a read condition.
Give longer references a table of contents. Keep each fact in one place.
Read [skill design details](references/skill-design-details.md) for resource
patterns, helper invocation and worked progressive-disclosure examples.

Include only files needed for the task. Do not add auxiliary README,
INSTALLATION_GUIDE, QUICK_REFERENCE, CHANGELOG or process/setup/testing narratives.
Skills producing reviews, handoffs, issue/PR comments, readiness reports or
summaries should link [talking with the owner](../references/talking-with-the-owner.md)
instead of copying its rules.

## Create Or Update

Follow these six steps in order; skip a step only for a clear applicability
reason. Existing skills skip initialization.

### 1. Establish Concrete Uses

Reuse requirements and examples clear from the request, conversation or
existing skill. Understand the purpose, audience, supported tasks and likely
trigger requests; generate examples validated with user feedback when needed.
Ask only for missing information that materially changes those decisions,
starting with the most important questions and avoiding an overwhelming list.
Proceed once the supported functionality is clear.

Name skills with lowercase letters, digits and hyphens; normalize supplied
titles to hyphen-case and keep generated names under 64 characters. Prefer
short verb-led names, namespace by tool when that clarifies triggering, and
make the folder name match the skill name.

### 2. Choose Reusable Contents

For each concrete use, consider execution from scratch and identify what would
otherwise be rewritten or rediscovered. Choose scripts for deterministic or
repeated work, references for details loaded on demand, and assets for templates,
icons or other files copied/modified in output without loading them as guidance.
Create only needed resource directories. Obtain user-provided brand assets or
other missing inputs when they are necessary.

Python helpers need PEP 723 metadata and `uv run`; document/invoke shell helpers
as shell helpers. Use stdin/files for fragile multiline payloads. Added scripts
must actually run against representative inputs and produce the intended output;
representative sampling is sufficient for many similar scripts.

### 3. Initialize New Skills In Maintained Source

Use an explicit destination or established maintained source first. Follow a
skills repo's protected-branch/worktree/runtime rules; never scaffold into an
installed runtime or generated `.system`/plugin cache. Resolve symlinks and host
configuration before choosing a destination.

Outside an established catalog, current Codex scopes are project
`.agents/skills`, personal `~/.agents/skills`, or the owning plugin's source.
See [Codex discovery documentation](https://learn.chatgpt.com/docs/build-skills).
Ask about location only when scope is materially ambiguous. `CODE_HOME`,
`$CODEX_HOME/skills` and `.code/skills` can be existing compatibility/runtime
layouts, not universal authoring defaults. For another host, verify its current
discovery contract in maintained source or documentation.

Before generating UI values, read [agents metadata](references/openai_yaml.md).
Always scaffold a new skill with `init_skill.py`. Commands below are relative
to this skill directory; elsewhere use absolute helper paths. Run bundled Python
helpers with `uv` and their declared dependencies; use `python-uv-workflow` if
setup is needed.

```sh
uv run scripts/init_skill.py <skill-name> --path <maintained-skill-root> [--resources scripts,references,assets] [--examples]
```

The initializer creates the directory, frontmatter/TODO template,
`agents/openai.yaml`, requested resource directories and optional example files.
Replace/delete unneeded placeholders from `--examples`.

Derive human-facing `display_name`, `short_description` and `default_prompt`
from the skill, and pass them as `--interface key=value` to the initializer.
For an absent metadata file, initial generation may also use:

```sh
uv run scripts/generate_openai_yaml.py <skill-folder> --interface key=value
```

On an existing metadata file, edit only intended fields in place, check that it
still matches the skill, and preserve policy, dependencies and unrelated
interface fields. The generator writes an interface-only file: do not use it
for updates. Include optional UI fields (such as icons/brand color) only when
explicitly supplied.

### 4. Implement Resources And Instructions

Author for another agent: keep reusable procedural/domain knowledge it needs,
not explanations of what it already knows. Start with the chosen resources,
execute added scripts, and remove unused example placeholders.

Write imperative/infinitive instructions. `SKILL.md` needs YAML `name` and
`description`; description is the full model-visible routing trigger, including
what the skill does and all activation contexts. Do not put activation-only
sections in the body, which loads after triggering. Optional
`metadata.short-description` is a compact human-facing listing summary, not a
replacement for routing detail. The body explains execution and resource use.

For explicit-only Codex invocation, put this in `agents/openai.yaml`:

```yaml
policy:
  allow_implicit_invocation: false
```

Preserve existing interface/dependencies. A same-named frontmatter field is not
Codex invocation control; preserve such fields only for a host/catalog that
consumes them and document that compatibility scope. Use runtime config for
installation-only selection. For this catalog's Claude Code binding, mirror
explicit-only behavior with `disable-model-invocation: true` in `SKILL.md`;
the validator checks agreement between both files.

Preserve tooling-consumed `resources`, `commands`, `workflow_defaults` and
command policies. Add catalog extensions only where the target catalog uses
them; portable Codex skills do not require them. Read
[skill design details](references/skill-design-details.md) before adding/changing
structured metadata, and [agents metadata](references/openai_yaml.md) for
supported UI/dependency/policy fields.

For a fragile/preferred command workflow, keep both the prose helper route and
machine-readable `policy.command_policies`. Read
[the command-policy contract](references/command-policy-contract.md) before
adding/changing those rules or relying on their enforcement: it owns the
frontmatter/runtime boundary, matcher precedence, path resolution and
execution-evidence limits. Keep one canonical owner per raw-command path and
the narrowest matching rule. Portable declarations and a passing catalog check
do not prove host command interception; verify the specific runtime consumer.

### 5. Validate Proportionally

```sh
uv run scripts/quick_validate.py <skill-folder>
```

This checks naming/frontmatter/catalog contracts, not host discovery or model
behavior. Complete the owning repo's required gates, fix failures and rerun the
affected check. Read [validation guidance](references/validation.md) when
selecting evidence for behavior-sensitive changes; instruction changes in this
catalog use its structure/reference, behavior and command-policy validators.
Added scripts need actual execution evidence; metadata changes need catalog
checks and the target host's documented loader contract. Public-safety validation precedes
publishing.

For routing, command-policy, safety or GitHub/repo workflow changes, normally
cover intended trigger/success, adjacent routing, and boundary cases; add a
negative/ambiguity case when practical. Judge observable actions/outputs.
Current Codex/Codex Lab execution, direct local-model responses and source
reviews have different scopes: report the one performed. Every Code is retired;
its fixtures/readers are historical, not a supported validation path.

For approval, safety or destructive changes, read
[reviews by another model](../references/model-review.md) before merging and
weigh the review there. 

### 6. Iterate And Forward-Test

Reuse an applicable baseline or collect one for behavior/performance comparisons.
Use measured struggles, failures and fresh feedback; change one instruction
family/resource/route at a time. Rerun affected cases with matched inputs and
compare actual behavior. Reuse current evidence for unchanged scope; report
unmeasured effects/missing evidence without claiming lower pauses, cost or time.

Forward-test substantial or tricky revisions with fresh agents/subagents when
possible. Read [forward-testing](references/forward-testing.md) when planning or
running one. Give realistic tasks and raw artifacts with minimal task-local
context, not expected answers, diagnosis, fixes or prior conclusions unless the
validation explicitly requires them. Do not ask a reviewer to pretend to be an
execution case. Inspect actions, outputs and artifacts; clean artifacts between
iterations so later agents cannot infer answers from earlier work.

Read [model-aware authoring](references/model-aware-authoring.md) when adapting
for a named model or investigating model-specific behavior. Use `openai-docs`
for current OpenAI guidance; keep shared policy in its owners, and do not turn
model-specific experiments into permanent defaults.
