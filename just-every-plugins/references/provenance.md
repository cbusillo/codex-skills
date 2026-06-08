# Just Every Plugin Provenance

This skill owns Codex plugin installation guidance and marketplace wiring for
Just Every plugins. It does not vendor plugin implementation source.

## Inspected Sources

Inspected on 2026-06-08:

| Source | Purpose | Inspected SHA | Declared license evidence |
| --- | --- | --- | --- |
| `just-every/plugins` | Marketplace catalog | `1f1cf574323f32a00351ad7223b4ba8f980a15a5` | `package.json` declares `MIT` |
| `just-every/plugin-ultracode` | Ultracode plugin | `5c995c4a51d85901260d4ed2b36d1e820b82c9bb` | `.codex-plugin/plugin.json` declares `MIT`; GitHub repo metadata had no detected root license |
| `just-every/plugin-auto-review` | Auto Code Review plugin | `d2715844e06d84b3180c4d2a361f0477b2ef36d2` | `.codex-plugin/plugin.json` and `package.json` declare `MIT`; GitHub repo metadata had no detected root license |

## Adoption Notes

- `.agents/plugins/marketplace.json` is adapted from `just-every/plugins` and
  points to the upstream plugin repositories rather than copying their source.
- The local marketplace name is `codex-skills-just-every` so it does not collide
  with the upstream marketplace id `just-every`.
- Plugin entries are pinned to the inspected upstream SHAs. Re-check upstream
  before updating those SHAs, broadening this skill into source vendoring, or
  moving to immutable release refs.
