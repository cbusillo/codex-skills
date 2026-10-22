# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared process and transcript facts; callers retain closure and terminal authority."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import subprocess
import re
import uuid
import sys
import ctypes


def native_uuid(value):
    """Canonical identity for the UUID forms accepted by native ThreadId parsing."""
    if not isinstance(value, str):
        return None
    hyphenated = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    if not re.fullmatch(r"[0-9a-fA-F]{32}|" + hyphenated + r"|\{" + hyphenated + r"\}|urn:uuid:" + hyphenated, value):
        return None
    return str(uuid.UUID(value))


def process_rows(output):
    rows = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            raise ValueError("unparseable process inventory")
        rows.append((int(fields[0]), fields[1], fields[2]))
    return rows


def inventory():
    return process_rows(subprocess.run(
        ["ps", "-ww", "-axo", "pid=,tty=,command="], check=True, capture_output=True,
        text=True, timeout=10).stdout)


def process_started_at(pid):
    value = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], check=True,
                           capture_output=True, text=True, timeout=2,
                           env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).stdout.strip()
    return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)


def process_absent(pid):
    """Only the kernel's missing-PID result establishes current absence."""
    if sys.platform not in {"darwin", "linux"} or type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        pass
    return False


def idle_terminal_command(command):
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    name = Path(argv[0]).name.lstrip("-")
    return name == "login" or name in {"zsh", "bash", "sh", "fish", "dash"} and all(
        arg in {"-l", "-i", "-il", "-li", "--login", "--interactive"} for arg in argv[1:])


def versioned_claude(executable):
    path = Path(executable)
    return (path.parent.name == "versions" and path.parent.parent.name == "claude"
            and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", path.name) is not None)


def harness_family(executable):
    name = Path(executable).name.casefold()
    if name in {"codex-session.py", "codex_session_proxy.py"}:
        return None  # Managed wrappers need their own launch binding.
    if name == "codex.js":
        return "codex"
    if name.endswith((".py", ".sh")):
        return None  # Repository utilities are not native harness executables.
    if name.startswith("codex"):
        return "codex"
    return "claude" if name in {"claude", "claude.js"} or versioned_claude(executable) else None


ARGUMENT_BYTES = 2 * 1024 * 1024
PROC_ROOT = Path("/proc")


def darwin_arguments(data):
    """Decode only argc and argv; never decode the trailing environment."""
    data = memoryview(data).cast("B")
    if len(data) < 4:
        raise ValueError("native argv header is unavailable")
    argc = int.from_bytes(data[:4], sys.byteorder)
    if not 0 < argc <= 4096:
        raise ValueError("native argc is unavailable")
    offset = 4
    while offset < len(data) and data[offset]:
        offset += 1  # Bare executable path, not argv[0].
    while offset < len(data) and not data[offset]:
        offset += 1  # Kernel alignment padding before argv[0].
    argv = []
    for _ in range(argc):
        start = offset
        while offset < len(data) and data[offset]:
            offset += 1
        if offset == len(data):
            raise ValueError("native argv is truncated")
        argv.append(bytes(data[start:offset]).decode("utf-8"))
        offset += 1
    return argv


def native_arguments(pid):
    """Read OS argument boundaries; never decode or report environment values."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("native argv requires a process PID")
    if sys.platform == "linux":
        with Path(f"/proc/{pid}/cmdline").open("rb") as stream:
            data = stream.read(ARGUMENT_BYTES + 1)
        if not data or len(data) > ARGUMENT_BYTES or not data.endswith(b"\0"):
            raise ValueError("native argv is unavailable or truncated")
        return [arg.decode("utf-8") for arg in data[:-1].split(b"\0")]
    if sys.platform != "darwin":
        raise OSError("native argv boundaries are unavailable on this host")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid.
    size = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "native argv size is unavailable")
    if not 4 < size.value <= ARGUMENT_BYTES:
        raise ValueError("native argv exceeds the bounded reader")
    data = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "native argv is unavailable")
    return darwin_arguments(memoryview(data)[:size.value])


def native_process_details(pid):
    """Kernel executable and incarnation, without reading process environments."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("native process identity requires a PID")
    if sys.platform == "linux":
        root = PROC_ROOT / str(pid)
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if root.stat().st_uid != os.getuid() or fields[0] == "Z":
            raise ValueError("native process owner or state is unavailable")
        return {"pid": pid, "ppid": int(fields[1]), "executable": str((root / "exe").resolve(strict=True)),
                "identity": {"startTicks": int(fields[19])}}
    if sys.platform != "darwin":
        raise ValueError("native process incarnation is unavailable on this host")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    path = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, path, len(path)) <= 0:
        raise OSError(ctypes.get_errno(), "native process executable is unavailable")
    # XNU PROC_PIDUNIQIDENTIFIERINFO identifies this incarnation even when a
    # PID is reused within the same second. Its native ABI contains no secrets.
    class UniqueInfo(ctypes.Structure):
        _fields_ = [("executable_uuid", ctypes.c_ubyte * 16), ("unique_id", ctypes.c_uint64),
                    ("parent_unique_id", ctypes.c_uint64), ("reserved", ctypes.c_uint64 * 3)]
    identity = UniqueInfo()
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    if library.proc_pidinfo(pid, 17, 1, ctypes.byref(identity), ctypes.sizeof(identity)) != ctypes.sizeof(identity):
        raise OSError(ctypes.get_errno(), "native process incarnation is unavailable")
    owner, parent = map(int, subprocess.run(["ps", "-p", str(pid), "-o", "uid=,ppid="],
        check=True, capture_output=True, text=True, timeout=2).stdout.split())
    if owner != os.getuid():
        raise ValueError("native process belongs to another user")
    return {"pid": pid, "ppid": parent, "executable": str(Path(os.fsdecode(path.value)).resolve()),
            "identity": {"uniqueId": identity.unique_id}}


