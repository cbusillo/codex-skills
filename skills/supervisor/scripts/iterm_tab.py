#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "iterm2==2.30",
#     "websockets==17.3",
# ]
# ///
"""Target iTerm or tmux sessions by exact identity; separate text and Return sends."""

import argparse
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import errno
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import NamedTuple

import account_choice
import claude_trust
import agent_tools
import gui_launch

CLAUDE_SUGGESTION_SETTING = "promptSuggestionEnabled"
CLAUDE_AUTO_MEMORY_SETTING = "autoMemoryEnabled"
CLAUDE_UNATTENDED_SETTINGS = (CLAUDE_SUGGESTION_SETTING, CLAUDE_AUTO_MEMORY_SETTING)
CLAUDE_SUGGESTION_ENV = "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"
CLAUDE_AUTOCOMPACT_OPTION = "--autocompact"
CLAUDE_QUESTION_TOOL = "AskUserQuestion"
# Codex draws its first screen seconds after exec (3.3s from an isolated home
# on Chris-Studio; account and MCP startup add more), so watch for its
# hook-review prompt until the brief's turn runs, within this bound.
CODEX_PROMPT_WINDOW = 20.0
LAUNCH_TIMEOUT = 30.0
# The catalog's Chrome opt-in; with it, browser tools come only from the pinned server.
CHROME_OPT_IN = Path(__file__).resolve().parents[3] / ".local" / "chrome.toml"
# Every brief carries the reviewed rules, by link or as an unchanged copy (Q587).
BRIEF_RULES = Path(__file__).resolve().parents[1] / "references" / "brief-rules.md"
BRIEF_RULES_URL = "https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/brief-rules.md"
# A link to main stays current; a pinned commit, relative path or longer name does not.
# The link ends at an anchor, whitespace, a closing delimiter or sentence punctuation.
_BRIEF_RULES_TAIL = (re.escape("skills/supervisor/references/brief-rules.md")
                     + r"(?=#|$|[\s)>\]`'\"]|[.,;:](?:\s|$))")
BRIEF_RULES_URL_LINK = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/blob/main/" + _BRIEF_RULES_TAIL)
# An absolute path counts only when that file holds the current rules block.
BRIEF_RULES_PATH_LINK = re.compile(r"(?<![^\s(<\[`'\"])(/(?:[^\s:()<>\[\]`'\"]+/)?" + _BRIEF_RULES_TAIL + ")")



class SendFailure(ValueError):
    """Input may have reached the terminal; never append another command blindly."""

    def __init__(self, identity, submission, reason, text_sent=True):
        self.detail = {"status": "send_failed", "session_id": identity,
                       "text_sent": text_sent, "submission": submission,
                       "input_may_contain_text": True, "readback_required": True,
                       "error": reason,
                       "next_action": "Read the exact session before any next send. If unsubmitted, "
                                      "clear with verified-target clear (Ctrl-U) and verify empty input."}
        text_outcome = "text was sent" if text_sent else "text send outcome unknown"
        super().__init__(text_outcome + "; Return " + submission + "; input may still contain text")


def input_line(lines, styles):
    """Report the last visible agent prompt's styling, never its author or authority."""
    for index in range(len(lines) - 1, -1, -1):
        match = re.match(r"^\s*[❯›>]\s?(.*)$", lines[index])
        if not match:
            continue
        text = match.group(1)
        flags = [styles[index][x] for x in range(match.start(1), len(lines[index]))
                 if not lines[index][x].isspace()]
        # Claude renders the first suggestion character as an inverse cursor.
        if flags and flags[0] == "cursor" and len(flags) > 1 and all(f is True for f in flags[1:]):
            flags = flags[1:]
        if not flags:
            style = "empty"
        elif None in flags:
            style = "unknown"
        elif all(f is True for f in flags):
            style = "dim"
        elif any(f is True for f in flags):
            style = "mixed"
        else:
            style = "non_dim"
        return {"line_index": index, "text": text, "style": style,
                "director_input_verified": False}
    return {"line_index": None, "text": None, "style": "unknown",
            "director_input_verified": False}


def ansi_screen(captured):
    """Decode tmux capture-pane -e SGR faint attributes alongside plain text."""
    lines, styles = [], []
    faint, inverse = False, False
    for raw in captured.splitlines():
        text, flags, offset = "", [], 0
        for match in re.finditer(r"\x1b(?:\[([0-9;:]*)m|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))", raw):
            chunk = raw[offset:match.start()]
            text += chunk
            flags.extend(["cursor" if inverse else faint] * len(chunk))
            params = match.group(1).split(";") if match.group(1) is not None else []
            i = 0
            while i < len(params):
                value = params[i]
                if value in {"38", "48", "58"} and i + 1 < len(params):
                    # RGB components (including 2 and 22) are not intensity codes.
                    i += 5 if params[i + 1] == "2" else 3
                    continue
                if value in {"", "0", "22"}:
                    faint = False
                    if value in {"", "0"}:
                        inverse = False
                elif value == "2":
                    faint = True
                elif value == "7":
                    inverse = True
                elif value == "27":
                    inverse = False
                i += 1
            offset = match.end()
        chunk = raw[offset:]
        lines.append(text + chunk)
        styles.append(flags + ["cursor" if inverse else faint] * len(chunk))
    return lines, styles


def iterm_input_line(screen):
    lines, styles = [], []
    for i in range(screen.number_of_lines):
        line = screen.line(i)
        lines.append(line.string)
        flags, consumed = [], 0
        try:
            # Wide characters can use two cells. Keep optional style data bounded.
            for cell in range(2 * len(line.string)):
                if consumed >= len(line.string):
                    break
                value = line.string_at(cell)
                style = line.style_at(cell)
                flag = None if style is None else "cursor" if style.inverse else style.faint
                flags.extend([flag] * len(value))
                consumed += len(value)
            if consumed != len(line.string):
                flags = [None] * len(line.string)
        except (AttributeError, IndexError):
            flags = [None] * len(line.string)
        styles.append(flags)
    return input_line(lines, styles)


class LaunchFailure(ValueError):
    """Preserve launch identities so a partial batch is never mistaken for refusal."""

    def __init__(self, launched, failed, error):
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        self.detail = {"status": "launch_failed", "reason": reason,
                       "launched": launched, "failed_launch": failed,
                       "next_action": "List and read these tabs before retrying; earlier launches may be running."}
        if isinstance(error, StartupFailure):
            self.detail["failed_launch"]["startup"] = error.snapshot
            if error.snapshot.get("blocked_prompt"):
                self.detail["status"] = "launch_blocked"
        super().__init__(reason)


class StartupFailure(ValueError):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        if snapshot.get("blocked_prompt"):
            reason = f"startup blocked by {snapshot['blocked_prompt']} prompt; inspect the exact terminal; do not relaunch or grant trust automatically"
        else:
            reason = ("terminal exited during startup; inspect its last output before retrying"
                      if snapshot["dead"] else
                      "startup observation unavailable; submitted agent may be running; inspect before retrying")
        super().__init__(reason)


def startup_prompt_window(choice):
    """Only Codex shows the hook-review prompt; other launches keep the short check."""
    return CODEX_PROMPT_WINDOW if choice and choice["provider"] == "openai" else 0.0


def running_turn(screen, submitted):
    """A Codex status row such as "• Working (0s • esc to interrupt)".

    The echoed launch command, brief included, is not evidence; an unknown
    row format only means the full prompt window is used.
    """
    return any(re.match(r"\s*[^\w\s]?\s*\w[\w ]*\(\d+[hms].*\besc to interrupt\b", line)
               and line.strip() not in submitted.casefold()
               for line in screen.splitlines())


