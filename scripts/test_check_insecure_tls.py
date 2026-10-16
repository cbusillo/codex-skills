#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

import check_insecure_tls as checker


class TlsTests(unittest.TestCase):
    def test_hand_built_context_and_import_aliases(self):
        for source in (
            "import ssl\nctx.check_hostname = False\nctx.verify_mode = ssl.CERT_NONE\n",
            "import ssl as tls\nctx.verify_mode = tls.CERT_NONE\n",
            "from ssl import CERT_NONE as unchecked\nctx.verify_mode = unchecked\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(checker.findings(source))

    def test_library_calls_and_session_instances(self):
        for source in (
            "import requests as r\nr.get('https://example.invalid', verify=False)",
            "from httpx import Client as C\nC(verify=False)",
            "import requests\ns = requests.Session()\ns.get('https://example.invalid', verify=False)",
            "import aiohttp\naiohttp.TCPConnector(ssl=False)",
            "import ssl\nfrom urllib3 import PoolManager\nPoolManager(cert_reqs=ssl.CERT_NONE)",
            "from ssl import _create_unverified_context as unchecked\nunchecked()",
        ):
            with self.subTest(source=source):
                self.assertTrue(checker.findings(source))

    def test_one_reasoned_annotation_covers_the_explicit_site_opt_out(self):
        source = '''import ssl
def build_ssl_context(verify_ssl=True):
    ctx = ssl.create_default_context()
    if not verify_ssl:
        # insecure-tls: verification is off only where a site sets verify_ssl=False.
        # Allowed for private network paths to vendor-owned databases (Fishbowl),
        # where enforcing certificates risks breaking imports.
        # Decision: https://github.com/cbusillo/odoo-tenant-cm/pull/175#issuecomment-6222897727
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
'''
        self.assertEqual(checker.findings(source), [])
        self.assertTrue(checker.findings(source + "\nother.check_hostname = False\n"))

    def test_bare_ignores_and_question_ids_do_not_explain_an_exception(self):
        for annotation in ("# noqa", "# insecure-tls:", "# insecure-tls: Q418",
                           "# insecure-tls: https://example.invalid/decision"):
            self.assertTrue(checker.findings(annotation + "\nctx.check_hostname = False\n"))

    def test_reasoned_library_call_annotation_uses_the_statement_indent(self):
        self.assertEqual(checker.findings('''import ssl
def fixture():
    # insecure-tls: disposable loopback peer exercises certificate rejection.
    ctx = ssl._create_unverified_context()
'''), [])

    def test_safe_defaults_strings_and_unrelated_calls_pass(self):
        source = '''import ssl, requests
ctx = ssl.create_default_context()
ctx.check_hostname = True
ctx.verify_mode = ssl.CERT_REQUIRED
requests.get("https://example.invalid", verify=True)
example = "ctx.check_hostname = False"
build_ssl_context(verify_ssl=False)
'''
        self.assertEqual(checker.findings(source), [])

    def test_cli_checks_tracked_files_and_fails_on_invalid_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            path = root / "client.py"
            path.write_text("ctx.check_hostname = False\n")
            subprocess.run(["git", "-C", str(root), "add", path.name], check=True)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checker.main(["--repo", str(root)]), 1)
                path.write_text("ctx.check_hostname = True\n")
                self.assertEqual(checker.main(["--repo", str(root)]), 0)
            path.write_text("def broken(:\n")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(checker.main(["--repo", str(root)]), 2)


if __name__ == "__main__":
    unittest.main()