def interpreter_family(executable):
    name = Path(executable).name
    if name in {"node", "nodejs", "bun", "deno"}:
        return "javascript"
    if name in {"sh", "bash", "zsh"}:
        return "shell"
    return "python" if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name) else None


def execution_arguments(command):
    """Find one executable/script prefix; later operands never supply identity."""
    flat = isinstance(command, str)
    words = command.split() if flat else list(command)
    wrappers = {"codex-session.py", "codex_session_proxy.py"}

    def candidate(value):
        return harness_family(value) is not None or Path(value).name in wrappers

    def path_span(index):
        value = words[index]
        end = index + 1
        # Flat text can lose spaces inside a path. Extend only this anchored
        # span, never across an option or a separate absolute/relative path.
        if flat and "/" in value and not (candidate(value) or interpreter_family(value)):
            while end < len(words) and not words[end].startswith(("-", "/", "./", "../")):
                value += " " + words[end]
                end += 1
                if candidate(value) or interpreter_family(value):
                    break
        return value, end

    if not words:
        return [], [], False
    executable, index = path_span(0)
    identity = [executable]
    interpreter = interpreter_family(executable)
    uncertain = False
    if interpreter:
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            identity.append(option)
            index += 1
            uncertain = True
            if option == "--":
                break
            # Node's preload operand precedes the main script. It is not the
            # script slot and cannot hide a later native CLI entrypoint.
            if Path(executable).name in {"node", "nodejs"}:
                preload = option.partition("=")[0] in {"-r", "--require", "--import", "--loader", "--experimental-loader"}
                if preload and "=" not in option:
                    if index == len(words):
                        return [], [], True
                    identity.append(words[index])
                    index += 1
                    continue
                if preload or option.startswith("-r"):
                    continue
            code_mode = (interpreter == "shell" and not option.startswith("--") and "c" in option[1:]
                         or interpreter == "python" and re.match(r"^-[bBdEhiIOqRsSuvVx]*[cm]", option)
                         or interpreter == "javascript" and (option.partition("=")[0] in {"--eval", "--print"}
                                                              or option.startswith(("-e", "-p"))))
            if code_mode:
                return [], identity, True
        if index == len(words):
            return [], [], uncertain  # Missing script cannot clear an earlier one.
        script, index = path_span(index)
        identity.append(script)
    argv = [identity[-1], *words[index:]] if candidate(identity[-1]) else []
    return argv, identity, uncertain


def harness_arguments(command):
    """Keep full argv; identity parsing and guarded prefix binding are separate."""
    return execution_arguments(command)[0]


