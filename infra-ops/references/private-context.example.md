# Private Context Contract Example

This file documents the public-safe shape of the private operations context.
It is not the source of truth for any real host, token, account, route, or
topology.

## Repo Pointer

Use the existing local context pointer for the private operations repo first:

- `~/.code/local-context.toml` `[docs].local_infra`: current pointer for the
  private operations repo checkout.

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
