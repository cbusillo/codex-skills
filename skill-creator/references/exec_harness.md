# Exec Harness Skill Evaluation

Use the Every Code exec harness when a skill change affects observable agent
behavior, especially routing, command policy, safety boundaries, GitHub workflow
semantics, or closeout/readiness order. Unit tests and static validators prove
helper code and metadata; exec-harness scenarios prove the model can use the
skill correctly in a realistic turn.

The harness lives in the sibling Every Code checkout:

```bash
python3 ../code/tools/code-exec-harness/harness.py <scenario.json> \
  --skill-root /Users/cbusillo/Developer/codex-skills
```

This repo also has a small opt-in wrapper:

```bash
scripts/validate-exec-harness-skills.sh <scenario.json> [...]
```

Set `CODE_EXEC_HARNESS` if the sibling checkout is not at `../code`.

Run it from the `codex-skills` repo unless a scenario says otherwise. The
harness materializes an isolated workspace, isolated `CODE_HOME`, optional fake
`gh`, optional fake Responses API, and JSON artifacts under
`../code/.tmp/code-exec-harness/` by default.

## When To Use It

Use at least one focused scenario when changing:

- skill trigger text or manual-only behavior
- `policy.command_policies` or command ownership boundaries
- GitHub issue, PR, plan, review, CI, merge, or closeout workflows
- public/private safety rules or local-context handling
- instructions that choose between sibling skills
- helper invocation guidance where the model previously chose the wrong shape

Do not use exec-harness scenarios for every wording edit. Prefer ordinary
validators for schema, link, command metadata, PEP 723, unit-test, and script
behavior checks.

## Harness Modes

Use fake Responses API mode for deterministic context and routing checks. Add a
`responses_api` object to the scenario. The harness records provider requests so
the test can assert that the prompt included or omitted specific text. This mode
does not spend model tokens.

Use fake `gh` mode for harmless GitHub workflow tests. Add a `gh` fixture to the
scenario. The harness places a fake `gh` executable first on `PATH`, records
calls in `artifacts/gh-calls.jsonl`, and can maintain simple fake issue state.
Assert with `expect.gh_contains` or by inspecting the run summary.

Use a real local provider for model behavior. This is the mode for checking that
a local model chooses the right skill, command, or refusal path from realistic
instructions. Keep scenarios short and assertions observable: final message
contains, command contains, fake `gh` calls, file changes, and return code.

## Local Provider Setup

The `local-llm` skill owns local model inventory and privacy guidance. The
public example config is `local-llm/references/local-llm.local.example.yaml`,
and private machine config lives in `.local/local-llm.yaml`.

The exec harness creates an isolated `CODE_HOME`, so it does not automatically
see the user's normal Every Code config. For local LLM scenarios, either embed a
provider in `config_toml` or pass equivalent config overrides.

LM Studio scenario fields:

```yaml
model: qwen3-coder-64b
config_toml: |
  model_provider = "lmstudio"

  [model_providers.lmstudio]
  name = "LM Studio"
  base_url = "http://127.0.0.1:1234/v1"
  wire_api = "chat"
timeout_seconds: 180
max_seconds: 90
```

Before a local-model run, check API visibility and runtime evidence:

```bash
uv run local-llm/scripts/lm_studio_inventory.py --json
```

Inventory shows catalog visibility and LM Studio native runtime state when that
endpoint exposes it; it is not by itself proof that the intended model is warm.
For private prompts, use the local-llm warm-up/readiness flow first, for example
`lm_studio_chat.py --role <role> --load-policy jit_chat --ttl <seconds>
--warmup --prompt "Reply with OK" --json`, and check the served model in the
result before running the harness scenario.

Prefer roles and models already curated by `local-llm/references/model-index.yaml`:

- `qwen3-coder-64b` for the default skill behavior pass
- `qwen3-coder-next` for faster iteration
- `qwen_qwen3.5-122b-a10b` for a slower second opinion
- `openai/gpt-oss-20b` for endpoint smoke tests, not deep behavior scoring

