# Security Policy

## Supported Versions

Security fixes target the current `main` branch. There are no separate
supported release lines.

## Reporting a Vulnerability

Report suspected vulnerabilities privately through GitHub's
[Report a vulnerability](https://github.com/cbusillo/codex-skills/security/advisories/new)
form. Do not open a public issue for a vulnerability.

Include the commit, the host (Claude Code or Codex), the impact, and the
smallest steps that reproduce it.

Do not send API keys, GitHub tokens or App private keys, session
transcripts, private repository content, or other personal data. Use redacted
or made-up values.

This is a single-maintainer project. Reports are handled on a best-effort
basis, and I aim to reply within seven days.

## Scope

Relevant reports include:

- helpers printing, logging, or storing credentials or tokens where they do
  not belong;
- ways to get past the command-policy hooks or approval rules the installer
  sets up;
- the installer writing outside its configured paths or trusting hooks
  without review; and
- dependency or GitHub Actions supply-chain problems.

Problems in Claude Code or Codex themselves should go to Anthropic or OpenAI.
