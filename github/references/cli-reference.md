# gh-plan.py CLI Reference

The `gh-plan.py` script is a compact helper for managing GitHub issues. It
returns compact JSON and avoids loading large issue bodies unless requested.

## Usage Standard

Always use `scripts/gh-plan.py` instead of ad hoc `gh` calls for planning state.
Prefer `uv run scripts/gh-plan.py` for hermetic execution.

## Common Commands

### Orientation
- `index`: List compact plan issues (no bodies).
- `search <query>`: Search for issues.
- `show <issue>`: Show selected sections. Use `--full` for the entire body.
- `deps <issue>`: Show dependencies and sub-issues.

### Management
- `create <title>`: Create a new plan issue. Supports `--title` (flag), `--body`,
  `--plan-status`, `--focus`, and `--finish-line`.
- `update-section <issue> <section>`: Patch a single markdown section.
- `link <issue> <rel> <target>`: Manage native `blocked-by`, `blocks`, or `subissue` relationships.
- `close <issue>`: Mark plan as done, update labels, and clear Project focus.

### Projects
- `project-list --owner <owner>`: List Projects.
- `project-add <issue> --project <name>`: Add issue to a Project.
- `project-set <issue>`: Update Project fields (`--focus`, `--manager`, `--finish-line`).

## Formatting Tip

For multiline comments or bodies, use `--body-file <path>` or `--comment-file <path>`.
Avoid passing escaped `\n` through shell-quoted `--body`.
