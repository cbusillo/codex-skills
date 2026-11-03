# Standing review decisions

Read before the first review or when a Director changes provider/model decisions.
Keep those decisions in the shared Code home's private
`skill-data/model-review.toml` (`CODE_HOME`, otherwise `~/.code`), outside the
catalog, reviewed repositories and provider account homes. Cite the recorded
decision there; update this one file when it changes, rather than copying a
provider list into briefs or shared skill text. The catalog installer does not
create or modify it. A missing policy under `CODE_HOME` also checks `~/.code`;
provider account homes never supply shared review policy.

For example, after the Director has recorded the corresponding decision:

```toml
version = 1
decision = "https://github.com/OWNER/REPO/issues/123#issuecomment-456"
providers = ["anthropic", "openai"]
supplementary = ["google"]

[provider.google]
timeout = 1800
```

`providers` lists allowed primary providers in preference order. `supplementary`
lists a separate role; capacity cannot promote one into the primary role. A
provider omitted from both lists is disabled. Each optional `provider.NAME`
table accepts `model` and a positive integer `timeout` in seconds. Omit `model`
to keep the provider CLI's configured default as its single source of truth.
Do not encode versions in catalog code. The helper reports the actual model
and effort when the CLI supplies that evidence, otherwise it records them as
requested or unknown. An explicit model must agree when a model setting exists;
otherwise an explicit provider/model pair overrides the CLI default. A configured
timeout is the maximum: callers may use a shorter budget. Without a configured
budget, reviews use 900 seconds. File-read probes default to 300 seconds or the
configured limit if shorter.

The private file is optional. Without it, select an installed provider other
than the author and use its CLI login. When present, it governs provider roles,
models and budgets; malformed or unreadable policy stops before inference. This is decision storage,
not a new request for approval. Never invent a fallback or supplement that the
recorded decision does not permit.

Pass the actual author's provider with `--author-provider`. The default run
uses the first allowed primary provider other than the author that has an
installed CLI and an available account choice:

```sh
uv run scripts/review_with_model.py run --author-provider openai --repo <repo> --prompt-file <file> --out <review.md>
```

Use the shared [account selection](../../supervisor/references/helpers.md#choosing-the-account)
with its optional Context Panel integration. Selection and inference use the same
account. Skipped providers and their reasons appear in the result. If no eligible
primary remains, report that outcome; do not elevate a supplement. Nothing retries
a review failure or refusal on another provider.

`--provider NAME` explicitly selects one permitted provider; it does not choose a
fallback. Pass it when using `--model`, so another provider cannot inherit that
model name. Explicit selection still enforces the standing role, independence
and the configured account choice. A file-read `check` uses the same gates.

Finish the primary review before running a permitted supplement. Run each
supplement once, serially, with its recorded budget:

```sh
uv run scripts/review_with_model.py run --author-provider openai --provider PROVIDER --supplementary --timeout 1800 --repo <repo> --prompt-file <file> --out <supplement.md>
```

The helper runs one review per invocation; serial scheduling across invocations
is the caller's responsibility. Record both results in the PR's review note,
keeping the primary review of record distinct from supplementary evidence.
Weigh findings from both under the shared review reference. No provider's
approval becomes a merge gate.
