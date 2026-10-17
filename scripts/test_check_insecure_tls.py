#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import contextlib
import io
import os
import shlex
import shutil
import sys
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
            "import requests\nself.session = requests.Session()\nself.session.get(u, verify=False)",
            "import requests\nwith requests.Session() as s:\n s.get(u, verify=False)",
            "import aiohttp\nasync with aiohttp.ClientSession() as s:\n await s.get(u, ssl=False)",
            "import requests\nrequests.Session().get(u, verify=False)",
            "import requests\nself.session: requests.Session = requests.Session()\nself.session.get(u, verify=False)",
            "import ssl\nctx.verify_mode = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE",
            "import requests\nrequests.get(u, verify=False if insecure else True)",
            "import ssl\nurlopen(u, context=ssl._create_stdlib_context())",
            "import ssl\nctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE",
            "import ssl\nssl._create_default_https_context = ssl._create_unverified_context",
            "import ssl\nsetattr(ssl, '_create_default_https_context', ssl._create_unverified_context)",
            "import ssl\nmonkeypatch.setattr(ssl, '_create_default_https_context', ssl._create_unverified_context)",
            "from ssl import _create_unverified_context as unchecked\nregister(context_factory=unchecked)",
            "import ssl\nctx.verify_mode = ssl.VerifyMode.CERT_NONE",
            "import urllib3\nurllib3.PoolManager(cert_reqs='CERT_NONE')",
            "import urllib3\nurllib3.PoolManager(assert_hostname=False)",
            "import aiohttp\naiohttp.TCPConnector(verify_ssl=False)",
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

    def test_choke_point_reason_does_not_cover_adjacent_call(self):
        source = "import requests, ssl\n# insecure-tls: explicit isolated vendor site setting.\nctx.check_hostname = False\nctx.verify_mode = ssl.CERT_NONE\nrequests.get(u, verify=False)\n"
        self.assertEqual(checker.findings(source), [5])

    def test_symlink_input_fails_with_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real.py").write_text("ctx.check_hostname = False\n")
            (root / "link.py").symlink_to(root / "real.py")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(checker.main(["--repo", str(root), "link.py"]), 2)
            self.assertIn("link.py", output.getvalue())

    def test_decorator_reason_precedes_the_decorator_and_does_not_cover_body(self):
        source = "import ssl\n# insecure-tls: isolated loopback fixture patches the TLS factory.\n@mock.patch.object(ssl, '_create_default_https_context', ssl._create_unverified_context)\ndef test_peer():\n    pass\n"
        self.assertEqual(checker.findings(source), [])
        self.assertTrue(checker.findings(source.replace("    pass", "    ctx.check_hostname = False")))

    def test_safe_defaults_strings_and_unrelated_calls_pass(self):
        source = '''import ssl, requests
ctx = ssl.create_default_context()
ctx.check_hostname = True
ctx.verify_mode = ssl.CERT_REQUIRED
requests.get("https://example.invalid", verify=True)
example = "ctx.check_hostname = False"
build_ssl_context(verify_ssl=False)
assert ssl._create_default_https_context is not ssl._create_unverified_context
if ssl._create_default_https_context is ssl._create_unverified_context:
    raise RuntimeError("unverified TLS factory")
ssl._create_unverified_context = ssl.create_default_context
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


class TlsGateTests(unittest.TestCase):
    def test_gate_rejects_either_linter_failure_from_outside_its_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory)
            root = outside / "repo"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            binary = outside / "bin"
            binary.mkdir()
            for name in ("lint-tls.sh", "check_insecure_tls.py"):
                shutil.copyfile(Path(__file__).with_name(name), scripts / name)
            (scripts / "run-python-helper.sh").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
            (scripts / "run-python-helper.sh").chmod(0o755)
            marker = "SIMULATED_RUFF_DIAGNOSTIC"
            ruff = binary / "ruff-fixture.py"
            ruff.write_text(f"""import os, sys
from pathlib import Path
if os.environ.get('TLS_TEST_RUFF_STATUS') != '0':
    raise SystemExit(1)
files = []
for argument in sys.argv[1:]:
    path = Path(argument)
    if path.is_dir():
        files.extend(path.rglob('*.py'))
    elif path.is_file() and path.suffix == '.py':
        files.append(path)
raise SystemExit(int(any({marker!r} in path.read_text() for path in files)))
""")
            (binary / "uv").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(ruff))} "$@"\n')
            (binary / "uv").chmod(0o755)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            path = root / "client.py"
            path.write_text("import ssl\nctx = ssl.create_default_context()\n")
            subprocess.run(["git", "-C", str(root), "add", path.name], check=True)
            (root / "untracked.py").write_text(repr(marker) + "\n")
            env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"], "TLS_TEST_RUFF_STATUS": "0"}

            def run():
                return subprocess.run(["bash", str(scripts / "lint-tls.sh")], cwd=outside,
                                      env=env, capture_output=True, text=True)

            self.assertEqual(run().returncode, 0)
            path.write_text("import ssl\nctx = ssl.create_default_context()\nctx.check_hostname = False\n")
            self.assertEqual(run().returncode, 1)
            path.write_text("import ssl\nctx = ssl.create_default_context()\n")
            env["TLS_TEST_RUFF_STATUS"] = "1"
            self.assertEqual(run().returncode, 1)


if __name__ == "__main__":
    unittest.main()
