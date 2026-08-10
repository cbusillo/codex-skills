# Private Context Contract Example

This file documents the public-safe shape of the private operations context.
It is not the source of truth for any real host, token, account, route, or
topology.

## Repo Pointer

Use the existing local context pointer for the private operations repo first,
rooted at `$CODE_HOME` when set, then `$CODEX_HOME`, then `~/.code`. Use the
first file in that order with a non-empty `[docs].local_infra` pointer:

- `$CODE_HOME/local-context.toml` `[docs].local_infra`: current pointer for the
  private operations repo checkout.
- `$CODEX_HOME/local-context.toml` `[docs].local_infra`: fallback for runtimes
  with a separate home.
- `~/.code/local-context.toml` `[docs].local_infra`: shared default fallback.

An explicit `--local-context` helper argument selects only that file and does
not continue through the discovery chain.

The pointer value is private. Do not print it into public issues, PRs, logs, or
skill docs unless the user explicitly asks for local-only debugging output.

## Expected Private Repo Shape

The private repo should own environment-specific material such as:

- docs index and service docs
- local helper scripts and dry-run/preflight commands
- ignored env files and credential loading instructions
- rollback, snapshot, and validation playbooks
- operator notes for hosts, networks, ingress, DNS, media, monitoring, and
  managed services

Public skills should reference those categories, not copy their contents.

## Rename Guidance

If the private repo is renamed, keep the old pointer working for a transition
period in private config. Public skills should continue to depend on the
`[docs].local_infra` pointer contract rather than a repository name, alias,
brand, or absolute path.
