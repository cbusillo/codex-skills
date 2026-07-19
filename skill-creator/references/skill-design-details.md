# Skill Design Details

Use this reference when a skill needs structured metadata, bundled-resource
decisions, or worked progressive-disclosure patterns. Keep the main `SKILL.md`
focused on goal, success evidence, autonomy, routing, and result contracts.

## Structured metadata

Use `resources` to declare bundled files, `commands` to declare routine
entrypoints, and `workflow_defaults` for simple stable defaults.

```yaml
resources:
  - path: scripts/example.py
    kind: script
    description: Runs the deterministic example workflow.
commands:
  - name: example-command
    source: skill
    resource_path: scripts/example.py
    example_argv: ["uv", "run", "scripts/example.py", "--flag"]
    purpose: Runs the example workflow.
  - name: repo-command
    source: repo
    example_argv: ["just", "check"]
    purpose: Runs the repository check.
  - name: external-command
    source: external
    example_argv: ["gh", "pr", "view"]
    purpose: Reads a pull request through GitHub CLI.
workflow_defaults:
  - name: default_path
    value: skills
    description: Default output directory.
```

- `resources[].kind` must be `script`, `reference`, `template`, or `asset`.
- Every command declares `source`.
- `source: skill` commands declare an existing `resource_path` that is also
  listed in `resources`.
- `source: repo` and `source: external` commands do not declare
  `resource_path`.
- Do not invent structured fields for options, variants, network requirements,
  sandbox hints, or output templates.

## Command policies

Use command policies when a raw command should route through a maintained
helper, require confirmation, or be rejected. They are portable metadata, not a
runtime blocker unless the host loads them into command execution.

```yaml
policy:
  command_policies:
    - id: prefer-helper
      match:
        argv_prefix: ["tool", "subcommand"]
      action: require_preferred
      message: Prefer the maintained helper for this workflow.
      preferred:
        - kind: script
          path: scripts/helper.py
          example_argv:
            ["uv", "run", "scripts/helper.py", "subcommand", "<target>"]
          purpose: Runs the workflow through the maintained helper.
```

Supported matchers are `argv_exact`, `argv_prefix`, and `shell_regex`; declare
exactly one. Supported actions are `require_preferred`, `require_confirm`, and
`reject`. Preferred entries may name `script`, `skill`, or `command` actions.
Read `command-policy-contract.md` for precedence, path resolution, simulator,
and exec-harness details.

## Resource design

### Scripts

Use scripts for deterministic reliability or work that would otherwise be
rewritten repeatedly.

- Python scripts include PEP 723 inline metadata and run with `uv run`.
- Shell helpers use direct or `bash` invocation and are never shown as Python
  or `uv run` commands.
- `commands[].example_argv` and policy preferred actions must match the helper
  type.
- Fragile multiline Markdown, JSON, or shell-sensitive payloads should use
  stdin or files instead of prose about quoting.
- Added scripts must be executed against representative inputs.

Example Python header:

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
```

### References

Use references for schemas, API documentation, domain knowledge, policies,
variant-specific instructions, and detailed workflow guidance. Load them only
when relevant.

- Keep information in either `SKILL.md` or a reference, not both.
- Link references directly from `SKILL.md` and state when to read each one.
- Give files over roughly 100 lines a table of contents.
- For very large references, include useful search terms in `SKILL.md`.

### Assets

Use assets for templates, images, icons, fonts, boilerplate, or sample files
that the agent should copy or modify without loading them as instructions.

## Progressive-disclosure patterns

### High-level guide with references

```markdown
# PDF Processing

Extract text with the primary helper.

- Form filling: read `references/forms.md`.
- API details: read `references/api.md`.
- Common examples: read `references/examples.md`.
```

### Domain or provider split

```text
cloud-deploy/
├── SKILL.md
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```

Keep provider selection and the shared workflow in `SKILL.md`; load only the
chosen provider reference.

### Conditional advanced details

```markdown
# DOCX Processing

Create documents with the primary helper.

- Tracked changes: read `references/redlining.md`.
- OOXML internals: read `references/ooxml.md`.
```

The main skill should remain usable without loading every advanced reference.
