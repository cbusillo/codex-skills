# Part-DB Private Context Contract

This reference defines the public-safe boundary between the reusable Part-DB
skill and a private Part-DB deployment. It is not a source of truth for any
real instance, credential, inventory, or local organization scheme.

## Resolution Order

Resolve the first candidate in this order that contains a non-empty
`[docs].local_infra` pointer:

1. `$CODE_HOME/local-context.toml`.
2. `$CODEX_HOME/local-context.toml`.
3. `~/.code/local-context.toml`.

Use the `[docs].local_infra` value as the private operations-repository pointer
when it is configured. Resolve Part-DB-specific docs or helpers from that
private repository; do not print the pointer value into public artifacts.
Missing, unreadable, malformed, invalid-UTF-8, or unconfigured candidates are
skipped without disclosing their paths or contents. The bundled Part-DB helpers
do not expose a local-context path override; they use this discovery order.
User-supplied local-only configuration or evidence may help repair that context,
but it is not an additional automatic file candidate.

If none is available, stop before API access. Do not infer the endpoint from a
repository name, shell history, unrelated environment files, browser state, or
common local-network conventions.

## Required Shape

The private provider invoked through the configured operations-repository
pointer must emit the contract version and environment-variable names rather
than credential values. A representative provider payload is:

```toml
schema_version = "partdb.context.v1"

[api]
base_url_env = "PARTDB_BASE_URL"
read_token_env = "PARTDB_READ_TOKEN"
env_file = ".env"
write_token_env = "PARTDB_WRITE_TOKEN"

[policy]
allow_mutations = false
private_taxonomy_ref = "local-only"
```

Use placeholders such as these only as a contract illustration. Store actual
values in ignored local configuration, a secret manager, or an approved runtime
environment; never in the public skill repository.

## Context Requirements

- Use separate configured read and write credential roles. Never select the
  write credential for read-only work or fall back from a missing read
  credential to a more powerful credential.
- Supply local category, unit, and storage-location conventions privately.
  Categories describe what an item is; locations describe where it is.
- Supply any source-of-truth route for private operating facts, such as service
  health, backups, ingress, migrations, and recovery. The inventory skill does
  not own those concerns.
- Mark the configured mutation posture explicitly. `allow_mutations = true`
  permits only a helper's explicit plan, typed approval, and `--apply` gates;
  a write-capable token alone never authorizes a change.

## Schema And Identity Handshake

Before a future helper queries an instance, it must:

1. Resolve the private context and required environment variables without
   printing their values.
2. Discover the instance's supported API schema through its documented local
   interface.
3. Configure a safe authenticated-identity check when the installed instance
   exposes one. Helpers that use a configured check must fail closed when it
   cannot be verified; the bundled helpers do not infer an identity endpoint.
4. Treat the declared read or write role as private-context policy, not proof
   that the installed API can introspect token scope. Confirm the role during
   private setup before a future helper permits a write.
5. Fail closed when the schema, identity, declared role, or expected instance
   does not match the private context.

The public skill does not prescribe endpoint paths or payload fields. Those are
version-sensitive and must come from the installed instance's schema.

## Output Rules

Public artifacts may contain generic workflow names, boolean readiness status,
and anonymized counts when necessary. They must not contain:

- endpoints, hostnames, IP addresses, tokens, headers, usernames, or paths
- part names, part numbers, serial numbers, descriptions, or attachments
- quantities, costs, suppliers, category trees, or storage-location maps
- inventory exports, screenshots, database files, or raw API responses

Keep task-relevant private results within the private session. Before creating a
public artifact, replace inventory detail with a generic summary or omit it.
