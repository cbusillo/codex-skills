---
name: model-review
description: Use when a change should be reviewed by a model from another provider, or when the user asks for a second opinion, an independent or outside review, dissent, or a Gemini, GPT, or Claude review of work. Covers shared agent instructions, approval and safety rules, destructive helpers, and contracts other repositories depend on. Runs OpenAI, Anthropic, and Google models read-only against a repository and says how to weigh what they return without adding edge cases nobody can produce. Do not use for ordinary code review of tested code, a human reviewer's comments, or type-checker and scanner output.
metadata:
  short-description: Read-only review by another provider's model
resources:
  - path: scripts/review_with_model.py
    kind: script
    description: Runs one provider's model read-only against a repository, reports model provenance, and fails loudly when the reviewer could not read or returned nothing.
commands:
  - name: model-review-run
    source: skill
    resource_path: scripts/review_with_model.py
    example_argv: ["uv", "run", "scripts/review_with_model.py", "run", "--author-provider", "openai", "--repo", ".", "--prompt-file", "<file>"]
    purpose: Asks one provider's model for a read-only review and returns JSON with model provenance.
  - name: model-review-check
    source: skill
    resource_path: scripts/review_with_model.py
    example_argv: ["uv", "run", "scripts/review_with_model.py", "check", "--author-provider", "openai", "--repo", "."]
    purpose: Shows which providers can actually read the repository from this machine.
  - name: model-review-repair
    source: skill
    resource_path: scripts/review_with_model.py
    example_argv: ["uv", "run", "scripts/review_with_model.py", "repair"]
    purpose: Backs up the user's agy settings and removes only stale command grants the reviewer policy no longer permits.
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

Read [standing review decisions](references/provider-selection.md) before the
first run or when the Director changes provider/model decisions. Keep the
recorded decision in its one private shared file. Supply the author's actual
provider; let the helper follow the standing order and Context Panel capacity.
An explicit provider cannot bypass that policy. Finish the primary review
before running any allowed supplementary review, serially with its recorded
budget, and put both results in the PR's review note.

