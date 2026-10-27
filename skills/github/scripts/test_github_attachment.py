#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline behavioral checks for the bounded personal attachment route."""

import importlib.util
import io
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stdout
from email.message import Message
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("attachment", pathlib.Path(__file__).with_name("gh-attachment.py"))
assert spec is not None and spec.loader is not None
attachment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(attachment)


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, **headers):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/octet-stream"
        for key, value in headers.items():
            self.headers[key] = value


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.evidence = self.root / "task-evidence" / "product" / "task" / "download"
        self.login = "director"
        self.repository = "director/product"
        self.url = "https://github.com/user-attachments/files/123/answer.csv"
        self.asset = "https://github-production-user-asset-6210df.s3.amazonaws.com/answer?signature=private"
        self.token = "personal-secret-fixture"
        self.body = b"private,client,answer\n1,2,3\n"
        self.repo = {"full_name": "director/product", "owner": {"type": "User"}}
        self.comment: dict[str, object] = {
            "id": 42, "issue_url": "https://api.github.com/repos/director/product/issues/9",
            "body": f"[Answer]({self.url})",
        }
        self.calls = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "CODEX_SKILLS_ENV_FILE": str(self.root / "missing.env"),
            "GH_WITH_ENV_TOKEN_OWN_USER": "1", "GH_WITH_ENV_TOKEN_OWN_USER_LOGIN": "director",
            "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH": "0", "GH_HOST": "github.com",
        }, clear=True))
        reader = self.stack.enter_context(patch.object(attachment.github_read, "GitHubReader"))
        self.reader_factory = reader
        reader.return_value.get_json.side_effect = lambda path, **_: self.comment if "/comments/" in path else self.repo
        self.run = self.stack.enter_context(patch.object(attachment.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, self.token.encode(), b"")))
        self.opener_factory = self.stack.enter_context(patch.object(attachment.urllib.request, "build_opener"))
        self.opener_factory.return_value.open.side_effect = self.open

    def open(self, request, **_kwargs):
        self.calls.append(request)
        if request.full_url == "https://api.github.com/user":
            return Response(json.dumps({"login": self.login, "type": "User"}).encode())
        if request.full_url == self.url:
            # Regression: an installation token gets 404; the declared user token succeeds.
            if request.get_header("Authorization") != f"token {self.token}":
                raise urllib.error.HTTPError(self.url, 404, "not found", Message(), None)
            return Response(status=302, Location=self.asset)
        if request.full_url == self.asset:
            return Response(self.body)
        raise AssertionError("unscoped network request")

    def invoke(self, **overrides):
        values = {"repo": self.repository, "comment": "42", "url": self.url,
                  "evidence-dir": str(self.evidence), **overrides}
        argv = [value for key, item in values.items() for value in (f"--{key}", str(item))]
        output = io.StringIO()
        with redirect_stdout(output):
            status = attachment.main(argv)
        rendered = output.getvalue()
        self.assertNotIn(self.token, rendered)
        self.assertNotIn(self.body.decode(), rendered)
        self.assertNotIn("signature=", rendered)
        return status, json.loads(rendered)

    def test_private_attachment_saved_with_user_token_without_forwarding_credentials(self):
        status, result = self.invoke()
        self.assertEqual(status, 0)
        saved = pathlib.Path(result["path"])
        self.assertEqual(saved.read_bytes(), self.body)
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(saved.parent.stat().st_mode), 0o700)
        self.assertEqual(result["bytes"], len(self.body))
        self.assertEqual(self.calls[-1].get_method(), "GET")
        self.assertIsNone(self.calls[-1].get_header("Authorization"))
        self.assertEqual(self.run.call_args.args[0], ["gh", "auth", "token", "--hostname", "github.com", "--user", "director"])
        self.assertEqual(self.reader_factory.call_args.kwargs["gh_prefix_args"],
                         attachment.github_read.automation_only_gh_prefix_args())

    def test_missing_opt_in_and_automation_only_refuse_before_authentication(self):
        for variable, value in (("GH_WITH_ENV_TOKEN_OWN_USER", "0"),
                                ("GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH", "1")):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: value}):
                self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()
        self.reader_factory.assert_not_called()

    def test_opt_in_in_local_env_is_not_command_approval(self):
        config = self.root / "local.env"
        config.write_text("GH_WITH_ENV_TOKEN_OWN_USER=1\nGH_WITH_ENV_TOKEN_OWN_USER_LOGIN=director\n")
        with patch.dict(os.environ, {"CODEX_SKILLS_ENV_FILE": str(config), "GH_WITH_ENV_TOKEN_OWN_USER": ""}):
            self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_other_repository_owner_refused_even_with_opt_in(self):
        self.assertEqual(self.invoke(repo="client/private")[0], 1)
        self.reader_factory.assert_not_called()
        self.run.assert_not_called()

    def test_wrong_comment_or_missing_link_refused_before_personal_token(self):
        for change in ({"issue_url": "https://api.github.com/repos/elsewhere/private/issues/9"},
                       {"body": self.url + ".other"}, {"id": 99}):
            with self.subTest(change=change), patch.dict(self.comment, change):
                self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_transferred_or_organization_repository_refused(self):
        for change in ({"full_name": "elsewhere/product"}, {"owner": {"type": "Organization"}}):
            with self.subTest(change=change), patch.dict(self.repo, change):
                self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_mismatched_personal_identity_never_fetches_attachment(self):
        self.opener_factory.return_value.open.side_effect = lambda *a, **k: Response(b'{"login":"someone-else","type":"User"}')
        self.assertEqual(self.invoke()[0], 1)

    def test_download_failure_has_no_file_or_secret_diagnostic(self):
        self.opener_factory.return_value.open.side_effect = urllib.error.HTTPError(
            self.asset, 404, self.token, Message(), io.BytesIO(self.body))
        status, result = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("404", result["error"])
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_size_limit_leaves_no_partial_file(self):
        self.assertEqual(self.invoke(**{"max-bytes": 3})[0], 1)
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_installation_token_failure_does_not_switch_identity(self):
        self.run.return_value.stdout = b"installation-token-fixture"
        status, result = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("404", result["error"])
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_unavailable_login_and_credential_errors_are_not_echoed(self):
        self.run.return_value.returncode = 1
        self.run.return_value.stderr = self.token.encode() + self.body
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.calls, [])

    def test_html_login_page_is_not_saved_as_attachment(self):
        original_open = self.open

        def html_response(request, **kwargs):
            if request.full_url == self.asset:
                response = Response(b"<html>login</html>")
                response.headers.replace_header("Content-Type", "text/html")
                return response
            return original_open(request, **kwargs)

        self.opener_factory.return_value.open.side_effect = html_response
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(list(self.evidence.iterdir()), [])

    def test_existing_file_and_symlink_are_not_overwritten(self):
        directory = self.evidence
        directory.mkdir(mode=0o700, parents=True)
        saved = directory / attachment.attachment_name(self.url)
        saved.write_bytes(b"original")
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(saved.read_bytes(), b"original")
        saved.unlink()
        target = self.root / "target"
        target.write_bytes(b"original")
        saved.symlink_to(target)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(target.read_bytes(), b"original")
        self.run.assert_not_called()

    def test_public_evidence_directory_refused(self):
        directory = self.evidence
        directory.mkdir(mode=0o755, parents=True)
        directory.chmod(0o755)
        self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_url_and_redirect_restrictions(self):
        for url in ("http://github.com/user-attachments/files/1/a", self.url + "?token=bad",
                    self.url + "#fragment", self.url + "/extra",
                    "https://github.com/user-attachments/assets/not-an-id",
                    "https://github.com/user-attachments/assets/../files/1/a",
                    "https://github.com/user-attachments/assets/%2Ftmp",
                    "https://github.com/user-attachments/files/1/%2Ftmp", "https://evil.invalid/file"):
            with self.subTest(url=url):
                self.assertEqual(self.invoke(url=url)[0], 1)
        for url in ("http://objects.githubusercontent.com/file", "https://evil.invalid/file",
                    "https://objects.githubusercontent.com.evil.invalid/file", "https://github.com/login"):
            self.assertFalse(attachment.allowed_redirect(url))
        self.reader_factory.assert_not_called()
        self.run.assert_not_called()

    def test_denied_redirect_is_not_requested(self):
        self.asset = "https://evil.invalid/?signature=private"
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(len(self.calls), 2)

    def test_real_redirect_handler_does_not_forward_authorization(self):
        # The downloader inspects Location itself; urllib must never auto-follow it.
        handler = attachment.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, self.asset))

    def standing_scope(self):
        self.login = attachment.STANDING_DIRECTOR
        self.repository = f"{self.login}/product"
        os.environ.pop("GH_WITH_ENV_TOKEN_OWN_USER", None)
        os.environ.pop("GH_WITH_ENV_TOKEN_OWN_USER_LOGIN", None)
        os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-automation"
        self.repo["full_name"] = self.repository
        self.comment.update({"issue_url": f"https://api.github.com/repos/{self.repository}/issues/9",
                             "author_association": "COLLABORATOR", "user": {"type": "User"}})
        # Actual first line of the recorded Q467 decision, not an invented yes/no reply.
        self.approval = {"id": attachment.APPROVAL_COMMENT,
                         "issue_url": f"https://api.github.com/repos/{attachment.APPROVAL_REPOSITORY}/issues/2021",
                         "body": "Director decision: Q467, and make it a standing approval",
                         "user": {"login": "fixture-automation"}, "created_at": "2026-10-17T21:20:42Z"}

        def metadata(path, **_kwargs):
            if path.endswith(f"/comments/{attachment.APPROVAL_COMMENT}"):
                return self.approval
            return self.comment if "/comments/" in path else self.repo

        self.reader_factory.return_value.get_json.side_effect = metadata
        self.reader_factory.return_value.paged_json.return_value = []

    def test_recorded_standing_approval_downloads_without_per_file_opt_in(self):
        self.standing_scope()
        status, result = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(pathlib.Path(result["path"]).read_bytes(), self.body)
        self.assertTrue(result["untrusted"])
        self.assertEqual(result["authority"], "standing_approval")
        self.assertEqual(self.run.call_args.args[0][-1], self.login)

    def test_standing_approval_withdrawal_refuses_before_personal_credential(self):
        self.standing_scope()
        self.reader_factory.return_value.paged_json.return_value = [{
            "id": attachment.APPROVAL_COMMENT + 1, "created_at": "2026-10-18T00:00:00Z",
            "user": {"login": self.login}, "body": f"{attachment.APPROVAL_QUESTION} no, stop downloads"}]
        self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_director_comment_downloads_without_per_command_approval(self) -> None:
        self.standing_scope()
        os.environ["GH_WITH_ENV_TOKEN_OWN_USER_LOGIN"] = self.login.upper()
        self.comment.update({"author_association": "OWNER", "user": {"type": "User", "login": self.login}})
        status, result = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(result["authority"], "standing_approval")
        self.assertEqual(pathlib.Path(result["path"]).read_bytes(), self.body)

    def test_director_owner_comment_downloads_with_per_command_approval(self):
        self.standing_scope()
        os.environ["GH_WITH_ENV_TOKEN_OWN_USER"] = "1"
        self.comment["author_association"] = "OWNER"
        status, result = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(result["authority"], "per_command")
        self.assertEqual(pathlib.Path(result["path"]).read_bytes(), self.body)
        self.reader_factory.return_value.paged_json.assert_not_called()

    def test_per_command_approval_downloads_after_standing_withdrawal(self):
        self.standing_scope()
        self.reader_factory.return_value.paged_json.return_value = [{
            "id": attachment.APPROVAL_COMMENT + 1, "created_at": "2026-10-18T00:00:00Z",
            "user": {"login": self.login}, "body": f"{attachment.APPROVAL_QUESTION} no"}]
        os.environ["GH_WITH_ENV_TOKEN_OWN_USER"] = "1"
        status, result = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(result["authority"], "per_command")
        self.assertEqual(pathlib.Path(result["path"]).read_bytes(), self.body)

    def test_automation_only_refuses_standing_director_per_command_approval(self):
        self.standing_scope()
        os.environ["GH_WITH_ENV_TOKEN_OWN_USER"] = "1"
        os.environ["GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH"] = "1"
        self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()
        self.reader_factory.assert_not_called()

    def test_quoted_or_foreign_approval_and_unreadable_discussion_do_not_authorize(self):
        self.standing_scope()
        for change in ({"body": f"> {attachment.RECORDED_AFFIRMATIVE}"},
                       {"user": {"login": "client"}},
                       {"body": f"Director decision: {attachment.APPROVAL_QUESTION} no"}):
            with self.subTest(change=change), patch.dict(self.approval, change):
                self.assertEqual(self.invoke()[0], 1)
        self.reader_factory.return_value.paged_json.side_effect = OSError("unreadable")
        self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_unrelated_client_text_does_not_withdraw_or_grant_authority(self):
        self.standing_scope()
        self.reader_factory.return_value.paged_json.return_value = [{
            "id": attachment.APPROVAL_COMMENT + 1, "created_at": "2026-10-18T00:00:00Z",
            "user": {"login": "client"}, "body": f"{attachment.APPROVAL_QUESTION} no"}]
        self.assertEqual(self.invoke()[0], 0)

    def test_unverified_author_and_bot_comments_are_outside_standing_scope(self):
        self.standing_scope()
        for change in ({"author_association": "NONE"}, {"author_association": "OWNER"},
                       {"author_association": "OWNER", "user": {"type": "User", "login": "third-party"}},
                       {"user": {"type": "Bot", "login": self.login}}):
            with self.subTest(change=change), patch.dict(self.comment, change):
                self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()

    def test_task_evidence_location_is_required(self):
        for path in (self.root / "private-download", self.root / "task-evidence" / "wrong-repo" / "task"):
            with self.subTest(path=path):
                self.assertEqual(self.invoke(**{"evidence-dir": path})[0], 1)
                self.assertFalse(path.exists())
        self.run.assert_not_called()

    def test_evidence_symlink_escape_and_git_checkout_are_refused(self):
        task = self.evidence.parent
        task.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir(mode=0o700)
        self.evidence.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.invoke()[0], 1)
        self.evidence.unlink()
        (self.root / ".git").mkdir()
        self.assertEqual(self.invoke()[0], 1)
        self.run.assert_not_called()


class EmbeddedImageTests(AttachmentTests):
    """The same authorization, transport and storage boundaries apply to images."""

    def setUp(self):
        super().setUp()
        self.url = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
        self.comment["body"] = f'<img width="2252" alt="Image" src="{self.url}" />'
        self.body = b"private-image-fixture"

    def test_markdown_image_downloads_under_standing_approval(self):
        self.standing_scope()
        self.comment["body"] = f"![Printer]({self.url})"
        status, result = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(pathlib.Path(result["path"]).read_bytes(), self.body)
        self.assertEqual(result["authority"], "standing_approval")


if __name__ == "__main__":
    unittest.main()
