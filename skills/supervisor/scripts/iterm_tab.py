#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "iterm2==2.25",
#     "websockets==17.2",
# ]
# ///
"""Target iTerm or tmux sessions by exact identity; separate text and Return sends."""

import argparse
import asyncio
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import account_choice

CLAUDE_SUGGESTION_SETTING = "promptSuggestionEnabled"
CLAUDE_SUGGESTION_ENV = "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"


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
        super().__init__(reason)


class StartupFailure(ValueError):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        super().__init__("terminal exited during startup; inspect its last output before retrying"
                         if snapshot["dead"] else
                         "startup observation unavailable; submitted agent may be running; inspect before retrying")


async def verify_startup(read, timeout=2.0):
    """Observe immediate exit; this is not full agent readiness."""
    deadline = time.monotonic() + timeout
    while True:
        snapshot = await read()
        # tmux can observe PTY EOF before SIGCHLD supplies the exit status.
        # Keep the failure, but use the remaining window to collect its cause.
        awaiting_status = snapshot["dead"] and snapshot.get("exit_status") == ""
        if snapshot["dead"] and (not awaiting_status or time.monotonic() >= deadline):
            raise StartupFailure(snapshot)
        if not snapshot["dead"] and time.monotonic() >= deadline:
            return
        await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))


async def submit_iterm_launch(app, session, command):
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
            await send(session, "exec /bin/zsh -c " + shlex.quote(command), allow_tabs=True)

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
                await verify_startup(read)
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
    """Keep the CLI's last settings object, overriding only prompt suggestions."""
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
                settings = json.loads(path.read_text(encoding="utf-8"))
                # Keep file settings in the file: inlining exposes private values
                # and changes the base for relative permission rules.
                if isinstance(settings, dict) and settings.get(CLAUDE_SUGGESTION_SETTING) is False:
                    return None
                raise ValueError("Set promptSuggestionEnabled to false in the supplied settings file")
            if not isinstance(settings, dict):
                raise ValueError()
        except (ValueError, OSError):
            raise ValueError("Claude --settings must be a readable literal JSON object or file; "
                             "file settings must set promptSuggestionEnabled to false; "
                             "use an absolute file path for shell-expanded paths") from None
    return {**settings, CLAUDE_SUGGESTION_SETTING: False}


def append_brief(command_text, brief):
    """Append a literal prompt only to a direct invocation, optionally after cd."""
    words = launch_words(command_text)
    agent = Path(words[0]).name
    if agent == "claude":
        settings = claude_settings(command_text, words)
        if settings is not None:
            command_text += " --settings " + shlex.quote(json.dumps(settings))
    prompt_option = " --prompt-interactive=" if agent == "agy" else " -- "
    return command_text + prompt_option + shlex.quote(brief)


async def send(session, text, submit=True, allow_tabs=False):
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
    def __init__(self, state, active_threads):
        self.state = state
        self.active_threads = active_threads
        super().__init__(f"Codex daemon admission {state}; {active_threads} active thread(s). "
                         "Do not restart or stop it. Wait for it to exit, or select another "
                         "Context Panel account with --account; inspect active threads before any manual restart")


