# Local Codex Agent Execution

Read this reference when a task needs a local model to perform tool-assisted
work through Codex or Codex Lab. For a direct model reply, use the ordinary
`lm_studio_chat.py` route instead.

## Select the host and model

Reuse the task's established host and model. Otherwise inspect the available
host's version/help and the trusted endpoint's current model inventory. Select
an installed model suited to the bounded task; public historical model-index
entries do not establish current availability or host compatibility. Keep model
installation and broad benchmarking scoped to tasks that need them.

Use `scripts/local_codex_agent.py` with an explicit `--host` and either a
verified `--model` or an explicitly configured private `--role`. The host's
Responses API path and the selected model must support the needed tool behavior.
Do not point the retired Every Code wrapper at a different binary.

From the catalog root, a bounded example is:

```sh
uv run local-llm/scripts/local_codex_agent.py \
  --host codex \
  --model <verified-local-model-id> \
  --max-seconds 120 \
  --workdir <task-workspace> \
  'Read the supplied fixture and report the requested result.'
```

Use `--host codex-lab` for that host. `--config` selects private endpoint/role
configuration; `--endpoint` selects an endpoint within it. The wrapper owns
`--max-seconds`; it does not pass the retired flag to either CLI. Use an output
path only when the task needs a retained artifact, following repository and
host artifact rules.

## Isolation and authority

The wrapper creates a temporary child home, selects a custom local Responses
provider, and uses a noninteractive sandboxed execution path. It does not copy
user authentication or fall back to a cloud provider. The default sandbox is
read-only; choose a broader supported sandbox only within the task's existing
authorization and workspace constraints.

This isolation also means inherited user settings and project trust are absent.
AGENTS instructions and managed host policy still apply, while loading of
project configuration, hooks, and rules follows the host's fresh-home trust
behavior. Use the ordinary authorized host path when testing those configured
runtime policies. An isolated local run does not establish that the user's
normal host session loaded the same skills, settings, or policies.

The wrapper bounds its owned process group and temporary state. It does not
claim to manage unrelated or independently detached services. Preserve the
host's diagnostic result if timeout, missing final output, or incompatibility
prevents a successful run.

## Qualify and report

Start with a synthetic task that requires one observable action, such as reading
a fixture and returning a value available only in that file. Check the executed
tool event, final artifact, host/version, endpoint locality, and requested model.
Report served-model identity only when the host exposes it. A plain OK response
alone is not a tool-use qualification.

Keep actual failure status and missing evidence visible. Local-model behavior
does not establish Astra behavior, and a successful synthetic run is not a
performance comparison or a guarantee for other models and tasks.