async def verify_startup(read, timeout=2.0, prompt_window=0.0, submitted=""):
    """Observe immediate exit or native trust prompts; not full readiness."""
    deadline = time.monotonic() + timeout
    # A running turn is past every startup prompt; stop watching there.
    watch_until = max(deadline, time.monotonic() + prompt_window)
    last_dead = None
    collect_until = deadline
    while True:
        try:
            snapshot = await read()
        except Exception as error:
            if last_dead is not None:
                raise StartupFailure({**last_dead, "observation_error": type(error).__name__}) from error
            raise
        if not snapshot["dead"]:
            screen = "\n".join(snapshot.get("screen", [])).casefold()
            choice = r"(?m)^[\s│┃|]*[›❯>]?\s*(?:[12][.)]\s*)?"
            if (re.search(choice + r"trust all and\s+continue\b", screen)
                    and re.search(choice + r"continue without trusting\b", screen)):
                raise StartupFailure({**snapshot, "blocked_prompt": "hook_trust"})
            if running_turn(screen, submitted):
                watch_until = deadline
        if snapshot["dead"]:
            if last_dead is None:
                # A death seen late in the prompt watch gets the same window
                # to collect its cause as one seen at launch.
                collect_until = max(deadline, time.monotonic() + timeout)
            last_dead = snapshot
        # tmux can observe PTY EOF before SIGCHLD supplies the exit status.
        # Keep the failure, but use the remaining window to collect its cause.
        awaiting_status = (last_dead is not None and last_dead.get("exit_status") == ""
                           and not last_dead.get("exit_signal"))
        if last_dead is not None and (not awaiting_status or time.monotonic() >= collect_until):
            raise StartupFailure(last_dead)
        if last_dead is None and time.monotonic() >= watch_until:
            return
        await asyncio.sleep(min(0.2, max(0, (collect_until if last_dead else watch_until) - time.monotonic())))


async def submit_iterm_launch(app, session, command, prompt_window=0.0):
    import iterm2

    last_screen = []
    async with iterm2.SessionTerminationMonitor(app.connection) as monitor:
        async def terminated():
            while await monitor.async_get() != session.session_id:
                pass

        termination = asyncio.create_task(terminated())
        try:
            # Replace the interactive shell so any agent exit ends this exact
            # terminal, even when the launch file didn't itself use exec.
            submitted = "exec /bin/zsh -c " + shlex.quote(command)
            await send(session, submitted, allow_tabs=True)

            async def read():
                nonlocal last_screen
                await app.async_refresh()
                matches = [s for s in sessions(app) if s.session_id == session.session_id]
                if termination.done() and termination.exception() is not None:
                    raise ValueError("iTerm termination monitor failed")
                dead = termination.done() or len(matches) != 1
                if matches:
                    try:
                        screen = await matches[0].async_get_screen_contents()
                        last_screen = [screen.line(i).string for i in range(screen.number_of_lines)][-80:]
                    except Exception:
                        if not dead:
                            raise
                return {"dead": dead, "screen": last_screen}

            try:
                # The shell echoes the quoted text as sent, wrapped at the
                # terminal width; compare screen rows against that text.
                await verify_startup(read, prompt_window=prompt_window, submitted=submitted)
            except StartupFailure:
                raise
            except Exception as error:
                raise StartupFailure({"dead": None, "screen": last_screen,
                                      "observation_error": type(error).__name__}) from error
        finally:
            termination.cancel()
            await asyncio.gather(termination, return_exceptions=True)


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


def validate_text(text, allow_tabs=False):
    allowed = "\n\t" if allow_tabs else "\n"
    if not text or any(ord(c) < 32 and c not in allowed for c in text):
        raise ValueError("text must be nonempty with no terminal control characters")


def brief_rules_block(path=BRIEF_RULES):
    """Return the marked rules block from a brief rules reference."""
    text = read_launch_text(Path(path))
    start, end = "<!-- brief-rules:start -->", "<!-- brief-rules:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"{path} needs exactly one marked rules block")
    return text.split(start, 1)[1].split(end, 1)[0].strip()


def current_rules_path(path, rules):
    try:
        return brief_rules_block(path) == rules
    except (OSError, ValueError):
        return False


def validate_brief_rules(brief):
    rules = brief_rules_block()
    if (BRIEF_RULES_URL_LINK.search(brief)
            or any(current_rules_path(path, rules) for path in BRIEF_RULES_PATH_LINK.findall(brief))
            or " ".join(rules.split()) in " ".join(brief.split())):
        return
    raise ValueError(f"brief must link the current Supervisor brief rules ({BRIEF_RULES_URL} or {BRIEF_RULES}) "
                     "or include their rules block unchanged")


def launch_parts(command_text):
    """Validate a direct agent invocation and return its executable and settings."""
    lexer = shlex.shlex(command_text, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    directory = None
    reason = "brief delivery needs a direct agent invocation; use cd PATH && exec AGENT SETTINGS"
    if (not tokens or "\n" in command_text or tokens.count("&&") > 1
            or any(token.startswith("#")
                   or (all(c in ";&|()<>" for c in token) and token != "&&") for token in tokens)):
        raise ValueError(reason)
    if "&&" in tokens:
        separator = tokens.index("&&")
        prefix = tokens[:separator]
        if len(prefix) != 2 or prefix[0] != "cd":
            raise ValueError(reason)
        directory = prefix[1]
        tokens = tokens[separator + 1:]
    words = tokens
    while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])
                     or words[0] in {"exec", "command", "nohup", "time", "env"}):
        words = words[1:]
    if not words or Path(words[0]).name not in {"codex", "claude", "agy"}:
        raise ValueError(reason)
    return words, directory


def launch_words(command_text):
    return launch_parts(command_text)[0]


def launch_agent(command_text):
    """Identify a direct invocation, optionally after cd, using the brief parser."""
    return Path(launch_words(command_text)[0]).name


