# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import ctypes
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
import claim_history

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
    if choice.get("provider") == "google":
        return claim_history.AGY_SETTINGS.expanduser()
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


def google_config(path: Path) -> bool:
    return path.resolve() == claim_history.AGY_SETTINGS.expanduser().resolve()


def read_projects(path):
    harness = "AGY" if google_config(path) else "Claude"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if google_config(path):
            raise ValueError(f"AGY settings {path} do not exist; inspect the existing profile before retrying") from None
        raise ValueError(f"Claude home config {path} does not exist; sign in to that home "
                         "interactively once before launching on it") from None
    except OSError as error:
        raise ValueError(f"cannot read {harness} folder trust in {path} ({type(error).__name__}); "
                         "inspect that config before retrying") from None
    return parse_projects(raw, path)


def parse_projects(raw, path):
    harness = "AGY" if google_config(path) else "Claude"
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"cannot read {harness} folder trust in {path} ({type(error).__name__}); "
                         "inspect that config before retrying") from None
    google = google_config(path)
    if google:
        projects = data.setdefault("trustedWorkspaces", []) if isinstance(data, dict) else None
        if not isinstance(projects, list) or not all(isinstance(root, str) for root in projects):
            raise ValueError(f"cannot read AGY folder trust in {path}: trustedWorkspaces is not a list of paths")
        return raw, data, projects
    projects = data.setdefault("projects", {}) if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise ValueError(f"cannot read Claude folder trust in {path}: projects is not an object")
    return raw, data, projects


def trusted(projects, root):
    if isinstance(projects, list):
        return str(root) in projects
    entry = projects.get(str(root))
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True


def plan(directory, selected, homes, main_root, developer=None, worktrees=None):
    google = selected.get("provider") == "google"
    harness = "AGY" if google else "Claude"
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError(f"{harness} launches require cd /absolute/repository before the direct invocation; "
                         "the folder must exist")
    directory = directory.resolve()
    developer = (developer or Path.home() / "Developer").resolve()
    worktrees = (worktrees or worktree_root()).resolve()
    roots, main = trust_roots(directory, main_root)
    in_allowed_scope = in_scope(directory, roots, main, developer, worktrees)
    if google:
        roots = [directory]
    if not in_allowed_scope:
        path = config_file(selected)
        _, _, projects = read_projects(path)
        missing = [str(root) for root in roots if not trusted(projects, root)]
        if missing:
            raise ValueError(
                f"{harness} folder is not trusted: {directory} (trust root: {', '.join(missing)}) in {path}. "
                f"Only folders under {developer} and their worktrees in {worktrees} are trusted "
                f"automatically; open {harness} in {directory} interactively on that home and review "
                "the trust dialog yourself, then retry")
        return []
    writes = []
    paths = [config_file(selected)] if google else [config_file(choice) for choice in [selected, *homes()]]
    default = config_file({"env": {}})
    if not google and default.exists():
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
    path = path.resolve(strict=True)
    expected, data, projects = read_projects(path)
    carried = 0
    while True:
        if isinstance(projects, list):
            projects.extend(str(root) for root in roots if str(root) not in projects)
        else:
            for root in roots:
                entry = projects.get(str(root))
                projects[str(root)] = {**(entry if isinstance(entry, dict) else {}), TRUST_KEY: True}
        mode = path.stat().st_mode & 0o777
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.trust-")
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
            harness = "AGY" if google_config(path) else "Claude"
            raise ValueError(f"{harness} folder trust did not persist in {path}; another writer kept "
                             "replacing it, retry the launch")
