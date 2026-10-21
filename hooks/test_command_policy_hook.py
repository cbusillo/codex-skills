#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run the Claude Code command-policy hook against the catalog's real policies."""

from __future__ import annotations

import contextlib
import functools
import io
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "command_policy_hook.py"
sys.path.insert(0, str(HOOK.parent))

import command_policy_hook  # noqa: E402

sys.path.insert(0, str(HOOK.parents[1] / "skills/github/scripts"))
import github_plan_lease  # noqa: E402

VALIDATOR_PATH = HOOK.parents[1] / "skills/skill-creator/scripts/quick_validate.py"
validator_spec = importlib.util.spec_from_file_location("quick_validate", VALIDATOR_PATH)
assert validator_spec is not None and validator_spec.loader is not None
quick_validate = importlib.util.module_from_spec(validator_spec)
validator_spec.loader.exec_module(quick_validate)

SIMULATOR = command_policy_hook.load_simulator()
# The catalog is parsed from every SKILL.md; parse it once for the in-process cases.
SIMULATOR.iter_policies = functools.cache(SIMULATOR.iter_policies)
command_policy_hook.load_simulator = lambda: SIMULATOR


def run_hook(payload: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), *arguments], input=payload, capture_output=True, text=True
    )


def bash(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the hook's entry point in-process; run_hook covers the real process."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or Path.cwd())})
    stderr = io.StringIO()
    with (
        mock.patch.object(sys, "stdin", io.StringIO(payload)),
        mock.patch.object(sys, "argv", [str(HOOK)]),
        contextlib.redirect_stderr(stderr),
    ):
        returncode = command_policy_hook.main()
    return subprocess.CompletedProcess([str(HOOK)], returncode, "", stderr.getvalue())


def linked_worktree(checkout: Path, linked: Path) -> Path:
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(checkout), "worktree", "add", "--detach", str(linked)], check=True, capture_output=True)
    return linked


