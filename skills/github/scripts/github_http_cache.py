#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Bounded private conditional GET bodies; no credentials or offline success."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
import time
import urllib.parse
from typing import Any, Callable

MAX_ENTRIES = 256
MAX_ENTRY_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_AGE_SECONDS = 86400
RESPONSE_HEADERS = frozenset({'etag', 'last-modified', 'link', 'content-type', 'x-poll-interval'})
TOKEN_PATTERN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+\S+)', re.I)
SENSITIVE_KEYS = frozenset({'token', 'access_token', 'refresh_token', 'authorization', 'password', 'secret', 'private_key'})


def repository_from_path(path: str) -> str | None:
    match = re.match(r'^/?repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/|$)', urllib.parse.urlsplit(path).path)
    return match[1] if match else None


def _root() -> pathlib.Path:
    # Share the retry state's isolation in fixtures and across session processes.
    runtime = os.environ.get('CODE_HOME') or os.environ.get('CODEX_HOME')
    state = pathlib.Path(os.environ.get('GITHUB_RETRY_STATE_DIR') or (
        str(pathlib.Path(runtime).expanduser() / 'state/github-retry') if runtime else str(pathlib.Path.home() / '.code/state/github-retry')
    )).expanduser()
    return pathlib.Path(os.environ.get('GITHUB_HTTP_CACHE_DIR') or state / 'http-cache').expanduser()


def _owned(path: pathlib.Path, *, directory: bool = False) -> bool:
    info = path.lstat()
    return info.st_uid == os.getuid() and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def _sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).casefold() in SENSITIVE_KEYS or _sensitive(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_sensitive(item) for item in value)
    return isinstance(value, str) and TOKEN_PATTERN.search(value) is not None


def _read(path: pathlib.Path, key: str) -> dict[str, Any] | None:
    try:
        if not _owned(path) or path.stat().st_size > MAX_ENTRY_BYTES:
            return None
        item = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(item, dict) or item.get('key') != key or 'body' not in item
                or not isinstance(item.get('headers'), dict)
                or not isinstance(item.get('saved_at'), (int, float))
                or not 0 <= time.time() - item['saved_at'] <= MAX_AGE_SECONDS):
            return None
        validators = item['headers']
        if not any(isinstance(validators.get(name), str) and validators[name] for name in ('etag', 'last-modified')):
            return None
        return item
    except (OSError, ValueError, TypeError):
        return None


def _publish(root: pathlib.Path, path: pathlib.Path, item: dict[str, Any]) -> None:
    data = json.dumps(item, separators=(',', ':')).encode()
    if len(data) > MAX_ENTRY_BYTES or _sensitive(item):
        path.unlink(missing_ok=True)
        return
    # No lock is held while contacting GitHub or waiting for a reset. Publication
    # is atomic; competing GETs can cause an extra 200 but cannot serve offline.
    fd, name = tempfile.mkstemp(prefix='.body-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(name, path)
        entries = []
        for entry in root.glob('*.json'):
            try:
                if _owned(entry):
                    info = entry.stat()
                    if time.time() - info.st_mtime > MAX_AGE_SECONDS:
                        entry.unlink(missing_ok=True)
                    else:
                        entries.append((info.st_mtime, info.st_size, entry))
            except FileNotFoundError:
                continue
        total = sum(size for _, size, _ in entries)
        entries.sort(key=lambda entry: entry[0])
        while entries and (len(entries) > MAX_ENTRIES or total > MAX_TOTAL_BYTES):
            _, size, old = entries.pop(0)
            old.unlink(missing_ok=True)
            total -= size
    finally:
        pathlib.Path(name).unlink(missing_ok=True)


def request(
    transport: Callable[[dict[str, str]], Any], *, method: str, path: str,
    body: Any, headers: dict[str, str], api_version: str, host: str,
    actor: str | None, expected_actor: str | None, identity_scope: str, enabled: bool,
) -> Any:
    normalized = {name.casefold(): value for name, value in headers.items()}
    # Identity probes, grants, credential surfaces, logs and caller-owned
    # validators keep their existing uncached contract.
    if (not enabled or method.upper() != 'GET' or body is not None or not expected_actor
            or (actor and actor.casefold() != expected_actor.casefold())
            or not repository_from_path(path)
            or any(name in normalized for name in ('authorization', 'if-none-match', 'if-modified-since'))
            or _sensitive(path) or urllib.parse.urlsplit(path).query and any(
                key.casefold() in SENSITIVE_KEYS for key, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
            or any(part in urllib.parse.urlsplit(path).path.split('/') for part in ('secrets', 'tokens', 'logs'))):
        return transport(headers)
    root = _root()
    key = hashlib.sha256(json.dumps([
        host.casefold(), expected_actor.casefold(), identity_scope, path,
        api_version, sorted(normalized.items()),
    ], separators=(',', ':')).encode()).hexdigest()
    entry = root / f'{key}.json'
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not _owned(root, directory=True):
            raise OSError('Cache directory is not owned')
        root.chmod(0o700)
        cached = _read(entry, key)
    except OSError:
        result = transport(headers)
        result.headers['x-codex-cache'] = 'unavailable'
        return result
    conditional = dict(headers)
    if cached:
        validators = cached['headers']
        if validators.get('etag'):
            conditional['If-None-Match'] = validators['etag']
        else:
            conditional['If-Modified-Since'] = validators['last-modified']
    result = transport(conditional)
    # Never persist or accept a response from an authorized alternate actor or
    # an actor-mismatch failure, even if a caller expected the original actor.
    same_actor = (result.expected_actor == expected_actor and
                  (result.actor is None or result.actor.casefold() == expected_actor.casefold()))
    if not same_actor:
        return result
    if result.status == 304 and cached and result.failure and result.failure.cause not in {'actor_mismatch', 'invalid_credentials'}:
        result.ok = True
        result.failure = None
        result.failed_step = None
        result.body = cached['body']
        result.headers = {**cached['headers'], **result.headers, 'x-codex-cache': 'revalidated'}
    elif not result.ok or result.status != 200:
        return result
    if not any(result.headers.get(name) for name in ('etag', 'last-modified')):
        return result
    try:
        _publish(root, entry, {'key': key, 'body': result.body, 'saved_at': time.time(),
                              'headers': {name: value for name, value in result.headers.items() if name in RESPONSE_HEADERS}})
    except OSError:
        # A completed remote request must not be repeated on a disk failure.
        result.headers['x-codex-cache'] = 'unavailable'
    return result
