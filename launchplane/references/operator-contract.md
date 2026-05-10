# Launchplane Operator Contract

This reference defines the safety rules and execution patterns for the
Launchplane Operator.

## Execution Rules

- **Auth**: Source `~/.config/launchplane/local-operator.env` for local
  operator credentials. Expected keys are:
  - `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`
  - `LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT`
  - `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL`
- **Targeting**: Use the deployed Launchplane service API or CLI paths. Do not
  fall back to direct provider mutation.
- **Verification**: Always perform a `dry-run` and inspect redacted evidence
  before applying changes.
- **Reason**: Apply operations require a concrete non-empty reason.

## Request/Response Shapes

See `references/context.available.example.json` for the structure of Launchplane
context payloads.

## Safety & Redaction

- Service paths return metadata only. Launchplane does not expose routine
  plaintext read commands.
- Dry-run and apply both evaluate runtime key-safety policy before returning
  sanitized key/count evidence.
- Runtime key-safety failures are blockers, not warnings.
- After product-config apply, run the sanctioned live-target-runtime sync/deploy
  path when the running target needs Launchplane-owned values applied.
- Report key names, actions, counts, trace IDs, record links, and status only.
