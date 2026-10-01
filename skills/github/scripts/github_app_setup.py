#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Owner-operated manifest registration and private automation configuration."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import github_capabilities
import github_identity as identity

Error = identity.GitHubAppError
MANAGED_IDENTITY_KEYS = frozenset({
    "GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_API_URL", "GH_HOST", "CODEX_AUTOMATION_LOGIN", "CODEX_AUTOMATION_EMAIL",
    "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN", "GIT_COMMIT_AS_BOT_NAME", "GIT_COMMIT_AS_BOT_EMAIL",
})
ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=")


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Error("setup directory must be owned by you with mode 700")


def private_write(path: Path, content: str) -> None:
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(content)


def manifest(name: str, callback: str) -> dict:
    return {
        "name": name,
        "url": "https://github.com/cbusillo/codex-skills",
        "description": "Repository automation for the owner's adopted catalogs",
        "public": False,
        "redirect_url": callback,
        "hook_attributes": {"url": "https://example.com/unused", "active": False},
        "default_events": [],
        "default_permissions": github_capabilities.permission_profile(
            github_capabilities.load_matrix())["permissions"]["repository"],
    }


def register(session: Path, owner: str, name: str, *, organization: bool = False,
             timeout: int = 900, open_browser=webbrowser.open, exchange=None) -> dict:
    """The owner submits GitHub's form; no active gh credential is used."""
    nonce = secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    result = {}
    failure = []
    deadline = time.monotonic() + timeout
    request_timeout = min(1.0, timeout)
    if exchange is None:
        def exchange(code):
            request = urllib.request.Request(
                f"https://api.github.com/app-manifests/{code}/conversions",
                data=b"", method="POST", headers={"Accept": "application/vnd.github+json"})
            return identity.github_app_request(request, operation="manifest conversion (not retried)")

    class Handler(BaseHTTPRequestHandler):
        server: HTTPServer
        timeout = request_timeout

        def log_message(self, _format: str, *args: object) -> None:
            pass  # Callback codes and private response bodies never enter access logs.

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            parsed = urllib.parse.urlsplit(self.path)
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return
            if parsed.path == f"/{nonce}":
                target = (f"https://github.com/organizations/{owner}/settings/apps/new"
                          if organization else "https://github.com/settings/apps/new")
                callback = f"http://127.0.0.1:{self.server.server_port}/{nonce}/callback"
                encoded = html.escape(json.dumps(manifest(name, callback)))
                body = (f'<form method="post" action="{target}?state={state}">'
                        f'<input type="hidden" name="manifest" value="{encoded}">'
                        '<p>Review and create this private automation App on GitHub.</p>'
                        '<button>Create GitHub App</button></form>')
            elif parsed.path == f"/{nonce}/callback":
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("state") != [state] or not re.fullmatch(
                        r"[A-Za-z0-9]+", (query.get("code") or [""])[0]) or len(query.get("code", [])) != 1:
                    self.send_error(403)
                    return
                if result or failure:
                    self.send_error(409)
                    return
                try:
                    payload = exchange(query["code"][0])
                    if not isinstance(payload, dict):
                        raise Error("manifest conversion returned an invalid response")
                    result.update(save_registration(session, owner, payload))
                    body = "App key saved privately. Return to the terminal to install the App."
                except (Error, OSError, ValueError, TypeError) as error:
                    failure.append(error if isinstance(error, Error) else Error(
                        "registration response or private save failed; inspect the App in GitHub and import its key"))
                    body = "Setup stopped. Return to the terminal; no callback retry was attempted."
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body.encode())

    def handler_factory(request, address, http_server):
        return Handler(request, address, http_server)

    with HTTPServer(("127.0.0.1", 0), handler_factory) as server:
        server.timeout = 1
        url = f"http://127.0.0.1:{server.server_port}/{nonce}"
        print(f"Open {url}", file=sys.stderr, flush=True)
        open_browser(url)
        while not result and not failure and time.monotonic() < deadline:
            server.handle_request()
    if failure:
        raise failure[0]
    if not result:
        raise Error("registration timed out; no credentials were configured")
    return result


def save_registration(session: Path, owner: str, payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), int) or payload["id"] <= 0:
        raise Error("manifest conversion returned an invalid App ID")
    slug = payload.get("slug", "")
    observed_owner = payload.get("owner", {}).get("login", "")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-zA-Z0-9-]+", slug) or not isinstance(observed_owner, str) or not observed_owner:
        raise Error("registered App owner or slug is invalid; inspect it on GitHub")
    key = payload.get("pem", "")
    if not isinstance(key, str) or "PRIVATE KEY" not in key:
        raise Error("manifest conversion did not return a private key")
    private_write(session / "app.pem", key)
    record = {"app_id": str(payload["id"]), "slug": slug, "owner": owner, "registered_owner": observed_owner}
    private_write(session / "registration.json", json.dumps(record) + "\n")
    if observed_owner.casefold() != owner.casefold():
        raise Error("registered App owner did not match; key saved privately. Correct the App ownership in GitHub, then resume, or import the intended App")
    return record  # OAuth client secret and webhook secret are unused and never persisted.


