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
The launcher writes that record, in every configured Claude home, only for
roots under ~/Developer and for linked worktrees of those repositories in the
configured worktree root. Every other folder must already be trusted in the
selected home. Only that one key changes; logins and other settings are kept.
"""

import importlib.util
import json
import os
import tempfile
from pathlib import Path

TRUST_KEY = "hasTrustDialogAccepted"


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Claude home config {path} does not exist; sign in to that home "
                         "interactively once before launching on it") from None
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read Claude folder trust in {path} ({type(error).__name__}); "
                         "inspect that config before retrying") from None
    projects = data.get("projects", {}) if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        raise ValueError(f"cannot read Claude folder trust in {path}: projects is not an object")
    return data, projects


def trusted(projects, root):
    entry = projects.get(str(root))
    return isinstance(entry, dict) and entry.get(TRUST_KEY) is True


def plan(directory, selected, homes, main_root, developer=None, worktrees=None):
    """Return the (config, roots) writes a launch needs, or refuse before any write.

    `homes` returns every configured Claude account; it is read only for folders in scope.
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
        _, projects = read_projects(path)
        missing = [str(root) for root in roots if not trusted(projects, root)]
        if missing:
            raise ValueError(
                f"Claude folder is not trusted: {directory} (trust root: {', '.join(missing)}) in {path}. "
                f"Only folders under {developer} and their worktrees in {worktrees} are trusted "
                f"automatically; open claude in {directory} interactively on that home and review "
                "the trust dialog yourself, then retry")
        return []
    writes = []
    for path in dict.fromkeys(config_file(choice) for choice in [selected, *homes()]):
        _, projects = read_projects(path)
        needed = [root for root in roots if not trusted(projects, root)]
        if needed:
            writes.append((path, needed))
    return writes


def apply(writes):
    """Set only the trust key, replacing the file atomically, then read it back."""
    for path, roots in writes:
        data, projects = read_projects(path)
        for root in roots:
            entry = projects.get(str(root))
            projects[str(root)] = {**(entry if isinstance(entry, dict) else {}), TRUST_KEY: True}
        mode = path.stat().st_mode & 0o777
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".claude.json.trust-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        _, projects = read_projects(path)
        if not all(trusted(projects, root) for root in roots):
            raise ValueError(f"Claude folder trust did not persist in {path}; another writer "
                             "replaced it, retry the launch")
