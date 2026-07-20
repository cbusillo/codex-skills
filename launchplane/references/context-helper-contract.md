# Launchplane Context Helper Contract

This contract defines the public-safe JSON shape that skills can consume from
the Launchplane context helper. The helper is optional: skills must behave as
they do today when it is missing, unconfigured, unauthorized, or unable to reach
Launchplane.

The helper is a provider adapter, not a planning backend. GitHub issues, pull
requests, Projects, checks, and comments remain the source of truth for planning
and code workflow. Launchplane may provide compact product/runtime/evidence
context that helps a skill decide what to inspect next.

For Launchplane-managed runtime state, the Launchplane service/operator record
is the authority. Checked-in repo metadata, workflow defaults, examples, and
archived workstation files may point to this helper or explain how to reach the
right service, but they are not authoritative for live product, tenant,
repository, branch, domain, lane, provider-target, runtime-environment, authz,
operator, route, or health-check values.

## Invocation

```sh
launchplane-context --repo OWNER/REPO [--branch BRANCH] [--issue NUMBER] [--pr NUMBER]
```

In this repo, the helper lives at `scripts/launchplane-context.py`. Public
skills should call the shared helper instead of embedding Launchplane API
requests in each `SKILL.md`.

## Configuration

The helper should work without configuration and return `no_context`. Private
configuration may come from environment variables or an ignored local JSON file.
Public examples must use fake hostnames only.

Every configured service URL is parsed as an absolute endpoint before any
request is built. Non-loopback destinations must use HTTPS. Plain HTTP is
allowed only for explicit loopback hosts such as `localhost`, `127.0.0.1`, or
`::1` during local development. The helper rejects missing hosts, userinfo,
unsupported schemes, query strings, fragments, malformed ports, and control
characters. Redirects are followed only when they remain on the same
scheme/host/port origin, so bearer credentials are not replayed across origins.

Default environment variables:

- `LAUNCHPLANE_CONTEXT_URL`: Launchplane service base URL.
- `LAUNCHPLANE_CONTEXT_TOKEN`: read-only Launchplane context token.
- `LAUNCHPLANE_CONTEXT_SUBJECT`: optional terminal-agent subject header value.
- `LAUNCHPLANE_CONTEXT_TOKEN_LABEL`: optional terminal-agent token-label header
  value.

A local config file may use the same shape as
`references/launchplane-context.local.example.json` and should be stored in an
ignored/private location. That file is a helper input config example, not an
example of emitted helper output.

## Exit And Fallback

- `0`: helper completed and emitted a valid JSON payload. This includes
  `no_context`.
- `2`: usage error, such as an invalid command-line option. Skills should treat
  this as helper unavailable and continue without Launchplane context.
- Other non-zero codes: unexpected helper failure. Skills should continue
  without Launchplane context and avoid printing raw helper stderr by default.

Stderr is for generic diagnostics only. It must not include Launchplane
hostnames, internal URLs, tokens, token prefixes, credential paths, private
repositories/products/contexts, copied issue bodies, prompt text, webhook ids,
provider payloads, or raw traces.

Safe diagnostic:

```text
Launchplane context unavailable; continuing without it.
```

## Top-Level JSON

The helper must emit one JSON object with these keys:

```json
{
  "schema_version": "1.0",
  "status": "available",
  "provider": "launchplane",
  "generated_at": "2026-01-02T03:04:05Z",
  "request": {},
  "summary": {},
  "sections": {},
  "links": [],
  "warnings": []
}
```

`status` values:

- `available`: context was read successfully.
- `no_context`: Launchplane is not configured for this machine/session.
- `unavailable`: Launchplane was configured but could not be reached or returned
  a transient failure.
- `unauthorized`: Launchplane rejected the configured credential or policy.
- `invalid`: helper received unusable input or an invalid provider response.

Skills must treat every status except `available` as optional context absence.

Section status values:

- `available`
- `unavailable`
- `unauthorized`
- `unsupported`
- `not_requested`

Unavailable or unauthorized sections should include a short `reason_code`, such
as `missing_config`, `auth_required`, `policy_denied`, `provider_unavailable`,
or `invalid_response`. Do not include raw HTTP responses, internal hostnames, or
provider payloads.

The current service response nests typed sections under
`context.sections.<name>.payload`. The helper also accepts the earlier flattened
section form while projecting both forms into the stable public `sections`
envelope. Only documented repository, state, URL, readiness, and summary fields
are copied; service-model provenance, host labels, internal target details, and
unknown payload fields remain private.

## Redaction Rules

The helper output must not include:

- plaintext secrets, ciphertext, token prefixes, cookies, or credential paths
- provider environment dumps or raw provider API payloads
- private hostnames or internal service URLs
- local filesystem paths, checkout paths, worker hostnames, or terminal session
  names
- issue bodies, prompt text, full PR review text, webhook delivery ids, or raw
  error traces
- product/customer names unless the configured Launchplane response already
  returns them and the helper is running in a private context where those names
  are expected

When in doubt, emit a source URL, record id, trace id, status, and safe reason
code instead of copied detail.

Context output is projected through the documented section schema rather than
generic provider dictionary pass-through. The helper accepts compact
`work_graph`, `repo_product_mapping`, `every_code`, and `preview_readiness`
fields documented by `context.available.example.json`. Unexpected section keys,
secret-looking keys, raw payload containers, unsafe summary text, and unsafe
trace identifiers fail closed as `invalid_response` instead of being copied into
public output.

## Skill Behavior

Skills consuming this contract should:

- call the helper at most once during preflight unless the user asks for a fresh
  read
- continue normally for `no_context`, `unavailable`, `unauthorized`, `invalid`,
  or helper execution failure
- keep GitHub issue/PR state as the canonical durable work state
- avoid writing helper output into public issues, PR bodies, handoffs, or docs
  unless it has been reviewed for public safety
- use Launchplane context as a hint for what to inspect next, not as permission
  to mutate provider state
- treat checked-in config as routing context only; if a needed live runtime value
  is absent from Launchplane context, stop for service/operator input instead of
  inferring it from repo-local files

Write-capable Launchplane workflows belong to explicit operator paths and
Launchplane route-specific authorization, not this read-context helper contract.
