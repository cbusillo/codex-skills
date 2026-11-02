#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path

DEFAULT_TRACKING_ISSUE = "cbusillo/codex-skills#2006"


class DigestError(ValueError):
    pass


def archive(report: str, tracking_issue: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    github_comment = importlib.import_module("skills.github.scripts.github_comment")

    target = re.fullmatch(r"([\w.-]+/[\w.-]+)#([0-9]+)", tracking_issue)
    if not target:
        raise DigestError("digest_tracking_issue_invalid")
    try:
        result = github_comment.comment("issue", int(target[2]), report, repo=target[1], dedupe_body=True)
    except github_comment.CommentError:
        raise DigestError("digest_archive_failed") from None
    url = result.get("url")
    if not isinstance(url, str) or not re.fullmatch(
            rf"https://github\.com/{re.escape(target[1])}/issues/{target[2]}#issuecomment-[0-9]+", url, re.IGNORECASE):
        raise DigestError("digest_archive_response_invalid")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--tracking-issue", default=DEFAULT_TRACKING_ISSUE)
    args = parser.parse_args(argv)
    try:
        print(archive(args.file.read_text(encoding="utf-8"), args.tracking_issue), flush=True)
        return 0
    except DigestError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError):
        print("digest_archive_failed", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
