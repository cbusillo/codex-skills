---
name: model-review
description: Use when a change should be reviewed by a model from another provider, or when the user asks for a second opinion, an independent or outside review, dissent, or a Gemini, GPT, or Claude review of work. Covers shared agent instructions, approval and safety rules, destructive helpers, and contracts other repositories depend on. Runs OpenAI, Anthropic, and Google models read-only against a repository and says how to weigh what they return without adding edge cases nobody can produce. Do not use for ordinary code review of tested code, a human reviewer's comments, or type-checker and scanner output.
metadata:
  short-description: Read-only review by another provider's model
resources:
  - path: scripts/review_with_model.py
    kind: script
    description: Runs one provider's model read-only against a repository, reports the model used, and fails loudly when the reviewer could not read or returned nothing.
commands:
  - name: model-review-run
    source: skill
    resource_path: scripts/review_with_model.py
    example_argv: ["uv", "run", "scripts/review_with_model.py", "run", "--provider", "google", "--repo", ".", "--prompt-file", "<file>"]
    purpose: Asks one provider's model for a read-only review and returns JSON with the model used.
  - name: model-review-check
    source: skill
    resource_path: scripts/review_with_model.py
    example_argv: ["uv", "run", "scripts/review_with_model.py", "check", "--repo", "."]
    purpose: Shows which providers can actually read the repository from this machine.
---

# Model Review

For repositories with `DIRECTION.md`, follow the shared
[executing loop](../references/executing-loop.md) when reviewing execution work.

Apply [task scope and authorization](../references/execution-scope.md) when
using this workflow.

## Outcome

A review of the change by at least one model from a different provider than the
author, with each finding acted on, declined with a reason, or left open, and a
short record of who reviewed and what was done. Read
[reviews by another model](../references/model-review.md) first: it owns when a
review is worth asking for and how to weigh findings. This skill owns running
one.

## Run A Review

1. Write a prompt file that names the change, the paths to read, and what the
   change is for. Do not paste file contents, your argument that the change is
   right, or the answer you expect. Ask for a realistic trigger and a severity
   for each finding, and allow the answer "none".
2. Run each reviewer from this skill's base directory:

   ```bash
   uv run scripts/review_with_model.py run --provider google --repo <repo> --prompt-file <file> --out <review.md>
   ```

   Providers are `openai`, `anthropic`, and `google`. Each is started so that
   it reads the repository with its own tools and has no way to write to it.
3. Read `model` in the JSON result and report reviewers by provider and that
   model. When `model_source` says the CLI did not report it, say so instead of
   stating it as fact. A provider's default may be the author's own model; pass
   `--model` to pick a different one, and do not call a same-model run
   independent.
4. Weigh the findings under the shared reference, then record in the pull
   request or owning issue: reviewers, what was acted on, what was declined and
   why, and what was reproduced.

## When A Run Fails

Exit 0 means a review came back. Exit 1 means the run failed, and exit 2 means
that provider's CLI is not installed. A reviewer that could not read files, or
returned nothing, is reported as a failure, never as "no findings"; do not
paper over it by pasting files into the prompt.

- Run `uv run scripts/review_with_model.py check --repo <repo>` to see which
  providers can read the repository from this machine before spending a review.
- A `google` failure that names denied actions carries a `hint` with the exact
  allow rules the user's own `agy` settings need. Show the user that hint. Apply
  it with the `configure` subcommand only when the user asks: it edits their
  personal tool configuration, and the rule applies to every `agy` session.
- A `google` failure that lists `write_file` rules means the user's own settings
  would let the reviewer change files. Do not work around it; show it.
- When no other provider can be made to work, say so and use what is available,
  as the shared reference describes.
