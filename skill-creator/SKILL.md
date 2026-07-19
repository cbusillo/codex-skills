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
    description: Generate agents/openai.yaml UI metadata for a skill.
  - path: scripts/validate-skill-behavior.py
    kind: script
    description: Run behavioral smoke checks for skill routing and invocation expectations.
  - path: scripts/validate-skill-repo.py
    kind: script
    description: Validate all active skills in this repository.
  - path: scripts/collect_exec_harness_performance.py
    kind: script
    description: Summarize public-safe performance metrics from local Every Code exec-harness artifacts.
  - path: references/openai_yaml.md
    kind: reference
    description: Field definitions and examples for agents/openai.yaml.
  - path: references/forward-testing.md
    kind: reference
    description: Guidance for subagent forward-testing of complex skill revisions.
  - path: references/exec_harness.md
    kind: reference
    description: How to run Every Code exec-harness scenarios for skill behavior validation.
  - path: references/command-policy-contract.md
    kind: reference
    description: Contract for portable command-policy metadata, runtime enforcement boundaries, and simulator/harness expectations.
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
    purpose: Generates or refreshes UI metadata for a skill.
  - name: validate-skill-behavior
    source: skill
    resource_path: scripts/validate-skill-behavior.py
    example_argv: ["uv", "run", "scripts/validate-skill-behavior.py"]
    purpose: Runs repository-level behavior checks for high-impact skill guidance.
  - name: validate-skill-repo
    source: skill
    resource_path: scripts/validate-skill-repo.py
    example_argv: ["uv", "run", "scripts/validate-skill-repo.py"]
    purpose: Runs repository-wide validation across active skills.
  - name: collect-exec-harness-performance
    source: skill
    resource_path: scripts/collect_exec_harness_performance.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/collect_exec_harness_performance.py",
        "--latest",
        "10",
      ]
    purpose: Emits public-safe advisory performance metrics from local exec-harness artifacts.
---

# Skill Creator

This skill provides guidance for creating effective skills.

## About Skills

Skills are modular, self-contained folders that extend Codex's capabilities by providing
specialized knowledge, workflows, and tools. Think of them as "onboarding guides" for specific
domains or tasks—they transform Codex from a general-purpose agent into a specialized agent
equipped with procedural knowledge that no model can fully possess.

### What Skills Provide

1. Specialized workflows - Multi-step procedures for specific domains
2. Tool integrations - Instructions for working with specific file formats or APIs
3. Domain expertise - Company-specific knowledge, schemas, business logic
4. Bundled resources - Scripts, references, and assets for complex and repetitive tasks

## Core Principles

### Concise is Key

The context window is a public good. Skills share the context window with everything else Codex needs: system prompt, conversation history, other Skills' metadata, and the actual user request.

**Default assumption: Codex is already very smart.** Only add context Codex doesn't already have. Challenge each piece of information: "Does Codex really need this explanation?" and "Does this paragraph justify its token cost?"

Prefer concise examples over verbose explanations.

### Set Appropriate Degrees of Freedom

Match the level of specificity to the task's fragility and variability:

**High freedom (text-based instructions)**: Use when multiple approaches are valid, decisions depend on context, or heuristics guide the approach.

**Medium freedom (pseudocode or scripts with parameters)**: Use when a preferred pattern exists, some variation is acceptable, or configuration affects behavior.

**Low freedom (specific scripts, few parameters)**: Use when operations are fragile and error-prone, consistency is critical, or a specific sequence must be followed.

Think of Codex as exploring a path: a narrow bridge with cliffs needs specific guardrails (low freedom), while an open field allows many routes (high freedom).

### Write Outcome-First Contracts

Describe the destination and completion bar before prescribing process. A
substantial skill should make five things easy to find:

1. **Goal** - the user-visible outcome the skill owns.
2. **Success evidence** - what must be true before the task is complete.
3. **Autonomy boundary** - which safe local actions are allowed and which
   destructive, external, costly, or scope-expanding actions require approval.
4. **Tool routing** - the primary helper or decision rule for choosing tools.
5. **Final result contract** - the status, evidence, gaps, and next action the
   user should receive.

Keep hard safety, permission, evidence, and output constraints. Remove repeated
rules, examples, or process narration that do not change behavior. Use absolute
language only for true invariants; use decision rules for judgment calls.

### Protect Validation Integrity

You may use subagents during iteration to validate whether a skill works on realistic tasks or whether a suspected problem is real. This is most useful when you want an independent pass on the skill's behavior, outputs, or failure modes after a revision. Only do this when it is possible to start new subagents.

When using subagents for validation, treat that as an evaluation surface. The goal is to learn whether the skill generalizes, not whether another agent can reconstruct the answer from leaked context.

