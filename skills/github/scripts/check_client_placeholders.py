# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import subprocess
from pathlib import Path

from github_plan_status.client_test_notes import CLIENT_PLACEHOLDER_PREFIX


def check_client_placeholders(
    repo_root: Path, paths: list[str], revision: str = "HEAD"
) -> int:
    result = subprocess.run(
        [
            "git",
            "grep",
            "-n",
            "-I",
            "-F",
            "-e",
            CLIENT_PLACEHOLDER_PREFIX,
            revision,
            "--",
            *paths,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 1:
        return 0
    if result.returncode != 0:
        raise RuntimeError(
            "Could not read the committed content for the Client placeholder check."
        )
    for match in result.stdout.splitlines():
        location = match.removeprefix(revision + ":").split(":", 2)
        print(f"{location[0]}:{location[1]}: unresolved Client placeholder")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    return check_client_placeholders(args.repo_root, args.paths, args.revision)


if __name__ == "__main__":
    raise SystemExit(main())
