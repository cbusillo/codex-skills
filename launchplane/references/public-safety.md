# Launchplane Public Safety

Launchplane context can be sensitive even when it contains no plaintext secrets.
Use this checklist before committing examples, helper output, handoff files,
issue text, PR descriptions, screenshots, or logs that mention Launchplane.

## Sensitive Context

Treat these as private unless the user explicitly says they are safe to publish:

- Launchplane service hosts, internal URLs, trace URLs, and route payloads.
- Token names when paired with real values or private paths.
- Product keys, context names, environment names, tenant names, customer names,
  and private repository names.
- Branch names, issue titles, PR titles, work-request ids, preview gate ids,
  and operational status text copied from a private workspace.
- Provider details copied from private runtime evidence, such as target ids,
  application names, health URLs, runtime env dumps, policy digests, and
  deployment evidence payloads.
- Local filesystem paths, checkout paths, terminal session names, worker
  hostnames, and ignored config paths that reveal machine topology.

## Safe Examples

Repo-owned product, app, preview, deploy, and health-check URLs are not
automatically Launchplane context. In private implementation repos, they may be
committed when they are intentionally documented operational metadata, such as
top-level `healthUrls` or readiness endpoints in operations docs. This does not
make Launchplane service/operator/context URLs, trace URLs, provider payloads,
or copied private runtime evidence safe to publish.

Use fake placeholders in committed examples:

- `https://launchplane.example.invalid`
- `example-org/example-app`
- `example-app`
- `example-context`
- `feature/example`
- `LAUNCHPLANE_CONTEXT_TOKEN`

Do not use real private hostnames, product names, org names, repo names, issue
titles, PR titles, or trace ids as examples.

## Helper Output

Before copying helper output anywhere durable:

1. Prefer the helper's `summary`, `links`, and section `status` fields over full
   payloads.
2. Replace private repositories, products, contexts, branches, and titles with
   fake placeholders.
3. Remove raw warnings or provider errors unless they have already been
   sanitized.
4. Keep source URLs only when the destination is allowed to know that URL.
5. Never copy credentials, token prefixes, provider env, issue bodies, prompt
   text, local paths, or worker hostnames.

## Skill And PR Review

For Launchplane context changes in this repo:

- Validate no-Launchplane behavior. Public users should not need Launchplane
  config and should not see alarming errors.
- Use fake-data fixtures for configured behavior when possible.
- Keep Launchplane optional and private-configured.
- Do not add real Launchplane hostnames or credentials to public config schemas.
- Avoid embedding Launchplane API calls in multiple skills; prefer the shared
  helper contract.
- Scan the changed files for common secret and topology markers before pushing.

Useful scan for Launchplane context changes:

```sh
rg -n --hidden --glob '!**/.git/**' \
  '(TOKEN|SECRET|PRIVATE|/Users/|github_pat_|ghp_|sk-[A-Za-z0-9]|launchplane)' \
  launchplane README.md repo-readiness work-closeout
```

Expected false positives should be documented in the PR validation notes.
