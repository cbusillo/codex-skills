#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Isolated setup/credential and no-bypass repository protocol fixtures."""

import json
import os
import stat
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import github_app_setup as setup
import github_identity as identity
import github_rulesets
from test_github_identity import test_private_key

SCRIPTS = Path(__file__).resolve().parent
OWNER = "fixture-owner"
SLUG = "fixture-automation"


def registration(session):
    return setup.save_registration(session, OWNER, {
        "id": 7, "slug": SLUG, "owner": {"login": OWNER},
        "pem": test_private_key(), "client_secret": "unused-secret",
        "webhook_secret": "unused-hook"})


class FixtureServer(ThreadingHTTPServer):
    owner: str
    permissions: dict
    suspended: str | None
    author: str
    approved: bool
    direction: dict


class FixtureHandler(BaseHTTPRequestHandler):
    server: FixtureServer

    def log_message(self, format: str, *args: object) -> None:
        pass

    def reply(self, status, payload):
        self.send_response(status)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/app":
            self.reply(200, {"slug": SLUG})
        elif self.path.startswith("/app/installations"):
            item = {"id": 31, "app_id": 7, "app_slug": SLUG,
                    "account": {"login": self.server.owner, "type": "User"},
                    "permissions": self.server.permissions,
                    "repository_selection": "selected", "suspended_at": self.server.suspended}
            self.reply(200, [item] if "?" in self.path else item)
        elif self.path.startswith("/users/"):
            self.reply(200, {"id": 99, "login": SLUG + "[bot]"})
        else:
            self.reply(404, {})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path.endswith("/access_tokens"):
            self.reply(201, {"token": "fixture-app-token", "expires_at": "2099-01-01T00:00:00Z"})
            return
        actor = SLUG + "[bot]" if self.headers.get("Authorization") == "Bearer fixture-app-token" else OWNER
        if self.path == "/fixture/pull":
            self.server.author = actor
            self.reply(201, {"author": actor})
        elif self.path == "/fixture/review":
            if actor == self.server.author:
                self.reply(422, {"message": "author cannot approve own PR"})
            else:
                if actor != self.server.owner:
                    self.reply(403, {})
                    return
                self.server.approved = True
                self.reply(200, {"reviewer": actor})
        elif self.path == "/fixture/merge":
            direction = self.server.direction
            requires_owner = any(rule.get("parameters", {}).get("require_code_owner_review")
                                 for rule in direction["rules"])
            allowed = bool(direction["bypass_actors"]) or not requires_owner or self.server.approved
            self.reply(200 if allowed else 403, {"merged": allowed})
        else:
            self.reply(404, {})


def fixture_server():
    def handler_factory(request, address, http_server):
        return FixtureHandler(request, address, http_server)

    server = FixtureServer(("127.0.0.1", 0), handler_factory)
    server.owner = OWNER
    server.permissions = setup.manifest("fixture", "http://127.0.0.1/callback")["default_permissions"]
    server.suspended = None
    server.approved = False
    server.direction = github_rulesets.standard_specs(7)[1].payload
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def configured_fixture(session, home, api, **options):
    real_config = identity.github_app_config

    def routed_config(env):
        return real_config(dict(env, GITHUB_APP_API_URL=api))

    with patch.object(identity, "github_app_config", side_effect=routed_config):
        return setup.configure(session, environ={"HOME": str(home)}, **options)


def test_manifest_callback_and_private_recovery():
    with tempfile.TemporaryDirectory() as directory:
        session = Path(directory)
        session.chmod(0o700)
        exchanges = []
        workers = []

        def browser(url):
            def visit():
                page = urllib.request.urlopen(url).read().decode()
                import html
                import re
                form = re.search(r'name="manifest" value="([^"]+)"', page)[1]
                manifest = json.loads(html.unescape(form))
                assert manifest["default_permissions"] == setup.manifest("", "")["default_permissions"]
                assert manifest["public"] is False and manifest["hook_attributes"]["active"] is False
                state = re.search(r'action="[^\"]+\?state=([^\"]+)"', page)[1]
                callback = manifest["redirect_url"]
                try:
                    urllib.request.urlopen(callback + "?code=fakecode&state=wrong")
                    raise AssertionError("wrong state accepted")
                except urllib.error.HTTPError as error:
                    assert error.code == 403
                assert not exchanges
                urllib.request.urlopen(callback + "?" + urllib.parse.urlencode({"code": "fakecode", "state": state})).read()
            browser_worker = threading.Thread(target=visit)
            workers.append(browser_worker)
            browser_worker.start()

        def exchange(code):
            exchanges.append(code)
            return {"id": 7, "slug": SLUG, "owner": {"login": OWNER}, "pem": test_private_key(),
                    "client_secret": "unused-secret", "webhook_secret": "unused-hook"}

        result = setup.register(session, OWNER, "Fixture", timeout=10, open_browser=browser, exchange=exchange)
        for worker in workers:
            worker.join()
        assert result["slug"] == SLUG and exchanges == ["fakecode"]
        assert stat.S_IMODE((session / "app.pem").stat().st_mode) == 0o600
        assert "unused-secret" not in (session / "registration.json").read_text()


