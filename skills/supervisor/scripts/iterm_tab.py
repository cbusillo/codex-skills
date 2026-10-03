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
import shlex
from pathlib import Path

import account_choice


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


def with_account(command_text, choice):
    """Prefix one agent invocation with the chosen account's environment."""
    if "\n" in command_text:
        raise ValueError("account selection needs a one-line launch command")
    for key in choice["env"]:
        if f"{key}=" in command_text:
            raise ValueError(f"launch file already sets {key}; remove it or omit --account-provider")
    settings = " ".join(
        f"{key}={shlex.quote(os.path.expanduser(value))}"
        for key, value in choice["env"].items()
    )
    return f"env {settings} {command_text}"


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
        command_text = args.command_file.read_text(encoding="utf-8").rstrip("\n")
        validate_text(command_text)
        choice = None
        if getattr(args, "account_provider", None):
            choice = account_choice.select(
                args.account_provider, args.account_config, args.account
            )
            command_text = with_account(command_text, choice)
        elif getattr(args, "account", None):
            raise ValueError("--account needs --account-provider")
        tab = await windows[0].async_create_tab()
        if not tab.current_session:
            raise ValueError("new tab has no session; inspect it before retrying")
        await send(tab.current_session, command_text)
        result = {
            "window_id": windows[0].window_id,
            "tab_id": tab.tab_id,
            "session_id": tab.current_session.session_id,
        }
        if choice:
            result["account"] = account_choice.public(choice)
    if previous_tab:
        await previous_tab.async_select()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("window")
    new = commands.add_parser("new")
    new.add_argument("--window-id", required=True)
    new.add_argument("--command-file", type=Path, required=True)
    new.add_argument(
        "--account-provider",
        choices=account_choice.PROVIDERS,
        help="launch on the account Context Panel says resets soonest with room",
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
        except (ValueError, OSError) as error:
            parser.exit(1, f"refused: {error}\n")

    iterm2.run_until_complete(connected)


if __name__ == "__main__":
    main()