Prefer raw artifacts such as example prompts, outputs, diffs, logs, or traces. Give the minimum task-local context needed to perform the validation. Avoid passing the intended answer, suspected bug, intended fix, or your prior conclusions unless the validation explicitly requires them.

Use the exec harness for behavior-sensitive skill changes when available. A
skill that changes routing, command policy, safety boundaries, or GitHub/repo
workflow semantics should normally have at least three representative prompts:
the intended trigger and success path, an adjacent request that should not
trigger or should route elsewhere, and an ambiguity or safety-boundary case.
Include a negative or ambiguity case when practical. Keep tests focused on
observable behavior rather than private reasoning. For model-specific prompt
migrations, consult current model guidance and preserve the existing eval
baseline; use `openai-docs` for OpenAI models. For GPT-5.6 specifically, compare
the target at the same reasoning effort and one level lower before changing the
prompt. Read `references/exec_harness.md` before designing or running scenarios.

### Anatomy of a Skill

Every skill consists of a required SKILL.md file and optional bundled resources:

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter metadata (required)
│   │   ├── name: (required)
│   │   ├── description: (required)
│   │   ├── metadata.short-description: (optional)
│   │   └── policy: allow_implicit_invocation and command_policies (optional)
│   └── Markdown instructions (required)
├── agents/ (recommended)
│   └── openai.yaml - UI metadata for skill lists and chips
└── Bundled Resources (optional)
    ├── scripts/          - Executable code (Python/Bash/etc.)
    ├── references/       - Documentation intended to be loaded into context as needed
    └── assets/           - Files used in output (templates, icons, fonts, etc.)