def process_arguments(pid, command):
    """ps discovers candidates; only OS argument boundaries grant exemptions."""
    fallback = harness_arguments(command)
    if not fallback:
        return [], False
    try:
        native = native_arguments(pid)
        argv, identity, uncertain = execution_arguments(native)
        parts = Path(native[0]).parts if native else ()
        framework_helper = any(part.endswith(".framework") and "Helpers" in parts[index + 1:]
                               for index, part in enumerate(parts))
        helper_role = (len(native) > 1 and native[1] in {
            "--type=gpu-process", "--type=utility", "--type=renderer"}
            and sum(arg.startswith("--type") for arg in native[1:]) == 1)
        if not argv or framework_helper and helper_role:
            # Never clear an earlier candidate using a replacement process.
            # ps rewrites prompt control characters; compare only the complete
            # executable/script prefix and retain all PID/launch/daemon checks.
            prefix = " ".join(identity)
            if isinstance(command, str):
                consistent = bool(identity) and (command == prefix or command.startswith(prefix + " "))
            else:
                consistent = bool(identity) and identity == list(command)[:len(identity)]
            return ([], True) if consistent else (fallback, False)
        return argv, not uncertain
    except (OSError, ValueError, UnicodeError):
        pass
    return fallback, False


# Fixed-arity options from the native CLI. Unknown or variadic option shapes
# retain conservative candidates but never establish unrelated-session proof.
VALUE_OPTIONS = {"-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env",
                 "-i", "--image", "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox",
                 "-C", "--cd", "--add-dir", "-a", "--ask-for-approval", "--thread-source",
                 "--cyber-access-program", "--output-schema", "-o", "--output-last-message"}
SWITCH_OPTIONS = {"--all", "--json", "--last", "--include-non-interactive", "--strict-config", "--oss",
                  "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
                  "--worktree", "--search", "--no-alt-screen", "--no-daemon", "--skip-git-repo-check",
                  "--ephemeral", "--ignore-user-config", "--ignore-rules", "--help", "-h", "--version", "-V"}


def claude_invocation(argv):
    """Known native option arities establish authority; uncertain tokens only veto."""
    required = {"--model", "--permission-mode", "--effort", "--agent", "--system-prompt",
                "--system-prompt-file", "--append-system-prompt", "--append-system-prompt-file",
                "--settings", "--setting-sources", "--output-format", "--input-format", "--debug-file"}
    switches = {"--verbose", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                "-p", "--print", "-c", "--continue"}
    words = list(argv[1:])
    parsed, index, uncertain, continued, kind = [], 0, False, False, None
    while index < len(words):
        arg = words[index]
        if arg == "--":
            break
        option, equals, value = arg.partition("=")
        if option in required:
            if not equals:
                # Native Commander consumes even a flag-shaped required value.
                index += 1
                uncertain |= index == len(words)
        elif len(arg) > 2 and arg[:2] in {"-r", "-d"} and not arg.startswith("--"):
            if arg[:2] == "-r":
                parsed.append(arg[2:])  # Native short attachment preserves a leading '='.
                kind, uncertain = "resume", True  # Attached resume remains veto only.
        elif option in {"--resume", "-r", "--debug", "-d"}:
            if not equals:
                value = words[index + 1] if index + 1 < len(words) else ""
                if value and (not value.startswith("-") or value == "-"):
                    index += 1
                else:
                    value = ""
            if option in {"--resume", "-r"}:
                kind = "resume"
                if value:
                    parsed.append(value)
                else:
                    uncertain = True  # A picker does not identify another session.
        elif arg in switches:
            continued |= arg in {"-c", "--continue"}
        elif len(arg) > 2 and arg[:2] in {"-p", "-c"} and not arg.startswith("--"):
            # Native boolean short options requeue the rest of their cluster.
            continued |= arg[:2] == "-c"
            words[index] = "-" + arg[2:]
            continue
        else:
            # Unknown arities and root command/prompt operands cannot grant an
            # exemption from a later flag that may belong to a subcommand.
            uncertain = True
        index += 1
    # Only a wholly known native walk can establish a prompt delimiter. An
    # unknown option may consume that token as its value instead.
    candidates, candidate_kind, _ = uncertain_invocation(argv if uncertain else argv[:index + 1])
    exact = parsed[0] if len(parsed) == 1 and not uncertain and not continued else None
    return list(dict.fromkeys([*candidates, *parsed])), "resume" if continued else kind or candidate_kind, exact


