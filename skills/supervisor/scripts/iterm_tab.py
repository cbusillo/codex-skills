#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["iterm2==2.25"]
# ///
"""Target iTerm sessions by exact identity, with separate text and Return sends."""

import argparse
import asyncio
import json
import os
import re
import shlex
from pathlib import Path

import account_choice


class LaunchFailure(ValueError):
    """Preserve launch identities so a partial batch is never mistaken for refusal."""

    def __init__(self, launched, failed, error):
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        self.detail = {"status": "launch_failed", "reason": reason,
                       "launched": launched, "failed_launch": failed,
                       "next_action": "List and read these tabs before retrying; earlier launches may be running."}
        super().__init__(reason)


def sessions(app):
    return [
        session
        for window in app.terminal_windows
        for tab in window.tabs
        for session in tab.sessions
    ]


def select_session(app, session_id):
    matches = [session for session in sessions(app) if session.session_id == session_id]
    if len(matches) != 1:
        raise ValueError(f"expected one session matching id; found {len(matches)}")
    return matches[0]


def validate_text(text):
    if not text or any(ord(c) < 32 and c != "\n" for c in text):
        raise ValueError("text must be nonempty with no terminal control characters")


async def send(session, text, submit=True):
    validate_text(text)
    if "\n" in text:
        text = "\x1b[200~" + text + "\x1b[201~"
    await session.async_send_text(text, suppress_broadcast=True)
    if submit:
        await asyncio.sleep(0.4)
        await session.async_send_text("\r", suppress_broadcast=True)


async def wait_for_session(app, tab, timeout=10.0):
    """Refresh only the created tab's identity until its session is available."""
    if tab is None:
        raise ValueError("new tab identity unavailable; run list and inspect before retrying")
    tab_id = tab.tab_id
    try:
        async with asyncio.timeout(timeout):
            while True:
                if tab.current_session:
                    return tab.current_session
                await app.async_refresh()
                tab = app.get_tab_by_id(tab_id)
                if tab is None:
                    raise ValueError(f"new tab {tab_id} closed before its session was available")
                if not tab.current_session:
                    await asyncio.sleep(0.1)
    except TimeoutError as error:
        raise ValueError(
            f"new tab {tab_id} has no session after {timeout:g}s; inspect it before retrying"
        ) from error


def account_settings(command_text, keys, depth=0):
    """Find shell setters, rather than matching text inside a brief argument."""
    if depth >= 5:
        raise ValueError("nested launch commands are too deep; use one invocation and a brief file")
    lexer = shlex.shlex(command_text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    segments = [[]]
    for token in lexer:
        if token and all(c in ";&|()" for c in token):
            segments.append([])
        else:
            segments[-1].append(token)
    found = set()
    for words in segments:
        index = 0
        while index < len(words):
            if Path(words[index]).name in {"exec", "command", "nohup", "time", "builtin", "{",
                                          "then", "do", "else", "!", "and", "or", "not", "begin"}:
                wrapper = Path(words[index]).name
                index += 1
                while index < len(words) and words[index].startswith("-"):
                    if wrapper == "exec" and words[index] == "-c":
                        found.update(keys)
                    if wrapper == "exec" and words[index] == "-a":
                        index += 1
                    index += 1
            elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", words[index]):
                found.add(words[index].split("=", 1)[0].rstrip("+"))
                index += 1
            else:
                break
        if index == len(words):
            continue
        command = Path(words[index]).name
        args = words[index + 1:]
        if command in {"export", "declare", "typeset", "local", "readonly", "unset", "set"}:
            found.update(arg.split("=", 1)[0].rstrip("+") for arg in args if not arg.startswith("-"))
        elif command in {"eval", "sh", "bash", "zsh", "dash", "fish"}:
            body = " ".join(args) if command == "eval" else ""
            if command != "eval":
                for i, arg in enumerate(args):
                    if command == "fish" and arg.startswith("--command="):
                        body = arg.split("=", 1)[1]
                        break
                    if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg) or (command == "fish" and arg == "--command"):
                        tail = args[i + 1:]
                        while tail and tail[0].startswith("-"):
                            tail = tail[1:]
                        body = tail[0] if tail else ""
                        break
            found.update(account_settings(body, keys, depth + 1))
        elif command == "env":
            index = 0
            while index < len(args):
                arg = args[index]
                if arg in {"-i", "--ignore-environment", "-"}:
                    found.update(keys)
                elif arg in {"-P", "-C", "--chdir"}:
                    index += 1
                elif arg in {"-S", "--split-string"}:
                    index += 1
                    if index < len(args):
                        found.update(account_settings(args[index], keys, depth + 1))
                elif arg.startswith("--split-string="):
                    found.update(account_settings(arg.split("=", 1)[1], keys, depth + 1))
                elif arg.startswith("-S"):
                    found.update(account_settings(arg[2:], keys, depth + 1))
                elif arg in {"-u", "--unset"}:
                    index += 1
                    if index < len(args):
                        found.add(args[index])
                elif arg.startswith("--unset="):
                    found.add(arg.split("=", 1)[1])
                elif arg.startswith("-u"):
                    found.add(arg[2:])
                elif "=" in arg and not arg.startswith("-"):
                    found.add(arg.split("=", 1)[0])
                elif not arg.startswith("-"):
                    found.update(account_settings(shlex.join(args[index:]), keys, depth + 1))
                    break
                index += 1
    return found.intersection(keys)


