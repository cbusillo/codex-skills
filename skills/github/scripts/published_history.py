# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

GitReader = Callable[..., str]


class PublicationError(ValueError):
    pass


def git(repo: Path | str, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        input=input_text,
        timeout=30,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
    )
    if result.returncode:
        raise PublicationError("publication evidence unavailable")
    return result.stdout.strip()


def verify_original_history(repo: Path | str, run: GitReader = git) -> None:
    grafts = Path(run(repo, "rev-parse", "--path-format=absolute", "--git-path", "info/grafts"))
    if grafts.exists() and grafts.stat().st_size:
        raise PublicationError("legacy grafts alter history; retry with GIT_GRAFT_FILE=/dev/null to verify original history")


def verify_remote(repo: Path | str, remote: str, run: GitReader = git) -> None:
    url = run(repo, "remote", "get-url", remote)
    local = url
    if url.startswith("file://"):
        local = unquote(urlsplit(url).path)
    elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", url) or (":" in url and "/" not in url.split(":", 1)[0]):
        return
    path = Path(os.path.expanduser(local))
    if not path.is_absolute():
        path = Path(repo) / path
    try:
        common = Path(run(path, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        try:
            root = Path(run(path, "rev-parse", "--show-toplevel"))
        except PublicationError:
            root = Path(run(path, "rev-parse", "--absolute-git-dir"))
    except PublicationError as exc:
        raise PublicationError(f"cannot resolve the {remote} local repository; use an absolute repository path") from exc
    if not (path.samefile(root) or path.samefile(common)):
        raise PublicationError(f"the {remote} local path is not a repository root")
    own = Path(run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    if common.samefile(own):
        raise PublicationError(f"the {remote} remote points to this repository; publish to an independent remote first")
    alternates = common / "objects/info/alternates"
    if alternates.exists() and alternates.stat().st_size:
        raise PublicationError(f"the {remote} local repository borrows objects; publish to an independent object store first")
    config = run(path, "config", "--local", "--name-only", "--list")
    if (common / "shallow").exists() or re.search(r"(?m)^(extensions\.partialclone|remote\..*\.promisor)$", config):
        raise PublicationError(f"the {remote} local repository has incomplete history or objects")


def advertisement(repo: Path | str, remote: str, run: GitReader = git) -> tuple[dict[str, str], str | None]:
    verify_original_history(repo, run)
    verify_remote(repo, remote, run)
    try:
        output = run(repo, "ls-remote", "--symref", "--", remote, "HEAD", "refs/heads/*", "refs/tags/*", "refs/pull/*/head")
    except PublicationError as exc:
        raise PublicationError(f"cannot read the {remote} remote") from exc
    refs: dict[str, str] = {}
    default = None
    for line in output.splitlines():
        value, _, ref = line.partition("\t")
        if value.startswith("ref: refs/heads/") and ref == "HEAD":
            default = value.removeprefix("ref: refs/heads/")
        elif re.fullmatch(r"[0-9a-f]{40,64}", value):
            refs[ref] = value
    return refs, default


def unpublished(repo: Path | str, roots: Iterable[str], tips: Iterable[str], run: GitReader = git) -> dict[str, str]:
    verify_original_history(repo, run)
    tips = set(tips)
    remaining = set(roots) - tips
    if not remaining:
        return {}
    objects = run(repo, "cat-file", "--batch-check=%(objectname) %(objecttype)", input_text="\n".join(tips) + "\n")
    available = [line.split()[0] for line in objects.splitlines() if line.split()[-1] in {"commit", "tag"}]
    output = run(
        repo,
        "rev-list",
        "--no-commit-header",
        "--format=%H %T",
        "--stdin",
        input_text="\n".join([*remaining, *(f"^{tip}" for tip in available)]) + "\n",
    )
    return dict(line.split() for line in output.splitlines())


def ref_coverage(repo: Path | str, oid: str, tips: dict[str, str], *, ancestors: bool, run: GitReader = git) -> dict:
    verify_original_history(repo, run)
    for ref, tip in tips.items():
        if tip == oid:
            return {"state": "covered", "method": "exact_advertisement", "remote_ref": ref}
    if ancestors:
        for ref, tip in tips.items():
            if ":refs/heads/" in ref and not unpublished(repo, [oid], [tip], run):
                return {"state": "covered", "method": "ancestor_of_live_tip", "remote_ref": ref}
    return {"state": "unknown", "method": "unknown", "remote_ref": None}


def check_retirement(repo: Path | str, run: GitReader = git) -> None:
    head = run(repo, "rev-parse", "HEAD")
    branch = run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    reflogs = ["HEAD", *([f"refs/heads/{branch}"] if branch != "HEAD" else [])]
    history = run(repo, "rev-list", "--walk-reflogs", "--no-commit-header", "--format=%H", *reflogs).split()
    snapshots = run(
        repo,
        "rev-list",
        "--no-commit-header",
        "--format=%T %H",
        "--stdin",
        "--not",
        head,
        "--branches",
        "--tags",
        input_text="\n".join(history) + "\n",
    )
    preserved = set(run(repo, "rev-list", "--no-commit-header", "--format=%T", head, "--branches", "--tags").split())
    roots = {head, *(line.split()[1] for line in snapshots.splitlines() if line.split()[0] not in preserved)}
    names = run(repo, "remote").splitlines()
    branch_remote = run(repo, "config", "--default", "", "--get", f"branch.{branch}.remote")
    remotes = dict.fromkeys(
        [
            "origin",
            *(["upstream"] if "upstream" in names else []),
            *([branch_remote] if branch_remote in names and branch_remote != "." else []),
        ]
    )
    for remote in remotes:
        refs, _ = advertisement(repo, remote, run)
        remaining = unpublished(repo, roots, refs.values(), run)
        if not remaining:
            return
        roots = set(remaining)
    raise PublicationError(
        "HEAD or unique retirement reflog content is not on origin, upstream or its branch remote; preserve it on a branch or publish it first"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify retirement history against live independent remotes without changing refs")
    parser.add_argument("repo", type=Path)
    args = parser.parse_args()
    try:
        check_retirement(args.repo)
    except (PublicationError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
