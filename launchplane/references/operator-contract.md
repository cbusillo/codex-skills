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
- **Verification**: Always perform a `dry-run` and inspect redacted evidence
  before applying changes.
- **Reason**: Apply operations require a concrete non-empty reason.
- **Merge Train**: Use
  `POST /v1/work-graph/merge-train/controller/run-once` as the default merge
  train operation. Phase-specific merge-train endpoints are troubleshooting and
  recovery surfaces, not the normal skill path.

## Private Operator Config

Terminal/operator execution is optional private configuration. It is not needed
for read-only Launchplane context and it is not the default path for ad hoc
plaintext secret entry.

Source order for non-browser operator execution:

1. Explicit command-line config path supplied by the caller, if the future
   helper exposes one.
2. Environment variables already present in the current process.
3. An ignored Launchplane-local config file, such as
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
