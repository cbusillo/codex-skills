---
name: google-seo
description: Use for Google Search Console, Bing Webmaster Tools, IndexNow, PageSpeed Insights, Lighthouse, Core Web Vitals, sitemap/indexing checks, SEO performance reports, and shared search tooling setup across sites.
metadata:
  short-description: Search Console, Bing, PageSpeed SEO
resources:
  - path: scripts/google-search-console.py
    kind: script
    description: Manage Search Console OAuth and run read-only Search Console reports.
  - path: scripts/bing-webmaster.py
    kind: script
    description: Run Bing Webmaster Tools API and IndexNow sitemap, URL inspection, and submission workflows.
  - path: scripts/google-cloud-inventory.sh
    kind: script
    description: Inventory Google Cloud projects, APIs, billing, keys, and service accounts with read-only gcloud commands.
  - path: references/google-docs.md
    kind: reference
    description: Google API and OAuth documentation notes for SEO workflows.
  - path: references/bing-docs.md
    kind: reference
    description: Bing Webmaster Tools API and IndexNow documentation notes for SEO workflows.
commands:
  - name: google-search-console-status
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv: ["uv", "run", "scripts/google-search-console.py", "status"]
    purpose: Shows local Search Console helper configuration without secrets.
  - name: google-search-console-auth
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv: ["uv", "run", "scripts/google-search-console.py", "auth"]
    purpose: Runs the loopback OAuth consent flow for Search Console reads.
  - name: google-search-console-auth-write
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv:
      ["uv", "run", "scripts/google-search-console.py", "auth-write"]
    purpose: Runs the explicit write-scope OAuth flow for sitemap submission.
  - name: google-search-console-sites
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv: ["uv", "run", "scripts/google-search-console.py", "sites"]
    purpose: Lists accessible Search Console properties.
  - name: google-search-console-init
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/google-search-console.py",
        "init",
        "~/Downloads/client_secret.json",
      ]
    purpose: Installs a desktop OAuth client JSON into the shared local config.
  - name: google-search-console-sitemaps
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/google-search-console.py",
        "sitemaps",
        "example.com",
      ]
    purpose: Lists submitted sitemaps for a Search Console property.
  - name: google-search-console-submit-sitemap
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/google-search-console.py",
        "submit-sitemap",
        "example.com",
        "https://www.example.com/sitemap.xml",
      ]
    purpose: Submits a sitemap using the separate write token.
  - name: google-search-console-search-analytics
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/google-search-console.py",
        "search-analytics",
        "example.com",
        "--start-date",
        "YYYY-MM-DD",
        "--end-date",
        "YYYY-MM-DD",
        "--dimension",
        "query",
      ]
    purpose: Queries Search Analytics rows for a site property.
  - name: google-search-console-inspect
    source: skill
    resource_path: scripts/google-search-console.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/google-search-console.py",
        "inspect",
        "example.com",
        "https://www.example.com/",
      ]
    purpose: Retrieves URL inspection status for a property URL.
  - name: google-cloud-inventory
    source: skill
    resource_path: scripts/google-cloud-inventory.sh
    example_argv:
      ["scripts/google-cloud-inventory.sh", "--project", "<project-id>"]
    purpose: Collects read-only Google Cloud project inventory for SEO tooling triage.
  - name: bing-webmaster-status
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv: ["uv", "run", "scripts/bing-webmaster.py", "status"]
    purpose: Shows Bing Webmaster and IndexNow credential configuration without secrets.
  - name: bing-webmaster-sites
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv: ["uv", "run", "scripts/bing-webmaster.py", "sites"]
    purpose: Lists Bing Webmaster Tools sites for the configured API key.
  - name: bing-webmaster-submit-feed
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "submit-feed",
        "https://www.example.com/",
        "https://www.example.com/sitemap.xml",
      ]
    purpose: Submits a sitemap or feed through Bing Webmaster Tools.
  - name: bing-webmaster-url-info
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "url-info",
        "https://www.example.com/",
        "https://www.example.com/",
      ]
    purpose: Reads Bing index details for one URL.
  - name: bing-webmaster-submit-url
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "submit-url",
        "https://www.example.com/",
        "https://www.example.com/",
      ]
    purpose: Submits one URL directly to Bing Webmaster Tools.
  - name: bing-webmaster-url-quota
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "url-quota",
        "https://www.example.com/",
      ]
    purpose: Checks Bing URL submission quota for a verified site.
  - name: bing-webmaster-submit-url-batch
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "submit-url-batch",
        "https://www.example.com/",
        "--url-file",
        "urls.txt",
      ]
    purpose: Submits up to 500 URLs directly to Bing Webmaster Tools.
  - name: bing-indexnow-verify
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "indexnow-verify",
        "https://www.example.com/",
      ]
    purpose: Shows IndexNow key hosting instructions, redacting the key unless explicitly revealed.
  - name: bing-indexnow-submit
    source: skill
    resource_path: scripts/bing-webmaster.py
    example_argv:
      [
        "uv",
        "run",
        "scripts/bing-webmaster.py",
        "indexnow-submit",
        "--url",
        "https://www.example.com/",
      ]
    purpose: Submits URL changes through IndexNow using the configured IndexNow key.
---

# Search SEO

Use this skill for search-side SEO evidence: Google Search Console
API/reporting, Bing Webmaster Tools and IndexNow workflows, PageSpeed Insights,
Lighthouse/Core Web Vitals, sitemap/indexing checks, and repeatable SEO
diagnostics across multiple websites.

## Boundaries

