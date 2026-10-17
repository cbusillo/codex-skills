# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Claude Code folder trust for Supervisor launches (codex-skills#2100).

Claude Code records an accepted trust dialog as
`projects["<root>"].hasTrustDialogAccepted` in the home's `.claude.json`; the
root is the repository root, or the main checkout's root for a linked worktree
(https://code.claude.com/docs/en/permissions#what-runs-before-you-trust-a-folder).
Trust on a parent folder does not cover a git repository nested inside it.

Director decision (October 17, 2026): everything under ~/Developer is trusted.
The launcher writes that record, in every configured Claude home and in the
default home (~/.claude.json, Q471 on codex-skills#2100), only for
roots under ~/Developer and for linked worktrees of those repositories in the
configured worktree root. Every other folder must already be trusted in the
selected home. Only that one key changes; logins and other settings are kept.
"""

import ctypes
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

TRUST_KEY = "hasTrustDialogAccepted"
# Both platforms number their atomic swap flag 2: macOS RENAME_SWAP, Linux RENAME_EXCHANGE.
EXCHANGE_FLAG = 2
LINUX_AT_FDCWD = -100
CARRY_FORWARD_LIMIT = 20


def worktree_root():
    """The worktree root that the catalog's dev-worktree helper creates under."""
    path = Path(__file__).resolve().parents[2] / "github/scripts/dev_worktree.py"
    spec = importlib.util.spec_from_file_location("dev_worktree", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {path} to read the worktree root setting")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module.configuration()[0]
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the worktree root setting ({type(error).__name__}); "
                         "see skills/github/references/worktrees.md") from None


def config_file(choice):
    """The selected home's `.claude.json`: ~/.claude.json without CLAUDE_CONFIG_DIR."""
    directory = choice["env"].get("CLAUDE_CONFIG_DIR")
    return (Path(directory).expanduser() if directory else Path.home()) / ".claude.json"


def git_top(directory):
    for root in (directory, *directory.parents):
        marker = root / ".git"
        if marker.is_file() or (marker / "HEAD").exists():
            return root
    return None


def trust_roots(directory, main_root):
    """Roots Claude may key trust on: the checkout's top and, for a worktree, its main root.

    The docs disagree on worktrees (permissions: main checkout root; worktrees:
    the worktree itself), so a linked worktree records both.
    """
    top = git_top(directory)
    if top is None:
        return [directory], None
    main = main_root(directory)
    # A Git pointer may name the main checkout through a symlink; scope uses the real path.
    main = main.resolve() if main is not None else None
    return list(dict.fromkeys(root for root in (main, top) if root is not None)), main


def within(path, root):
    return path != root and path.is_relative_to(root)


def in_scope(directory, roots, main, developer, worktrees):
    if all(within(root, developer) for root in roots):
        return True
    # A linked worktree in the worktree root whose validated main checkout is under ~/Developer.
    return (main is not None and within(main, developer) and within(directory, worktrees)
            and all(root == main or within(root, worktrees) for root in roots))


def read_projects(path):
    """Return the raw bytes, the parsed config, and its projects object (attached if absent)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(f"Claude home config {path} does not exist; sign in to that home "
                         "interactively once before launching on it") from None
    except OSError as error:
        raise ValueError(f"cannot read Claude folder trust in {path} ({type(error).__name__}); "
                         "inspect that config before retrying") from None
    return parse_projects(raw, path)


def parse_projects(raw, path):
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"cannot read Claude folder trust in {path} ({type(error).__name__}); "
                         "inspect that config before retrying") from None
    projects = data.setdefault("projects", {}) if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise ValueError(f"cannot read Claude folder trust in {path}: projects is not an object")
    return raw, data, projects


def trusted(projects, root):
    entry = projects.get(str(root))
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True


def plan(directory, selected, homes, main_root, developer=None, worktrees=None):
    """Return the (config, roots) writes a launch needs, or refuse before any write.

    `homes` returns every configured Claude account; it is read only for folders in scope.
    The default home is included too when its `.claude.json` exists; it is never created.
    """
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("Claude launches require cd /absolute/repository && exec claude; "
                         "the folder must exist")
    directory = directory.resolve()
    developer = (developer or Path.home() / "Developer").resolve()
    worktrees = (worktrees or worktree_root()).resolve()
    roots, main = trust_roots(directory, main_root)
    if not in_scope(directory, roots, main, developer, worktrees):
        path = config_file(selected)
        _, _, projects = read_projects(path)
        missing = [str(root) for root in roots if not trusted(projects, root)]
        if missing:
            raise ValueError(
                f"Claude folder is not trusted: {directory} (trust root: {', '.join(missing)}) in {path}. "
                f"Only folders under {developer} and their worktrees in {worktrees} are trusted "
                f"automatically; open claude in {directory} interactively on that home and review "
                "the trust dialog yourself, then retry")
        return []
    writes = []
    paths = [config_file(choice) for choice in [selected, *homes()]]
    default = config_file({"env": {}})
    if default.exists():
        paths.append(default)
    for path in dict.fromkeys(paths):
        _, _, projects = read_projects(path)
        needed = [root for root in roots if not trusted(projects, root)]
        if needed:
            writes.append((path, needed))
    return writes


def apply(writes):
    """Set only the trust key in each home, keeping every other change."""
    for path, roots in writes:
        write_trust(path, roots)


def exchange(source, target):
    """Atomically swap two paths, so the caller gets back exactly the file it displaced."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        result = libc.renamex_np(os.fsencode(source), os.fsencode(target), ctypes.c_uint(EXCHANGE_FLAG))
    elif sys.platform.startswith("linux"):
        result = libc.renameat2(LINUX_AT_FDCWD, os.fsencode(source), LINUX_AT_FDCWD,
                                os.fsencode(target), ctypes.c_uint(EXCHANGE_FLAG))
    else:
        raise ValueError(f"cannot update {target} safely: no atomic exchange on {sys.platform}")
    if result != 0:
        raise ValueError(f"cannot update {target} safely: atomic exchange failed "
                         f"({os.strerror(ctypes.get_errno())})")


def write_trust(path, roots, attempts=3):
    """Set the trust key without discarding a save another Claude session makes meanwhile.

    Claude Code documents no lock for `.claude.json` and saves it by renaming a
    temporary file over it. The new file is swapped in atomically and the file
    it displaced comes back; if that is not the file this read, a session saved
    in between, so the key is applied to its content and swapped in again. A
    save after the swap replaces this file whole, keeping that session's data;
    the readback then sees the key missing and applies it again.
    """
    expected, data, projects = read_projects(path)
    carried = 0
    while True:
        for root in roots:
            entry = projects.get(str(root))
            projects[str(root)] = {**(entry if isinstance(entry, dict) else {}), TRUST_KEY: True}
        mode = path.stat().st_mode & 0o777
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".claude.json.trust-")
        keep = False
        try:
            body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            with os.fdopen(handle, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            exchange(temporary, path)
            displaced = Path(temporary).read_bytes()
            if displaced != expected:
                carried += 1
                if carried > CARRY_FORWARD_LIMIT:
                    keep = True
                    raise ValueError(f"{path} kept changing during the trust update; the last save "
                                     f"another session made is kept at {temporary}; merge it back "
                                     "before relaunching")
                # Path now holds this body; carry the displaced save forward onto it.
                expected = body
                _, data, projects = parse_projects(displaced, path)
                continue
        finally:
            if not keep:
                Path(temporary).unlink(missing_ok=True)
        expected, data, projects = read_projects(path)
        if all(trusted(projects, root) for root in roots):
            return
        attempts -= 1
        if attempts <= 0:
            raise ValueError(f"Claude folder trust did not persist in {path}; another writer kept "
                             "replacing it, retry the launch")