def with_account(command_text, choice):
    """Prefix one agent invocation with the chosen account's environment."""
    if "\n" in command_text:
        raise ValueError("account selection needs a one-line launch command")
    default_claude = choice["provider"] == "anthropic" and "CLAUDE_CONFIG_DIR" not in choice["env"]
    checked_keys = set(choice["env"]) | set(account_choice.ACCOUNT_VARIABLES.values())
    if default_claude:
        checked_keys.add("CLAUDE_CONFIG_DIR")
    conflicts = account_settings(command_text, checked_keys)
    if conflicts:
        raise ValueError(f"launch file already sets {', '.join(sorted(conflicts))}; "
                         "remove those settings and use --account for an explicit override")
    settings = " ".join(
        f"{key}={shlex.quote(os.path.expanduser(value))}"
        for key, value in choice["env"].items()
    )
    # export, not env: the account must also reach an agent after `cd dir &&`.
    prefixes = ["unset CLAUDE_CONFIG_DIR"] if default_claude else []
    if settings:
        prefixes.append(f"export {settings}")
    if choice["provider"] == "anthropic":
        prefixes.append("export CLAUDE_ACCOUNT_MOVE_ENABLED=1")
    return " && ".join([*prefixes, command_text])


async def operate(app, args):
    if args.command == "list":
        result = []
        for window in app.terminal_windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    result.append(
                        {
                            "window_id": window.window_id,
                            "tab_id": tab.tab_id,
                            "session_id": session.session_id,
                            "title": session.name,
                            "tty": await session.async_get_variable("tty"),
                        }
                    )
        return result
    if args.command in {"send", "read", "clear"}:
        session = select_session(app, args.session_id)
        if args.command == "read":
            screen = await session.async_get_screen_contents()
            return {
                "session_id": session.session_id,
                "screen": [
                    screen.line(i).string for i in range(screen.number_of_lines)
                ],
            }
        if not args.verified_target:
            raise ValueError(
                "verify the target, input line and prompt before --verified-target"
            )
        if args.command == "clear":
            await session.async_send_text("\x15", suppress_broadcast=True)
        else:
            text = (
                args.text_file.read_text(encoding="utf-8")
                if args.text_file
                else args.text
            )
            await send(session, text, not args.no_submit)
        return {
            "session_id": session.session_id,
            "sent": True,
            "readback_required": True,
        }
    import iterm2

    previous = app.current_terminal_window
    previous_tab = previous.current_tab if previous else None
    if args.command == "window":
        window = await iterm2.Window.async_create(app.connection)
        result = {"window_id": window.window_id}
    else:
        windows = [
            window
            for window in app.terminal_windows
            if window.window_id == args.window_id
        ]
        if len(windows) != 1:
            raise ValueError("window id does not identify one window")
        files = args.command_file if isinstance(args.command_file, list) else [args.command_file]
        command_texts = [path.read_text(encoding="utf-8").rstrip("\n") for path in files]
        for text in command_texts:
            validate_text(text)
        choices = [None] * len(files)
        if getattr(args, "account_provider", None):
            if len(files) == 1:
                choices = [account_choice.select(args.account_provider, args.account_config, args.account)]
            else:
                choices = account_choice.select_batch(
                    args.account_provider, args.account_config, args.account, count=len(files)
                )
            command_texts = [with_account(text, choice) for text, choice in zip(command_texts, choices)]
        elif getattr(args, "account", None):
            raise ValueError("--account needs --account-provider")
        elif any(account_settings(text, account_choice.ACCOUNT_VARIABLES.values())
                 for text in command_texts):
            raise ValueError("account settings need --account-provider; remove them from the launch file "
                             "and use --account for an explicit override")
        if choices[0]:
            account_choice.prepare_launch(choices[0])
        results = []
        for command_text, choice in zip(command_texts, choices):
            progress = {"window_id": windows[0].window_id, "tab_id": None,
                        "session_id": None, "receipt_written": False,
                        "submission": "not_attempted", "phase": "create_tab"}
            if choice:
                progress["account"] = account_choice.public(choice)
            try:
                tab = await windows[0].async_create_tab(select=False)
                progress.update(tab_id=tab.tab_id if tab else None, phase="wait_for_session")
                session = await wait_for_session(app, tab)
                progress.update(session_id=session.session_id, phase="write_receipt")
                if choice:
                    account_choice.record_launch(choice)
                    progress["receipt_written"] = True
                progress.update(submission="unknown", phase="submit_command")
                await send(session, command_text)
            except Exception as error:
                raise LaunchFailure(results, progress, error) from error
            result = {"window_id": windows[0].window_id,
                      "tab_id": tab.tab_id, "session_id": session.session_id}
            if choice:
                result["account"] = account_choice.public(choice)
            results.append(result)
        return results[0] if len(results) == 1 else results
    if args.command == "window" and previous_tab:
        await previous_tab.async_select()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("window")
    new = commands.add_parser("new")
    new.add_argument("--window-id", required=True)
    new.add_argument("--command-file", type=Path, action="append", required=True,
                     help="repeat for a batch, in launch order")
    new.add_argument(
        "--account-provider",
        required=True,
        choices=account_choice.PROVIDERS,
        help="launch on Context Panel's use-next account or ranked batch order",
    )
    new.add_argument("--account", help="use this configured account by name")
    new.add_argument("--account-config", type=Path)
    for name in ("send", "read", "clear"):
        sub = commands.add_parser(name)
        sub.add_argument("--session-id", required=True)
        if name != "read":
            sub.add_argument("--verified-target", action="store_true")
        if name == "send":
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument("--text")
            source.add_argument("--text-file", type=Path)
            sub.add_argument("--no-submit", action="store_true")
    args = parser.parse_args()
    import iterm2

    async def connected(connection):
        app = await iterm2.async_get_app(connection)
        try:
            print(json.dumps(await operate(app, args), indent=2))
        except LaunchFailure as error:
            print(json.dumps(error.detail, indent=2))
            parser.exit(1, "launch failed; inspect the reported tabs before retrying\n")
        except (ValueError, OSError) as error:
            parser.exit(1, f"refused: {error}\n")

    iterm2.run_until_complete(connected)


if __name__ == "__main__":
    main()