class CommandPolicyHookTests(unittest.TestCase):
    def test_each_harness_tool_call_renews_its_claim_lease_without_skipping_policy(self) -> None:
        payloads = {
            "codex": {"hook_event_name": "PreToolUse", "session_id": "codex-session", "turn_id": "turn", "model": "gpt",
                      "tool_name": "Bash", "tool_input": {"command": "kill -9 $(pgrep -f codex)"}},
            "claude": {"hook_event_name": "PreToolUse", "session_id": "claude-session", "transcript_path": "/t.jsonl",
                       "permission_mode": "default", "tool_name": "Bash", "tool_input": {"command": "kill -9 $(pgrep -f codex)"}},
        }
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            for harness, payload in payloads.items():
                with self.subTest(harness=harness):
                    result = subprocess.run([sys.executable, str(HOOK), "--json"], input=json.dumps(payload),
                                            capture_output=True, text=True, env={**os.environ, "HOME": home})
                    self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
                    self.assertEqual(github_plan_lease.state(payload["session_id"]), github_plan_lease.LIVE)

    def test_unwritable_lease_store_still_applies_policy(self) -> None:
        payload = {"session_id": "codex-session", "tool_name": "Bash", "tool_input": {"command": "kill -9 $(pgrep -f codex)"}}
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".code").write_text("not a directory")
            result = subprocess.run([sys.executable, str(HOOK), "--json"], input=json.dumps(payload),
                                    capture_output=True, text=True, env={**os.environ, "HOME": home})
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pattern_process_kills_are_refused_without_running_them(self) -> None:
        for line in (
            "pkill sleep", "pkill -f 'sleep 10|sleep 15'", "killall sleep",
            "command /usr/bin/pkill -f watcher", "env MODE=test killall sleep",
            "bash -lc 'pkill -f watcher'", "sudo pkill -f watcher",
            "timeout 5 killall sleep", "pgrep sleep | xargs /bin/kill",
            "ps -axo pid,command | awk '/sleep/ {print $1}' | xargs -r kill -9",
            "kill $(pgrep -f 'sleep 10')", 'kill "$(pgrep sleep)"',
            "kill -TERM `pgrep sleep`", "builtin kill $(pgrep sleep)",
            "kill $(ps -axo pid,command | awk '/sleep/ {print $1}')",
            "bash <<'EOF'\nkill $(pgrep sleep)\nEOF",
            'pids=$(pgrep -f vite); kill $pids',
            'for p in $(pgrep -f watcher); do kill "$p"; done',
            'pgrep sleep | while read p; do kill "$p"; done',
            'kill "$task_pid"', 'kill "$(cat server.pid)"',
            "kill 0", "kill -9 -1", "nice -n 5 pkill watcher",
            "caffeinate -i killall watcher", "doas pkill watcher",
            "stdbuf -o0 pkill watcher", "sudo -u worker pkill watcher",
            "pgrep sleep | xargs -I{} kill {}",
            "kill 12345 0 > log", "kill 12345 -1 > log",
            "sudo timeout 5 pkill watcher", "timeout 5 sudo pkill watcher",
            "sudo env MODE=test pkill watcher", "nice -n5 pkill watcher",
            "pgrep sleep | xargs -d '\\n' kill", "xargs -a pids kill",
            "pgrep sleep | xargs --max-args 1 kill",
            "xargs -i kill {}", "xargs -i{} kill {}", "xargs -J % kill %",
            "stdbuf -o L pkill watcher", "sudo -D /tmp pkill watcher",
            "sudo -T 30 pkill watcher", "nice --adjustment 5 pkill watcher",
            "sudo nohup pkill watcher", "timeout 5 command pkill watcher",
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2)
                blocked = command_policy_hook.blocking_policy(line)
                self.assertIsNotNone(blocked)
                if blocked is not None:
                    self.assertIn(blocked[0]["message"], result.stderr)

    def test_recorded_pid_kills_and_process_reads_remain_allowed(self) -> None:
        for line in (
            "kill 12345", "kill -TERM 12345", "/bin/kill -s TERM 12345",
            "builtin kill 12345", "kill -- 12345", "kill -0 12345",
            "kill --signal=TERM 12345", "kill -TERM 12345 12346",
            "kill 12345 2>/dev/null || true",
            "kill -0 12345 2>/dev/null && echo alive",
            "kill -TERM 12345 >/dev/null 2>&1",
            'kill 12345 > "task log.txt"',
            "kill 12345 >| log", "kill $!", 'kill "$!"',
            'uv run server.py & kill "$!"',
            "pgrep -f 'sleep 10'", "ps -axo pid,command", "kill -l",
            "git status", "rg 'pkill|killall' skills/",
            "printf '%s' 'kill $(pgrep sleep)'",
            "cat <<'EOF'\npkill sleep\nkill $(pgrep sleep)\nEOF",
            "git ls-files | xargs grep -n kill", "xargs rg -w pkill",
            "timeout 30 rg -n killall skills/", "sudo grep -r pkill /var/log",
            "grep -n kill $(git ls-files)",
            'git log -S kill -- "$(git rev-parse --show-toplevel)/hooks"',
            "stdbuf -o L rg pkill", "sudo -D /tmp rg kill",
            "sudo nohup rg killall", "xargs -i grep -n1 kill",
            "sudo -D pkill rg killall", "sudo -u pkill grep killall",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 0)

    def test_legacy_command_policies_still_find_wrapped_commands(self) -> None:
        for line in (
            "timeout 60 git pull", "sudo git merge origin/main",
            "xargs -n1 git rebase", "timeout 120 git -C /repo push origin br",
            "nice git -c user.name=Fixture commit -m demo",
            "caffeinate -i codex resume example",
            "timeout 600 codex exec resume example",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_global_keyboard_synthesis_is_refused_without_sending_keys(self) -> None:
        apple = 'tell application "System Events" to tell process "Google Chrome"\nkeystroke "/path/proposal.json"\nkey code 36'
        commands = (
            "osascript -e " + shlex.quote(apple),
            "osascript -e 'tell application \"System Events\" to key code 36'",
            "osascript -l JavaScript -e 'Application(\"System Events\").keyCode(36)'",
            "osascript -e 'tell application \"System Events\" to key down command'",
            "cliclick 't:/path/proposal.json' kp:return",
            "cliclick kd:cmd ku:cmd",
            "xdotool search --name Chrome windowactivate --sync type /path/proposal.json",
            "xdotool key --window 123 Return",
            "ydotool type /path/proposal.json",
            "ydotool key 28:1 28:0",
            "wtype /path/proposal.json",
            "printf 'type path' | dotool",
            "xte 'str /path/proposal.json' 'key Return'",
            "uv run python -c 'import pyautogui; pyautogui.write(\"path\")'",
            "python3 -c 'from pynput.keyboard import Controller; Controller().type(\"path\")'",
            "python3 -c 'from pynput import keyboard; controller = keyboard.Controller(); controller.type(\"path\"); controller.press(keyboard.Key.enter)'",
            "python3 -c 'import keyboard; keyboard.send(\"enter\")'",
            "python3 -c 'from pyautogui import write, press; write(\"path\"); press(\"enter\")'",
            "python3 -c 'from pyautogui import write as type_text; type_text(\"path\")'",
            "python3 -c 'from keyboard import send; send(\"enter\")'",
            "python3 -c 'import pyautogui as pg; pg.write(\"path\"); pg.press(\"enter\")'",
            "python3 -c 'import keyboard as kb; kb.send(\"enter\")'",
            "python3 -c 'import keyboard; keyboard.press_and_release(\"enter\")'",
            "python3 -c 'import keyboard as kb; kb.press_and_release(\"enter\")'",
            "python3 -c 'from keyboard import press_and_release; press_and_release(\"enter\")'",
            "python3 -c 'import keyboard; keyboard.play(events)'",
            "python3 -c 'import keyboard as kb; kb.restore_modifiers([42])'",
            "python3 -m keyboard < events.jsonl",
            "python3 -c 'import pyautogui; context = pyautogui.hold(\"shift\"); context.__enter__()'",
            "python3 -c 'from pyautogui import hold; hold(\"shift\").__enter__()'",
            "python3 -c 'from pynput.keyboard import Controller; Controller().tap(\"a\")'",
            "python3 -c 'from pynput.keyboard import Controller; Controller().pressed(\"a\").__enter__()'",
            "node -e 'const robot = require(\"robotjs\"); robot.keyTap(\"enter\")'",
            "node -e 'const robotjs = require(\"robotjs\"); robotjs.typeString(\"path\"); robotjs.keyTap(\"enter\")'",
            "node --input-type=module -e 'import robotjs from \"robotjs\"; robotjs.keyTap(\"enter\")'",
            "node -e 'const robotjs = require(\"robotjs\"); robotjs.typeStringDelayed(\"path\", 100)'",
            "swift -e 'CGEventCreateKeyboardEvent(nil, 36, true)'",
            "swift -e 'import CoreGraphics; let event = CGEvent(keyboardEventSource: nil, virtualKey: 36, keyDown: true)!; event.post(tap: .cghidEventTap)'",
            "osascript <<'APPLESCRIPT'\n" + apple + "\nAPPLESCRIPT",
            "cat <<'APPLESCRIPT' | osascript\n" + apple + "\nAPPLESCRIPT",
            "sudo /usr/bin/osascript <<'APPLESCRIPT'\n" + apple + "\nAPPLESCRIPT",
            "timeout 10 python3 - <<'PY'\nimport pyautogui\npyautogui.press('enter')\nPY",
            "python3 - <<'PY'\nimport pyautogui\npyautogui.press('enter')\nPY",
            "node <<'JS'\nconst robot = require('robotjs'); robot.typeString('path');\nJS",
            "xdotool - <<'INPUT'\ntype path\nkey Return\nINPUT",
            "xte <<'INPUT'\nstr path\nkey Return\nINPUT",
            "cliclick -f - <<'INPUT'\nt:path\nkp:return\nINPUT",
            "python3 -I -W ignore - <<'PY' 2>/dev/null\nimport pyautogui\npyautogui.press('enter')\nPY",
            "osascript -l AppleScript <<-'APPLESCRIPT'\n\t" + apple.replace("\n", "\n\t") + "\n\tAPPLESCRIPT",
            "osascript <<EOF; cat <<EOF\ntell application \"System Events\" to key code 36\nEOF\nget 42\nEOF",
            "cat <<EOF; osascript <<EOF\nget 42\nEOF\ntell application \"System Events\" to key code 36\nEOF",
            "printf '%s\\n' 'tell application \"System Events\" to keystroke \"path\"' 'tell application \"System Events\" to key code 36' | osascript",
            "echo 'tell application \"System Events\" to key code 36' | sudo /usr/bin/osascript",
            "printf 'import keyboard\\nkeyboard.press_and_release(\"enter\")\\n' | python3 -",
            "printf '%s\\n' 'tell application \"System Events\" to key code 36' | tee picker.applescript | osascript",
            "echo 'tell application \"System Events\" to key code 36' | cat | osascript",
            "python3 /dev/stdin <<'PY'\nimport pyautogui\npyautogui.write('path')\npyautogui.press('enter')\nPY",
            "python3 /dev/fd/0 <<'PY'\nimport pyautogui\npyautogui.press('enter')\nPY",
            "python3 /proc/self/fd/0 <<'PY'\nimport pyautogui\npyautogui.press('enter')\nPY",
        )
        for command in commands:
            with self.subTest(command=command):
                blocked = command_policy_hook.blocking_policy(command)
                self.assertIsNotNone(blocked)
                policy, skill, argv = blocked
                result = bash(command)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.rstrip(), command_policy_hook.describe(policy, skill, argv))

        for prefix in ("/usr/bin/", "env FOO=1 ", "sudo ", "timeout 5 "):
            self.assertEqual(bash(prefix + commands[0]).returncode, 2)
        for command in (
            "bash -lc " + shlex.quote(commands[0]),
            "true; " + commands[0],
            "printf '%s' \"$(xdotool key Return)\"",
            "osascript <<< " + shlex.quote(apple),
        ):
            self.assertEqual(bash(command).returncode, 2)

        # Both entry-point formats deny before a host could execute the command.
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": commands[0]}})
        self.assertEqual(run_hook(payload).returncode, 2)
        decision = run_hook(payload, "--json")
        self.assertEqual(decision.returncode, 0)
        self.assertEqual(json.loads(decision.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_scoped_input_and_non_keyboard_commands_remain_allowed(self) -> None:
        for command in (
            "uv run skills/supervisor/scripts/iterm_tab.py send --session-id exact --text path --verified-target",
            "node -e 'await page.locator(\"input[type=file]\").setInputFiles(\"/path/proposal.json\")'",
            "node -e 'await tab.send(\"DOM.setFileInputFiles\", {files: [\"/path/proposal.json\"], nodeId: 42})'",
            "node -e 'await page.keyboard.press(\"Enter\")'",
            "node -e 'const keyboard = page.keyboard; await keyboard.press(\"Enter\")'",
            "python3 -c 'page.keyboard.press(\"Enter\")'",
            "python3 -c 'keyboard = page.keyboard; keyboard.press(\"Enter\")'",
            "swift -e 'let event = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: .zero, mouseButton: .left)'",
            "osascript -e 'tell application \"Google Chrome\" to get URL of active tab of front window'",
            "osascript -e 'tell application \"Google Chrome\" to execute active tab of front window javascript \"document.title\"'",
            "osascript <<'APPLESCRIPT'\ntell application \"Finder\" to get name of startup disk\nAPPLESCRIPT",
            "cliclick p:.",
            "cliclick m:100,100",
            "xdotool getactivewindow getwindowname",
            "xdotool mousemove 100 100",
            "ydotool mousemove 100 100",
            "python3 -c 'import pyautogui; print(pyautogui.size())'",
            "rg 'osascript keystroke|xdotool key|pyautogui.write' docs",
            "printf '%s' 'osascript keystroke path'",
            "cat <<'DOC'\nosascript -e 'keystroke path'\nxdotool key Return\nDOC",
            "cat <<'DOC'\npyautogui.press('enter')\nDOC\npython3 -c 'print(42)'",
            "cat <<'DOC'; python3 -c 'print(42)'\npyautogui.press('enter')\nDOC",
            "cat <<'DOC' | python3 -c 'print(42)'\npyautogui.press('enter')\nDOC",
            "osascript -e 'get 42' <<'DOC'\nkeystroke path\nDOC",
            "python3 format_notes.py <<'DOC'\npyautogui.press('enter')\nDOC",
            "osascript query.applescript <<'DOC'\nkeystroke path\nDOC",
            "node format_notes.js <<'DOC'\nrobot.keyTap('enter')\nDOC",
            "python3 -c 'from pyautogui import size; print(size())'",
            "python3 -c 'import pyautogui as pg; print(pg.size())'",
            "python3 -c 'from pynput.keyboard import Listener; print(Listener)'",
            "python3 -c 'from pynput.keyboard import Controller; print(Controller)'",
            "python3 -c 'import keyboard; print(keyboard.is_pressed(\"enter\"))'",
            "node -e 'const robotjs = require(\"robotjs\"); console.log(robotjs.getScreenSize())'",
            "python3 -Ic 'print(42)' <<'DOC'\npyautogui.press('enter')\nDOC",
            "python3 -I -X dev format_notes.py <<'DOC'\npyautogui.press('enter')\nDOC",
            "cat <<'DOC'; python3 - <<'PY'\npyautogui.press('enter')\nDOC\nprint(42)\nPY",
            "cat <<EOF; python3 - <<EOF\npyautogui.press('enter')\nEOF\nprint(42)\nEOF",
            "printf '%s\\n' 'pyautogui.press(\"enter\")' | cat",
            "printf '%s\\n' 'pyautogui.press(\"enter\")' | python3 format_notes.py",
            "echo 'keystroke path' | osascript -e 'get 42'",
            "printf -v script '%s' 'keystroke path' | osascript",
            "printf '%s\\n' 'pyautogui.press(\"enter\")' | tee notes.txt | python3 format_notes.py",
            "osascript -e 'tell application \"Google Chrome\" to execute tab id 42 of window id 24 javascript \"document.body.dispatchEvent(new KeyboardEvent(\\\"keydown\\\", {key: \\\"Enter\\\"}))\"'",
            "osascript -e 'tell application \"System Events\" to get name of every process'",
            "python3 - <<'PY'\nprint('osascript keystroke path')\nPY",
        ):
            with self.subTest(command=command):
                result = bash(command)
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_launchplane_data_is_allowed_but_invocations_are_blocked(self) -> None:
        for command in (
            "uv run python -c 'import json; print({\"repository\": \"launchplane\"})'",
            "printf '%s' launchplane",
            "rg launchplane settings.json",
        ):
            with self.subTest(command=command):
                result = bash(command)
                self.assertEqual(result.returncode, 0)
                self.assertFalse(result.stderr, "allowance must come from successful policy evaluation")
        for command in ("launchplane status", "uv run launchplane status",
                        "printf '%s' launchplane && launchplane status"):
            with self.subTest(command=command):
                self.assertEqual(bash(command).returncode, 2)

    def test_repository_exports_are_scoped_to_real_checkouts_and_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            frontend = checkout / "frontend"
            frontend.mkdir()
            commands = (
                "launchplane service export-openapi --output generated/openapi.json",
                "launchplane service export-agent-contract --output contracts/agent.json",
                "launchplane service export-owner-control-contract --output contracts/owner.json",
                "launchplane ci unittest-shard local",
                "launchplane ci unittest-shard plan --shard-count 2 --timings-file timings.json",
                "launchplane ci unittest-shard run --shard-count 2 --shard-index 0",
                "launchplane service audit-config-authority --control-plane-root .",
                "launchplane odoo-ownership check --workspace-root ..",
                "launchplane action-pins check --repo-root . --revision HEAD --repository cbusillo/launchplane",
                "launchplane action-pins update --release-sha 0123abc",
                "launchplane action-pins check --repo-root . --revision HEAD --repository=cbusillo/launchplane",
            )
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(bash("uv run " + command, checkout).returncode, 0)
                    self.assertEqual(bash("uv run " + command, frontend).returncode, 0)
                    self.assertEqual(bash("uv run --extra dev " + command, checkout).returncode, 0)
                    self.assertEqual(bash("uv run " + command, root).returncode, 2)
                    self.assertEqual(SIMULATOR.simulate(shlex.split(command), cwd=checkout), [])
                    self.assertTrue(SIMULATOR.simulate(shlex.split(command)))
            self.assertEqual(bash("pnpm --dir frontend generate:openapi", checkout).returncode, 0)
            self.assertEqual(bash("launchplane merge-train run-once", checkout).returncode, 2)
            self.assertEqual(bash("launchplane service start", checkout).returncode, 2)
            self.assertEqual(bash("uv run " + commands[0] + " && launchplane service start", checkout).returncode, 2)
            # A real linked worktree, not a mocked directory name or host runtime.
            linked = linked_worktree(checkout, root / "linked")
            self.assertEqual(bash("uv run " + commands[0], linked).returncode, 0)
            for origin in (
                "https://github.com/cbusillo/launchplane.git",
                "ssh://git@github.com/cbusillo/launchplane.git",
                "https://github.com/cbusillo/launchplane",
            ):
                subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", origin], check=True)
                self.assertEqual(bash("uv run " + commands[0], checkout).returncode, 0)
            for origin in (
                "https://github.com/other/launchplane.git",
                "https://github.com/cbusillo/launchplane-other.git",
                "https://github.com.evil.invalid/cbusillo/launchplane.git",
                "https://evil.invalid/cbusillo/launchplane.git",
            ):
                subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", origin], check=True)
                self.assertEqual(bash("uv run " + commands[0], checkout).returncode, 2)
            subprocess.run(["git", "-C", str(checkout), "config", "--unset", "remote.origin.url"], check=True)
            self.assertEqual(bash("uv run " + commands[0], checkout).returncode, 2)

    def test_repository_exception_cannot_follow_unverified_command_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "https://github.com/cbusillo/launchplane.git"], check=True)
            command = "launchplane service export-openapi --output artifact.json"
            for line in (
                "cd /other && " + command,
                ". /other/setup && uv run " + command,
                "builtin . /other/setup && uv run " + command,
                "{ . /other/setup; uv run " + command + "; }",
                "if . /other/setup; then uv run " + command + "; fi",
                "! . /other/setup; uv run " + command,
                "uv run --directory /other " + command,
                "uv run --project=/other " + command,
                "env -C /other " + command,
                "env -C/other " + command,
                "uv run --no-project " + command,
                "uv run --with other-package " + command,
                "UV_PROJECT=/other uv run " + command,
                "/global/bin/" + command,
                "bash -lc 'cd /other && " + command + "'",
                "true && bash -lc 'cd /other && " + command + "'",
                "bash -lc 'true' && cd /other && " + command,
                "uv run --with=other-package " + command,
                "(cd /other && uv run " + command + ")",
                "true&&cd /other&&uv run " + command,
                "true&&bash -lc 'cd /other && uv run " + command + "'",
                command,
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)
            self.assertEqual(bash("bash -lc 'uv run " + command + "'", checkout).returncode, 0)
            with mock.patch("subprocess.run", side_effect=OSError("git unavailable")):
                self.assertEqual(bash("uv run " + command, checkout).returncode, 2)
            with mock.patch.dict(os.environ, {"GIT_DIR": "/other", "GIT_WORK_TREE": "/other"}):
                self.assertEqual(bash("uv run " + command, checkout).returncode, 0)
            payload = json.dumps({"tool_name": "Bash", "cwd": str(checkout), "tool_input": {"command": "uv run " + command}})
            self.assertEqual(run_hook(payload).returncode, 0)

    def test_literal_cd_prefix_uses_only_the_verified_target_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "launchplane checkout"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            linked = linked_worktree(checkout, root / "linked")
            gate = "uv run --extra dev launchplane ci unittest-shard local"
            export = "uv run launchplane service export-openapi --output artifact.json"
            for target in (checkout, linked):
                for command in (gate, export):
                    for prefix in ("cd ", "cd -- "):
                        line = prefix + shlex.quote(str(target)) + "&&" + command
                        with self.subTest(line=line):
                            self.assertEqual(bash(line, root).returncode, 0)
                            self.assertEqual(bash("bash -lc " + shlex.quote(line), root).returncode, 0)
            for line in (
                "cd " + shlex.quote(str(root)) + " && " + gate,
                "cd /missing-launchplane-checkout && " + gate,
                "cd linked && " + gate,
                "cd '$CHECKOUT' && " + gate,
                "cd " + shlex.quote(str(checkout)) + " ; " + gate,
                "cd " + shlex.quote(str(checkout)) + " || " + gate,
                "cd " + shlex.quote(str(checkout)) + " && false || " + gate,
                "cd " + shlex.quote(str(checkout)) + " && true; " + gate,
                "(cd " + shlex.quote(str(checkout)) + " && " + gate + ")",
                "cd " + shlex.quote(str(checkout)) + " && cd " + shlex.quote(str(root)) + " && " + gate,
                "cd " + shlex.quote(str(checkout)) + " && uv run --project /other launchplane ci unittest-shard local",
                "cd " + shlex.quote(str(checkout)) + " && uv run launchplane service start",
                "cd " + shlex.quote(str(checkout)) + " && " + gate + " && uv run launchplane merge-train run-once",
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)

    def test_offline_gate_exceptions_do_not_allow_other_cli_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            for command in (
                "launchplane ci postgres-integration",
                "launchplane ci unittest-shard run-targets",
                "launchplane ci unittest-shard list",
                "launchplane ci unittest-shard local-other",
                "launchplane ci other-command",
                "launchplane service audit-config-authority-other",
                "launchplane odoo-ownership other-command",
                "launchplane odoo-targets replacement-plan",
                "launchplane action-pins report --revision HEAD --repository cbusillo/launchplane",
                "launchplane action-pins check --repo-root . --revision HEAD --repository /global/bin/launchplane",
                "launchplane action-pins check --repo-root . --revision HEAD --repository=/global/bin/launchplane",
                "launchplane merge-train run-once",
                "launchplane service start",
            ):
                with self.subTest(command=command):
                    self.assertEqual(bash("uv run --extra dev " + command, checkout).returncode, 2)
            self.assertEqual(bash("launchplane ci unittest-shard local", checkout).returncode, 2)
            self.assertEqual(bash("uv run --project /other launchplane ci unittest-shard local", checkout).returncode, 2)
            self.assertEqual(bash("echo --repository; tools/launchplane action-pins check --revision HEAD", checkout).returncode, 2)

    def test_postgres_gate_runs_only_against_a_named_loopback_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            gate = "uv run --extra dev launchplane ci postgres-integration"
            local = "postgresql+psycopg://test@127.0.0.1:55432/postgres"
            for line in (
                "LAUNCHPLANE_TEST_POSTGRES_URL=" + local + " " + gate,
                gate + " --database-url " + local,
                gate + " --database-url=postgresql://test@localhost/postgres",
                gate + " --database-url 'postgresql://test@[::1]:5432/postgres?options=-c%20statement_timeout%3D5s'",
                "cd " + shlex.quote(str(checkout)) + " && LAUNCHPLANE_TEST_POSTGRES_URL=" + local + " " + gate,
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 0)
            for line in (
                gate,  # An inherited database URL is invisible to the hook.
                gate + " --database-url",
                gate + " --database-url postgresql://test@db.example.com/postgres",
                gate + " --database-url 'postgresql://test@127.0.0.1/postgres?host=db.example.com'",
                gate + " --database-url 'postgresql://test@127.0.0.1/postgres?service=shared'",
                gate + " --database-url postgresql://test@127.0.0.1,db.example.com/postgres",
                gate + " --database-url postgresql://test@127.0.0.1.example.com/postgres",
                gate + " --database-url 'postgresql://test@127.0.0.1/postgres?application_name=ci#x&host=db.example.com'",
                "LAUNCHPLANE_TEST_POSTGRES_URL=postgresql://test@db.example.com/postgres " + gate + " --database-url " + local,
                "LAUNCHPLANE_TEST_POSTGRES_URL=" + local + " " + gate + " --database-url postgresql://test@db.example.com/postgres",
                "LAUNCHPLANE_TEST_POSTGRES_URL=" + local + " uv run launchplane service start",
                "LAUNCHPLANE_TEST_POSTGRES_URL=" + local + " UV_PROJECT=/other " + gate,
                "uv run --env-file=.env --extra dev launchplane ci postgres-integration --database-url " + local,
                "uv run --env-file .env --extra dev launchplane ci postgres-integration --database-url " + local,
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)
            self.assertEqual(bash("LAUNCHPLANE_TEST_POSTGRES_URL=" + local + " " + gate, Path(directory).parent).returncode, 2)
            for name in ("PGHOSTADDR", "PGSERVICE", "UV_ENV_FILE"):
                with self.subTest(variable=name), mock.patch.dict(os.environ, {name: "db.example.com"}):
                    self.assertEqual(bash(gate + " --database-url " + local, checkout).returncode, 2)

    def test_gate_batches_keep_newline_and_substitution_live_commands_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            gate = "uv run --extra dev launchplane ci unittest-shard local"
            live = "uv run launchplane merge-train run-once"
            for line in (
                gate + "\n" + live,
                gate + " # it's fine\n" + live,
                "ls\n" + gate + "\n" + live,
                "cd " + shlex.quote(str(checkout)) + " && " + gate + "\n" + live,
                "cd " + shlex.quote(str(checkout)) + " && " + gate + " # fine\n" + live,
                'uv run launchplane service export-openapi --output "$(' + live + ')"',
                'uv run launchplane service export-openapi --output "`' + live + '`"',
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)
            self.assertEqual(bash(gate + "\n", checkout).returncode, 0)
            self.assertEqual(bash("printf '%s' 'literal\ntext'\n" + gate, checkout).returncode, 0)

    def test_newline_parsing_preserves_heredoc_baseline_without_gate_exceptions(self) -> None:
        self.assertEqual(bash("cat > release.sh <<'EOF'\ngit push origin main\nEOF").returncode, 0)
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            line = "uv run launchplane service export-openapi --output artifact.json && cat <<'EOF'\ntext\nEOF"
            self.assertEqual(bash(line, checkout).returncode, 2)

    def test_cd_path_partial_quotes_do_not_skip_other_policies(self) -> None:
        self.assertEqual(bash("cd /tmp/a'b && x' || gh pr merge 17").returncode, 2)

    def test_shell_comments_preserve_existing_blocks(self) -> None:
        for line in ("gh pr merge 17 # it's green", "# don't bypass\ngh pr merge 17"):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_unrelated_commands_do_not_read_repository_identity(self) -> None:
        with mock.patch.dict(vars(SIMULATOR), {"verified_repository": mock.Mock(side_effect=AssertionError("unexpected Git read"))}):
            self.assertIsNone(command_policy_hook.blocking_policy("git status && printf ok"))

    def test_repository_exception_metadata_rejects_ambiguous_or_empty_scopes(self) -> None:
        policy = {"id": "fixture", "match": {"argv_prefix": ["demo"]}, "action": "reject"}
        self.assertIsNone(quick_validate.validate_command_policies([policy]))
        scoped = {"repository": "owner/repo", "argv_prefix": ["demo", "export"]}
        self.assertIsNone(quick_validate.validate_command_policies([{**policy, "exceptions": [scoped]}]))
        for exceptions in (
            "owner/repo", [{}], [{**scoped, "repository": "https://github.com/owner/repo"}],
            [{**scoped, "argv_prefix": []}], [{**scoped, "shell_regex": ".*"}],
            [{**scoped, "argv_prefix": ["demo"]}], [{**scoped, "argv_prefix": ["other", "export"]}],
        ):
            with self.subTest(exceptions=exceptions):
                self.assertIsNotNone(quick_validate.validate_command_policies([{**policy, "exceptions": exceptions}]))

    def test_json_mode_denies_without_using_the_launcher_error_exit_code(self) -> None:
        result = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh-with-env-token pr merge 17"}}), "--json")
        self.assertEqual(result.returncode, 0)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("Load the `github` skill", decision["permissionDecisionReason"])

    def test_registered_launcher_does_not_turn_uv_failure_into_a_denial(self) -> None:
        definition = json.loads(HOOK.with_name("hooks.json").read_text())
        command = definition["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "uv"
            launcher.write_text("#!/bin/sh\nexit 2\n")
            launcher.chmod(0o700)
            result = subprocess.run(["/bin/sh", "-c", command], env={**os.environ, "PATH": str(root), "CLAUDE_PLUGIN_ROOT": str(root)}, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_auth_wrapper_keeps_gh_policy_ownership(self) -> None:
        for line in (
            "gh-with-env-token pr merge 17 --merge",
            "skills/github/scripts/gh-with-env-token --print-auth-account pr merge 17 --merge",
            "command /catalog/github/scripts/gh-with-env-token --require-automation-auth pr merge 17",
            "env -u GH_TOKEN /catalog/github/scripts/gh-with-env-token pr merge 17",
            "/usr/bin/env -C /repo FOO=1 gh-with-env-token pr merge 17",
            "uv run --python 3.12 gh-with-env-token pr merge 17",
            "bash -lc 'cd /repo && gh-with-env-token pr merge 17'",
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Load the `github` skill", result.stderr)
                self.assertIn("gh-pr.py", result.stderr)

    def test_wrapper_reads_auth_checks_and_preferred_helpers_remain_allowed(self) -> None:
        for line in (
            "gh-with-env-token api repos/owner/repo",
            "gh-with-env-token --check pr merge 17",
            "gh-with-env-token --print-auth-account --check pr merge 17",
            "uv run skills/github/scripts/gh-pr.py merge 17 --method merge",
            "skills/github/scripts/git-push-as-bot origin work/task",
            "printf '%s' 'gh-with-env-token pr merge 17'",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 0, bash(line).stderr)

    def test_every_argv_policy_blocks_its_command_with_its_own_message(self) -> None:
        catalog = {(entry["skill"], entry["id"]): entry for entry in SIMULATOR.policy_catalog()}
        argv_policies = [
            entry for entry in catalog.values() if {"argv_prefix", "argv_exact"} & set(entry["match"])
        ]
        self.assertTrue(argv_policies)
        for entry in argv_policies:
            argv = [str(token) for token in entry["match"].get("argv_exact") or entry["match"]["argv_prefix"]]
            # Another policy may legitimately take precedence; the simulator decides which.
            winner = SIMULATOR.primary_match(argv)
            expected = catalog[(winner.skill, winner.policy_id)]
            with self.subTest(policy=entry["id"]):
                result = bash(shlex.join(argv))
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected["id"], result.stderr)
                if expected.get("message"):
                    self.assertIn(str(expected["message"]), result.stderr)

    def test_a_policy_command_is_found_inside_a_compound_line(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        command = shlex.join(str(token) for token in entry["match"]["argv_prefix"])
        for line in (
            f"cd /tmp && {command} 5",
            f"PAGER=cat OTHER=1 {command} 5",
            f"true; ( {command} 5 ) | cat",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_commands_no_policy_names_run(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        command = shlex.join(str(token) for token in entry["match"]["argv_prefix"])
        for line in ("git status", f"printf %s {shlex.quote(command)}"):
            with self.subTest(line=line):
                self.assertIsNone(SIMULATOR.primary_match(shlex.split(line), line), "fixture must be policy-free")
                result = bash(line)
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_ruleset_policy_blocks_writes_but_allows_reads(self) -> None:
        for line in (
            "gh ruleset list",
            "gh ruleset view 123",
            "gh-with-env-token api repos/owner/repo/rulesets --method GET",
        ):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))
        for line in (
            "gh api -X POST repos/owner/repo/rulesets --input payload.json",
            "gh-with-env-token api repos/owner/repo/rulesets/123 --method PUT --input payload.json",
            "gh-with-env-token api repos/owner/repo/rulesets --input payload.json",
            "gh-with-env-token api repos/owner/repo/rulesets -f name=x -f target=branch",
            "gh-with-env-token api --method=PUT repos/owner/repo/rulesets/123 --input payload.json",
            "gh-with-env-token api -XDELETE repos/owner/repo/rulesets/123",
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2)

    def test_commit_creating_git_commands_require_bot_path(self) -> None:
        catalog = {(entry["skill"], entry["id"]): entry for entry in SIMULATOR.policy_catalog()}
        for command in ("merge --no-edit origin/main", "pull", "rebase --continue",
                        "cherry-pick abc", "revert HEAD", "am patch.mbox",
                        "merge --ff-only --no-ff main", "merge --ff-only --ff main",
                        "rebase --skip", "am --skip"):
            for prefix in ("git ", "git -C 'a path' ", "env FLAG=x git --no-pager ", "git -Cpath -cuser.name=Human "):
                line = prefix + command
                with self.subTest(command=line):
                    winner = SIMULATOR.primary_match(shlex.split(line), line)
                    self.assertIsNotNone(winner)
                    expected = catalog[(winner.skill, winner.policy_id)]
                    result = bash(line)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(expected["id"], result.stderr)
                    self.assertIn(str(expected["message"]), result.stderr)
        for command in ("git merge-base main topic", "git log --grep merge",
                        "git-commit-as-bot --git-command merge --no-edit main",
                        "git -C path -c core.hooksPath=/dev/null merge --ff-only --no-autostash --no-overwrite-ignore abc",
                        "git merge --ff-only main", "git merge --abort", "git rebase --abort",
                        "git cherry-pick --quit", "git am --abort"):
            self.assertEqual(bash(command).returncode, 0, command)

    def test_git_global_options_do_not_hide_commit_or_push(self) -> None:
        for line in (
            "git -c commit.gpgsign=false commit -m demo",
            "cd /tmp && git -C 'a path' commit -m demo",
            "bash -lc 'git --no-pager -C repo commit'",
            "git -c user.name=\"Shiny Code\" commit -m demo",
            "git -C repo push origin branch",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)
        for line in (
            "git -C repo status",
            "git -C repo commit-graph write",
            "git -C repo log --grep commit",
            "rg -n 'git -C repo commit' docs/",
        ):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))

    def test_a_preferred_replacement_is_itself_allowed(self) -> None:
        for entry in SIMULATOR.policy_catalog():
            for preferred in entry["preferred"]:
                if preferred.get("example_argv"):
                    line = shlex.join(str(token) for token in preferred["example_argv"])
                    with self.subTest(policy=entry["id"], line=line):
                        self.assertEqual(bash(line).returncode, 0, bash(line).stderr)

    def test_the_replacement_shown_names_a_script_that_exists_here(self) -> None:
        for entry in SIMULATOR.policy_catalog():
            for preferred in entry["preferred"]:
                for token in map(str, preferred.get("example_argv") or []):
                    if not token.endswith((".py", ".sh")) or token.startswith("/"):
                        continue
                    with self.subTest(policy=entry["id"], token=token):
                        shown = command_policy_hook.runnable(token, entry["skill"])
                        self.assertTrue(Path(shown).is_absolute(), shown)
                        self.assertTrue(Path(shown).is_file(), shown)

    def test_the_block_message_shows_the_resolved_replacement_and_it_is_allowed(self) -> None:
        result = bash("gh issue list")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("$CODE_HOME", result.stderr)
        shown = next(line for line in result.stderr.splitlines() if line.startswith("Run instead: "))
        self.assertTrue(Path(shlex.split(shown.removeprefix("Run instead: "))[2]).is_file(), shown)
        self.assertEqual(bash(shown.removeprefix("Run instead: ")).returncode, 0)

    def test_placeholders_and_flags_are_shown_as_written(self) -> None:
        for token in ("<query>", "--body-file", "uv", "/path/to/repo", "$HOME/x/y.py", "missing/script.py"):
            with self.subTest(token=token):
                self.assertEqual(command_policy_hook.runnable(token, "github-plan"), token)

    def test_a_policy_command_is_found_behind_what_agents_put_in_front_of_it(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        prefix = [str(token) for token in entry["match"]["argv_prefix"]]
        command = shlex.join(prefix)
        tool, rest = prefix[0], shlex.join(prefix[1:])
        for line in (
            f"command {command} 5",
            f"exec {command} 5",
            f"time {command} 5",
            f"uv run --quiet {command} 5",
            f"env -i FOO=1 {command} 5",
            f"FOO=1 command /opt/homebrew/bin/{tool} {rest} 5",
            f"bash -lc {shlex.quote(f'cd /tmp && {command} 5')}",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_unwrapping_does_not_block_what_no_policy_names(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        command = shlex.join(str(token) for token in entry["match"]["argv_prefix"])
        # The last two are wrappers the hook deliberately does not unwrap; see its docstring.
        for line in ("env", "uv run", "bash -c 'git status'", f"echo {command}", f"xargs {command}", f"sudo {command}"):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))

    def test_the_simulators_cases_hold_for_whole_shell_lines(self) -> None:
        for argv, shell, skill in SIMULATOR.EXPECTATIONS:
            line = shell or shlex.join(argv)
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"`{skill}`", result.stderr)
        for argv, shell in SIMULATOR.NEGATIVE_EXPECTATIONS:
            line = shell or shlex.join(argv)
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))

    def test_quoted_arguments_and_heredoc_data_are_not_commands(self) -> None:
        # Each line was refused in a real session although nothing in it ran the named tool (#671, #1014).
        for line in (
            "ps -axo pid,command | rg '^ *[0-9]+ .*gh api --paginate repos/cbusillo/verireel/actions/runs --jq'",
            "uv run gh-plan.py search \"command policy gh api quoted user:cbusillo\" --state all",
            "python3 - <<'EOF'\nprint('git -c commit.gpgsign=false commit --dry-run')\nEOF",
            "cat > fixture.sh <<'EOF'\ngit commit -qm base\n(cd ../landing && git commit -qam \"next\")\nEOF",
            "cat > notes.md <<'EOF'\n| launchplane | skill |\n| --- | --- |\nEOF\ngit status",
            "cat <<EOF > body.md\nRun gh api repos/o/r/pulls; then git push origin main.\nEOF",
            "python3 - <<-'EOF'\n\tlaunchplane merge-train run-once\n\tEOF",
            "printf '%s\\n' \"$((1 << 2))\" 'gh pr merge 17'",
        ):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))

    def test_commands_that_run_from_heredocs_quotes_and_wrappers_stay_blocked(self) -> None:
        for line, matched in (
            ("bash <<'EOF'\ngit push origin main\nEOF", "git push origin main"),
            ("cat <<'EOF' | sh -e\ngh pr merge 17\nEOF", "gh pr merge 17"),
            ("cat <<EOF\n$(gh pr merge 17)\nEOF", "gh pr merge 17"),
            ("cat <<EOF\nmerged: `gh pr merge 17`\nEOF", "gh pr merge 17"),
            ("cat <<'EOF'\ntext\nEOF\ngh pr merge 17", "gh pr merge 17"),
            ("cat <<-EOF\n\tdata\n\tEOF\ngh pr merge 17", "gh pr merge 17"),
            ("git commit -m \"$(cat <<'EOF'\nmessage\nEOF\n)\"", "git commit -m"),
            ("echo \"$(gh api repos/o/r/pulls)\"", "gh api repos/o/r/pulls"),
            ("echo \"`gh api repos/o/r/pulls`\"", "gh api repos/o/r/pulls"),
            ("echo `gh api repos/o/r/pulls`", "gh api repos/o/r/pulls"),
            ("diff <(gh api repos/o/r/pulls) saved.json", "gh api repos/o/r/pulls"),
            ("sudo gh api repos/o/r/pulls", "gh api repos/o/r/pulls"),
            ("timeout 30 gh api repos/o/r/pulls", "gh api repos/o/r/pulls"),
            ("eval \"gh api repos/o/r/pulls\"", "gh api repos/o/r/pulls"),
            ("echo $((1 << 2))\ngh pr merge 17", "gh pr merge 17"),
            ("printf '%s\\n' \"$(( $(gh api repos/o/r/pulls | wc -l) + 1 ))\"", "gh api repos/o/r/pulls"),
            ("bash <<< 'gh api repos/o/r/pulls'", "gh api repos/o/r/pulls"),
            ("cat <<< \"text\"\ngh api repos/o/r/pulls", "gh api repos/o/r/pulls"),
            ("grep foo <<< \"$text\"\ngit -c commit.gpgsign=false commit -m demo", "git -c commit.gpgsign=false commit"),
            ("sudo sh -c 'gh api repos/o/r/pulls'", "gh api repos/o/r/pulls"),
            ("timeout 60 bash -lc 'git push origin main'", "git push origin main"),
            ("bash -c \"bash -c 'gh api repos/o/r/pulls'\"", "gh api repos/o/r/pulls"),
            ("sudo bash <<'EOF'\ngh api repos/o/r/pulls\nEOF", "gh api repos/o/r/pulls"),
            ("((mask = 1 << 2))\ngh api repos/o/r/pulls", "gh api repos/o/r/pulls"),
            ("ssh -p 2222 build-host 'gh api repos/o/r/pulls'", "gh api repos/o/r/pulls"),
            ("ssh build-host bash <<'EOF'\ngh api repos/o/r/pulls\nEOF", "gh api repos/o/r/pulls"),
            ("gh-with-env-token api repos/o/r/issues/1 --method PATCH -f body=x", "repos/o/r/issues/1"),
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2)
                self.assertIn(matched, result.stderr)

    def test_other_tools_and_unreadable_events_are_let_through(self) -> None:
        self.assertEqual(run_hook(json.dumps({"tool_name": "Read", "tool_input": {}})).returncode, 0)
        for payload in ("not json", json.dumps({"tool_name": "Bash"})):
            with self.subTest(payload=payload):
                result = run_hook(payload)
                self.assertEqual(result.returncode, 0)
                self.assertIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