def configure(session: Path, *, installation_id: str | None = None,
              replace_identity: bool = False, environ=None) -> dict:
    private_directory(session)
    record_path = session / "registration.json"
    if not record_path.exists():
        raise Error("no completed registration is saved; inspect GitHub's App settings and use import with its private key, or start again if no App was created")
    if record_path.is_symlink():
        raise Error("registration record must not be a symlink")
    record = json.loads(record_path.read_text())
    env = dict(os.environ if environ is None else environ)
    target = identity.env_file_path(env)
    if target is None:
        raise Error("cannot locate the wrapper's local.env")
    config_env = {"HOME": env.get("HOME", str(Path.home())),
                  "CODEX_SKILLS_ENV_FILE": str(session / "no-env"),
                  "GITHUB_APP_ID": record["app_id"],
                  "GITHUB_APP_INSTALLATION_ID": installation_id or "0",
                  "GITHUB_APP_PRIVATE_KEY_PATH": str(session / "app.pem")}
    # The public CLI always verifies against GitHub; loopback is injectable only in fixtures.
    config = identity.github_app_config(config_env)
    headers = identity.github_app_headers(config)
    app = identity.github_app_request(urllib.request.Request(
        f"{config.api_url}/app", method="GET", headers=headers), operation="setup App ownership")
    if not isinstance(app, dict) or str(app.get("id")) != record["app_id"] or app.get("slug") != record["slug"] or app.get("owner", {}).get("login", "").casefold() != record["owner"].casefold():
        raise Error("App ownership does not match the intended account; correct it in GitHub before resuming")
    if installation_id is None:
        matches = []
        page = 1
        while True:
            data = identity.github_app_request(urllib.request.Request(
                f"{config.api_url}/app/installations?per_page=100&page={page}", method="GET", headers=headers),
                operation="setup installation discovery")
            if not isinstance(data, list):
                raise Error("installation discovery returned an invalid response")
            matches.extend(item for item in data if item.get("account", {}).get("login", "").casefold() == record["owner"].casefold())
            if len(data) < 100:
                break
            page += 1
            if page > 10:
                raise Error("installation discovery incomplete; resume with --installation-id")
        if len(matches) != 1:
            raise Error("install the App on the intended account, then resume; use --installation-id if ambiguous")
        installation_id = str(matches[0]["id"])
    config_env["GITHUB_APP_INSTALLATION_ID"] = installation_id
    config = identity.github_app_config(config_env)
    observed = identity.github_app_installation_metadata(config)
    if observed.get("account", {}).get("login", "").casefold() != record["owner"].casefold() or observed.get("actor") != record["slug"] + "[bot]":
        raise Error("installation does not belong to the registered App and intended account")
    if observed.get("suspended"):
        raise Error("installation is suspended")
    required = manifest("", "")["default_permissions"]
    if github_capabilities.permission_gaps(required, observed.get("permissions", {})):
        raise Error("installation lacks the catalog permissions; accept its update, then resume")
    login = record["slug"] + "[bot]"
    user = identity.github_app_request(urllib.request.Request(
        f"{config.api_url}/users/{urllib.parse.quote(login)}",
        method="GET", headers={"Accept": "application/vnd.github+json"}), operation="public bot identity")
    if not isinstance(user, dict) or user.get("login") != login or not isinstance(user.get("id"), int) or user["id"] <= 0:
        raise Error("public bot identity did not match")
    values = {"GITHUB_APP_ID": config.app_id, "GITHUB_APP_INSTALLATION_ID": config.installation_id,
              "GITHUB_APP_PRIVATE_KEY_PATH": str(config.private_key_path),
              "GITHUB_APP_API_URL": config.api_url, "GH_HOST": "github.com",
              "CODEX_AUTOMATION_LOGIN": login,
              "CODEX_AUTOMATION_EMAIL": f'{user["id"]}+{login}@users.noreply.github.com'}
    write_configuration(target, values, backup_directory=session, replace_identity=replace_identity)
    limits = ([{"kind": "all_repositories", "detail": "App installation covers all repositories, including future repositories. Review its settings if only adopted repositories were intended."}]
              if observed.get("repository_selection") == "all" else [])
    return {"schema_version": 1, "ok": True, "actor": login, "installation_verified": True,
            "repository_selection": observed.get("repository_selection"),
            "limits": limits, "write_proof": "not_exercised", "configuration_written": True}


