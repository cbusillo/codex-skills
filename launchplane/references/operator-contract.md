# Launchplane Operator Contract

This reference defines the safety rules and execution patterns for the
Launchplane Operator.

## Execution Rules

- **Auth**: Prefer signed-in, scoped operator sessions for human-approved
  product-config dry-run/apply and other UI-backed operator mutations. Source
  terminal/local operator credentials only from explicit private config when the
  workflow requires non-browser execution. A common private config shape is
  `~/.config/launchplane/local-operator.env` with:
  - `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`
  - `LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT`
  - `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`
- **Targeting**: Use the deployed Launchplane service API or CLI paths. Do not
  fall back to direct provider mutation.
- **Verification**: Always perform a `dry-run` and inspect redacted evidence
  before applying changes.
- **Reason**: Apply operations require a concrete non-empty reason.
- **Merge Train**: Use
  `POST /v1/work-graph/merge-train/controller/run-once` as the default merge
  train operation. Phase-specific merge-train endpoints are troubleshooting and
  recovery surfaces, not the normal skill path.

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
