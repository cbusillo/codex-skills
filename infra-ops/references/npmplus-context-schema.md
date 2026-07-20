# NPMplus Context Schema

The public `infra-ops` NPMplus engine is generic. It must not contain private
hostnames, domains, route ids, SSH aliases, container ids, env file paths,
remote commands, or rollback instructions.

Private repos provide environment-specific context through the configured
private operations repo pointer:

- `$CODE_HOME/local-context.toml` `[docs].local_infra`, falling back to
  `$CODEX_HOME/local-context.toml` and then `~/.code/local-context.toml`

The default private provider path is `scripts/infra-context.py` inside that
private repo. The provider command shape is:

```bash
python3 scripts/infra-context.py npmplus --profile default --format json
```

## Schema Version

The provider must emit JSON with:

```json
{
  "schema_version": "npmplus.ops.v2"
}
```

Unknown or missing schema versions fail closed.

The engine continues to accept `npmplus.ops.v1` for read-only inventory during
private-provider migration. Lifecycle writes require `npmplus.ops.v2`; a v1
context cannot authorize `--apply` even if it still contains the legacy global
action list.

## Public-Safe Shape

Example with placeholder names only:

```json
{
  "schema_version": "npmplus.ops.v2",
  "api": {
    "env_file": ".code/local.env",
    "base_url_env": "NPMPLUS_BASE_URL",
    "identity_env": "NPMPLUS_AUTOMATION_EMAIL",
    "secret_env": "NPMPLUS_AUTOMATION_PASSWORD",
    "expected_base_url": "https://npmplus.example.invalid",
    "expected_principal": {
      "id": 7,
      "email": "automation@example.invalid"
    }
  },
  "refs": {
    "canary": {
      "kind": "proxy_host",
      "id": 123,
      "identity": {
        "domain_names": [
          "canary.example.invalid"
        ]
      },
      "allowed_apply_actions": [
        "proxy-host-enable",
        "proxy-host-disable"
      ],
      "write_evidence": {
        "snapshot_ready": true,
        "rollback_ready": true,
        "external_validation_ready": true
      }
    }
  },
  "pilot": {
    "default_ref": "canary"
  }
}
```

The `env_file` path must be relative to the private repo and must not contain
`..`. The public engine reads only the named env vars from that file and the
process environment. Secrets must not be passed as command-line arguments.

`refs` map public-safe operator aliases to NPMplus proxy-host ids. Ref names
must match `^[a-z][a-z0-9-]{0,63}$` and must not contain domains, hostnames,
raw ids, site names, or topology. The public engine may use the ids internally,
but public output should name only the ref, never the raw id.

`expected_base_url` is the canonical configured service origin. Any environment
override must resolve to that exact origin or the command stops before network
authentication. `expected_principal` is checked twice: the login identity must
match its email, and the authenticated `/api/users/me` response must match the
configured email plus the optional numeric id. Neither assertion is emitted in
public output.

Each ref owns its lifecycle policy. `allowed_apply_actions` is not inherited
from a global policy. `identity.domain_names` is an exact private fingerprint;
the returned target id and domain-name set must match immediately before the
mutation and again afterward. The CLI exposes no raw-id lifecycle argument, and
the client mutator derives the id only from the validated ref.

`write_evidence` carries public-safe booleans from the private provider. All
three values must be true before `--apply`:

- `snapshot_ready`: any required snapshot or backup gate completed privately.
- `rollback_ready`: a private rollback path and owner are ready.
- `external_validation_ready`: required private preflight checks completed.

The private provider owns the underlying commands, timestamps, host details,
and evidence records. It should compute these booleans immediately before
invoking the public helper. The public engine deliberately consumes only the
typed booleans and never prints private rollback or validation details.

## Output Rules

Public output may include:

- counts
- booleans
- target ref names
- configured/not-configured status
- generic operation names

Public output must not include:

- raw domains
- upstream hosts or IPs
- private route ids
- base URLs
- usernames, passwords, tokens, or cookies
- private command stdout/stderr
- remote hostnames, container names, or topology

## Remote Validation

Remote validation such as nginx syntax checks, SSH probes, snapshots, rollback
checks, Proxmox commands, or container commands stays private. A private wrapper
may run those checks and populate the ref's `write_evidence` booleans, but the
public engine must not construct those commands or ship private defaults.

The lifecycle helper re-reads and verifies the target immediately before its
POST, then re-reads and verifies the same identity and requested enabled state
afterward. NPMplus does not expose a conditional lifecycle mutation in this
contract, so the private operator remains responsible for excluding concurrent
target replacement during the short GET-to-POST interval.

## Public Leak Tests

Public tests should include representative private-looking fixture strings and
assert that summaries, errors, and help output do not contain them. Public code
should also avoid known private literals entirely.