def configuration_before(target: Path, *, replace_identity: bool) -> bytes:
    before = b""
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Error("local.env must be a regular file owned by you")
        before = target.read_bytes()
    existing = [line for line in before.decode().splitlines()
                if (match := ASSIGNMENT.match(line)) and match[1] in MANAGED_IDENTITY_KEYS]
    if existing and not replace_identity:
        raise Error("identity variables already exist; inspect them or add --replace-identity before setup (private backup kept)")
    return before


def write_configuration(target: Path, values: dict, *, backup_directory: Path, replace_identity: bool) -> None:
    target = target.expanduser().absolute()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = target.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise Error("local.env parent must be owned by you and not writable by others")
    before = configuration_before(target, replace_identity=replace_identity)
    text = before.decode()
    overrides = {"GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": values["CODEX_AUTOMATION_LOGIN"],
                 "GIT_COMMIT_AS_BOT_NAME": values["CODEX_AUTOMATION_LOGIN"],
                 "GIT_COMMIT_AS_BOT_EMAIL": values["CODEX_AUTOMATION_EMAIL"]}
    managed = set(values) | set(overrides)
    output = []
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match and match[1] in managed:
            if match[1] in overrides:
                values[match[1]] = overrides[match[1]]
            continue
        output.append(line)
    output.extend(f"{key}={shlex.quote(value)}" for key, value in values.items())
    if before:
        private_directory(backup_directory)
        backup = backup_directory / f"local-env-before-{secrets.token_hex(6)}.env"
        private_write(backup, text)
    descriptor, temporary = tempfile.mkstemp(prefix=".app-env-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(output) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink() or (target.read_bytes() if target.exists() else b"") != before:
            raise Error("local.env changed during setup; preserved it, resume after resolving the change")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Owner browser creates an App, installs it, and configures automation")
    start.add_argument("--owner", required=True)
    start.add_argument("--name", required=True)
    start.add_argument("--organization", action="store_true")
    start.add_argument("--timeout", type=int, default=900)
    finish = commands.add_parser("resume", help="Finish a privately saved registration after installing the App")
    finish.add_argument("--session", type=Path, required=True)
    finish.add_argument("--installation-id")
    existing = commands.add_parser("import", help="Recover an existing GitHub.com App using a downloaded private key")
    existing.add_argument("--owner", required=True)
    existing.add_argument("--app-id", type=int, required=True)
    existing.add_argument("--slug", required=True)
    existing.add_argument("--key", type=Path, required=True)
    existing.add_argument("--installation-id")
    for command in (start, finish, existing):
        command.add_argument("--replace-identity", action="store_true")
    args = parser.parse_args(argv)
    session = None
    try:
        if args.command in ("start", "import"):
            if not re.fullmatch(r"[A-Za-z0-9-]+", args.owner) or not 1 <= getattr(args, "timeout", 900) <= 3600:
                raise Error("use a GitHub owner name and a timeout from 1 to 3600 seconds")
            target = identity.env_file_path()
            if target is None:
                raise Error("cannot locate the wrapper's local.env")
            configuration_before(target, replace_identity=args.replace_identity)
            root = Path.home() / ".config" / "codex-skills" / "github-app"
            private_directory(root)
            session = Path(tempfile.mkdtemp(prefix="setup-", dir=root))
            print(f"Private setup record: {session}; resume with --session if interrupted.", file=sys.stderr)
            if args.command == "import":
                source = args.key.expanduser()
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise Error("import key must be a regular file owned by you with mode 600")
                record = save_registration(session, args.owner, {
                    "id": args.app_id, "slug": args.slug, "pem": source.read_text(),
                    "owner": {"login": args.owner}})
            else:
                record = register(session, args.owner, args.name, organization=args.organization, timeout=args.timeout)
            url = f'https://github.com/apps/{record["slug"]}/installations/new'
            print(f"Install on {record['owner']}, selecting the adopted repositories: {url}", file=sys.stderr)
            if args.command == "start":
                webbrowser.open(url)
                input("After installing in the browser, press Enter here: ")
        else:
            session = args.session.expanduser().absolute()
        result = configure(session, installation_id=getattr(args, "installation_id", None),
                           replace_identity=args.replace_identity)
        for limit in result["limits"]:
            print("Scope notice: " + limit["detail"], file=sys.stderr)
        print(json.dumps(result))
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError, EOFError, KeyboardInterrupt) as error:
        # OSError/parse text can contain private paths or provider payloads.
        message = str(error) if isinstance(error, Error) else "setup interrupted or local state unreadable; inspect the saved session and resume"
        if session is not None and not (session / "registration.json").exists():
            message = "no completed registration is saved; inspect GitHub's App settings and import its key if it exists, or start again if no App was created"
        print(json.dumps({"schema_version": 1, "ok": False, "error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
