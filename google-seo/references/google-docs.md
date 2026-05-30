# Google SEO Docs

Use official Google docs for setup and API behavior.

## Search Console API

- API overview and reference: https://developers.google.com/webmaster-tools/v1/api_reference_index
- Search Analytics query method: https://developers.google.com/webmaster-tools/v1/searchanalytics/query
- Sites resource: https://developers.google.com/webmaster-tools/v1/sites
- Sitemaps resource: https://developers.google.com/webmaster-tools/v1/sitemaps
- URL Inspection API: https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect

## OAuth

- OAuth 2.0 for installed/native apps: https://developers.google.com/identity/protocols/oauth2/native-app
- OAuth scopes for Search Console: https://developers.google.com/identity/protocols/oauth2/scopes
- Google Cloud OAuth consent setup: https://support.google.com/cloud/answer/10311615

Use `https://www.googleapis.com/auth/webmasters.readonly` for read-only Search
Console reports. Use broader scopes only when a requested workflow truly needs
mutation.

## PageSpeed

- PageSpeed Insights API: https://developers.google.com/speed/docs/insights/v5/get-started
- PageSpeed API reference: https://developers.google.com/speed/docs/insights/rest/v5/pagespeedapi/runpagespeed
- Core Web Vitals: https://web.dev/articles/vitals

PageSpeed lab data and CrUX field data can differ. For SEO prioritization,
field data is stronger once enough real-user traffic exists.