Do not silently fall back from a local/trusted provider to a cloud provider for
private scenarios. If the local endpoint is unavailable, report that the local
eval could not run.

## CI Promotion Posture

`scripts/validate-skills.sh` is required CI for static validators and focused
helper script tests. Deterministic fake `gh` and fake Responses API scenarios
remain opt-in until the external harness checkout is hermetic in CI; when that
checkout is unavailable, record `not_run_reason: harness_unavailable`.

Local LLM scenarios are advisory-only, require a trusted local endpoint, and
must not silently fall back to a cloud provider. When the endpoint is
unavailable, record `not_run_reason: local_endpoint_unavailable`.

Performance summaries are advisory and read local-only harness artifacts. The
source of truth for each check class's promotion decision is
`skill-creator/references/skill-scorecard.yaml`.

## Scenario Design

Keep each scenario narrow. A good scenario has one behavioral claim and one or
two observable assertions. Include a negative or ambiguity case when practical.

Recommended high-value scenarios for this repo:

- GitHub branch discipline: on `main`, implementation work creates a task branch
  and does not edit/push the default branch directly.
- GitHub planning boundary: issue search, project planning, and GraphQL planning
  operations route through `github-plan` instead of raw `gh` commands.
- GitHub execution boundary: issue/PR create, edit, close, and merge operations
  route through the `github` helper scripts.
- Helper invocation shape: PEP 723/Python helpers use `uv run`; installed shell
  helpers may be called directly when documented.
- Readiness to closeout: "are we done?" performs readiness evidence first, then
  cleanup and final state reporting.
- Public safety: private local context and token-shaped strings are not pasted
  into public issue/PR/comment surfaces.
- Bing SEO ambiguity: `url-info` allows `domain:...` for the inspected URL while
  `siteUrl` commands use real `http(s)` site URLs.
- Helper narration versus action: the final answer must match fake `gh` output
  and artifacts must show the helper was actually called.

## Minimal Scenario Skeleton

```json
{
  "name": "github-plan-boundary",
  "prompt": "Find related open issues. Should this become a parent plan?",
  "files": {
    "README.md": "# Harness fixture\n"
  },
  "gh": {
    "repo": "cbusillo/codex-skills",
    "issues": [
      {"number": 100, "title": "Existing plan", "state": "OPEN"}
    ]
  },
  "expect": {
    "returncode": 0,
    "gh_contains": ["issue list"],
    "assistant_contains": ["plan"]
  },
  "max_seconds": 90,
  "timeout_seconds": 180
}
```

For deterministic prompt-shape checks, use `responses_api` and assertions under
`expect.responses`. See `../code/tools/code-exec-harness/scenarios/` for current
examples.

## Reporting Results

Record exec-harness evidence in PR bodies and closeout summaries with:

- scenario file or scenario name
- model/provider mode: fake Responses API, fake `gh`, or local LLM model
- command used
- pass/fail result and artifact directory
- any not-run reason, such as unavailable local endpoint

Do not commit private prompts, private model outputs, raw traces, real endpoint
hostnames, tokens, or local machine inventories. Keep committed scenarios
public-safe and synthetic.

## Performance Summaries

After local harness runs, summarize advisory performance metrics from local
artifacts with:

```bash
uv run skill-creator/scripts/collect_exec_harness_performance.py --latest 10
```

The collector reads local `artifacts/summary.json` and `stdout.jsonl` files and
emits public-safe JSON containing run labels, scenario names, pass/fail counts,
duration metrics when available, command/tool-call counts, and token usage when
reported. Raw harness artifacts remain local and uncommitted.

Treat performance budgets as advisory. Keep deterministic fake Responses/fake
`gh` runs separate from local LLM runs, and record cold and warm local model
runs separately when those scenarios exist.
