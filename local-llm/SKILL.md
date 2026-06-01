---
name: local-llm
description: Use when the user explicitly asks about local LLMs, LM Studio, local model inventory, local model chat, local model benchmarking, model role curation, or configuring private/local model endpoints. Provides public-safe mechanics and model index guidance for locally hosted or trusted-network OpenAI-compatible models without deciding when other domain skills should use a local model.
metadata:
  short-description: Use local LLM endpoints
resources:
  - path: references/model-index.yaml
    kind: reference
    description: Public curated model roles, strengths, weaknesses, and tested notes.
  - path: references/local-llm.local.example.yaml
    kind: reference
    description: Public-safe template for private `.local/local-llm.yaml` endpoint and role overrides.
  - path: scripts/lm_studio_inventory.py
    kind: script
    description: List models from an LM Studio/OpenAI-compatible `/v1/models` endpoint with local config awareness.
  - path: scripts/lm_studio_chat.py
    kind: script
    description: Send a bounded one-shot chat prompt to an LM Studio/OpenAI-compatible `/v1/chat/completions` endpoint.
  - path: scripts/lm_studio_benchmark.py
    kind: script
    description: Benchmark configured or explicit local models with short cold/fast response probes.
commands:
  - name: local-llm-inventory
    source: skill
    resource_path: scripts/lm_studio_inventory.py
    example_argv: ["uv", "run", "local-llm/scripts/lm_studio_inventory.py", "--json"]
    purpose: Lists configured endpoint trust/locality and available model IDs.
  - name: local-llm-chat
    source: skill
    resource_path: scripts/lm_studio_chat.py
    example_argv: ["uv", "run", "local-llm/scripts/lm_studio_chat.py", "--role", "rollout_scout", "--prompt", "Reply with OK", "--json"]
    purpose: Sends one bounded prompt through a role or explicit model.
  - name: local-llm-benchmark
    source: skill
    resource_path: scripts/lm_studio_benchmark.py
    example_argv: ["uv", "run", "local-llm/scripts/lm_studio_benchmark.py", "--role", "rollout_scout", "--json"]
    purpose: Benchmarks one or more role/model candidates before changing model defaults.
workflow_defaults:
  - name: default_endpoint
    value: http://127.0.0.1:1234/v1
    description: LM Studio default endpoint when `.local/local-llm.yaml` is absent.
  - name: private_config
    value: .local/local-llm.yaml
    description: Gitignored local endpoint, trust, and model override configuration.
---

# Local LLM

This skill documents how to inventory, call, benchmark, and curate local or trusted-network LLMs. It does not decide when every other skill should use a local model; domain skills keep their own evidence and prompting rules.

## Configuration

Public model curation lives in `references/model-index.yaml`. Machine-specific endpoint choices, trusted LAN settings, installed model IDs, and user overrides live in `.local/local-llm.yaml`.

Use repo-root `.local/` for private config. Keeping private files out of skill folders preserves skill portability while still giving each skill a clear private filename.

Copy the shape from `references/local-llm.local.example.yaml` when creating private config. Do not commit real hostnames, LAN addresses, tokens, installed-model inventories tied to a private machine, or private benchmark notes.

## Locality

- `localhost`: same machine as the agent, typically `http://127.0.0.1:1234/v1`.
- `trusted_lan`: a private LAN or trusted mesh/VPN endpoint. Treat as acceptable for private work when local config says the network is trusted.
- `remote_private`: a private endpoint reached over an authenticated tunnel or managed private network.
- `cloud`: an internet-hosted provider, even when the API is OpenAI-compatible.

The endpoint determines data handling. A model name alone does not prove locality.

## Sensitive Material

Localhost and trusted-LAN models are preferred over cloud APIs for private or
sensitive local material when the runtime is trusted. The restriction is on
durable artifacts, not on local inference itself. When local config marks an
endpoint as `locality: localhost` or `locality: trusted_lan` with private/local
trust, it may receive original private local inputs for the current task,
including unredacted rollout snippets, memory drafts, local config extracts, and
repo-specific context. Do not redact away useful local signal just because the
input would be unsafe for a public issue or cloud model.

Keep these boundaries:

- Do not commit raw private prompts, outputs, traces, memory drafts, hostnames, tokens, or account details.
- Do not paste private model output into public issues, PR comments, docs, or skill examples.
- Check whether LM Studio logging/history is enabled before sending material that should not be retained.
- Strip or withhold obvious secrets such as tokens, passwords, API keys, and
  private keys even for trusted local models unless the user explicitly asks for
  secret analysis.
- Redact, summarize, or avoid sensitive material for `cloud`, unknown, disabled,
  or untrusted endpoints unless the user explicitly approves that provider and
  context.

## Model Index

The public model index can include curated model IDs, model roles, strengths, weaknesses, minimum token budgets, and last-tested notes. It is allowed to be opinionated, but it should stay public-safe and time-stamped.

Private config may add, remove, or override role choices. Prefer roles over hard-coded model names in task-specific scripts when the role has already been curated locally.

## Benchmarking

Do not benchmark routinely. Benchmark when adding a model, choosing a model for a new role, investigating odd behavior, after model/runtime updates, or when latency/cold-load behavior matters.

Benchmark output is local machine evidence, not public truth. Keep private endpoint details and raw sensitive prompts out of committed results.

## LM Studio Mechanics

LM Studio exposes local models through OpenAI-compatible endpoints such as `/v1/models` and `/v1/chat/completions`. The `lms` CLI can list, download, load, and unload models. These scripts use the HTTP API because it also works for compatible local or private endpoints.

Large reasoning models may return no assistant content when `max_tokens` is too low because the budget is consumed by reasoning. Increase `max_tokens` for deep models before declaring them unusable.

## Related Skills

Other skills may reference this one for local model mechanics and model selection. They should keep their own domain-specific evidence rules and remain usable when `local-llm` is absent.
