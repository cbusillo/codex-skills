# Latest model guide

This file is an offline fallback. Verify current recommendations against the
live OpenAI developer docs before repeating them to a user.

## Current text and reasoning family

| Model ID | Use for |
| --- | --- |
| `gpt-5.6` | Family alias that currently routes to `gpt-5.6-sol`; use only when the integration intentionally prefers aliases |
| `gpt-5.6-sol` | Explicit flagship target for frontier capability, complex reasoning, coding, and tool-heavy work |
| `gpt-5.6-terra` | Strong capability with a lower cost profile than Sol |
| `gpt-5.6-luna` | Efficient, high-volume, classification, extraction, and latency-sensitive work |
| `gpt-5.5` | Existing GPT-5.5 integrations and migration baselines |
| `gpt-5.5-pro` | Existing explicit GPT-5.5 Pro integrations; preserve as a historical target rather than mapping to a `gpt-5.6-pro` slug |
| `gpt-5.4` | Existing GPT-5.4 integrations and migration baselines |
| `gpt-5.4-mini` | Intentionally pinned lower-cost routes that have not yet been mapped to Terra |
| `gpt-5.4-nano` | Intentionally pinned high-throughput routes that have not yet been mapped to Luna |
| `gpt-4.1-mini` | Existing cheaper no-reasoning text routes |
| `gpt-4.1-nano` | Existing fast, low-cost no-reasoning text routes |
| `gpt-5.3-codex` | Existing coding integrations, comparisons, and eval baselines; not the default recommendation for new coding work |
| `gpt-5.1-codex-mini` | Existing lower-cost coding integrations and fixtures |

The current guide names Sol, Terra, and Luna as the GPT-5.6 tiers. Sol is the
flagship tier, Terra is the balanced lower-cost tier, and Luna is the efficient
high-volume tier. Pro is a reasoning mode, not a model slug; do not invent
`gpt-5.6-pro`, `gpt-5.6-mini`, or `gpt-5.6-nano`.

For migrations from GPT-5.5 or GPT-5.4, preserve the current reasoning effort
for the first GPT-5.6 comparison, then test the same setting and one level lower
on representative tasks. Current guidance says omitted GPT-5.6 effort defaults
to `medium`; verify live endpoint guidance before depending on an omitted value.

## Other modalities

These rows are maintained separately from the GPT-5.6 text migration. Reverify
them against current modality-specific docs before changing them.

| Model ID | Use for |
| --- | --- |
| `gpt-image-2` | Best image generation and edit quality |
| `gpt-image-1.5` | Less expensive image generation and edit quality |
| `gpt-image-1-mini` | Cost-optimized image generation |
| `gpt-4o-mini-tts` | Text-to-speech |
| `gpt-4o-mini-transcribe` | Speech-to-text, fast and cost-efficient |
| `gpt-realtime-1.5` | Realtime voice and multimodal sessions |
| `gpt-realtime-mini` | Cheaper realtime sessions |
| `gpt-audio` | Chat Completions audio input and output |
| `gpt-audio-mini` | Cheaper Chat Completions audio workflows |
| `sora-2` | Faster iteration and draft video generation |
| `sora-2-pro` | Higher-quality production video |
| `omni-moderation-latest` | Text and image moderation |
| `text-embedding-3-large` | Higher-quality retrieval embeddings; default in this skill because no best-specific row exists |
| `text-embedding-3-small` | Lower-cost embeddings |

## Maintenance notes

- This file will drift unless it is periodically reverified against current
  OpenAI docs.
- If current OpenAI pages disagree, state the conflict and avoid encoding the
  disputed value until it is resolved.
- If this file conflicts with current docs, the current docs win.
