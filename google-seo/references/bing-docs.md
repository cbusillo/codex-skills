# Bing SEO Docs

Use official Microsoft and IndexNow docs for API behavior.

## Bing Webmaster Tools API

- API interface overview: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi?view=bing-webmaster-dotnet
- GetUserSites: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.getusersites?view=bing-webmaster-dotnet
- SubmitFeed: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.submitfeed?view=bing-webmaster-dotnet
- GetUrlInfo: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.geturlinfo?view=bing-webmaster-dotnet
- GetUrlSubmissionQuota: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.geturlsubmissionquota?view=bing-webmaster-dotnet
- SubmitUrl: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.submiturl?view=bing-webmaster-dotnet
- SubmitUrlBatch: https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.submiturlbatch?view=bing-webmaster-dotnet

Microsoft's JSON examples use endpoints under:

```text
https://ssl.bing.com/webmaster/api.svc/json/<Method>?apikey=<key>
```

Observed method shapes from the official examples:

- `GetUserSites`: `GET /json/GetUserSites?apikey=...`
- `SubmitFeed`: `POST /json/SubmitFeed?apikey=...` with JSON
  `siteUrl` and `feedUrl`.
- `GetUrlInfo`: `GET /json/GetUrlInfo?siteUrl=...&url=...&apikey=...`.
- `GetUrlSubmissionQuota`: `GET /json/GetUrlSubmissionQuota?siteUrl=...&apikey=...`.
- `SubmitUrl`: `POST /json/SubmitUrl?apikey=...` with JSON `siteUrl` and
  `url`.
- `SubmitUrlBatch`: `POST /json/SubmitUrlBatch?apikey=...` with JSON
  `siteUrl` and `urlList`. Check quota first for large submissions; the helper
  caps batches at 500 URLs.

Bing Webmaster API keys are generated in Bing Webmaster Tools under Settings ->
API access -> API Key. Store the key as `BING_WEBMASTER_API_KEY` in private
local config.

## IndexNow

- Documentation: https://www.indexnow.org/documentation.html
- FAQ: https://www.indexnow.org/faq

IndexNow is separate from the Bing Webmaster API. It uses a site ownership key,
not the Bing Webmaster API key. Store this value as `BING_INDEXNOW_KEY`.

For a set of URLs, IndexNow accepts a JSON POST containing `host`, `key`, and
`urlList`. If the key file is not hosted at the site root, include
`keyLocation`. All submitted URLs must belong to the declared host or the path
scope allowed by the key file location.

IndexNow response codes mean the notification was received, queued, rejected, or
rate-limited. A successful notification does not guarantee crawl, indexing,
ranking, or immediate search result changes.