1. Commit the change and leave the worktree clean. Write a prompt file that
   names the change, the paths to read, and what the change is for. For a
   [pending decision](../references/model-review.md#before-a-decision), there
   is no change: describe the saved issue instead. Never ask
   the reviewer to run a command or tests, and do not forbid commands either:
   the OpenAI reviewer reads files through its read-only shell. Say, for example,
   "Your access is read-only; read whatever files you need, and do not modify anything."
   The helper names a temporary diff file in
   its preamble when the branch has committed changes;
   ask the reviewer to read it directly. Do not paste file contents, your
   argument that the change is right, or the answer you expect. Ask for a
   realistic trigger and a severity for each finding, and allow the answer
   "none". For each defect, ask for every sibling path with the same cause, not
   just one counterexample, so the whole kind is fixed at once. On a rereview,
   name what changed since the last review and ask the reviewer to cover that
   plus what it touches.
2. Run each reviewer from this skill's base directory:

   ```bash
   uv run scripts/review_with_model.py run --author-provider openai --repo <repo> --prompt-file <file> --out <review.md>
   ```

   Providers are `openai`, `anthropic`, and `google`. Each is started so that
   it reads the repository with its own tools and has no way to write to it.
   That includes dropping MCP servers, which the harnesses do not sandbox, and
   the reviewed repository's own tool settings, which could start hooks. The
   `openai` reviewer also ignores `~/.codex/config.toml`, so settings there,
   such as its default model, do not apply.

   Reviewers have no network, and some read only granted roots. Pass each
   file outside the repository that the review needs, such as a saved issue
   or the overall DIRECTION.md, with a repeated `--input FILE`. The helper
   copies it next to the diff, names it in the preamble and removes it after
   the run. Links in the prompt stay unopened: the helper tells the reviewer
   so and lists them in the result's `unreachable_links`. Before weighing the
   review, pass any linked content it needed with `--input` and run it again.

   All reviews select their provider's Context Panel use-next
   account at launch through the [Supervisor account reader](../supervisor/scripts/account_choice.py).
   They use its private `skill-data/supervisor.toml` mappings and snapshot command;
   see [account configuration](../supervisor/references/helpers.md). The chosen
   `CODEX_HOME` or `CLAUDE_CONFIG_DIR` applies only to the reviewer child. A default
   Anthropic choice removes an inherited home override. Inherited provider token/key and cloud-routing
   overrides are removed by name so they cannot bypass the selected home's login;
   no credentials or logins are read or changed by the helper. Missing config,
   unavailable choices or receipt-storage failures stop the review before launch.
   JSON reports the account name and opaque ID plus the count-only launch receipt.
   The receipt goes through the installed Context Panel's `--record-launch`, so a
   session macOS refuses the App Group store, such as a worker under the
   Supervisor's launchd-started tmux server, runs its own review, with or without
   a PR ([#2069](https://github.com/cbusillo/codex-skills/issues/2069)).
   No fixed review-home
   export is needed in a Supervisor launch file. Google uses the account reader's
   single existing agy login binding, without an account override.

3. Read `model` and `effort` in the JSON result and report the provider and
   confirmed model. When `model_source` says the CLI did not report it, say so
   instead of stating it as fact. A provider's default may be the author's own model; pass
   `--provider NAME --model MODEL` to pick a permitted different one, and do not call a same-model run
   independent.
4. Weigh the findings under the shared reference, then record in the pull
   request or owning issue: reviewers, what was acted on, what was declined and
   why, and what was reproduced.

## When A Run Fails

Exit 0 means a completed review came back. Exit 1 means the run failed, and exit 2 means
that provider's CLI is not installed. A reviewer that could not read files, or
returned nothing, is reported as a failure, never as "no findings"; do not
paper over it by pasting files into the prompt.

The helper requests a provider-neutral JSON completion result internally and
returns just its report as `response` or the output file. Only `status: completed`
with a nonempty `review` succeeds; a completed no-findings report may say `none`.
The `review` field is a string; requests for structured findings are encoded as
JSON text inside it, so callers still receive that text as their report.
Each provider's CLI enforces that result's JSON schema, and the helper reads
the CLI's validated object rather than the model's final text, which may carry
narration around the JSON. Anthropic and Google results say whether that object
was present in `structured_output`; Codex writes the constrained answer itself.
An explicit `refused` or `incomplete` status fails with that bounded outcome.
Missing or malformed completion evidence is `unavailable`, even after file reads;
its `completion_shape` says why (for example, text before the JSON object)
without quoting the response.
These failures retain model/account provenance but omit partial or withheld text;
they do not produce an output file or consume a planted finding. No refusal is
retried to obtain a different answer. This confirms the reviewer's declared
completion, not the correctness or coverage of its report; weigh those yourself.

OpenAI runs must show a successful shell command in their JSONL tool evidence;
an answer without one fails even when the CLI exits 0. This proves tool activity,
not that the reviewer read every relevant file; check the review's coverage too.
Failures distinguish no command attempts from attempts without a successful command.
Nonzero exits retain the JSON provider error and distinct stderr cause as bounded,
credential-redacted diagnostics; OpenAI's malformed stdout is never excerpted.
OpenAI's `execution` also reports elapsed seconds, the CLI exit code (null on
timeout), last and terminal event types, JSONL validity, whether an answer file
was produced, and failed command count. `completion_shape` distinguishes a
reviewer-declared `incomplete` or `refused` from malformed completion evidence.
`command_diagnostics` recognizes GitHub connection failures even when a later
command made the shell exit 0. These are observed execution signals, not proof
of the reviewer's reason; absent signals leave the cause unknown. No command,
source output, narration or partial report is quoted. A completed Codex turn
can still contain an incomplete review; it remains a failure without a retry.

- Run `uv run scripts/review_with_model.py check --author-provider openai --repo <repo>` to see which
  providers can read the repository from this machine before spending a review.
- Anthropic and Google diff scratch stays inside the authorized checkout and is
  removed after the run. That makes the review a writer in the worktree: let it
  finish before an inspection of the same worktree starts, or start it after.
  Anthropic permission failures report `denied_targets` with the read tool and
  a bounded path when the CLI supplies one; search
  patterns and other tool inputs are omitted. A denied read remains a failure.
- Google uses `read_file` (`view_file`) for file reads, including
  successive line ranges for large files. If a shell command is refused, the
  helper reads only that run's saved conversation to name the command and
  retries once with file tools only when the stopped run returned no response.
  A nonempty response alongside a command denial remains unavailable without a retry.
  `recovery` records the first refusal;
  a second refusal remains a failure. The timeout applies to each attempt.
  When the conversation is unavailable or
  unreadable, `command_diagnostic` says why the exact command could not be found.
  A command-only refusal does not call for broader permission grants.
- A `google` failure that names denied file actions carries a `hint` with the exact
  allow rules the user's own `agy` settings need. Show the user that hint. Apply
  it with the `configure` subcommand only when the user asks: it edits their
  personal tool configuration, and the rule applies to every `agy` session.
- A `google` failure that lists `rules` outside the read-only set means the
  user's own settings would let the reviewer do more than read. When every
  listed rule is in `stale_command_grants`, they are the plain `command(find)`
  and `command(rg)` grants this helper itself once told users to add and now
  refuses. Run `repair`: it backs up the settings file beside itself, removes
  only those grants, keeps every other setting, and prints the backup path.
  During an executing-loop run, `repair` needs no Director step; it only takes
  back what the helper asked for and would now refuse to start with. The helper
  cannot tell whether the user later granted those two commands for their own
  sessions, so the backup and the record are the safeguard: record the removed
  grants and the backup path in the pull request, so the Director can put them
  back with one copy if they want them. A `write_file` rule,
  a command grant with arguments, a grant for a program the user allowed for
  their own sessions such as `command(git)`, or anything else the helper calls
  `ambiguous` is the Director's to change: `repair` refuses it and changes
  nothing, so show the result and ask, or use another provider. Do not edit
  the settings file by any other route.
- When no allowed provider can run, report the outcome. Keep the standing
  decision in force; do not substitute a provider through another command.
