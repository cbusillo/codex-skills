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


class AttachmentError(Exception):
    """Only fixed, body-free diagnostics belong in this exception."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def personal_login(repository: str) -> str:
    if not github_identity.own_user_opted_in():
        raise AttachmentError("requires Director approval and GH_WITH_ENV_TOKEN_OWN_USER=1 on this command")
    if str(github_identity.configured_value("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH") or "").casefold() in {"1", "true", "yes"}:
        raise AttachmentError("automation-only mode refuses personal attachment authentication")
    if os.environ.get("GH_HOST", "github.com") != "github.com":
        raise AttachmentError("only github.com attachments are supported")
    login = github_identity.configured_value("GH_WITH_ENV_TOKEN_OWN_USER_LOGIN") or ""
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login):
        raise AttachmentError("configure GH_WITH_ENV_TOKEN_OWN_USER_LOGIN as the person's personal login")
    if not github_identity.REPOSITORY_PATTERN.fullmatch(repository) or repository.split("/")[0].casefold() != login.casefold():
        raise AttachmentError("attachment reads are limited to repositories owned by the declared person")
    return login


def attachment_name(url: str) -> str:
    # No alternate authority, query credentials, encoded path separators or traversal.
    match = re.fullmatch(r"https://github\.com/user-attachments/files/([0-9]+)/([A-Za-z0-9._%+-]+)", url)
    if not match:
        raise AttachmentError("expected a github.com/user-attachments/files URL")
    name = urllib.parse.unquote(match[2])
    if not name or name in {".", ".."} or any(c in name for c in "/\\\x00\r\n"):
        raise AttachmentError("invalid attachment filename")
    # The caller and server cannot choose executable paths or escape evidence.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:160]
    return f"{match[1]}-{safe_name}"


def verify_comment(repository: str, comment: int, url: str) -> None:
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
    links = re.findall(r'https://github\.com/user-attachments/files/[0-9]+/[^\s<>()\]"\']+', str(record.get("body", "")))
    if url not in links:
        raise AttachmentError("attachment URL is absent from the requested comment")


def personal_token(login: str) -> str:
    # Capture gh output privately; never delegate an arbitrary gh command with it.
    env = {key: value for key, value in os.environ.items()
           if key not in {"GH_TOKEN", "GITHUB_TOKEN", "CODEX_GITHUB_TOKEN", "GH_DEBUG"}}
    result = subprocess.run(
        ["gh", "auth", "token", "--hostname", "github.com", "--user", login],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=30, check=False,
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


def evidence_directory(value: str) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise AttachmentError("--evidence-dir must be an absolute private task-evidence directory")
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
        verify_comment(args.repo, args.comment, args.url)
        directory = evidence_directory(args.evidence_dir)
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            raise AttachmentError("evidence file already exists; choose a new evidence directory")
        token = personal_token(login)
        opener = urllib.request.build_opener(NoRedirect())
        verify_person(opener, token, login)
        result = download(opener, args.url, token, destination, args.max_bytes)
        print(json.dumps({"schema_version": 1, "ok": True, "operation": "github.attachment.download", **result}))
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