```

#### SKILL.md (required)

Every SKILL.md consists of:

- **Frontmatter** (YAML): Contains `name` and `description` fields. `description` is the full model-visible trigger/routing text. Optional `metadata.short-description` provides compact human-facing listing text, and optional `policy.allow_implicit_invocation: false` marks a skill as manual-only.
- **Structured metadata** (YAML): Optional `resources`, `commands`, and `workflow_defaults` fields describe bundled files and routine commands. Keep judgment, routing, safety, and procedural nuance in prose.
- **Body** (Markdown): Instructions and guidance for using the skill. Only loaded AFTER the skill triggers (if at all).

##### Structured resources and commands

Use `resources` for bundled files, `commands` for routine entrypoints, and
`workflow_defaults` for simple stable defaults. Keep descriptions and purposes
short, distinguish similar commands clearly, and keep judgment, routing,
safety, and exceptions in prose. Read `references/skill-design-details.md` when
adding or changing structured metadata.

##### Command policies

When a skill owns a fragile or preferred command workflow, put the machine-readable
mapping in `policy.command_policies` instead of relying only on prose. Keep prose
for judgment, sequencing, and exceptions.

Use `references/command-policy-contract.md` as the source of truth for the
frontmatter/runtime boundary, matcher precedence, path resolution, and
exec-harness limits. Keep one canonical owner for each raw command path and use
the narrowest matcher that represents the workflow. Command policies are
portable metadata, not a runtime enforcement guarantee by themselves.

#### Agents metadata (recommended)

- UI-facing metadata for skill lists and chips
- Read references/openai_yaml.md before generating values and follow its descriptions and constraints
- Create: human-facing `display_name`, `short_description`, and `default_prompt` by reading the skill
- Generate deterministically by passing the values as `--interface key=value` to `scripts/generate_openai_yaml.py` or `scripts/init_skill.py`
- On updates: validate `agents/openai.yaml` still matches SKILL.md; regenerate if stale
- Only include other optional interface fields (icons, brand color) if explicitly provided
- See references/openai_yaml.md for field definitions and examples

#### Bundled Resources (optional)

Use scripts for deterministic or repeatedly rewritten work, references for
details loaded only when needed, and assets for files copied or modified in the
final output. Python helpers require PEP 723 metadata and should run with
`uv run`; shell helpers must be documented and invoked as shell helpers. Prefer
stdin or body files for fragile multiline payloads. Keep information in one
place rather than duplicating it between `SKILL.md` and references.

Read `references/skill-design-details.md` for resource patterns, helper
invocation rules, and progressive-disclosure examples. Skills that generate
reviews, handoffs, issue or PR comments, readiness reports, or final summaries
should point to `../references/every-code-formatting.md` instead of copying its
rules.

#### What to Not Include in a Skill

A skill should only contain essential files that directly support its functionality. Do NOT create extraneous documentation or auxiliary files, including:

- README.md
- INSTALLATION_GUIDE.md
- QUICK_REFERENCE.md
- CHANGELOG.md
- etc.

The skill should only contain the information needed for an AI agent to do the job at hand. It should not contain auxiliary context about the process that went into creating it, setup and testing procedures, user-facing documentation, etc. Creating additional documentation files just adds clutter and confusion.

### Progressive Disclosure Design Principle

Skills use a three-level loading system to manage context efficiently:

1. **Metadata (name + description)** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<5k words)
3. **Bundled resources** - As needed by Codex (Unlimited because scripts can be executed without reading into context window)

#### Progressive Disclosure Patterns

Keep the body to essential routing, judgment, safety, and workflow instructions
and under 500 lines. Move variant-specific details, examples, configuration,
schemas, and exhaustive command documentation to one-level-deep references.
Name each reference from `SKILL.md` and say when to read it. Add a table of
contents to longer reference files. See `references/skill-design-details.md` for
worked patterns.

## Skill Creation Process

Skill creation involves these steps:

1. Understand the skill with concrete examples
2. Plan reusable skill contents (scripts, references, assets)
3. Initialize the skill (run init_skill.py)
4. Edit the skill (implement resources and write SKILL.md)
5. Validate the skill (run quick_validate.py)
6. Iterate based on real usage and forward-test complex skills.

Follow these steps in order, skipping only if there is a clear reason why they are not applicable.

### Skill Naming

- Use lowercase letters, digits, and hyphens only; normalize user-provided titles to hyphen-case (e.g., "Plan Mode" -> `plan-mode`).
- When generating names, generate a name under 64 characters (letters, digits, hyphens).
- Prefer short, verb-led phrases that describe the action.
- Namespace by tool when it improves clarity or triggering (e.g., `gh-address-comments`, `linear-address-issue`).
- Name the skill folder exactly after the skill name.

### Step 1: Understanding the Skill with Concrete Examples

Skip this step only when the skill's usage patterns are already clearly understood. It remains valuable even when working with an existing skill.

To create an effective skill, clearly understand concrete examples of how the skill will be used. This understanding can come from either direct user examples or generated examples that are validated with user feedback.

For example, when building an image-editor skill, relevant questions include:

- "What functionality should the image-editor skill support? Editing, rotating, anything else?"
- "Can you give some examples of how this skill would be used?"
- "I can imagine users asking for things like 'Remove the red-eye from this image' or 'Rotate this image'. Are there other ways you imagine this skill being used?"
- "What would a user say that should trigger this skill?"
- "Where should I create this skill? If you do not have a preference, I will
  place it in `$CODE_HOME/skills` when set, use `$CODEX_HOME/skills` for
  compatibility, then prefer `~/.code/skills` if present, and finally fall back
  to `~/.codex/skills`."

To avoid overwhelming users, avoid asking too many questions in a single message. Start with the most important questions and follow up as needed for better effectiveness.

Conclude this step when there is a clear sense of the functionality the skill should support.

### Step 2: Planning the Reusable Skill Contents

To turn concrete examples into an effective skill, analyze each example by:

1. Considering how to execute on the example from scratch
2. Identifying what scripts, references, and assets would be helpful when executing these workflows repeatedly

Example: When building a `pdf-editor` skill to handle queries like "Help me rotate this PDF," the analysis shows:

1. Rotating a PDF requires re-writing the same code each time
2. A `scripts/rotate_pdf.py` script would be helpful to store in the skill

Example: When designing a `frontend-webapp-builder` skill for queries like "Build me a todo app" or "Build me a dashboard to track my steps," the analysis shows:

1. Writing a frontend webapp requires the same boilerplate HTML/React each time
2. An `assets/hello-world/` template containing the boilerplate HTML/React project files would be helpful to store in the skill

Example: When building a `big-query` skill to handle queries like "How many users have logged in today?" the analysis shows:

1. Querying BigQuery requires re-discovering the table schemas and relationships each time
2. A `references/schema.md` file documenting the table schemas would be helpful to store in the skill

To establish the skill's contents, analyze each concrete example to create a list of the reusable resources to include: scripts, references, and assets.

### Step 3: Initializing the Skill

At this point, it is time to actually create the skill.

Skip this step only if the skill being developed already exists. In this case, continue to the next step.

Before running `init_skill.py`, ask where the user wants the skill created. If
they do not specify a location, default to `$CODE_HOME/skills` when set, use
`$CODEX_HOME/skills` for compatibility, then prefer `~/.code/skills` if
present, and finally fall back to `~/.codex/skills` so the skill is
auto-discovered.

When creating a new skill from scratch, always run the `init_skill.py` script. The script conveniently generates a new template skill directory that automatically includes everything a skill requires, making the skill creation process much more efficient and reliable.

Usage:

```bash
scripts/init_skill.py <skill-name> --path <output-directory> [--resources scripts,references,assets] [--examples]
```

Examples:

```bash
skills_home="${CODE_HOME:-${CODEX_HOME:-$HOME/.code}}"
skills_dir="$skills_home/skills"
scripts/init_skill.py my-skill --path "$skills_dir"
scripts/init_skill.py my-skill --path "$skills_dir" --resources scripts,references
scripts/init_skill.py my-skill --path ~/work/skills --resources scripts --examples
```

The script:

- Creates the skill directory at the specified path
- Generates a SKILL.md template with proper frontmatter and TODO placeholders
- Creates `agents/openai.yaml` using agent-generated `display_name`, `short_description`, and `default_prompt` passed via `--interface key=value`
- Optionally creates resource directories based on `--resources`
- Optionally adds example files when `--examples` is set

After initialization, customize the SKILL.md and add resources as needed. If you used `--examples`, replace or delete placeholder files.

Generate `display_name`, `short_description`, and `default_prompt` by reading the skill, then pass them as `--interface key=value` to `init_skill.py` or regenerate with:

```bash
scripts/generate_openai_yaml.py <path/to/skill-folder> --interface key=value
```

Only include other optional interface fields when the user explicitly provides them. For full field descriptions and examples, see references/openai_yaml.md.

### Step 4: Edit the Skill

When editing the (newly-generated or existing) skill, remember that the skill is
being created for another coding agent instance to use. Include information that
would be beneficial and non-obvious to that agent. Consider what procedural
knowledge, domain-specific details, or reusable assets would help another agent
execute these tasks more effectively.

After substantial revisions, or if the skill is particularly tricky, you should use subagents to forward-test the skill on realistic tasks or artifacts. When doing so, pass the artifact under validation rather than your diagnosis of what is wrong, and keep the prompt generic enough that success depends on transferable reasoning rather than hidden ground truth.

#### Start with Reusable Skill Contents

To begin implementation, start with the reusable resources identified above: `scripts/`, `references/`, and `assets/` files. Note that this step may require user input. For example, when implementing a `brand-guidelines` skill, the user may need to provide brand assets or templates to store in `assets/`, or documentation to store in `references/`.

Added scripts must be tested by actually running them to ensure there are no bugs and that the output matches what is expected. If there are many similar scripts, only a representative sample needs to be tested to ensure confidence that they all work while balancing time to completion.

If you used `--examples`, delete any placeholder files that are not needed for the skill. Only create resource directories that are actually required.

#### Update SKILL.md

**Writing Guidelines:** Always use imperative/infinitive form.

##### Frontmatter

Write the YAML frontmatter for the Every Code agent with `name` and
`description`:

- `name`: The skill name
- `description`: This is the primary triggering mechanism for your skill, and helps the agent understand when to use the skill.
  - Include both what the Skill does and specific triggers/contexts for when to use it.
  - Include all "when to use" information here - Not in the body. The body is only loaded after triggering, so "When to Use This Skill" sections in the body are not helpful to the agent.
  - Example description for a `docx` skill: "Comprehensive document creation, editing, and analysis with support for tracked changes, comments, formatting preservation, and text extraction. Use when Codex needs to work with professional documents (.docx files) for: (1) Creating new documents, (2) Modifying or editing content, (3) Working with tracked changes, (4) Adding comments, or any other document tasks"
- `metadata.short-description`: Optional compact human-facing summary for UI/listing surfaces. Keep the full routing and trigger detail in `description`.
- `policy.allow_implicit_invocation`: Optional boolean. Set to `false` only for skills that should be discoverable and explicitly invokable, but excluded from default implicit routing.
- `resources`: Optional list of bundled files with `path`, `kind`, and `description`.
- `commands`: Optional list of executable entrypoints with `name`, `source`, `example_argv`, and `purpose`. Use `resource_path` only for `source: skill` commands.
- `workflow_defaults`: Optional list of simple defaults with `name`, `value`, and `description`.

Example:

```yaml
---
name: example-skill
description: Full model-visible trigger/routing description. Use when ...
metadata:
  short-description: Compact human-facing summary
policy:
  allow_implicit_invocation: false
---
```

Runtime config may also disable skills by name or `SKILL.md` path for a local
installation. Use frontmatter policy when the skill's own contract is
manual-only; use runtime config for environment-specific selection.

##### Body

Write instructions for using the skill and its bundled resources.

### Step 5: Validate the Skill

Once development of the skill is complete, validate the skill folder to catch basic issues early:

```bash
scripts/quick_validate.py <path/to/skill-folder>
```

The validation script checks YAML frontmatter format, required fields, and naming rules. If validation fails, fix the reported issues and run the command again.

### Step 6: Iterate

After testing the skill, forward-test substantial or fragile changes and use
fresh user feedback to identify measured failures.

**Forward-testing and iteration workflow:**

1. Record a baseline on representative tasks.
2. Identify a concrete struggle, inefficiency, or contract failure.
3. Change one instruction group, resource, or tool route at a time.
4. Rerun the same cases and compare observable outcomes.
5. Keep the change only when it improves behavior without weakening boundaries.

Read `references/forward-testing.md` only when planning or running a subagent
forward-test for a tricky skill revision.
