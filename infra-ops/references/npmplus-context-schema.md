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
  "schema_version": "npmplus.ops.v1"
}
```

Unknown or missing schema versions fail closed.

## Public-Safe Shape

Example with placeholder names only:

```json
{
  "schema_version": "npmplus.ops.v1",
  "api": {
    "env_file": ".code/local.env",
    "base_url_env": "NPMPLUS_BASE_URL",
    "identity_env": "NPMPLUS_AUTOMATION_EMAIL",
    "secret_env": "NPMPLUS_AUTOMATION_PASSWORD"
  },
  "refs": {
    "canary": {
      "kind": "proxy_host",
      "id": 123
    }
  },
  "pilot": {
    "default_ref": "canary"
  },
  "policy": {
    "allowed_apply_actions": [
      "proxy-host-enable",
      "proxy-host-disable"
    ]
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
may run those checks and return a redacted typed result, but the public engine
must not construct those commands or ship private defaults.

## Public Leak Tests

Public tests should include representative private-looking fixture strings and
assert that summaries, errors, and help output do not contain them. Public code
should also avoid known private literals entirely.
