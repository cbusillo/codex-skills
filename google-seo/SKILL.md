---
name: google-seo
description: Use for Google Search Console, PageSpeed Insights, Lighthouse, Core Web Vitals, sitemap/indexing checks, SEO performance reports, and shared Google OAuth setup for SEO tooling across sites.
metadata:
  short-description: Google SEO, Search Console, and PageSpeed
---

# Google SEO

Use this skill for Google-side SEO evidence: Search Console API/reporting,
PageSpeed Insights, Lighthouse/Core Web Vitals, sitemap/indexing checks, and
repeatable SEO diagnostics across multiple websites.

## Boundaries

- Keep reusable workflow and scripts in this skill.
- Keep OAuth client JSON, refresh tokens, API keys, exports, and customer data in
  private local config, not in the skill repo or product repos.
- Prefer read-only Google scopes first. Escalate to write scopes only when the
  user asks for a mutation such as sitemap submission.
- Do not paste Google secrets, OAuth refresh tokens, or Search Console exports
  into public GitHub issues, PRs, docs, or chat summaries.

## Shared Local State

Default private location:

```text
~/.code/google-search/
```

Expected files:

- `oauth-client.json`: downloaded Google OAuth desktop client JSON.
- `search-console-token.json`: local OAuth token created by the helper.

For PageSpeed API keys, first check the current environment and then
`~/.code/local.env` for `PAGESPEED_INSIGHTS_API_KEY`. Never print the value.

## Search Console OAuth

The OAuth client should be a Google Auth Platform / APIs & Services
**Desktop app** client in the shared tooling project. If the app is in Testing,
the Google account used for consent must be listed as a test user.

Use the bundled helper:

```sh
uv run scripts/google-search-console.py status
uv run scripts/google-search-console.py init ~/Downloads/client_secret.json
uv run scripts/google-search-console.py auth
uv run scripts/google-search-console.py sites
```

`auth` starts a loopback callback on `127.0.0.1` and prints the authorization
URL in case the browser does not open automatically.

The helper uses the read-only scope:

```text
https://www.googleapis.com/auth/webmasters.readonly
```

Use this for Search Analytics, Sites, Sitemaps listing, and URL Inspection reads.

## Common Reports

After auth, collect compact evidence with commands like:

```sh
uv run scripts/google-search-console.py sites
uv run scripts/google-search-console.py sitemaps example.com
uv run scripts/google-search-console.py search-analytics example.com --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dimension query --format csv
uv run scripts/google-search-console.py search-analytics example.com --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dimension page --format csv
uv run scripts/google-search-console.py inspect example.com https://www.example.com/
```

For domain properties, `example.com` is normalized to `sc-domain:example.com`.
For URL-prefix properties, pass the full prefix URL.

## Google Cloud Project Triage

When the user has multiple confusing Google Cloud projects:

1. Prefer `scripts/google-cloud-inventory.sh` when `gcloud` is installed
   and authenticated. Use browser access for UI-only OAuth consent/client setup.
2. Inventory projects, billing state, enabled APIs, OAuth clients, service
   accounts, API keys, and recent activity.
3. Classify projects as active, likely unused, unknown, or candidate shared SEO
   tooling project.
4. Do not delete, disable billing, rotate keys, or change IAM without explicit
   user approval and a clear rollback path.

The inventory script is read-only and intentionally reports API key metadata,
not secret key strings.

## PageSpeed

Use PageSpeed Insights API when a key is configured; otherwise use local
Lighthouse against production. Prioritize mobile, then desktop. Record:

- URL and date.
- Performance, accessibility, best-practices, and SEO scores.
- LCP, CLS, INP/Total Blocking Time, Speed Index.
- Whether field data is available.
- High-impact opportunities, especially LCP image, JavaScript, caching, and
  render-blocking findings.

Treat PageSpeed as evidence, not score chasing. Field data outranks one lab run
once enough traffic exists.

## Durable Work

For GitHub-backed product repos, use `github-plan` for durable issue tracking.
Put recovery-critical findings in the owning issue or PR comment, not only in
local scratch files.

## References

Read `references/google-docs.md` when setting up OAuth, confirming scopes, or
explaining Google API behavior to the user.
