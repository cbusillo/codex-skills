# Launchplane Context Helper Contract

This contract defines the public-safe JSON shape that future skills can consume
from a Launchplane context helper. The helper is optional: skills must behave as
they do today when the helper is missing, unconfigured, unauthorized, or unable
to reach Launchplane.

The helper is a provider adapter, not a planning backend. GitHub issues, pull
requests, Projects, checks, and comments remain the source of truth for planning
and code workflow. Launchplane may provide compact product/runtime/evidence
context that helps a skill decide what to inspect next.

## Invocation

Future scripts should support this baseline shape:

```sh
launchplane-context --repo OWNER/REPO [--branch BRANCH] [--issue NUMBER] [--pr NUMBER]
```

The exact script path is intentionally undefined in this contract. Public skills
should call a shared helper once it exists instead of embedding Launchplane API
requests in each `SKILL.md`.

## Configuration

The helper should work without configuration and return `no_context`. Private
configuration may come from environment variables or an ignored local JSON file.
Public examples must use fake hostnames only.

Default environment variables:

- `LAUNCHPLANE_CONTEXT_URL`: Launchplane service base URL.
- `LAUNCHPLANE_CONTEXT_TOKEN`: read-only Launchplane context token.
- `LAUNCHPLANE_CONTEXT_SUBJECT`: optional terminal-agent subject header value.
- `LAUNCHPLANE_CONTEXT_TOKEN_LABEL`: optional terminal-agent token-label header
  value.

A local config file may use the same shape as
`launchplane-context/examples/launchplane-context.local.example.json` and should
be stored in an ignored/private location.

## Exit Codes

- `0`: helper completed and emitted a valid JSON payload. This includes the
  `no_context` status.
- `2`: usage error, such as an invalid command-line option. Skills should treat
  this as helper unavailable and continue without Launchplane context.
- Other non-zero codes: unexpected helper failure. Skills should continue
  without Launchplane context and avoid printing raw helper stderr by default.

## Stderr

Stderr is for generic diagnostics only. It must not include:

- Launchplane hostnames or internal URLs
- tokens, token prefixes, cookies, or credential file paths
- private repository, product, context, environment, or branch names
- copied issue bodies, prompt text, webhook delivery ids, or provider payloads

A safe diagnostic is:

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

### `schema_version`

Required string. Start at `"1.0"`. Backward-incompatible changes require a new
major version.

### `status`

Required enum:

- `available`: context was read successfully.
- `no_context`: Launchplane is not configured for this machine/session.
- `unavailable`: Launchplane was configured but could not be reached or returned
  a transient failure.
- `unauthorized`: Launchplane rejected the configured credential or policy.
- `invalid`: helper received unusable input or an invalid provider response.

Skills must treat every status except `available` as optional context absence.

### `provider`

Required string. Use `"launchplane"` for Launchplane-sourced context.

### `generated_at`

Required RFC 3339 UTC timestamp when available. For `no_context`, the helper may
still emit the current timestamp.

### `request`

Required object. Echo only normalized, caller-supplied selectors that are already
safe to print:

```json
{
  "repository": "example-org/example-app",
  "branch": "feature/example",
  "issue_number": 123,
  "pr_number": 456
}
```

Omit unknown keys instead of filling them with `null`.

### `summary`

Required object. Intended for a skill preflight. Keys are optional, but when
present they must be compact strings or booleans:

```json
{
  "recommendation": "Inspect the linked pull request before starting new work.",
  "safe_to_start": false,
  "state": "waiting",
  "blocked_by": "Preview readiness is waiting on required checks.",
  "source_of_truth_url": "https://github.com/example-org/example-app/pull/456"
}
```

`summary` must not contain issue bodies, prompt text, raw provider payloads, or
secret values.

### `sections`

Required object. Each section has a status and optional data:

```json
{
  "work_graph": {
    "status": "available",
    "items": []
  },
  "repo_product_mapping": {
    "status": "available",
    "repositories": []
  },
  "every_code": {
    "status": "available",
    "requests": []
  },
  "preview_readiness": {
    "status": "available",
    "items": []
  }
}
```

Section `status` uses:

- `available`
- `unavailable`
- `unauthorized`
- `unsupported`
- `not_requested`

Unavailable or unauthorized sections should include a short `reason_code`, such
as `missing_config`, `auth_required`, `policy_denied`, `provider_unavailable`,
or `invalid_response`. Do not include raw HTTP responses, internal hostnames, or
provider payloads.

### `links`

Optional array of safe source links:

```json
[
  {
    "label": "Pull request",
    "url": "https://github.com/example-org/example-app/pull/456",
    "kind": "github_pr"
  }
]
```

Only include links that are already appropriate for the current user/session to
see. Prefer source links over copied prose.

### `warnings`

Optional array of compact warning objects:

```json
[
  {
    "code": "partial_context",
    "message": "Work graph context is unavailable; continuing with GitHub state."
  }
]
```

Warnings must be sanitized with the same rules as stderr.

## `no_context` Payload

When Launchplane is not configured, emit a valid payload and exit `0`:

```json
{
  "schema_version": "1.0",
  "status": "no_context",
  "provider": "launchplane",
  "generated_at": "2026-01-02T03:04:05Z",
  "request": {
    "repository": "example-org/example-app"
  },
  "summary": {
    "recommendation": "Continue without Launchplane context."
  },
  "sections": {},
  "links": [],
  "warnings": [
    {
      "code": "missing_config",
      "message": "Launchplane context is not configured."
    }
  ]
}
```

Skills should not treat this as a failure.

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

## Skill Behavior

Skills consuming this contract should:

- call the helper at most once during preflight unless the user asks for a fresh
  read
- continue normally for `no_context`, `unavailable`, `unauthorized`, or helper
  execution failure
- keep GitHub issue/PR state as the canonical durable work state
- avoid writing helper output into public issues, PR bodies, handoffs, or docs
  unless it has been reviewed for public safety
- use Launchplane context as a hint for what to inspect next, not as permission
  to mutate provider state

Write-capable Launchplane workflows belong to explicit operator skills and
Launchplane route-specific authorization, not this read-context helper contract.