def uncertain_invocation(argv):
    """Lost boundaries may veto a source, never prove another source or fresh exec."""
    family = harness_family(argv[0]) if argv else None
    if family is None:
        return [], None, None
    # With lost boundaries or unknown arities, a standalone '--' may be an
    # option value or part of quoted text. It cannot truncate veto candidates.
    words = argv
    markers = [i for i, arg in enumerate(words) if arg in {"--resume", "-r"} or family == "codex" and arg == "resume"
               or family == "claude" and re.fullmatch(r"-[pc]*r", arg)]
    candidates = [value for i in markers for value in words[i + 1:] if value and not value.startswith("-")]
    candidates.extend(arg.split("=", 1)[1] for arg in words if arg.startswith("--resume=") and arg.split("=", 1)[1])
    if family == "claude":
        candidates.extend(arg[2:] for arg in words if arg.startswith("-r") and len(arg) > 2)
        candidates.extend(match.group(1) for arg in words
                          if (match := re.fullmatch(r"-[pc]+r(.+)", arg)))
    resume = bool(markers or candidates or family == "claude" and any(arg in {"-c", "--continue"} for arg in words[1:]))
    return list(dict.fromkeys(candidates)), "resume" if resume else None, None


def invocation(argv, *, bounded=True):
    """Return source candidates, command kind, and an exact native resume operand."""
    if not bounded or argv and versioned_claude(argv[0]):
        return uncertain_invocation(argv)
    if not argv:
        return [], None, None
    family = harness_family(argv[0])
    if family is None:
        return [], None, None
    if family == "claude":
        return claude_invocation(argv)

    last = False
    uncertain = False

    def options(index, *, interactive, operand_bound=False):
        nonlocal last, uncertain
        while index < len(argv):
            arg = argv[index]
            if arg == "--":
                return index + 1, True
            option, equals, _value = arg.partition("=")
            attached = len(arg) > 2 and arg[:2] in VALUE_OPTIONS and not arg.startswith("--")
            if option in VALUE_OPTIONS or attached:
                option = arg[:2] if attached else option
                if option in {"-i", "--image"} and interactive and not operand_bound:
                    uncertain = True  # The interactive image option is variadic.
                if not equals and not attached:
                    if index + 1 == len(argv):
                        uncertain = True
                    index += 1
                index += 1
            elif arg in SWITCH_OPTIONS:
                last |= arg == "--last"
                index += 1
            elif arg.startswith("-") and arg != "-":
                uncertain = True
                return index, False
            else:
                return index, False
        return index, False

    index, separated = options(1, interactive=True)
    if uncertain:
        return uncertain_invocation(argv)
    if separated or index == len(argv):
        return [], None, None
    command = argv[index]
    exec_mode = command in {"exec", "e"}
    if exec_mode:
        index, separated = options(index + 1, interactive=False)
        if uncertain:
            return uncertain_invocation(argv)
        if separated or index == len(argv) or argv[index] != "resume":
            return [], "exec", None
    elif command != "resume":
        return [], None, None  # The first prompt ends command discovery.

    index, separated = options(index + 1, interactive=not exec_mode)
    if last:
        return [], "resume", None
    if uncertain:
        return uncertain_invocation(argv)
    operand = argv[index] if index < len(argv) else None
    # Options may follow the first positional too. They can remove authority,
    # but later prompt words never supply another source selector.
    if operand is not None and not separated:
        tail = index + 1
        while tail < len(argv) and not separated and not uncertain:
            end, separated = options(tail, interactive=not exec_mode, operand_bound=True)
            tail = end + 1 if end == tail else end
    if last:
        return [], "resume", None
    if uncertain:
        return [operand] if operand is not None else [], "resume", None
    return [operand] if operand is not None else [], "resume", operand


