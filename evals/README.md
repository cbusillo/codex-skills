# Owning-skill routing evaluation

The cases under `owning-skill/` cover a merge during a direction session,
CI watching during GitHub work, an adjacent spelling correction, and a merge
discussion with no authority to act. Only the initial skill is named in the
task. The second skill must be discovered from the catalog.

Run the plugin suite on Claude Code with a narrow Bash grant; operational
commands must stay denied in these offline fixtures:

```sh
claude plugin eval . --tag owning-skill --ablation none --runs 3 \
  --allow-tools 'Bash(git status:*)' --no-publish --trust-plugin --keep-temp
```

Use the same cases and model on the baseline and candidate catalog revisions;
a no-plugin ablation is not a before/after catalog comparison. Inspect raw
traces as well as grader scores. An absent call cannot prove that a helper was
chosen. The merge and watch graders require a skill load before the attempted
operation and reject raw commands even when a policy hook redirects them.

On hosts where the plugin eval's Bash sandbox cannot start, the direct CLI
runner supplies a fixture hook that permits simple file reads and refuses
operational shell commands. It is also the Codex comparison path:

```sh
uv run evals/run-routing.py --host claude --catalog /path/to/catalog \
  --out /path/to/artifacts/claude --runs 3
uv run evals/run-routing.py --host codex --catalog /path/to/catalog \
  --out /path/to/artifacts/codex --model gpt-6-astra --runs 3
```

Use an artifact location permitted by the host's workspace policy. The runner
does not change the installed catalog, user configuration, or credentials.
Codex uses a read-only sandbox and invocation-scoped trust for the test hooks
authored here. Claude uses a separate settings file and only the listed tools.
The same policy hook runs on both hosts, and Claude receives its startup hook
only through the plugin. Each fixture is its own repository, so its adoption
state and reminders do not depend on the surrounding worktree. Receipts record
catalog and harness hashes, configured models, and deterministic routing scores.
The runner exits nonzero for a failed grade as well as for a failed CLI run.
Traces establish skill source paths and command order; distinguish configured
models from model names independently reported by the host. Existing personal
instructions can still affect direct CLI runs, so keep the environment fixed.
These tests establish routing and attempted tool calls, not successful GitHub
mutations. A real interactive merge and CI watch remain separate acceptance
evidence.

The matched comparison measures the combined instruction and hook changes;
it does not isolate the Claude-only protocol from the new routing instructions
in `direction` and the execution loop.

The native plugin-eval schema and limitations are documented in
[Anthropic's eval reference](https://code.claude.com/docs/en/plugin-evals).
The context-delivery and blocking contracts are in
[Anthropic's hook reference](https://code.claude.com/docs/en/hooks).
