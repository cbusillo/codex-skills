#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Download a comment attachment without exposing the declared person's token."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import github_identity
import github_read
import director_mentions


APPROVAL_REPOSITORY = "cbusillo/codex-skills"
APPROVAL_COMMENT = 6241276473
APPROVAL_QUESTION = "Q467"
APPROVAL_URL = f"https://github.com/{APPROVAL_REPOSITORY}/issues/2021#issuecomment-{APPROVAL_COMMENT}"
STANDING_DIRECTOR = APPROVAL_REPOSITORY.split("/")[0]
# First line of the Director decision recorded on the approval comment.
RECORDED_AFFIRMATIVE = f"{APPROVAL_QUESTION}, and make it a standing approval"


class AttachmentError(Exception):
    """Only fixed, body-free diagnostics belong in this exception."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def personal_login(repository: str) -> str:
    if str(github_identity.configured_value("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH") or "").casefold() in {"1", "true", "yes"}:
        raise AttachmentError("automation-only mode refuses personal attachment authentication")
    if os.environ.get("GH_HOST", "github.com") != "github.com":
        raise AttachmentError("only github.com attachments are supported")
    login = github_identity.configured_value("GH_WITH_ENV_TOKEN_OWN_USER_LOGIN") or STANDING_DIRECTOR
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
        raise AttachmentError("configure GH_WITH_ENV_TOKEN_OWN_USER_LOGIN as the person's personal login")
    if not github_identity.REPOSITORY_PATTERN.fullmatch(repository) or repository.split("/")[0].casefold() != login.casefold():
        raise AttachmentError("attachment reads are limited to repositories owned by the declared person")
    if login.casefold() != STANDING_DIRECTOR.casefold() and not github_identity.own_user_opted_in():
        raise AttachmentError("no standing approval for this Director; requires the existing per-command own-user approval")
    return login


def attachment_name(url: str) -> str:
    # No alternate authority, query credentials, encoded path separators or traversal.
    image = re.fullmatch(
        r"https://github\.com/user-attachments/assets/([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", url,
    )
    if image:
        return f"asset-{image[1]}"
    match = re.fullmatch(r"https://github\.com/user-attachments/files/([0-9]+)/([A-Za-z0-9._%+-]+)", url)
    if not match:
        raise AttachmentError("expected a github.com/user-attachments/files or assets URL")
    name = urllib.parse.unquote(match[2])
    if not name or name in {".", ".."} or any(c in name for c in "/\\\x00\r\n"):
        raise AttachmentError("invalid attachment filename")
    # The caller and server cannot choose executable paths or escape evidence.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:160]
    return f"{match[1]}-{safe_name}"


def verify_comment(repository: str, comment: int, url: str):
    reader = github_read.GitHubReader(
        operation="github.attachment.download", repository=repository,
        gh_prefix_args=github_read.automation_only_gh_prefix_args(), strict_actor=True,
    )
    repo = reader.get_json(f"/repos/{repository}", step="verify_repository")
    if (not isinstance(repo, dict) or str(repo.get("full_name", "")).casefold() != repository.casefold()
            or not isinstance(repo.get("owner"), dict) or repo["owner"].get("type") != "User"):
        raise AttachmentError("repository identity changed or is not personally owned")
    record = reader.get_json(f"/repos/{repository}/issues/comments/{comment}", step="verify_comment")
    expected_issue = rf"https://api\.github\.com/repos/{re.escape(repository)}/issues/[0-9]+"
    if (not isinstance(record, dict) or record.get("id") != comment
            or not re.fullmatch(expected_issue, str(record.get("issue_url", "")), re.IGNORECASE)):
        raise AttachmentError("comment does not belong to the requested repository")
    links = re.findall(r'https://github\.com/user-attachments/(?:files/[0-9]+|assets)/[^\s<>()\]"\']+', str(record.get("body", "")))
    if url not in links:
        raise AttachmentError("attachment URL is absent from the requested comment")
    return reader, record


def authorization(login: str, reader, record: dict) -> str:
    if github_identity.own_user_opted_in():
        return "per_command"
    if (record.get("author_association") not in {"COLLABORATOR", "MEMBER"}
            or not isinstance(record.get("user"), dict) or record["user"].get("type") != "User"):
        raise AttachmentError("standing approval covers Client/staff collaborator comments, not unverified authors")

    def read_comment(repository: str, comment: int):
        if repository != APPROVAL_REPOSITORY or comment != APPROVAL_COMMENT:
            raise AttachmentError("unexpected standing-approval source")
        value = reader.get_json(f"/repos/{repository}/issues/comments/{comment}", step="standing_approval")
        if not isinstance(value, dict) or value.get("issue_url") != f"https://api.github.com/repos/{APPROVAL_REPOSITORY}/issues/2021":
            raise AttachmentError("unexpected standing-approval thread")
        return value

    def read_later(issue_url: str, since: str):
        path = director_mentions.thread_comments_path(issue_url)
        values = reader.paged_json(f"/{path}", step_prefix="approval_withdrawals",
                                   params={"since": since}, limit=1001)
        if len(values) > 1000:
            raise AttachmentError("standing-approval discussion coverage is incomplete")
        return values

    recorders = set(github_identity.configured_bot_logins())
    if primary := github_identity.automation_login():
        recorders.add(primary)
    gap = director_mentions.approval_gap(
        APPROVAL_URL, login, recorders, read_comment, read_later,
        question=APPROVAL_QUESTION, recorded_affirmative=RECORDED_AFFIRMATIVE,
    )
    if gap:
        raise AttachmentError(f"{APPROVAL_QUESTION} standing approval unavailable or withdrawn; no personal read attempted")
    return "standing_approval"


def personal_token(login: str) -> str:
    # Capture gh output privately; never delegate an arbitrary gh command with it.
    env = {key: value for key, value in os.environ.items()
           if key not in {"GH_TOKEN", "GITHUB_TOKEN", "CODEX_GITHUB_TOKEN", "GH_DEBUG"}}
    result = subprocess.run(
        ["gh", "auth", "token", "--hostname", "github.com", "--user", login],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=30,
    )
    token = result.stdout.decode().strip()
    if result.returncode or not token or any(c.isspace() for c in token):
        raise AttachmentError("declared personal gh login is unavailable; no fallback attempted")
    return token


def open_get(opener, url: str, token: str = ""):
    headers = {"User-Agent": "codex-skills-attachment"}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        return opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=30)
    except urllib.error.HTTPError as error:
        # Never display the response body, signed URL, or exception string.
        if error.code in {301, 302, 303, 307, 308}:
            return error
        status = error.code
        error.close()
        raise AttachmentError(f"GitHub attachment read returned HTTP {status}; no fallback attempted") from None


def verify_person(opener, token: str, login: str) -> None:
    with open_get(opener, "https://api.github.com/user", token) as response:
        if response.status != 200:
            raise AttachmentError("personal identity read did not return HTTP 200")
        user = json.loads(response.read(65536))
    if (not isinstance(user, dict) or str(user.get("login", "")).casefold() != login.casefold()
            or user.get("type") != "User"):
        raise AttachmentError("credential does not identify the declared personal login")


def allowed_redirect(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and parsed.port in {None, 443}
            and not parsed.username and not parsed.password and not parsed.fragment
            and (host.endswith(".githubusercontent.com")
                 or bool(re.fullmatch(r"github-production-user-asset-[0-9a-f]+\.s3\.amazonaws\.com", host))))


def download(opener, url: str, token: str, destination: pathlib.Path, max_bytes: int) -> dict:
    current = url
    for hop in range(6):
        with open_get(opener, current, token if hop == 0 else "") as response:
            if response.status in {301, 302, 303, 307, 308}:
                current = urllib.parse.urljoin(current, response.headers.get("Location", ""))
                if not allowed_redirect(current):
                    raise AttachmentError("attachment redirect is outside supported GitHub asset hosts")
                continue
            if response.status != 200:
                raise AttachmentError("attachment read did not return HTTP 200")
            if response.headers.get_content_type() == "text/html":
                raise AttachmentError("attachment returned an HTML page instead of a file")
            digest = hashlib.sha256()
            size = 0
            with tempfile.TemporaryFile(dir=destination.parent) as temporary:
                while chunk := response.read(min(65536, max_bytes - size + 1)):
                    size += len(chunk)
                    if size > max_bytes:
                        raise AttachmentError("attachment exceeds --max-bytes")
                    digest.update(chunk)
                    temporary.write(chunk)
                # O_EXCL refuses existing files and symlinks. Partial files are removed.
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as output:
                        temporary.seek(0)
                        while chunk := temporary.read(65536):
                            output.write(chunk)
                except BaseException:
                    destination.unlink()
                    raise
            return {"path": str(destination), "bytes": size, "sha256": digest.hexdigest()}
    raise AttachmentError("attachment exceeded the redirect limit")


def evidence_directory(value: str, repository: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise AttachmentError("--evidence-dir must be an absolute private task-evidence directory")
    roots = [parent for parent in path.parents if parent.name == "task-evidence"]
    if not roots:
        raise AttachmentError("downloads must stay under task-evidence/REPO/TASK")
    root = roots[0].resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise AttachmentError("evidence path escapes task evidence") from None
    if (len(relative.parts) < 2 or relative.parts[0] != repository.split("/")[1]
            or any((parent / ".git").exists() for parent in (resolved, *resolved.parents))):
        raise AttachmentError("downloads require task-evidence/REPO/TASK outside Git checkouts")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise AttachmentError("evidence directory must be owned by this user with mode 0700")
    return path.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--comment", required=True, type=int, help="numeric issue-comment ID")
    parser.add_argument("--url", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        login = personal_login(args.repo)
        name = attachment_name(args.url)
        if args.comment < 1 or args.max_bytes < 1:
            raise AttachmentError("comment and max-bytes must be positive")
        reader, record = verify_comment(args.repo, args.comment, args.url)
        authority = authorization(login, reader, record)
        directory = evidence_directory(args.evidence_dir, args.repo)
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            raise AttachmentError("evidence file already exists; choose a new evidence directory")
        token = personal_token(login)
        opener = urllib.request.build_opener(NoRedirect())
        verify_person(opener, token, login)
        result = download(opener, args.url, token, destination, args.max_bytes)
        print(json.dumps({"schema_version": 1, "ok": True, "operation": "github.attachment.download",
                          "authority": authority, "untrusted": True, **result}))
        return 0
    except AttachmentError as error:
        message = str(error)
    except github_read.GitHubReadError as error:
        # Metadata errors retain shared diagnostics, without comment bodies.
        message = f"repository/comment verification failed (HTTP {error.result.status}); no fallback attempted"
    except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
        message = "attachment read failed; no fallback attempted (details withheld to protect credentials and file contents)"
    print(json.dumps({"schema_version": 1, "ok": False, "operation": "github.attachment.download", "error": message}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