def claude_settings(command_text, words):
    source = None
    for index, word in enumerate(words[1:], 1):
        if word == "--settings":
            if index + 1 >= len(words):
                raise ValueError("Claude --settings requires a JSON object or file")
            source = words[index + 1]
        elif word.startswith("--settings="):
            source = word.split("=", 1)[1]
    settings = {}
    if source is not None:
        try:
            if source.lstrip().startswith("{"):
                settings = json.loads(source)
            else:
                path = Path(source).expanduser()
                directory = launch_parts(command_text)[1]
                if not path.is_absolute():
                    if directory is None:
                        raise ValueError()
                    directory_path = Path(directory).expanduser()
                    if not directory_path.is_absolute():
                        raise ValueError()
                    path = directory_path / path
                settings = json.loads(read_launch_text(path))
                # Keep file settings in the file: inlining exposes private values
                # and changes the base for relative permission rules.
                if isinstance(settings, dict) and all(settings.get(key) is False for key in CLAUDE_UNATTENDED_SETTINGS):
                    return None
                raise ValueError()
            if not isinstance(settings, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise ValueError("Claude --settings must be a readable literal JSON object or file; "
                             f"file settings must set {' and '.join(CLAUDE_UNATTENDED_SETTINGS)} to false; "
                             "use an absolute file path for shell-expanded paths") from None
    return {**settings, **dict.fromkeys(CLAUDE_UNATTENDED_SETTINGS, False)}


def codex_daemon_without_ingress(command_text):
    words = launch_words(command_text)
    return Path(words[0]).name == "codex" and any(
        word == "--remote" or word.startswith("--remote=") for word in words[1:])


def append_brief(command_text, brief):
    """Append a literal prompt only to a direct invocation, optionally after cd."""
    words = launch_words(command_text)
    agent = Path(words[0]).name
    if agent == "claude":
        settings = claude_settings(command_text, words)
        if settings is not None:
            command_text += " --settings " + shlex.quote(json.dumps(settings))
        command_text += f" --disallowedTools {CLAUDE_QUESTION_TOOL}"
        # A launch-flag window stops the autocompact offer dialog, which an unattended
        # session cannot answer; "auto" keeps the window Claude Code tunes for the model.
        if not any(word == CLAUDE_AUTOCOMPACT_OPTION or word.startswith(CLAUDE_AUTOCOMPACT_OPTION + "=")
                   for word in words[1:]):
            command_text += f" {CLAUDE_AUTOCOMPACT_OPTION} auto"
    elif agent == "codex":
        from codex_session_proxy import MEMORY_SETTING
        command_text += " -c " + shlex.quote(f"{MEMORY_SETTING}=false")
    prompt_option = " --prompt-interactive=" if agent == "agy" else " -- "
    return command_text + prompt_option + shlex.quote(brief)


async def send(session, text, submit=True, allow_tabs=False):
    if resume_input(text):
        validate_resume_input(text, tty=await session.async_get_variable("tty"))
    validate_text(text, allow_tabs=allow_tabs)
    if "\n" in text or "\t" in text:
        text = "\x1b[200~" + text + "\x1b[201~"
    try:
        await session.async_send_text(text, suppress_broadcast=True)
    except Exception as error:
        raise SendFailure(session.session_id, "not_attempted", "text send outcome unknown", text_sent=None) from error
    if submit:
        await asyncio.sleep(0.4)
        try:
            await session.async_send_text("\r", suppress_broadcast=True)
        except Exception as error:
            raise SendFailure(session.session_id, "unknown", "Return send failed") from error


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


def codex_daemon_endpoint(home):
    return home / "app-server-control" / "app-server-control.sock"


class DaemonRefusal(ValueError):
    def __init__(self, state, active_threads=None, inventory_error=None):
        self.state = state
        self.active_threads = active_threads
        self.inventory_error = inventory_error
        inventory = ("active threads unknown" if active_threads is None
                     else f"{active_threads} active thread(s)")
        detail = f"; {inventory_error}" if inventory_error else ""
        super().__init__(f"Codex daemon admission {state}; {inventory}{detail}. "
                         "Do not restart or stop it. Wait for it to exit, or select another "
                         "Context Panel account with --account; inspect active threads before any manual restart")


def inspect_codex_daemon(home, directory=None):
    """Check admission; inventory only explains a draining refusal, never permits restart."""
    from websockets.sync.client import unix_connect

    deadline = time.monotonic() + 10
    with unix_connect(str(codex_daemon_endpoint(home)), open_timeout=2, close_timeout=1,
                      max_size=32 * 1024 * 1024) as ws:
        request_id = 0

        def request(method, params):
            nonlocal request_id
            request_id += 1
            ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"daemon {method} timed out")
                try:
                    message = json.loads(ws.recv(timeout=remaining))
                except TimeoutError as error:
                    raise ValueError(f"daemon {method} timed out") from error
                if message.get("id") == request_id:
                    return message

        def result(method, params):
            response = request(method, params)
            if "error" in response or not isinstance(response.get("result"), dict):
                error = response.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                raise ValueError(f"daemon {method} read failed (RPC code {code})")
            return response["result"]

        result("initialize", {"clientInfo": {"name": "supervisor_launch_preflight", "version": "1"}})
        ws.send(json.dumps({"method": "initialized", "params": {}}))
        # The daemon gates thread/resume before parsing the native UUID. An
        # invalid UUID cannot load/create a thread or run its hooks. There is
        # currently no read-only draining flag in the native protocol.
        probe = request("thread/resume", {"threadId": "supervisor-launch-preflight-invalid-id"})
        error = probe.get("error", {})
        if not isinstance(error, dict):
            raise DaemonRefusal("unknown")
        message = error.get("message", "")
        if not isinstance(message, str):
            raise DaemonRefusal("unknown")
        if error.get("code") == -32600 and message.startswith("invalid session id:"):
            if directory is None:
                return None
            try:
                return result("plugin/list", {"cwds": [str(directory)], "marketplaceKinds": ["local"]})
            except Exception:
                return {"inventory_error": "Codex plugin inventory unavailable; daemon admission passed"}
        if "Server is draining" not in message:
            raise DaemonRefusal("unknown")

        # Admission is already refused. A concurrent thread unload, storage
        # failure or slow inventory must not obscure that known draining state.
        active, inventory_error = 0, None
        cursor, seen = None, set()
        try:
            while True:
                page = result("thread/loaded/list", {"cursor": cursor, "limit": 100})
                if not isinstance(page.get("data"), list):
                    raise ValueError("daemon thread inventory unavailable")
                for thread_id in page["data"]:
                    thread = result("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                    state = thread["status"]["type"]
                    if state not in {"active", "idle", "systemError", "notLoaded"}:
                        raise ValueError("daemon thread state unknown")
                    active += state == "active"
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise ValueError("daemon thread pagination unavailable")
                seen.add(cursor)
        except Exception as error:
            active = None
            inventory_error = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise DaemonRefusal("draining", active, inventory_error)


def check_codex_daemon(choice):
    """Start the selected home's daemon natively, then verify its Unix socket."""
    if choice["provider"] != "openai":
        return
    home = Path(choice["env"]["CODEX_HOME"]).expanduser()
    command = f"CODEX_HOME={shlex.quote(str(home))} codex app-server daemon start"
    try:
        started = subprocess.run(
            ["codex", "app-server", "daemon", "start"],
            env={**os.environ, "CODEX_HOME": str(home)},
            capture_output=True, text=True, timeout=LAUNCH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(
            f"Codex daemon start failed for CODEX_HOME={home} ({type(error).__name__}); "
            f"run {command} to diagnose, then retry the launch"
        ) from error
    if started.returncode:
        diagnostic = (started.stderr.strip() or started.stdout.strip())[-2000:]
        raise ValueError(
            f"Codex daemon start failed for CODEX_HOME={home} (exit {started.returncode}): "
            f"{diagnostic or 'no command diagnostic'}; run {command} to diagnose, then retry the launch"
        )
    endpoint = codex_daemon_endpoint(home)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            probe.connect(str(endpoint))
    except OSError as error:
        raise ValueError(
            f"Codex daemon unavailable after start for CODEX_HOME={home} ({type(error).__name__}); "
            f"run {command}, then retry the launch"
        ) from error
    preflight_codex_daemon(choice)


def preflight_codex_daemon(choice, *, exact_source=False):
    """Inspect without startup, with one error boundary for both launch modes."""
    home = Path(choice["env"]["CODEX_HOME"]).expanduser()
    try:
        inspect_plugins = choice.get("require_browser", False)
        directory = choice.get("tool_directory") if inspect_plugins else None
        if inspect_plugins and directory is None:
            raise ValueError("plugin inventory needs the validated launch directory")
        inventory = inspect_codex_daemon(home, directory=directory)
        if inspect_plugins and isinstance(inventory, dict) and not choice.get("tools", {}).get("remote_endpoint_unverified"):
            plugins = agent_tools.daemon_codex_plugins(inventory)
            missing = agent_tools.missing_codex_plugins(plugins, agent_tools.CODEX_BROWSER_PLUGINS)
            choice.setdefault("tools", {"warnings": []})
            if missing or inventory.get("inventory_error"):
                message = inventory.get("inventory_error") or (
                    "daemon-routed Codex is missing enabled bundled plugins: " + ", ".join(missing))
                raise ValueError(message +
                                 "; use local codex --no-daemon -C with the launcher's account-relative override; "
                                 "-c and -p do not forward marketplace settings through --remote")
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        if exact_source and isinstance(error, DaemonRefusal):
            active = "unknown" if error.active_threads is None else str(error.active_threads)
            detail = f"admission {error.state}; active threads: {active}"
            if error.inventory_error:
                detail += "; " + error.inventory_error
        if isinstance(error, TimeoutError):
            detail = "daemon Unix WebSocket transport timed out; admission unknown"
        remedy = ("keep this source account and wait for its daemon to admit resumption; "
                  "do not restart the daemon" if exact_source else
                  "do not restart the daemon automatically")
        if exact_source and isinstance(error, OSError) and error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            from codex_session_proxy import stopped_daemon_remedy
            remedy = stopped_daemon_remedy(home)
        raise ValueError(f"Codex daemon preflight refused for CODEX_HOME={home}: {detail}; "
                         f"no terminal was launched; {remedy}") from error


def scoped_exec(command_text: str, settings: str = "CLAUDE_ACCOUNT_MOVE_ENABLED=1") -> str | None:
    """Scope the marker to a final simple exec without exporting into the shell."""
    lexer = shlex.shlex(command_text, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    try:
        words = []
        for token in lexer:
            if token and all(c in ";&|()" for c in token):
                if token != "&&":
                    return None
                words = []
            else:
                words.append(token)
    except ValueError:
        return None
    if (len(words) < 2 or words[0] != "exec" or words[1].startswith("-")
            or any(token and all(c in ";&|()" for c in token) for token in words)):
        return None
    for match in reversed(list(re.finditer(r"\bexec\s+", command_text))):
        try:
            if shlex.split(command_text[match.start():]) == words:
                return (command_text[:match.end()] + f"env {settings} "
                        + command_text[match.end():])
        except ValueError:
            continue
    return None


def scoped_environment(command_text: str, settings: str) -> str:
    return scoped_exec(command_text, settings) or f"( export {settings}; {command_text}\n)"


def with_account(command_text, choice, brief=None, *, coordinate=False):
    """Prefix one agent invocation with the chosen account's environment."""
    if "\n" in command_text:
        raise ValueError("account selection needs a one-line launch command")
    default_claude = choice["provider"] == "anthropic" and "CLAUDE_CONFIG_DIR" not in choice["env"]
    checked_keys = set(choice["env"]) | set(account_choice.ACCOUNT_VARIABLES.values())
    if choice["provider"] == "openai":
        checked_keys.add(agent_tools.CODEX_SOURCE_ENV)
    if choice["provider"] == "anthropic":
        checked_keys.update({CLAUDE_SUGGESTION_ENV, agent_tools.CHROME_ENV})
        if CHROME_OPT_IN.is_file() and "--chrome" in launch_words(command_text)[1:]:
            raise ValueError("remove --chrome: it starts the built-in Chrome server on this session's own "
                             "login, which cannot reach the Agents browser; the catalog-chrome server "
                             "supplies browser tools on every account")
    google = choice["provider"] == "google"
    if google:
        checked_keys.add("HOME")
    if default_claude:
        checked_keys.add("CLAUDE_CONFIG_DIR")
    conflicts = account_settings(command_text, checked_keys)
    if conflicts:
        remedy = ("remove those settings to use the existing AGY login" if google else
                  "remove those settings and use --account for an explicit override")
        raise ValueError(f"launch file already sets {', '.join(sorted(conflicts))}; "
                         + remedy)
    expected = {"openai": "codex", "anthropic": "claude", "google": "agy"}[choice["provider"]]
    if launch_agent(command_text) != expected:
        raise ValueError(f"{choice['provider']} account selection requires a direct {expected} invocation")
    original_words, directory = launch_parts(command_text)
    original_tokens = shlex.split(command_text)
    if directory is not None:
        original_tokens = original_tokens[original_tokens.index("&&") + 1:]
    prefix_count = len(original_tokens) - len(original_words)
    if brief is not None:
        command_text = append_brief(command_text, brief)
    if coordinate and choice["provider"] == "openai":
        # Preserve assignments and direct invocation prefixes, while the
        # wrapper registers its own PID before exec starts the native client.
        tokens = shlex.split(command_text)
        if directory is not None:
            tokens = tokens[tokens.index("&&") + 1:]
        prefix, words = tokens[:prefix_count], tokens[prefix_count:]
        from codex_session_proxy import managed_argv
        words = managed_argv(Path(choice["env"]["CODEX_HOME"]), words)
        wrapper = ["uv", "run", str(Path(__file__).with_name("codex-session.py")), "launch",
                   "--codex-home", choice["env"]["CODEX_HOME"], "--", *words]
        command_text = shlex.join([*prefix, *wrapper])
        if directory is not None:
            command_text = "cd " + shlex.quote(directory) + " && " + command_text
    settings = " ".join(
        f"{key}={shlex.quote(os.path.expanduser(value))}"
        for key, value in choice["env"].items()
    )
    # export, not env: the account must also reach an agent after `cd dir &&`.
    prefixes = ["unset CLAUDE_CONFIG_DIR"] if default_claude else []
    if settings:
        prefixes.append(f"export {settings}")
    session_settings = "DISCORD_BLUE_SESSION_KIND=spawned"
    if choice["provider"] == "anthropic":
        prefixes.append(f"export {CLAUDE_SUGGESTION_ENV}=false")
        if CHROME_OPT_IN.is_file():
            prefixes.append(f"export {agent_tools.CHROME_ENV}=0")
        session_settings = "CLAUDE_ACCOUNT_MOVE_ENABLED=1 " + session_settings
    command_text = scoped_environment(command_text, session_settings)
    return " && ".join([*prefixes, command_text])


def git_trust_metadata(path):
    """Read bounded regular Git metadata, as Codex's trust-root resolver does."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("unusable Git trust metadata")
    return path.read_text(encoding="utf-8").strip()


def git_trust_pointer(marker):
    text = git_trust_metadata(marker)
    if not text.startswith("gitdir:") or not text[7:].strip():
        raise ValueError("invalid Git directory pointer")
    return Path(os.path.normpath(marker.parent / text[7:].strip()))


def codex_git_trust_root(directory):
    """Resolve the main checkout, validating linked-worktree backlinks read-only.

    Mirrors openai/codex codex-rs/git-utils/src/trust.rs: an arbitrary .git
    pointer (including a submodule) does not inherit another checkout's trust.
    """
    try:
        for root in (directory, *directory.parents):
            marker = root / ".git"
            if marker.is_dir():
                if (marker / "HEAD").exists():
                    return root
                continue
            if not marker.exists():
                continue
            git_dir = git_trust_pointer(marker)
            if git_dir.is_symlink() or not git_dir.is_dir():
                return None
            canonical = git_dir.resolve()
            if canonical.parent.name != "worktrees":
                return None
            common = canonical.parent.parent
            backlink = canonical / git_trust_metadata(canonical / "gitdir")
            linked_common = canonical / git_trust_metadata(canonical / "commondir")
            if (backlink.name != ".git" or backlink.parent.resolve() != root.resolve()
                    or linked_common.resolve() != common):
                return None
            main_root = git_dir.parent.parent.parent
            main_marker = main_root / ".git"
            main_git_dir = main_marker if main_marker.is_dir() else git_trust_pointer(main_marker)
            return main_root if main_git_dir.resolve() == common else None
    except (OSError, ValueError):
        return None
    return None


def check_claude_tools(command_text, choice, without_browser=False, probes=None, require_browser=False):
    """Check the pinned route; browser briefs require it, ordinary work gets warnings."""
    if choice["provider"] != "anthropic":
        return
    # Validate account setters and conflicting flags before spawning a server.
    with_account(command_text, choice)
    if not CHROME_OPT_IN.is_file():
        if require_browser:
            raise ValueError("required browser has no catalog Chrome setup; see README: Chrome across Claude account homes")
        return
    home_value = choice["env"].get("CLAUDE_CONFIG_DIR")
    home = Path(home_value).expanduser() if home_value else None
    directory = launch_parts(command_text)[1]
    if require_browser and (directory is None or not Path(directory).expanduser().is_absolute()):
        raise ValueError("required Claude browser needs cd /absolute/repository && exec claude")
    with preparation_read():
        directory = (Path(directory).expanduser() if directory else Path.cwd()).resolve()
    # The launch exports CLAUDE_CODE_ENABLE_CFC=0, so the built-in server stays off.
    tools = {"warnings": agent_tools.claude_problems(
        home, directory, {agent_tools.CHROME_ENV: "0"}, check_computer_use=False)}
    if without_browser:
        tools["browsers"] = "not checked (--without-browser)"
    elif tools["warnings"]:
        # Static Chrome failures already explain why this route cannot work.
        # Do not spawn a server or repeat the same diagnostic.
        if require_browser:
            raise ValueError("Chrome check failed: " + "; ".join(tools["warnings"]) +
                             "; no terminal was launched")
    else:
        try:
            server = agent_tools.chrome_server(home)
            key = json.dumps(server, sort_keys=True)
            probes = {} if probes is None else probes
            if key not in probes:
                try:
                    probes[key] = agent_tools.connected_browsers(server)
                except ValueError as error:
                    probes[key] = error
            if isinstance(probes[key], ValueError):
                raise probes[key]
            if not probes[key]:
                raise ValueError("no Chrome browser is connected to the pinned account; "
                                 "ask Chris to open the Agents profile")
            tools["browsers"] = len(probes[key])
        except ValueError as error:
            if require_browser:
                raise ValueError(f"Chrome check failed: {error}; no terminal was launched") from None
            tools["warnings"].append(str(error))
    choice["tools"] = tools


def prepare_codex_tools(command_text, choice, required=False, *, gui=False):
    """Use native local loading; remote sessions keep the daemon's own inventory."""
    if choice["provider"] != "openai":
        return command_text
    choice["require_browser"] = required
    words = launch_words(command_text)
    with preparation_read():
        choice["tool_directory"] = str(codex_directory(words).resolve())
    endpoints = [words[index + 1] if word == "--remote" and index + 1 < len(words)
                 else word.partition("=")[2] for index, word in enumerate(words)
                 if word == "--remote" or word.startswith("--remote=")]
    remote = bool(endpoints)
    if remote and endpoints != ["unix://"]:
        warning = "explicit remote endpoint's plugins are unverified; use local codex -C for a browser brief"
        if required:
            raise ValueError(warning)
        choice["tools"] = {"warnings": [warning], "remote_endpoint_unverified": True}
    must_work = required or gui
    if remote:
        return command_text
    local_flag = "--no-daemon" in words
    if not must_work and not CHROME_OPT_IN.is_file():
        if local_flag:
            choice["tools"] = {"warnings": [], "route": "local"}
        return command_text
    needed = ((agent_tools.CODEX_BROWSER_PLUGINS if required else set())
              | ({agent_tools.COMPUTER_USE} if gui else set())) if must_work else None
    home = Path(choice["env"]["CODEX_HOME"]).expanduser()
    try:
        market = gui_launch.marketplace(home, Path.home() / ".codex")
    except ValueError as error:
        if must_work:
            raise
        route = "local" if local_flag else "daemon"
        detail = ("source launch keeps its explicit local configuration" if local_flag
                  else "source launch falls back to the selected-home daemon")
        choice["tools"] = {"warnings": [str(error) + "; " + detail], "route": route}
        return command_text
    try:
        missing = agent_tools.missing_codex_plugins(
            agent_tools.codex_plugins(home, Path(market["path"]), codex_directory(words)), needed)
    except ValueError as error:
        if must_work:
            raise
        # The validated local marketplace route still works for source tasks;
        # an optional discovery failure must not silently switch transports.
        warnings = [str(error)]
    else:
        warnings = ["Codex bundled plugins missing or disabled: " + ", ".join(missing)] if missing else []
    if must_work and warnings:
        raise ValueError(warnings[0] + f"; install/enable them through Codex in CODEX_HOME={shlex.quote(str(home))} "
                         "before retrying this tool launch")
    choice["tools"] = {"warnings": warnings, "marketplace": market, "route": "local"}
    choice["env"][agent_tools.CODEX_SOURCE_ENV] = market["path"]
    override = "marketplaces.openai-bundled.source=" + json.dumps(market["path"])
    return command_text + ("" if local_flag else " --no-daemon") + " -c " + shlex.quote(override)


def codex_directory(words):
    """Parse the one literal workspace used by trust and plugin discovery."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    parser.add_argument("-C", "--cd", action="append")
    try:
        options, _ = parser.parse_known_args(words[1:])
    except argparse.ArgumentError as error:
        raise ValueError("Codex launches require -C /absolute/repository") from error
    if not options.cd or len(options.cd) != 1:
        raise ValueError("Codex launches require exactly one -C /absolute/repository (or --cd)")
    return Path(options.cd[0])


class CodexProjectEntry(NamedTuple):
    target: Path
    project: object
    matched: bool


def codex_project_entry(directory, config_path):
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError) as error:
        raise ValueError(f"Cannot verify Codex folder trust in {config_path} "
                         f"({type(error).__name__}); inspect that config before retrying") from None
    projects = config.get("projects", {})
    trust_root = codex_git_trust_root(directory)
    target = trust_root or directory
    project = {}
    matched = False
    if isinstance(projects, dict):
        for path in (directory, trust_root):
            if path is None:
                continue
            for key in (str(path.resolve()), str(path)):
                if key in projects:
                    target, project = Path(key), projects[key]
                    matched = True
                    break
            else:
                continue
            break
    return CodexProjectEntry(target, project, matched)


def codex_folder_trusted(project):
    return isinstance(project, dict) and project.get("trust_level") == "trusted"


def check_codex_project(command_text, choice):
    """Require an explicit, already-trusted directory in the selected Codex home."""
    if choice["provider"] != "openai":
        if launch_agent(command_text) == "codex":
            raise ValueError("Codex launches require --account-provider openai")
        return
    words, _ = launch_parts(command_text)
    if Path(words[0]).name != "codex":
        raise ValueError("openai account selection requires a direct Codex invocation")
    directory = codex_directory(words)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("Codex -C/--cd must name an existing absolute repository directory")
    home = Path(choice["env"]["CODEX_HOME"]).expanduser()
    config_path = home / "config.toml"
    target, project, matched = codex_project_entry(directory, config_path)
    if not codex_folder_trusted(project):
        command = shlex.join(["codex", "-C", str(directory)])
        recovery = (
            f"review the existing projects entry for {target} yourself and deliberately set "
            "its trust_level to trusted only if appropriate, then retry; Codex may not "
            "show another folder-trust prompt"
            if matched else
            f"open CODEX_HOME={shlex.quote(str(home))} {command} interactively and review "
            "the folder-trust prompt, then retry"
        )
        raise ValueError(
            f"Codex folder is not trusted: {directory} (trust target: {target}) in {config_path}; "
            f"{recovery}. An unrelated trusted parent is insufficient; "
            "the launcher never edits config.toml"
        )


def claude_trust_writes(command_text, choice, config_path):
    """Plan folder trust in every configured Claude home, so no launch stops at the dialog."""
    if choice["provider"] != "anthropic":
        return []
    directory = launch_parts(command_text)[1]
    if directory is None:
        raise ValueError("Claude launches require cd /absolute/repository && exec claude")
    def homes():
        return [account for account in account_choice.load_config(config_path)["accounts"]
                if account["provider"] == "anthropic"]
    return claude_trust.plan(directory, choice, homes, codex_git_trust_root)


def prepare_resume(args):
    """Build only the maintained exact-source helper, never caller shell text."""
    import session_record
    from codex_session_proxy import source_endpoint

    session = args.resume_session_id
    if (args.account_provider != "openai" or not getattr(args, "ledger", None)
            or getattr(args, "command_file", None) or getattr(args, "brief_file", None)
            or getattr(args, "computer_use", None)
            or getattr(args, "require_browser", False) or getattr(args, "without_browser", False)):
        raise ValueError("resume needs --account-provider openai and --ledger, without launch/brief files or browser/Computer Use flags")
    with preparation_read():
        entries = [entry for entry in session_record.load_ledger(args.ledger) if entry["session_id"] == session]
    if len(entries) != 1 or entries[0].get("supervisor_owned") is not True or entries[0]["harness"] != "codex":
        raise ValueError("resume source is not uniquely Supervisor-owned in the ledger")
    entry = entries[0]
    if not isinstance(entry.get("codex_home"), str):
        raise ValueError("resume ledger needs the source's absolute codex_home")
    home = Path(entry["codex_home"]).expanduser()
    if not home.is_absolute():
        raise ValueError("resume ledger needs the source's absolute codex_home")
    with preparation_read():
        home = home.resolve(strict=True)
        transcript = Path(entry["transcript"])
        if not any(transcript.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")):
            raise ValueError("resume transcript is outside the source account")
        source_endpoint(session, home, transcript=transcript)
        config = account_choice.load_config(args.account_config)
        accounts = [account for account in config["accounts"] if account["provider"] == "openai"
                    and Path(account["env"]["CODEX_HOME"]).expanduser().resolve() == home]
    if len(accounts) != 1 or (args.account is not None and args.account != accounts[0]["name"]):
        raise ValueError("resume requires the source's unique configured account; account switching is unsupported")
    choice = account_choice.select("openai", args.account_config, accounts[0]["name"])
    with preparation_read():
        if Path(choice["env"]["CODEX_HOME"]).expanduser().resolve() != home:
            raise ValueError("selected resume account differs from the source")
    # Read the existing daemon only. Recovery never starts or restarts one.
    preflight_codex_daemon(choice, exact_source=True)
    account_choice.prepare_launch(choice)
    choice["resume_session_id"] = session
    invocation = ["uv", "run", str(Path(__file__).with_name("codex-session.py").resolve()),
                  "resume", "--session-id", session, "--codex-home", str(home)]
    settings = [f"{key}={os.path.expanduser(value)}" for key, value in choice["env"].items()]
    settings.append("DISCORD_BLUE_SESSION_KIND=spawned")
    return [shlex.join(["exec", "env", *settings, *invocation])], [choice]


class PreparationExpired(BaseException):
    pass


def expire_preparation(signum: int, frame: object) -> None:
    raise PreparationExpired


@contextmanager
def preparation_read() -> Iterator[None]:
    previous_handler = signal.signal(signal.SIGALRM, expire_preparation)
    started = time.monotonic()
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, min(LAUNCH_TIMEOUT, previous_timer[0]) if previous_timer[0] else LAUNCH_TIMEOUT)
    try:
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    finally:
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def read_launch_text(path: Path) -> str:
    with preparation_read():
        return path.read_text(encoding="utf-8")


def check_launch_issue(brief: str) -> None:
    anchors = re.findall(r"(?mi)^\s*(?:[-*] )?(?:\*\*)?(Read-only anchor|Anchor(?: and claim)?):(?:\*\*)?\s*"
                         r"(https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*)(?=$|[#\s).,;])", brief)
    if not anchors:
        raise ValueError("Brief needs Anchor: <issue URL> for execution, or Read-only anchor: <issue URL> for work that will not claim")
    resume = re.search(r"(?mi)^Resume from:\s*([1-9]\d*)\s*$", brief)
    helper = Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py"
    for mode, anchor in dict.fromkeys(anchors):
        repo = "/".join(anchor.split("/")[3:5])
        argv = ["uv", "run", str(helper), "--repo", repo, "show", anchor, "--full", "--launch-check"]
        if mode.casefold() == "read-only anchor":
            argv.append("--read-only")
        if resume:
            argv.extend(["--resume-from", resume[1]])
        try:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
            payload = json.loads(result.stdout)
            check = payload["issue"]["launch_check"] if not result.returncode and payload.get("ok") else {}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Launch state unavailable for {anchor}; read its full status before dispatch") from error
        if not check.get("allowed"):
            queue = check.get("queue") or {}
            raise ValueError(f"Launch refused for {anchor}: {check.get('status', 'unknown')}, "
                             f"{queue.get('reason', 'launch state unavailable')}; "
                             "keep the existing hold or owner; dispatch only after the relevant fact changes")


def prepare_launches(args: argparse.Namespace) -> tuple[list[str], list[dict | None]]:
    try:
        return prepare_launch_batch(args)
    except PreparationExpired:
        progress = {"phase": "prepare_launches", "window_id": getattr(args, "window_id", None),
                    "backend": getattr(args, "backend", None),
                    "tmux_socket": getattr(args, "tmux_socket", None),
                    "resume_session_id": getattr(args, "resume_session_id", None),
                    "readback_required": True}
        for name in ("command_file", "brief_file", "ledger"):
            paths = getattr(args, name, None)
            progress[name] = [str(path) for path in paths] if isinstance(paths, list) else str(paths) if paths else None
        error = LaunchFailure([], progress, ValueError(
            f"launch preparation unavailable after {LAUNCH_TIMEOUT:g}s; a pending macOS file-access prompt "
            "or missing disk access may be blocking worktree-volume reads"))
        error.detail.update(status="preparation_unavailable", next_action=(
            "Preserve this attempt and inspect its files, target terminals and any pending macOS file-access prompt; "
            "do not relaunch, change permissions or restart the server automatically."))
        raise error from None


def validate_computer_use_launch(args: argparse.Namespace, commands: list[str], briefs: list[str]) -> None:
    mode = getattr(args, "computer_use", None)
    for command, brief in zip(commands, briefs):
        app = gui_launch.declared_app(brief)
        if mode == "required" and not app:
            raise ValueError("--computer-use required needs GUI task: <app-bundle-id> as the brief's first nonblank line")
        if mode == "unused" and app:
            raise ValueError("--computer-use unused conflicts with GUI task; use --computer-use required for screen work")
        if app and mode != "required":
            raise ValueError("GUI task needs --computer-use required")
        if mode:
            continue
        task = " ".join(brief.split()).replace(" ".join(brief_rules_block().split()), "")
        if re.search(r"\bcomputer[\s-]+use\b", task, re.IGNORECASE) and launch_agent(command) == "codex":
            raise ValueError("Computer Use brief needs --computer-use required and a first-line GUI task: <app-bundle-id> "
                             "for local Codex with the selected home's plugin; "
                             "use --computer-use unused for source-only mentions")


def prepare_launch_batch(args: argparse.Namespace) -> tuple[list[str], list[dict | None]]:
    """Validate the full batch and prepare accounts before creating terminals."""
    if getattr(args, "resume_session_id", None):
        return prepare_resume(args)
    if getattr(args, "ledger", None):
        raise ValueError("new --ledger requires --resume-session-id")
    if not getattr(args, "command_file", None):
        raise ValueError("new launches require --command-file or --resume-session-id")
    if getattr(args, "require_browser", False) and getattr(args, "without_browser", False):
        raise ValueError("--require-browser and --without-browser cannot be used together")
    if (getattr(args, "require_browser", False)
            and getattr(args, "account_provider", None) not in {"anthropic", "openai"}):
        raise ValueError("--require-browser needs --account-provider anthropic or openai")
    files = args.command_file if isinstance(args.command_file, list) else [args.command_file]
    command_texts = [read_launch_text(path).rstrip("\n") for path in files]
    for text in command_texts:
        validate_text(text)
    brief_files = getattr(args, "brief_file", None)
    if not brief_files:
        raise ValueError("new launches require --brief-file; put the prompt in that file")
    brief_files = brief_files if isinstance(brief_files, list) else [brief_files]
    if len(brief_files) != len(files):
        raise ValueError("provide one --brief-file per --command-file, in launch order")
    briefs = [read_launch_text(path) for path in brief_files]
    for brief in briefs:
        if not brief.strip():
            raise ValueError("brief must be nonempty")
        validate_text(brief, allow_tabs=True)
        validate_brief_rules(brief)
    for brief in briefs:
        check_launch_issue(brief)
    validate_computer_use_launch(args, command_texts, briefs)
    apps = [gui_launch.declared_app(brief) for brief in briefs]
    if any(apps):
        if len(files) != 1 or getattr(args, "account_provider", None) != "openai":
            raise ValueError("GUI tasks require one local Codex launch with --account-provider openai")
        words = launch_words(command_texts[0])
        if (Path(words[0]).name != "codex" or "--" in words
                or any(word == "--remote" or word.startswith("--remote=") for word in words)):
            raise ValueError("GUI tasks need local Codex; remove --remote and any prompt from the run file; put the prompt in --brief-file")
        gui_launch.require_unlocked()
        briefs[0] += ("\n\nGUI safety: Use only the named app " + apps[0] +
                      ". Never target Chris's terminals or other windows. Check screen lock "
                      "before each GUI action and stop if locked or unknown; never unlock. "
                      "Use app-scoped computer-use operations, never global keyboard/focus fallbacks. "
                      "Missing permissions or app approvals require Chris; do not change them.\n")
    choices = [None] * len(files)
    if getattr(args, "account_provider", None):
        if len(files) == 1:
            choices = [account_choice.select(args.account_provider, args.account_config, args.account)]
        else:
            choices = account_choice.select_batch(
                args.account_provider, args.account_config, args.account, count=len(files)
            )
        trust_writes = []
        probes = {}
        # Validate command/account/trust inputs for the whole batch first.
        # Live preflight follows; its reusable marketplace links are persistent.
        for text, choice, brief in zip(command_texts, choices, briefs):
            with preparation_read():
                with_account(text, choice, brief)
                check_codex_project(text, choice)
                trust_writes += claude_trust_writes(text, choice, args.account_config)
        for text, choice in zip(command_texts, choices):
            check_claude_tools(text, choice, getattr(args, "without_browser", False), probes,
                               getattr(args, "require_browser", False))
        command_texts = [prepare_codex_tools(text, choice, getattr(args, "require_browser", False), gui=bool(apps[index]))
                         for index, (text, choice) in enumerate(zip(command_texts, choices))]
        claude_trust.apply(trust_writes)
        if apps[0]:
            home = Path(choices[0]["env"]["CODEX_HOME"]).expanduser()
            if not home.is_absolute() or not home.is_dir():
                raise ValueError("GUI launch needs the selected account's existing absolute CODEX_HOME")
            source_path = home / gui_launch.BUNDLED_MARKETPLACE
            override = "marketplaces.openai-bundled.source=" + json.dumps(str(source_path))
            if choices[0].get("tools", {}).get("route") != "local":
                command_texts[0] += " --no-daemon -c " + shlex.quote(override)
        command_texts = [with_account(text, choice, brief,
                                     coordinate=not app and choice.get("tools", {}).get("route") != "local")
                         for text, choice, brief, app in zip(command_texts, choices, briefs, apps)]
    elif getattr(args, "account", None):
        raise ValueError("--account needs --account-provider")
    elif any(account_settings(text, account_choice.ACCOUNT_VARIABLES.values())
             for text in command_texts):
        raise ValueError("account settings need --account-provider; remove them from the launch file "
                         "and use --account for an explicit override")
    elif any(codex_daemon_without_ingress(text) for text in command_texts):
        raise ValueError("Codex --remote launches need --account-provider openai, so the launcher's "
                         "ingress can start the thread with memory generation off")
    else:
        command_texts = [scoped_environment(append_brief(text, brief), "DISCORD_BLUE_SESSION_KIND=spawned")
                         for text, brief in zip(command_texts, briefs)]
    if choices[0]:
        for choice in choices:
            if choice.get("tools", {}).get("route") != "local":
                check_codex_daemon(choice)
        account_choice.prepare_launch(choices[0])
    if apps[0]:
        reservation = gui_launch.reserve(apps[0])
        try:
            market = choices[0].get("tools", {}).get("marketplace") or gui_launch.marketplace(home, Path.home() / ".codex")
            choices[0]["gui"] = {**reservation, "marketplace": market,
                                  "override": override, "screen": "unlocked"}
            command_file = gui_launch.stage_command(reservation, command_texts[0])
            command_texts[0] = shlex.join([
                sys.executable, str(Path(gui_launch.__file__).resolve()), "run",
                "--state", reservation["state_path"], "--token", reservation["token"],
                "--command-file", str(command_file),
            ])
        except Exception as error:
            try:
                gui_launch.release(Path(reservation["state_path"]), reservation["token"])
            except (OSError, ValueError) as rollback:
                reason = str(error) if isinstance(error, ValueError) else type(error).__name__
                raise ValueError(f"GUI preparation failed: {reason}; reservation {reservation['token']} "
                                 f"retained after rollback failed ({type(rollback).__name__}); "
                                 "inspect shared GUI state before release") from error
            raise
    return command_texts, choices


def resume_input(text):
    return "\n" not in text and re.match(r"^\s*/resume(?:\s|$)", text, re.IGNORECASE)


def validate_resume_input(text, *, tty=None):
    if resume_input(text):
        if isinstance(tty, str) and tty:
            from session_liveness import inventory
            commands = [command.split() for _, terminal, command in inventory()
                        if terminal == tty.removeprefix("/dev/")]
            # A positively identified Claude-only target keeps its own resume
            # command. Unknown or mixed targets retain the conservative route.
            claude = any(argv and (Path(argv[0]).name in {"claude", "claude.js"} or
                         len(argv) > 1 and Path(argv[0]).name in {"node", "nodejs", "bun"} and Path(argv[1]).name in {"claude", "claude.js"}) for argv in commands)
            codex = any(Path(arg).name.startswith("codex") for argv in commands for arg in argv)
            if claude and not codex:
                return
        raise ValueError("Run codex-session.py resume --session-id <native-id> --codex-home <source-home> instead; TUI resume bypasses ownership coordination")


def source_launch_metadata(result, choice):
    if not choice or choice.get("provider") != "openai":
        return result
    home = choice["env"]["CODEX_HOME"]
    result["codex_home"] = str(Path(home).expanduser().resolve())
    if choice.get("resume_session_id"):
        result["native_session_id"] = choice["resume_session_id"]
    try:
        from session_liveness import harness_arguments, inventory
        github = str(Path(__file__).resolve().parents[2] / "github/scripts")
        if github not in sys.path:
            sys.path.append(github)
        import github_plan_closure
        tty = result.get("tty")
        if not isinstance(tty, str) or not tty:
            raise ValueError("launch TTY is unavailable")
        if choice.get("resume_session_id"):
            session = choice["resume_session_id"]
            rows = [row for row in github_plan_closure.log_rows() if row.get("session_id") == session]
            if len(rows) != 1:
                raise ValueError("resumed native generation is not unique; read codex-session.py status")
            generation = rows[0].get("resumed_generation")
            if (not isinstance(generation, dict) or not isinstance(generation.get("tty"), str)
                    or type(generation.get("pid")) is not int or generation["pid"] <= 0
                    or not isinstance(generation.get("launch_generation"), str) or not generation["launch_generation"]
                    or generation.get("codex_home") != result["codex_home"]
                    or generation["tty"].removeprefix("/dev/") != tty.removeprefix("/dev/")
                    or generation.get("pid") != generation.get("launch_pid")
                    or github_plan_closure.process_started_at(generation.get("pid")).isoformat()
                    != generation.get("launch_started_at")):
                raise ValueError("resumed generation does not match this terminal")
            result.update(generation)
            return result
        candidates = []
        for pid, terminal, command in inventory():
            argv = harness_arguments(command)
            if terminal == tty.removeprefix("/dev/") and argv and Path(argv[0]).name.casefold().startswith("codex"):
                candidates.append((pid, command.split()[0] == argv[0]))
        try:
            registrations = github_plan_closure.log_rows()
        except FileNotFoundError:
            registrations = []
        proxies = {r.get("launch_pid"): r for r in registrations
                   if r.get("record_kind") == "managed_proxy" and r.get("codex_home") == result["codex_home"]}
        matches = [pid for pid, _ in candidates if pid in proxies and
                   github_plan_closure.process_started_at(pid).isoformat() == proxies[pid].get("launch_started_at")]
        if matches:
            if len(matches) != 1 or not proxies[matches[0]].get("launch_generation"):
                raise ValueError("guarded launch generation is not unique on the launch TTY")
            result.update({key: proxies[matches[0]][key] for key in (
                "codex_home", "launch_pid", "launch_generation", "launch_started_at")})
        else:
            matches = [pid for pid, direct in candidates if direct]
            if len(matches) == 1:
                result.update(github_plan_closure.launch_metadata(matches[0], result["session_id"], home))
        if len(matches) != 1:
            raise ValueError("native client PID is not unique on the launch TTY")
        result["pid"] = matches[0]
    except (OSError, ValueError, ImportError, subprocess.SubprocessError):
        result["launch_identity_unavailable"] = True
    return result


def cancel_unsubmitted_gui(choice, progress):
    if choice and "gui" in choice and progress["submission"] == "not_attempted":
        gui = choice["gui"]
        try:
            gui_launch.release(Path(gui["state_path"]), gui["token"])
            progress["gui_reservation"] = "released_unsubmitted"
        except (OSError, ValueError) as error:
            progress["gui_reservation"] = "retained; inspect before release: " + str(error)


async def close_session(app, session_id, ledger):
    """Close one owned, unsplit tab only with current idle-shell/input proof."""
    import session_liveness
    import session_record
    import iterm2

    entry = session_record.terminal_entry(ledger, "iterm_session_id", session_id)

    async def verify():
        await app.async_refresh()
        session = select_session(app, session_id)
        tabs = [(w, t) for w in app.terminal_windows for t in w.tabs
                if any(s.session_id == session_id for s in t.sessions)]
        if len(tabs) != 1 or len(tabs[0][1].sessions) != 1:
            raise ValueError("split or ambiguous tab; tab kept")
        window, tab = tabs[0]
        for key, value in (("window_id", window.window_id), ("tab_id", tab.tab_id)):
            if key in entry and entry[key] != value:
                raise ValueError("terminal location differs from launch ledger")
        live_tty = await session.async_get_variable("tty")
        expected_tty = entry.get("tty")
        if not isinstance(live_tty, str) or not isinstance(expected_tty, str):
            raise ValueError("terminal TTY is missing or invalid")
        tty = live_tty.removeprefix("/dev/")
        if not tty or tty != expected_tty.removeprefix("/dev/"):
            raise ValueError("terminal TTY differs from launch ledger")
        rows = session_liveness.inventory()
        jobs = [row for row in rows if row[1] == tty]
        session_liveness.verify_process_exit(entry, rows=rows, require_tty=True, scope="terminal")
        prompt = await iterm2.async_get_last_prompt(app.connection, session_id)
        if prompt is None or prompt.state != iterm2.PromptState.EDITING:
            raise ValueError("empty input cannot be proved; shell integration must report an editing prompt")
        if prompt.command != "":
            raise ValueError("input line is not empty; tab kept")
        return session, (window.window_id, tab.tab_id, tty, jobs, prompt.unique_id)

    _, before = await verify()
    closing_session, after = await verify()
    if after != before:
        raise ValueError("terminal state changed during verification; tab kept")
    await closing_session.async_close(force=False)
    return {"session_id": session_id, "close_requested": True, "readback_required": True}


async def operate(app, args):
    if args.command == "close":
        return await close_session(app, args.session_id, args.ledger)
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
                "input_line": iterm_input_line(screen),
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
        command_texts, choices = prepare_launches(args)
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
                await submit_iterm_launch(app, session, command_text, prompt_window=startup_prompt_window(choice))
                progress.update(submission="submitted", phase="startup_observed")
            except Exception as error:
                cancel_unsubmitted_gui(choice, progress)
                if isinstance(error, StartupFailure):
                    progress.update(submission="submitted", phase="verify_startup")
                raise LaunchFailure(results, progress, error) from error
            result = {"window_id": windows[0].window_id,
                      "tab_id": tab.tab_id, "session_id": session.session_id}
            if choice:
                result["account"] = account_choice.public(choice)
            if choice and choice.get("provider") == "openai":
                try:
                    result["tty"] = await session.async_get_variable("tty")
                except (AttributeError, OSError, ValueError):
                    result["launch_identity_unavailable"] = True
                source_launch_metadata(result, choice)
            results.append(result)
        return results[0] if len(results) == 1 else results
    if args.command == "window" and previous_tab:
        await previous_tab.async_select()
    return result


def view_state_path():
    return Path(os.environ.get("CODE_HOME", str(Path.home() / ".code"))) / "skill-data" / "supervisor-view.json"


def remember_view_window(path, identity):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump({"iterm_window_id": identity}, handle)
            handle.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


async def open_tmux_view(connection, terminal, container, *, window_id=None, window_number=None, state_path=None):
    """Create a gateway with its command; never send input to an iTerm session."""
    import iterm2

    command = terminal.view_command(container)
    # Serialize hierarchy lookup and creation across simultaneous launchers.
    # The gateway's creation-time command also identifies a pending attachment.
    # iTerm's create-window RPC does not complete inside an API transaction.
    with terminal.view_lock(container) as acquired:
        if not acquired:
            return {"status": "pending_elsewhere", "readback_required": True,
                    "next_action": "Another opener holds the view lock. Inspect iTerm, then use view --window-id if needed."}
        app = await iterm2.async_get_app(connection)
        await app.async_refresh()
        state_path = state_path or view_state_path()
        if window_number is not None:
            matches = [w for w in app.terminal_windows if w.window_number == window_number]
            if len(matches) != 1:
                raise ValueError("chosen iTerm window number does not identify one window")
            window_id = matches[0].window_id
        if window_id is None and state_path.exists():
            try:
                window_id = json.loads(state_path.read_text(encoding="utf-8"))["iterm_window_id"]
                if not isinstance(window_id, str) or not window_id:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, OSError):
                raise ValueError("saved iTerm window is unreadable; choose again with --iterm-window-number or --iterm-window-id") from None
        chosen = None
        if window_id is not None:
            matches = [w for w in app.terminal_windows if w.window_id == window_id]
            if len(matches) != 1:
                raise ValueError("chosen or remembered iTerm window is gone; choose another with --iterm-window-number or --iterm-window-id")
            chosen = matches[0]
        clients = terminal.control_clients(container)
        candidates = sessions(app) + app.buried_sessions
        for session in candidates:
            tty = await session.async_get_variable("tty")
            if any(tty and tty.removeprefix("/dev/") == client.removeprefix("/dev/")
                   for client in clients):
                return {"status": "attached", "session_id": session.session_id}
            profile = await session.async_get_profile()
            if terminal.view_command_matches(profile.command, command):
                return {"status": "opening", "session_id": session.session_id,
                        "readback_required": True}

        preference = await iterm2.preferences.async_get_preference(
            connection, iterm2.preferences.PreferenceKey.OPEN_TMUX_WINDOWS_IN)
        # The SDK has no named setter for Initial Text; its public constructor
        # accepts creation-time profile properties. Suppress inherited input.
        profile = iterm2.LocalWriteOnlyProfile({"Initial Text": ""})
        profile.set_send_code_when_idle(False)
        profile.set_triggers([])
        profile.set_name("Supervised agents")
        profile.set_use_custom_command(iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
        profile.set_command(command)
        profile.set_use_custom_window_title(True)
        profile.set_custom_window_title("Supervised agents")
        # These overrides belong only to this newly created gateway. They never
        # save a profile or change preferences, and its exit leaves tmux running.
        profile.set_close_sessions_on_end(True)
        memory_failed = False
        if chosen is None:
            window = await iterm2.Window.async_create(connection, profile_customizations=profile)
            created = window is not None
        else:
            window = chosen
            tab = await chosen.async_create_tab(profile_customizations=profile, select=False)
            created = tab is not None
        if created:
            try:
                remember_view_window(state_path, window.window_id)
            except OSError:
                memory_failed = True
        result = {"status": "opening" if created else "unavailable",
                  "window_id": window.window_id if window else None,
                  "readback_required": True, "restore_windows_as": preference,
                  "focus_warning": "Explicit view requested: iTerm may move focus when restoring tmux tabs. Use Detach before background launches if this is disruptive.",
                  "close_view": "Shell > tmux > Detach, or close only the gateway. Cmd-W on an agent tab kills that agent."}
        if memory_failed:
            result["window_memory_warning"] = "View created, but its window id could not be saved; use --iterm-window-id with this receipt's window_id."
        if not created:
            result["next_action"] = "Inspect iTerm, then use view --window-id; do not relaunch agents."
        if type(preference) is not int or preference != 2:
            result["warning"] = (
                'iTerm Settings > General > tmux > "When attaching, restore windows as" '
                'is not "tabs in the attaching window". The view may use other windows; '
                'only the Director changes this setting.')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("iterm", "tmux"), default="iterm")
    parser.add_argument("--tmux-socket", default="supervisor",
                        help="dedicated tmux -L server; used only with --backend tmux")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    commands.add_parser("window")
    view = commands.add_parser("view", help="open the Supervisor tmux view after unlocking")
    view.add_argument("--window-id", required=True)
    target = view.add_mutually_exclusive_group()
    target.add_argument("--iterm-window-id", help="choose and remember an exact stable iTerm window id")
    target.add_argument("--iterm-window-number", type=int, help="resolve the current iTerm window number once, then remember its stable id")
    close = commands.add_parser("close")
    close.add_argument("--session-id", required=True)
    close.add_argument("--ledger", type=Path, required=True,
                       help="private Supervisor launch ledger; Director-owned entries refuse")
    new = commands.add_parser("new")
    new.add_argument("--window-id", required=True)
    new.add_argument("--command-file", type=Path, action="append",
                     help="one agent invocation and settings, without its prompt; repeat for a batch")
    new.add_argument("--brief-file", type=Path, action="append",
                     help="nonempty prompt to append as one argument; one per command file, in launch order")
    new.add_argument("--resume-session-id", help="resume one exact Supervisor-owned Codex source from --ledger")
    new.add_argument("--ledger", type=Path, help="private source ledger for --resume-session-id")
    new.add_argument(
        "--account-provider",
        required=True,
        choices=account_choice.PROVIDERS,
        help="launch on Context Panel's use-next account or ranked batch order",
    )
    new.add_argument("--without-browser", action="store_true",
                     help="launch Claude without the connected-browser check, for briefs that need no browser")
    new.add_argument("--require-browser", action="store_true",
                     help="require a working browser route before launching a browser brief")
    new.add_argument("--computer-use", choices=("required", "unused"),
                     help="required: use the app-scoped local Codex route and check its plugin; "
                          "unused: source-only brief mentions Computer Use but uses no screen tools")
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
    if args.command == "view":
        if args.backend != "tmux":
            parser.error("view requires --backend tmux")
        if args.tmux_socket != "supervisor":
            print(json.dumps({"status": "skipped", "reason": "not the Supervisor tmux server"}))
            return
        import iterm2
        import tmux_terminal

        terminal = tmux_terminal.Terminal(args.tmux_socket)
        try:
            terminal.verify_container(args.window_id)
            terminal.require_readonly_view(args.window_id)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, f"refused: {error}\n")

        async def view_connected(connection):
            try:
                print(json.dumps(await open_tmux_view(connection, terminal, args.window_id,
                    window_id=args.iterm_window_id, window_number=args.iterm_window_number)))
            except Exception as error:
                # A view failure never changes a launch's submission outcome.
                print(json.dumps({"status": "unavailable", "reason": str(error) if isinstance(error, ValueError) else type(error).__name__,
                                  "readback_required": True,
                                  "next_action": "Inspect iTerm after unlocking, then use view --window-id; do not relaunch agents."}))

        iterm2.run_until_complete(view_connected)
        return
    if args.backend == "tmux":
        import tmux_terminal

        try:
            print(json.dumps(asyncio.run(tmux_terminal.operate(args)), indent=2))
        except LaunchFailure as error:
            print(json.dumps(error.detail, indent=2))
            parser.exit(1, "launch failed; inspect the reported terminals before retrying\n")
        except SendFailure as error:
            print(json.dumps(error.detail, indent=2))
            parser.exit(1, f"SEND FAILED: {error}; read and clear verified input before another send\n")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, f"refused: {error}\n")
        return
    import iterm2

    async def connected(connection):
        app = await iterm2.async_get_app(connection)
        try:
            print(json.dumps(await operate(app, args), indent=2))
        except LaunchFailure as error:
            print(json.dumps(error.detail, indent=2))
            parser.exit(1, "launch failed; inspect the reported tabs before retrying\n")
        except SendFailure as error:
            print(json.dumps(error.detail, indent=2))
            parser.exit(1, f"SEND FAILED: {error}; read and clear verified input before another send\n")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, f"refused: {error}\n")
        except iterm2.rpc.RPCException:
            parser.exit(1, "iTerm RPC failed; preserve the target and read list before any retry\n")

    iterm2.run_until_complete(connected)


if __name__ == "__main__":
    main()