def verify_process_exit(entry, *, rows=None, ended_at=None, started_at=None,
                        independent_harness=None, replacement_owner=None, require_tty=False,
                        scope="session"):
    """Share process facts; physical terminal cleanup does not establish session closure."""
    if scope not in {"session", "terminal"}:
        raise ValueError("unknown process verification scope")
    rows = inventory() if rows is None else rows
    started_at = started_at or process_started_at
    tty = entry.get("tty")
    if not isinstance(tty, str) or not tty:
        if not entry.get("_inventory"):
            raise ValueError("ledger requires verified pid and tty")
    else:
        tty = tty.removeprefix("/dev/")
    if scope == "session" and not entry.get("_inventory") and (type(entry.get("pid")) is not int or entry["pid"] <= 0):
        raise ValueError("ledger requires verified pid and tty")
    pids = (entry.get("pid"), entry.get("launch_pid"), entry.get("_terminal_pid"))
    if any(value is not None and (type(value) is not int or value <= 0) for value in pids):
        raise ValueError("ledger requires verified pid and tty")
    same_tty = [row for row in rows if row[1] == tty]
    if require_tty and not same_tty:
        raise ValueError("no process rows match the terminal TTY; inventory is uncertain")
    source = native_uuid(entry.get("session_id")) or entry.get("session_id")
    for pid, process_tty, command in rows:
        if pid in pids:
            raise ValueError("source PID (possibly reused) or work on its TTY remains; preserve claims and verify native identity")
        argv, command_kind, exact_resume = [], None, None
        if scope == "session":
            argv, bounded = process_arguments(pid, command)
            resumed, command_kind, exact_resume = invocation(argv, bounded=bounded)
            if source in [native_uuid(value) or value for value in resumed]:
                raise ValueError("source native session is visible in a resumed harness process")
            if entry.get("_inventory") and argv and harness_family(argv[0]) == entry.get("harness"):
                if command_kind == "resume" and native_uuid(exact_resume) is None:
                    raise ValueError("Unresolved native process may own the source; preserve claims")
                if entry.get("_historical_exit"):
                    raise ValueError("Live source-family process remains; preserve historical claims")
        if process_tty != tty or idle_terminal_command(command):
            continue
        if not entry.get("_backfill"):
            try:
                born = started_at(pid) if ended_at is not None else None
                newer = ended_at is not None and ended_at < born <= datetime.now(timezone.utc)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
                raise ValueError("process birth is unavailable; terminal kept") from error
            if not newer:
                raise ValueError("source PID (possibly reused) or work on its TTY remains; preserve claims and verify native identity")
        owner_known = replacement_owner is not None and replacement_owner(pid)
        if replacement_owner is not None and not owner_known:
            raise ValueError("process belongs to no verified new TTY owner; terminal kept")
        if scope == "session" and argv and (
            harness_family(argv[0]) in ({entry["harness"]} if entry.get("harness") else {"codex", "claude"})
            or Path(argv[0]).name in {"codex-session.py", "codex_session_proxy.py"}
        ):
            explicit_other = native_uuid(exact_resume) is not None
            noninteractive = command_kind == "exec"
            guarded = independent_harness is not None and independent_harness(pid, argv)
            if not (explicit_other or noninteractive or guarded):
                raise ValueError("unidentified harness on the recorded TTY; preserve claims and verify native identity")
    return rows


def require_safe_status(status):
    """Check the declaration only after the caller independently proves exit."""
    if not status["safe_verdict"]:
        raise ValueError("latest transcript lacks an unqualified Safe to exit: yes")


def native_exit_cutoff(entry):
    """Read an exact dead pane, never a caller's proposed ledger timestamp."""
    if not entry.get("tmux_session_id"):
        return None
    from tmux_terminal import Terminal
    try:
        terminal = Terminal(entry.get("tmux_socket", "supervisor"))
        row = terminal.dead_pane(entry["tmux_session_id"], entry)
        return datetime.fromisoformat(row["exited_at"]) if row.get("exited_at") else None
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        # A cutoff only permits verified replacement work on a reused TTY.
        # Its absence retains the stricter PID/TTY path; it is not exit proof.
        return None


def replacement_tty_owner(entry, rows, *, started_at):
    """Prove one newer process tree after the exact source pane disappeared."""
    socket = entry.get("tmux_socket", "supervisor")
    identity = entry.get("tmux_session_id")
    tty = entry.get("tty")
    if (entry.get("supervisor_owned") is not True or entry.get("_inventory") or entry.get("_backfill")
            or not isinstance(socket, str) or not isinstance(identity, str)
            or not re.fullmatch(r"tmux:" + re.escape(socket) + r":[0-9a-f]{32}", identity)
            or not isinstance(tty, str) or not tty or not entry.get("launch_started_at")):
        return None
    rows = rows() if callable(rows) else rows
    occupants = {pid for pid, process_tty, _command in rows if process_tty == tty.removeprefix("/dev/")}
    if not occupants:
        return None
    from tmux_terminal import Terminal, process_parents, belongs_to
    try:
        if any(pane["session_id"] == identity for pane in Terminal(socket).list()):
            return None
        parents = process_parents()
        if not occupants <= parents.keys():
            return None
        roots = [pid for pid in occupants if parents[pid] not in occupants]
        if len(roots) != 1 or not all(belongs_to(pid, roots[0], parents) for pid in occupants):
            return None
        # A sole surviving source child can also look like a new root after
        # reparenting to init/launchd. It does not prove a replacement terminal.
        if parents[roots[0]] <= 1 or parents[roots[0]] not in parents:
            return None
        born = started_at(roots[0])
        launched = datetime.fromisoformat(entry["launch_started_at"])
        if launched.tzinfo is None or not launched < born <= datetime.now(timezone.utc):
            return None
        return roots[0], born, occupants
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return None
