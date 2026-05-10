---
name: python-uv-workflow
description: Use for Python repo tasks involving setup, commands, scripts, tests, dependencies, lockfiles, packaging, builds, releases, PyPI/TestPyPI, or environment management. Steer toward uv and repo-defined entrypoints instead of system Python, pip, or ad hoc virtualenv commands.
---

# Python uv Workflow

Use this skill when Python environment or command execution matters. Do not
trigger just because a Python file is being edited; trigger when setup, tests,
scripts, dependencies, packaging, release, or runtime commands are involved.

## Core Rules

- Prefer `uv run ...` for Python commands.
- Do not call system `python`, `pip`, or ad hoc virtualenv paths unless a repo
  explicitly requires it or you are diagnosing environment bootstrap failure.
- Inspect `pyproject.toml` before choosing commands.
- Prefer `[project.scripts]` entrypoints and repo wrapper commands over generic
  `uv run pytest` or `uv run python`.
- Use repo `AGENTS.md`, README, and docs for exact gates and release policy.
- Keep `pyproject.toml` and `uv.lock` in sync when dependency metadata changes.
- Do not use real credentials for normal tests unless the repo explicitly gates
  live tests behind environment variables and the user asks for them.

## Setup And Commands

- Environment setup: `uv sync` or the repo-specific documented variant.
- Run scripts: `uv run <script-name>` when `[project.scripts]` provides one.
- One-off Python: `uv run python <script.py>`.
- Tests: prefer repo wrappers such as `uv run test`, `uv run mcp-test`,
  `uv run platform ...`, or documented `uv run pytest ...` commands.
- Formatting/linting/type checks: use documented uv commands and respect the
  user's linting constraints. Do not run broad lint unless requested or scoped
  to changed files.

## Dependencies And Lockfiles

- If dependencies, optional dependencies, build-system requirements, project
  metadata, or Python version constraints change, refresh/check the lockfile
  according to repo policy.
- Prefer `uv lock` for lockfile refresh and `uv sync --locked` or repo-specific
  lock checks for verification when documented.
- Commit `pyproject.toml` and `uv.lock` together when both changed for the same
  dependency or metadata update.
- Do not use `pip install` to mutate the environment in a uv-managed repo.

## Packaging And Release

For package/release work, inspect repo docs first. Typical uv-backed checks:

```bash
uv build
uv run twine check dist/*
```

Use these only when the repo has the relevant dependencies/tooling or documents
the workflow. Release-specific work may also require:

- version bump in `pyproject.toml`
- `uv lock`
- changelog or release notes
- tag naming policy
- GitHub Actions trusted publishing configuration
- TestPyPI/PyPI environment rules
- verifying package import name versus distribution name

Do not publish, tag, push, or dispatch release workflows unless the user
explicitly asks.

## Repo-Specific Execution

Repository-specific execution details—such as custom test commands, lockfile policies, or platform-specific entrypoints—should be managed via repository metadata:

- **`.github/github.json`**: Use the `qualityGate` and `metadataFreshness` blocks to define canonical commands.
- **`AGENTS.md`**: Refer to this file for behavioral quirks, lockfile consistency requirements, or stewardship rules specific to the repository.
- **Environment Variables**: Use repository-documented environment variables for gating live tests or providing necessary credentials.

Always favor the repository's own defined agent instructions and metadata over generic defaults.

## Reporting

When commands cannot be run, state why and separate environment/tooling blockers
from code failures. In final summaries, report the uv commands used rather than
raw command output unless the user asks for details.
