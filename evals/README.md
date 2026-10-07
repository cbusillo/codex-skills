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
Routing scores now record grader_version 3. Archived acceptance receipts keep
their original grades; a new rubric or source revision requires a separately
identified comparison, not an edit to those receipts.

Read delivery and command success are separate observations. When a compound
shell command returns nonzero, the grader credits only complete pinned catalog
or fixture file text actually present in its output. A denied read, a filename,
or unrelated stdout does not prove delivery. Partial excerpts remain unproven
for that nonzero-command recovery. Native Claude Read results also establish
source attribution; bundled .system skills do not count as maintained catalog
sources. The forbid_read check matches attempted shell and native Read/Grep
paths, including failed attempts and targeted globs, rather than search-pattern
text. The existing forbid check continues to apply to operational commands.
Traces establish skill source paths and command order; distinguish configured
models from model names independently reported by the host. Existing personal
instructions can still affect direct CLI runs, so keep the environment fixed.
These tests establish routing and attempted tool calls, not successful GitHub
mutations. A real interactive merge and CI watch remain separate acceptance
evidence.

Cases under `multi-turn/` use `turns.yaml`, which only the direct runner reads,
and grade each turn on its own: a skill loaded in an earlier turn does not
cover a later step. A quiet turn must load nothing, and `/compact` as a turn
compacts a Claude Code session before the next step. Claude Code turns run in
one stream-json process without session persistence. Codex turns resume a
session recorded in a per-run `CODEX_HOME` that links only `auth.json`, so the
Director's session history is untouched. Every Codex run gets that home and an
empty `HOME`, because Codex always scans `$HOME/.agents/skills` and an installed
catalog there would compete with the tested one. Claude Code does not inject a skill's
text again while it is still in context, so a repeated `Skill` call counts
when that skill was confirmed from the tested catalog earlier in the run.

Cases under `pr-monitoring/` use the same `turns.yaml` format to grade what
`babysit-pr` decides, not only which skill owned the step. A turn's `expect`
may add `operation` (a pattern the first operational command must match),
`require` (some operation must match), `forbid` (no operation may match), and
`final` (the turn's final answer must match), and `prior` (patterns that
must each match a read before the first operation, such as a fingerprint). A
refused command still counts as attempted. The offline shell boundary supports
print-only numeric sed line selections such as `sed -n '1,80p' file` (also
semicolon-separated print selections, an optional trailing semicolon, and a
single `-e` before the print script). Other sed programs and extra options
are refused; use that form or ordinary `cat`, `head`, or `tail` reads instead.
Cases under `closeout/` use the same
grades for `work-closeout`; their `setup` scripts build real Git state, such as
an upstream one commit ahead and a dirty or untracked file, with a fixed
identity and clock so commit IDs are reproducible.

The quiet expectation grades routing only, not task completion. For a quiet
local-fact task, use an empty `owner` list, quiet: true, a required read, and
answer_from_fixture with file, a capture pattern containing one value group,
and accepted full-answer forms containing {value}. The expected value comes
from the fixture itself; a refusal, a guessed value without a delivered read,
or a value contradicted by the fixture fails. Accepted forms normalize case
and whitespace, Markdown formatting and final punctuation, but remain an
explicit bounded output contract. The qualified local-fact forms include the
observed sentence “In local.py, MAX_RETRIES is set to {value}.” and compare
its whole normalized answer with the captured fixture value. Additional prose,
including a second contradictory value, falls outside this contract; this does
not establish general semantic answer grading. Historical trial scores stay
unchanged when the form list is extended.

Private-documentation cases also use `owner_before_read` with an owning skill and
a read pattern. It requires a delivered matching read and the owning skill
before its first delivery, including turns with no operational command. Loading
the skill afterward fails even if it is loaded before a later reread. This
checks observed delivery order; it does not prove every recursive search path,
the timing of a failed read attempt, or completion of a skill load before a
read chosen in the same command or parallel tool batch. Inspect raw tool calls
and results too.
The nested source-planning fixture moves the synthetic route and operations
file out of the repository root to qualify discovery cues separately from
private-documentation authority. Its AGENTS override is offline plumbing,
not evidence about real home configuration or production access. Its hidden
directory also reduces discovery by searches that skip hidden paths by default;
a passing nested run alone does not establish model restraint.

The final_any check accepts a listed alternative pattern; final_none rejects
listed contradictory answers. These are lexical checks, not a general semantic
judge: inspect actual finals when the answer falls outside the qualified rubric.

Cases under `github-execution/` grade the `github` workflow the same way: a
train landing and its exact-SHA runtime refresh, an adjacent planning request,
missing merge and identity authority, an unknown merge outcome, and an
approval request for a deploy path whose blockers sit in different files, which
must name every blocker at once. They may
also set `read`, a pattern that some file read before the turn's first
operation must match, so a case can require the reference that holds the
contract it tests.

The matched comparison measures the combined instruction and hook changes;
it does not isolate the Claude-only protocol from the new routing instructions
in `direction` and the execution loop.

The native plugin-eval schema and limitations are documented in
[Anthropic's eval reference](https://code.claude.com/docs/en/plugin-evals).
The context-delivery and blocking contracts are in
[Anthropic's hook reference](https://code.claude.com/docs/en/hooks).
