#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Send digest Markdown from --file or stdin; print confirmed message links.

No retries: a failed or interrupted multipart send needs receiver reconciliation.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import digest_summary

KEY = "DIRECTOR_DIGEST_DISCORD_WEBHOOK_URL"
DEFAULT_TRACKING_ISSUE = "cbusillo/codex-skills#2006"


class DeliveryError(Exception):
    """A value-free diagnostic safe for retained sender logs."""


def webhook_url():
    value = os.environ.get(KEY)
    if value is None:
        home = os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".code")
        path = Path(home) / "local.env"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            match = re.match(r"^\s*(?:export\s+)?" + KEY + r"\s*=(.*)$", line)
            if match:
                try:
                    parts = shlex.split(match[1], comments=True)
                except ValueError:
                    raise DeliveryError("digest_webhook_invalid") from None
                if len(parts) > 1:
                    raise DeliveryError("digest_webhook_invalid")
                value = parts[0] if parts else ""
    if not value or not value.strip():
        raise DeliveryError("digest_webhook_missing")
    return value.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeliveryError("digest_http_redirect")


def request_json(opener: urllib.request.OpenerDirector, url: str, payload: dict | None = None,
                 attachment: str | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    content_type = "application/json"
    if attachment is not None:
        boundary = uuid.uuid4().hex
        content_type = f"multipart/form-data; boundary={boundary}"
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                f'Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="stranded-work.md"\r\n'
                f'Content-Type: text/plain; charset=utf-8\r\n\r\n{attachment}\r\n--{boundary}--\r\n').encode()
    request = urllib.request.Request(url, data=data, headers={
        "Content-Type": content_type, "User-Agent": "codex-skills-digest/1.0"})
    try:
        with opener.open(request, timeout=20) as response:
            result = json.load(response)
            # Pace successful multipart sends using Discord's bucket headers.
            # An HTTP failure is still never replayed.
            if response.headers.get("X-RateLimit-Remaining") == "0":
                delay = float(response.headers.get("X-RateLimit-Reset-After", "0"))
                if not 0 <= delay <= 30:
                    raise DeliveryError("digest_rate_limit_invalid")
                time.sleep(delay)
    except (OSError, ValueError, http.client.HTTPException):
        # HTTP errors and their response bodies can echo credentials. Never emit them.
        raise DeliveryError("digest_http_failed") from None
    if not isinstance(result, dict):
        raise DeliveryError("digest_response_invalid")
    return result


def snowflake(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise DeliveryError("digest_response_invalid")
    return value


def chunks(report):
    # UTF-16 units also keep astral Unicode within Discord's length accounting.
    part = ""
    units = 0
    for char in report:
        size = len(char.encode("utf-16-le")) // 2
        if units + size > 2000:
            yield part
            part, units = "", 0
        part += char
        units += size
    if part:
        yield part


def validate(report, url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise DeliveryError("digest_webhook_invalid")
    # Loopback HTTP supports offline fixtures; production credentials use HTTPS.
    if not ((parsed.scheme == "https" and parsed.hostname == "discord.com") or
            (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})):
        raise DeliveryError("digest_webhook_invalid")
    if not re.fullmatch(r"/api/(?:v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9._-]+", parsed.path):
        raise DeliveryError("digest_webhook_invalid")
    if not report.strip():
        raise DeliveryError("digest_report_empty")
    if url in report or parsed.path.rsplit("/", 1)[-1] in report:
        raise DeliveryError("digest_report_contains_credential")


def deliver(report: str, url: str, attachment: str | None = None) -> None:
    validate(report, url)
    if attachment is not None:
        validate(attachment, url)
        if digest_summary.units(report) >= 2000:
            raise DeliveryError("digest_summary_too_large")
    opener = urllib.request.build_opener(NoRedirect())
    metadata = request_json(opener, url)
    guild = snowflake(metadata.get("guild_id"))
    channel = snowflake(metadata.get("channel_id"))
    for part in chunks(report):
        message = request_json(opener, url + "?wait=true", {
            "content": part, "allowed_mentions": {"parse": []}}, attachment)
        message_id = snowflake(message.get("id"))
        if snowflake(message.get("channel_id")) != channel:
            raise DeliveryError("digest_response_invalid")
        print(f"https://discord.com/channels/{guild}/{channel}/{message_id}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Markdown file; otherwise read stdin")
    parser.add_argument("--tracking-issue", default=DEFAULT_TRACKING_ISSUE,
                        help="Pinned OWNER/REPO#NUMBER for stranded-work full lists")
    args = parser.parse_args(argv)
    stage = "digest_webhook_read_failed"
    try:
        url = webhook_url()
        stage = "digest_report_read_failed"
        report = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        stage = "digest_report_validation_failed"
        validate(report, url)
        attachment = None
        if report.startswith("Daily stranded-work sweep\n"):
            stage = "digest_summary_failed"
            digest_summary.presentation(report, "https://github.com/" + args.tracking_issue.replace("#", "/issues/")
                                        + "#issuecomment-" + "9" * 20)
            stage = "digest_archive_failed"
            archive_url = digest_summary.archive(report, args.tracking_issue)
            stage = "digest_summary_failed"
            prepared = digest_summary.presentation(report, archive_url)
            report, attachment = prepared.content, prepared.attachment
        stage = "digest_delivery_failed"
        if attachment is None:
            deliver(report, url)
        else:
            deliver(report, url, attachment)
        return 0
    except (DeliveryError, digest_summary.DigestError) as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError):
        print(stage, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
