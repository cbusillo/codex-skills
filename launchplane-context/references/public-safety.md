# Launchplane Context Public Safety

Launchplane context can be sensitive even when it contains no plaintext secrets.
Use this checklist before committing examples, helper output, handoff files,
issue text, PR descriptions, screenshots, or logs that mention Launchplane.

## Sensitive Context

Treat these as private unless the user explicitly says they are safe to publish:

- Launchplane service hosts, internal URLs, trace URLs, and route payloads
- terminal-agent, local-operator, GitHub, or worker token names when paired with
  real values or paths
- product keys, context names, environment names, tenant names, customer names,
  and private repository names
- branch names, issue titles, PR titles, work-request ids, preview gate ids, and
  operational status text copied from a private workspace
- provider details such as Dokploy target ids, application names, health URLs,
  runtime env dumps, policy digests, and deployment evidence payloads
- local filesystem paths, checkout paths, terminal session names, worker
  hostnames, and ignored config paths that reveal machine topology

## Safe Examples

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
  '(TOKEN|SECRET|PRIVATE|/Users/|github_pat_|ghp_|sk-[A-Za-z0-9]|launchplane\\.shinycomputers|shinycomputers|cbusillo)' \
  launchplane-context README.md github-plan repo-readiness github-repo-workflow work-closeout
```

Expected false positives should be documented in the PR validation notes.
