# Launchplane Operator Contract

This reference defines the safety rules and execution patterns for the
Launchplane Operator.

## Execution Rules

- **Auth**: Source `~/.config/launchplane/local-operator.env` for the
  `LAUNCHPLANE_LOCAL_OPERATOR_TOKEN`.
- **Targeting**: Use the deployed Launchplane service API or CLI paths. Do not
  fall back to direct provider mutation.
- **Verification**: Always perform a `dry-run` and inspect redacted evidence
  before applying changes.

## Request/Response Shapes

See `references/context.available.example.json` for the structure of Launchplane
context payloads.

## Safety & Redaction

- Service paths return metadata only. Launchplane does not expose routine
  plaintext read commands.
- Dry-run and apply both evaluate runtime key-safety policy before returning
  sanitized key/count evidence.