- Keep reusable workflow and scripts in this skill.
- Keep OAuth client JSON, refresh tokens, API keys, IndexNow keys, exports, and
  customer data in private local config, not in the skill repo or product repos.
- Prefer read-only Google scopes first. Escalate to write scopes only when the
  user asks for a mutation such as sitemap submission.
- Do not paste Google secrets, OAuth refresh tokens, or Search Console exports
  into public GitHub issues, PRs, docs, or chat summaries.

## Shared Local State

Default private location, rooted at `$CODE_HOME` when set, then `$CODEX_HOME`,
then `~/.code`:

```text
$CODE_HOME/google-search/
```

Expected files:

- `oauth-client.json`: downloaded Google OAuth desktop client JSON.
- `search-console-token.json`: local OAuth token created by the helper.
- `search-console-write-token.json`: local OAuth token created only by the
  explicit write-scope helper.

For PageSpeed API keys, first check the current environment and then
`$CODE_HOME/local.env`, falling back to `$CODEX_HOME/local.env` and
`~/.code/local.env`, for `PAGESPEED_INSIGHTS_API_KEY`. Never print the value.

For Bing Webmaster Tools, first check the current environment and then the same
`local.env` fallback chain for `BING_WEBMASTER_API_KEY`. For IndexNow, use
`BING_INDEXNOW_KEY`. Never print either value except when the user explicitly
runs `indexnow-verify --reveal-key` to copy the key into the required
verification file. Bing Webmaster API keys are user-scoped in Bing Webmaster
Tools, so treat them like account credentials.

## Search Console OAuth

Search Console commands use OAuth client JSON and local OAuth tokens; they do
not use `PAGESPEED_INSIGHTS_API_KEY`, `BING_WEBMASTER_API_KEY`, or
`BING_INDEXNOW_KEY`. Those API keys belong to PageSpeed, Bing Webmaster Tools,
and IndexNow workflows only.

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

For sitemap submission, use the explicit write-scope flow. This stores a
separate token and leaves the read-only reporting token untouched:

```sh
uv run scripts/google-search-console.py auth-write
uv run scripts/google-search-console.py submit-sitemap example.com https://www.example.com/sitemap.xml
```

The write helper uses:

```text
https://www.googleapis.com/auth/webmasters
```

Sitemap submission can encourage recrawl but does not guarantee indexing,
ranking, or immediate URL Inspection changes.

## Common Reports

After auth, collect compact evidence with commands like:

```sh
uv run scripts/google-search-console.py sites
uv run scripts/google-search-console.py sitemaps example.com
uv run scripts/google-search-console.py search-analytics example.com --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dimension query --format csv
uv run scripts/google-search-console.py search-analytics example.com --start-date YYYY-MM-DD --end-date YYYY-MM-DD --dimension page --format csv
uv run scripts/google-search-console.py inspect example.com https://www.example.com/
uv run scripts/google-search-console.py submit-sitemap example.com https://www.example.com/sitemap.xml
```

For domain properties, `example.com` is normalized to `sc-domain:example.com`.
For URL-prefix properties, pass the full prefix URL.

## Bing Webmaster Tools

Use Bing Webmaster Tools when the user asks for Bing verification, Bing sitemap
submission, Bing index status, direct Bing URL submission, or Bing-specific
debugging.

API key setup path in Bing Webmaster Tools: Settings gear -> API access -> API
Key -> Generate API Key. Store the key as `BING_WEBMASTER_API_KEY` in the
private `local.env` file with user-only permissions.

Common commands:

```sh
uv run scripts/bing-webmaster.py status
uv run scripts/bing-webmaster.py sites
uv run scripts/bing-webmaster.py submit-feed https://www.example.com/ https://www.example.com/sitemap.xml
uv run scripts/bing-webmaster.py url-info https://www.example.com/ https://www.example.com/
uv run scripts/bing-webmaster.py url-quota https://www.example.com/
uv run scripts/bing-webmaster.py submit-url https://www.example.com/ https://www.example.com/
uv run scripts/bing-webmaster.py submit-url-batch https://www.example.com/ --url https://www.example.com/page-a --url https://www.example.com/page-b
```

Prefer `submit-feed` for sitemap follow-through after verification. Use
`url-info` for inspection evidence before deciding whether direct URL
submission is useful. Use direct URL submission sparingly and check quota for
batch work.

The API helper uses Bing's JSON endpoints under
`https://ssl.bing.com/webmaster/api.svc/json/...` with the API key in the query
string. It never prints the configured key.

## IndexNow

Use IndexNow when the site can host an IndexNow key file and the task is to
notify participating search engines about added, updated, or deleted URLs. This
is distinct from the Bing Webmaster API key.

Store the IndexNow key as `BING_INDEXNOW_KEY` in private local config. To see
placeholder key-file instructions without printing the key:

```sh
uv run scripts/bing-webmaster.py indexnow-verify https://www.example.com/
```

Use `--reveal-key` only when terminal output is an acceptable place to display
the key so it can be copied into the verification file.

After the key file is hosted, submit URL changes:

```sh
uv run scripts/bing-webmaster.py indexnow-submit --url https://www.example.com/page-a --url https://www.example.com/page-b
```

Use `--key-location` when the key file is not at the site root. IndexNow
acceptance means the notification was received or queued for key validation; it
does not guarantee crawl, indexing, ranking, or immediate search result changes.

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
explaining Google API behavior to the user. Read `references/bing-docs.md` when
setting up Bing API keys, confirming Bing endpoint shapes, or deciding whether
to use Bing Webmaster direct submission versus IndexNow.
