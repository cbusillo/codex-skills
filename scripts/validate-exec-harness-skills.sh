#!/usr/bin/env bash
# Retired Every Code exec-harness wrapper; intentionally performs no execution.
# Historical scenarios and results are described in
# skills/skill-creator/references/exec_harness.md.
set -euo pipefail

cat >&2 <<'MSG'
error: validate-exec-harness-skills.sh is retired with Every Code.
Use scripts/validate-skills.sh and skills/skill-creator/references/validation.md
for current Codex and Claude Code validation. Arguments are not forwarded.
MSG
exit 2
