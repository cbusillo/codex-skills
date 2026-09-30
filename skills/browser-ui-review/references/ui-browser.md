# ui-browser Helper

Read only when the installed, working `ui-browser` CLI is the selected
interface. Check its help before relying on these command shapes; do not
install or restore Every Code to obtain a controller. Apply the entry point's
browser-selection, visible-signoff, QA and substitute-evidence rules.

## Session and interaction loop

For multi-step tasks, prefer one Bash block with a named session variable and
pass `--session "$session"` to every command. Do not rely on the shared default
session when other agents or background tasks may use the helper. Start or
reuse a session with `open`, keep its identity stable, and close only the
named session created for this task when complete; preserve user-owned sessions.

```bash
session="browser-$RANDOM"
ui-browser open "https://example.com" --session "$session"
ui-browser wait-for "text=Ready" --session "$session"
ui-browser snapshot --session "$session"
ui-browser click "role=button[name='Continue']" --session "$session"
ui-browser snapshot --session "$session"
```

Wait for app readiness with `wait-for`, then snapshot before the first
interaction. Snapshot again after navigation/routes, opening or closing
modals, menus, popovers, tabs, drawers or accordions, form submission,
filter/sort/search changes, mode/settings toggles, or a stale selector failure.
Recover stale selectors from the fresh visible state instead of forcing them
through `eval`.

## Common commands

- `ui-browser open <url> [wait_ms]`
- `ui-browser click <selector> [after_wait_ms]`
- `ui-browser fill <selector> <text>`
- `ui-browser type <selector> <text>`
- `ui-browser press <key> [after_wait_ms]`
- `ui-browser select <selector> <value>`
- `ui-browser wait <ms>`
- `ui-browser wait-for <selector> [timeout_ms]`
- `ui-browser scroll <dx> <dy>`
- `ui-browser scroll-to <selector>`
- `ui-browser snapshot`
- `ui-browser text <selector>`
- `ui-browser exists <selector>`
- `ui-browser eval <expression>`
- `ui-browser screenshot <output-path>`
- `ui-browser close`

## Selectors and evidence

Prefer stable user-facing Playwright selectors: roles, labels, placeholders or
visible text. Use specific CSS when that target is ambiguous or for layout-only
checks. Use `exists` to confirm conditional UI before branching; inspect a
click's changed result with `snapshot` or `text` before continuing.

Use `eval` only for diagnostics, layout measurements or state not otherwise
visible, not normal user-flow signoff. Capture screenshots when they add
evidence or were requested. `screenshot` validates PNG output and retries once
if capture is blank or background-only. The separate `ui-capture` helper is
only for pure one-shot capture tasks when available.