def inspect_codex_daemon(home):
    """Read loaded thread states and check admission without creating a thread."""
    from websockets.sync.client import unix_connect

    deadline = time.monotonic() + 10
    active = 0
    with unix_connect(str(codex_daemon_endpoint(home)), open_timeout=2, close_timeout=1) as ws:
        request_id = 0

        def request(method, params):
            nonlocal request_id
            request_id += 1
            ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("daemon inspection timed out")
                message = json.loads(ws.recv(timeout=remaining))
                if message.get("id") == request_id:
                    return message

        def result(method, params):
            response = request(method, params)
            if "error" in response or not isinstance(response.get("result"), dict):
                raise ValueError(f"daemon {method} read failed; active threads unknown; do not restart")
            return response["result"]

        result("initialize", {"clientInfo": {"name": "supervisor_launch_preflight", "version": "1"}})
        ws.send(json.dumps({"method": "initialized", "params": {}}))
        cursor, seen = None, set()
        while True:
            page = result("thread/loaded/list", {"cursor": cursor, "limit": 100})
            if not isinstance(page.get("data"), list):
                raise ValueError("daemon thread inventory unavailable; do not restart")
            for thread_id in page["data"]:
                thread = result("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
                state = thread["status"]["type"]
                if state not in {"active", "idle", "systemError", "notLoaded"}:
                    raise ValueError("daemon thread state unknown; do not restart")
                active += state == "active"
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("daemon thread pagination unavailable; do not restart")
            seen.add(cursor)
        # The daemon gates thread/resume before parsing the native UUID. An
        # invalid UUID cannot load/create a thread or run its hooks. There is
        # currently no read-only draining flag in the native protocol.
        probe = request("thread/resume", {"threadId": "supervisor-launch-preflight-invalid-id"})
        error = probe.get("error", {})
        message = error.get("message", "")
        if "Server is draining" in message:
            raise DaemonRefusal("draining", active)
        if error.get("code") != -32600 or not message.startswith("invalid session id:"):
            raise DaemonRefusal("unknown", active)


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
            capture_output=True, text=True, timeout=30,
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
    try:
        inspect_codex_daemon(home)
    except Exception as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        raise ValueError(f"Codex daemon preflight refused for CODEX_HOME={home}: {detail}; "
                         "no terminal was launched; do not restart the daemon automatically") from error


def scoped_exec(command_text):
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
                return (command_text[:match.end()] + "env CLAUDE_ACCOUNT_MOVE_ENABLED=1 "
                        + command_text[match.end():])
        except ValueError:
            continue
    return None


def with_account(command_text, choice, brief=None):
    """Prefix one agent invocation with the chosen account's environment."""
    if "\n" in command_text:
        raise ValueError("account selection needs a one-line launch command")
    default_claude = choice["provider"] == "anthropic" and "CLAUDE_CONFIG_DIR" not in choice["env"]
    checked_keys = set(choice["env"]) | set(account_choice.ACCOUNT_VARIABLES.values())
    if choice["provider"] == "anthropic":
        checked_keys.add(CLAUDE_SUGGESTION_ENV)
    google = choice["provider"] == "google"
    if google:
        if launch_agent(command_text) != "agy":
            raise ValueError("google account selection requires a direct AGY invocation")
        checked_keys.add("HOME")
    if default_claude:
        checked_keys.add("CLAUDE_CONFIG_DIR")
    conflicts = account_settings(command_text, checked_keys)
    if conflicts:
        remedy = ("remove those settings to use the existing AGY login" if google else
                  "remove those settings and use --account for an explicit override")
        raise ValueError(f"launch file already sets {', '.join(sorted(conflicts))}; "
                         + remedy)
    if brief is not None:
        command_text = append_brief(command_text, brief)
    settings = " ".join(
        f"{key}={shlex.quote(os.path.expanduser(value))}"
        for key, value in choice["env"].items()
    )
    # export, not env: the account must also reach an agent after `cd dir &&`.
    prefixes = ["unset CLAUDE_CONFIG_DIR"] if default_claude else []
    if settings:
        prefixes.append(f"export {settings}")
    if choice["provider"] == "anthropic":
        prefixes.append(f"export {CLAUDE_SUGGESTION_ENV}=false")
        command_text = scoped_exec(command_text) or (
            "( export CLAUDE_ACCOUNT_MOVE_ENABLED=1; " + command_text + "\n)"
        )
    return " && ".join([*prefixes, command_text])


def prepare_launches(args):
    """Validate the full batch and prepare accounts before creating terminals."""
    files = args.command_file if isinstance(args.command_file, list) else [args.command_file]
    command_texts = [path.read_text(encoding="utf-8").rstrip("\n") for path in files]
    for text in command_texts:
        validate_text(text)
    brief_files = getattr(args, "brief_file", None)
    if not brief_files:
        raise ValueError("new launches require --brief-file; put the prompt in that file")
    brief_files = brief_files if isinstance(brief_files, list) else [brief_files]
    if len(brief_files) != len(files):
        raise ValueError("provide one --brief-file per --command-file, in launch order")
    briefs = [path.read_text(encoding="utf-8") for path in brief_files]
    for brief in briefs:
        if not brief.strip():
            raise ValueError("brief must be nonempty")
        validate_text(brief, allow_tabs=True)
    choices = [None] * len(files)
    if getattr(args, "account_provider", None):
        if len(files) == 1:
            choices = [account_choice.select(args.account_provider, args.account_config, args.account)]
        else:
            choices = account_choice.select_batch(
                args.account_provider, args.account_config, args.account, count=len(files)
            )
        command_texts = [with_account(text, choice, brief)
                         for text, choice, brief in zip(command_texts, choices, briefs)]
    elif getattr(args, "account", None):
        raise ValueError("--account needs --account-provider")
    elif any(account_settings(text, account_choice.ACCOUNT_VARIABLES.values())
             for text in command_texts):
        raise ValueError("account settings need --account-provider; remove them from the launch file "
                         "and use --account for an explicit override")
    else:
        command_texts = [append_brief(text, brief) for text, brief in zip(command_texts, briefs)]
    if choices[0]:
        for choice in choices:
            check_codex_daemon(choice)
        account_choice.prepare_launch(choices[0])
    return command_texts, choices


async def close_session(app, session_id, ledger):
    """Close one owned, unsplit tab only with current idle-shell/input proof."""
    # Loaded here because close_ttys also uses this module's exact selector.
    from close_ttys import inventory, idle_terminal_command
    import iterm2

    entries = json.loads(ledger.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ValueError("ledger must contain session objects")
    matches = [e for e in entries if e.get("iterm_session_id") == session_id]
    if len(matches) != 1 or matches[0].get("supervisor_owned") is not True:
        raise ValueError("exact session is not uniquely Supervisor-owned in the launch ledger")
    entry = matches[0]

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
        rows = inventory()
        jobs = [row for row in rows if row[1] == tty]
        if not jobs:
            raise ValueError("no processes match this TTY; preserve ended or uncertain tab and report its profile setting")
        if any(not idle_terminal_command(row[2]) for row in jobs):
            raise ValueError("process remains on this TTY; tab kept")
        if isinstance(entry.get("pid"), int) and any(row[0] == entry["pid"] for row in rows):
            raise ValueError("recorded agent PID remains or was reused; tab kept")
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
                await submit_iterm_launch(app, session, command_text)
                progress.update(submission="submitted", phase="startup_observed")
            except Exception as error:
                if isinstance(error, StartupFailure):
                    progress.update(submission="submitted", phase="verify_startup")
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
    new.add_argument("--command-file", type=Path, action="append", required=True,
                     help="one agent invocation and settings, without its prompt; repeat for a batch")
    new.add_argument("--brief-file", type=Path, action="append", required=True,
                     help="nonempty prompt to append as one argument; one per command file, in launch order")
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
