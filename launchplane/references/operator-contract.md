# Launchplane Operator Contract

This reference defines the safety rules and execution patterns for the
Launchplane Operator.

## Execution Rules

- **Auth**: Prefer signed-in, scoped operator sessions for human-approved
  product-config dry-run/apply and other UI-backed operator mutations. Source
  terminal/local operator credentials only from explicit private config when the
  workflow requires non-browser execution.
- **Targeting**: Use the deployed Launchplane service API or CLI paths. Do not
  fall back to direct provider mutation.
- **Runtime Authority**: Checked-in config, workflow defaults, checked-in
  examples, and archived workstation files are not authoritative for real
  product, tenant, repository, branch, domain, lane, provider-target,
  runtime-environment, authz, operator, route, or health-check values. If a live
  value is missing from Launchplane records or explicit scoped operator input,
  fail closed and ask for the service/operator source instead of inferring it
  from repo-local files.
- **Verification**: Always perform a `dry-run` and inspect redacted evidence
  before applying changes.
- **Reason**: Apply operations require a concrete non-empty reason.
- **Helper**: Use `scripts/launchplane-write-action.py` and
  `references/write-action-helper-contract.md` for bounded terminal helper
  calls. Do not open-code Launchplane write routes in skill guidance.
- **Merge Train**: Use
  `POST /v1/work-graph/merge-train/controller/run-once` as the default merge
  train operation. Phase-specific merge-train endpoints are troubleshooting and
  recovery surfaces, not the normal skill path.

## Private Operator Config

Terminal/operator execution is optional private configuration. It is not needed
for read-only Launchplane context and it is not the default path for ad hoc
plaintext secret entry.

Source order for non-browser operator execution:

1. Explicit command-line JSON config path supplied by the caller.
2. Environment variables already present in the current process.
3. An ignored Launchplane-local `.env` file at
   `~/.config/launchplane/local-operator.env`, containing only the documented
   Launchplane operator environment keys.
4. An ignored Launchplane-local JSON config file at
   `~/.config/launchplane/local-operator.json`, using the fake shape in
   `references/launchplane-operator.local.example.json`.

Environment variable names:

- `LAUNCHPLANE_OPERATOR_URL`: Launchplane service base URL for write-capable
  operator requests.
- `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`: operator bearer token. Never print or
  copy this value.
- `LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT`: optional operator subject header value.
- `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`: optional operator token-label header
  value.

The local config file stores environment variable names and a fake/public-safe
service URL example only. Real token values stay in the operator's private
environment or secret manager. Missing private config is a normal unavailable
state for terminal execution; explicit write actions must fail closed instead of
falling back to direct provider mutation or read-only context credentials.

Do not use `.github/github.override.json` for secrets. That file is suitable for
repo metadata overrides only, not Launchplane operator credentials.

## Request/Response Shapes

See `references/context.available.example.json` for the structure of Launchplane
context payloads.

See `references/write-action-helper-contract.md` for the helper request/response
shape, supported product-config and merge-train entrypoints, idempotency
requirements, and compact failure statuses.

Product-config helper work starts with `POST /v1/agent/write-intents/evaluate`
for `intent: "product_config_apply"` and `mode: "dry_run"`. That preflight may
name managed secret binding keys as metadata, plus a runtime destination for
runtime key-safety evaluation. It must not carry plaintext secret values.

`POST /v1/product-config/apply` accepts plaintext secret values only from an
approval-capable operator surface that already has a private value source. The
terminal helper may submit that route only from a private local payload file and
must never accept plaintext secret values as CLI arguments, stdin, chat, issue
text, PR text, or committed examples. The payload file is explicit private
operator input, not checked-in repo config or copied provider topology.
Local-operator apply requires a prior matching dry-run and a stable idempotency
key.

`POST /v1/work-graph/merge-train/controller/run-once` accepts repository,
base-branch, and mutate mode. Mutating helper calls require an idempotency key;
dry-run calls may omit it. Stop and report controller attention states instead
of calling phase-specific endpoints by default.

## Safety & Redaction

- Service paths return metadata only. Launchplane does not expose routine
  plaintext read commands.
- Dry-run and apply both evaluate runtime key-safety policy before returning
  sanitized key/count evidence.
- Agents may prepare request shapes and summarize redacted results, but a
  signed-in/scoped operator supplies and approves plaintext runtime or managed
  secret values through the operator path.
- Runtime key-safety failures are blockers, not warnings.
- After product-config apply, run the sanctioned live-target-runtime sync/deploy
  path when the running target needs Launchplane-owned values applied.
- Report key names, actions, counts, trace IDs, record links, and status only.
- Do not hardcode repositories, labels, tokens, branch names, private hosts, or
  local file config in skill guidance or helper examples.