def test_fixture_repository_setup_and_direction_review():
    server, api = fixture_server()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            setup.private_directory(session)
            registration(session)
            home = root / "home"
            env_file = home / ".code" / "local.env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text("# preserve operator context\nUNRELATED='kept value'\n")
            result = configured_fixture(session, home, api)
            assert result["actor"] != OWNER and result["installation_verified"]
            local = identity.load_local_env({"HOME": str(home)})
            assert local["UNRELATED"] == "kept value"
            assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
            assert local["CODEX_AUTOMATION_LOGIN"] == SLUG + "[bot]"
            assert (session / "app.pem").read_text() not in env_file.read_text()
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "DIRECTION.md").write_text("Fixture owner direction\n")
            (repo / ".github").mkdir()
            (repo / ".github" / "CODEOWNERS").write_text(f"/DIRECTION.md @{OWNER}\n")
            subprocess.run(["git", "-C", str(repo), "add", "DIRECTION.md", ".github/CODEOWNERS"], check=True)
            environ = {key: value for key, value in os.environ.items() if not key.startswith(
                ("GITHUB_", "GH_", "CODEX_", "CODE_HOME", "GIT_"))}
            environ.update(HOME=str(home), GH_WITH_ENV_TOKEN_PYTHON=os.sys.executable)
            commit = subprocess.run([str(SCRIPTS / "git-commit-as-bot"), "-m", "fixture direction"],
                                    cwd=repo, env=environ, capture_output=True, text=True)
            assert commit.returncode == 0, commit.stderr
            author = subprocess.check_output(["git", "-C", str(repo), "log", "-1", "--format=%an"], text=True).strip()
            assert author == result["actor"]
            gh = root / "fake-gh"
            gh.write_text('#!' + os.sys.executable + '\n' +
                          'import os,sys,urllib.request\n' +
                          'path={"create":"pull","review":"review","merge":"merge"}[sys.argv[2]]\n' +
                          'request=urllib.request.Request(os.environ["FIXTURE_API"]+"/fixture/"+path,data=b"{}",headers={"Authorization":"Bearer "+os.environ["GH_TOKEN"]})\n' +
                          'try: print(urllib.request.urlopen(request).read().decode())\n' +
                          'except Exception: sys.exit(1)\n')
            gh.chmod(0o700)
            environ.update(GH_WITH_ENV_TOKEN_GH=str(gh), FIXTURE_API=api)

            def wrapper(action):
                return subprocess.run([str(SCRIPTS / "gh-with-env-token"), "pr", action], cwd=repo,
                                      env=environ, capture_output=True, text=True)

            created = wrapper("create")
            assert created.returncode == 0, created.stderr
            assert server.author == result["actor"]
            assert wrapper("review").returncode != 0  # An App author cannot self-approve.
            assert wrapper("merge").returncode != 0  # No bypass before an owner review.
            request = urllib.request.Request(api + "/fixture/review", data=b"{}",
                                             headers={"Authorization": "Bearer fixture-owner-token"})
            review = json.loads(urllib.request.urlopen(request).read())
            assert review["reviewer"] == OWNER
            assert wrapper("merge").returncode == 0
            print("fixture acceptance: manifest/private storage/discovery/bot commit/bot PR/owner approval/no-bypass merge passed")
    finally:
        server.shutdown()
        server.server_close()


def test_wrong_installation_and_missing_permissions_preserve_configuration():
    server, api = fixture_server()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            setup.private_directory(session)
            registration(session)
            home = root / "home"
            for owner, permissions, suspended in [("wrong-owner", server.permissions, None),
                                                   (OWNER, {}, None), (OWNER, server.permissions, "today")]:
                server.owner, server.permissions, server.suspended = owner, permissions, suspended
                try:
                    configured_fixture(session, home, api, installation_id="31")
                    raise AssertionError("bad installation accepted")
                except setup.Error:
                    assert not (home / ".code" / "local.env").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_existing_identity_requires_explicit_replacement_and_keeps_backup():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "local.env"
        old = "CODEX_AUTOMATION_LOGIN=old-bot\nGIT_COMMIT_AS_BOT_NAME=old-bot\nGH_WITH_ENV_TOKEN_EXPECTED_LOGIN=old-bot\nOTHER=keep\n"
        target.write_text(old)
        values = {"CODEX_AUTOMATION_LOGIN": "new[bot]", "CODEX_AUTOMATION_EMAIL": "fixture@example.test"}
        backups = Path(directory) / "private-backups"
        setup.private_directory(backups)
        try:
            setup.write_configuration(target, values, backup_directory=backups, replace_identity=False)
            raise AssertionError("existing identity replaced implicitly")
        except setup.Error:
            assert target.read_text() == old
        setup.write_configuration(target, values, backup_directory=backups, replace_identity=True)
        local = identity.load_local_env({"CODEX_SKILLS_ENV_FILE": str(target)})
        assert local["GIT_COMMIT_AS_BOT_NAME"] == local["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"] == "new[bot]"
        backup = next(backups.glob("local-env-before-*"))
        assert backup.read_text() == old and stat.S_IMODE(backup.stat().st_mode) == 0o600


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_") and callable(value) and name != "test_private_key"]
    for test in tests:
        test()
    print(f"PASS: {len(tests)} guided App setup tests")
