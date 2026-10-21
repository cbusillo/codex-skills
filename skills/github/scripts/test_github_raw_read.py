#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Raw-read routing preserves identity and requires live cache revalidation."""
import os
import json
from pathlib import Path

import test_gh_with_env_token as wrapper
import tempfile
import unittest
from unittest.mock import patch

import github_api
import github_raw_read


class RawReadTests(unittest.TestCase):
    def test_binary_response_headers_never_change_body_bytes(self):
        payload = b"binary\x00\xff\r\ntext\r\n\r\nend"
        for headers in (b"HTTP/2 200\n\n", b"HTTP/2 200\r\n\r\n",
                        b"HTTP/2 302\n\nHTTP/2 200\n\n"):
            self.assertEqual(github_raw_read.response_body(headers + payload), payload)

    def test_unsupported_cli_options_and_mutations_are_left_to_gh(self):
        for args in (["api", "repos/example/app/tarball/main"],
                     ["api", "repos/example/app/zipball/main"],
                     ["api", "repos/example/app/actions/artifacts/7/zip"],
                     ["api", "repos/example/app", "--jq", ".name"],
                     ["api", "repos/example/app", "--paginate"],
                     ["api", "repos/example/app", "-X", "DELETE"],
                     ["api", "repos/example/app", "--input", "-"],
                     ["api", "repos/example/app/actions/runs/1/logs"],
                     ["api", "https://other.invalid/repos/example/app"],
                     ["api", "graphql"], ["pr", "view", "7"]):
            with self.subTest(args=args):
                self.assertIsNone(github_raw_read.endpoint(args))
        self.assertEqual(github_raw_read.endpoint(["api", "repos/example/app?per_page=100", "-X", "GET"]),
                         "repos/example/app?per_page=100")

    def test_real_wrapper_preserves_json_and_records_each_http_reply_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env_file, identity, classifier = wrapper.app_fixture(root)
            backend = root / "gh"
            wrapper.write(backend, "#!/usr/bin/env python3\nimport sys,json\n"
                f"path={str(root / 'calls')!r}\n"
                "with open(path,'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                "cached=any('If-None-Match:' in a for a in sys.argv)\n"
                "print('HTTP/2 '+('304' if cached else '200')+'\\netag: \"version\"\\n')\n"
                "if not cached: print('{\"number\":7}')\n"
                "sys.exit(1 if cached else 0)\n")
            outputs = [wrapper.run_wrapper(env_file, classifier, identity, "api", "/repos/example/app/pulls/7",
                       gh_command=backend) for _ in range(2)]
            for output in outputs:
                self.assertEqual(output.returncode, 0, output.stderr)
                self.assertEqual(json.loads(output.stdout), {"number": 7})
            self.assertEqual(len((root / "calls").read_text().splitlines()), 2)
            receipts = [json.loads(line) for path in root.rglob("requests-*.jsonl") for line in path.read_text().splitlines()]
            self.assertEqual([item["status"] for item in receipts], [200, 304])

    def test_real_wrapper_keeps_errors_and_non_json_bytes_without_replay(self):
        for status, payload in [(404, b'{"message":"Not Found"}\n'), (200, b"text\r\n\r\nend")]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env_file, identity, classifier = wrapper.app_fixture(root)
                backend = root / "gh"
                wrapper.write(backend, "#!/usr/bin/env python3\nimport sys\n"
                    f"with open({str(root / 'calls')!r},'a') as f: f.write('call\\n')\n"
                    f"sys.stdout.buffer.write(b'HTTP/2 {status}\\netag: version\\n\\n'+{payload!r})\n"
                    + ("sys.stderr.write('gh: Not Found (HTTP 404)\\n')\n" if status == 404 else "")
                    + f"sys.exit({int(status >= 400)})\n")
                for _ in range(2):
                    output = wrapper.run_wrapper(env_file, classifier, identity, "api", "/repos/example/app/contents/DIRECTION.md", gh_command=backend)
                    self.assertEqual(output.returncode, int(status >= 400), output.stderr)
                    self.assertEqual(output.stdout, payload.decode().replace("\r\n", "\n"))
                    if status == 404:
                        self.assertIn("HTTP 404", output.stderr)
                self.assertEqual(len((root / "calls").read_text().splitlines()), 2)

    def test_non_json_responses_do_not_enter_the_conditional_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_HTTP_CACHE_DIR": directory}), \
                patch.object(github_api, "_call_gh_uncached") as transport:
            transport.side_effect = [github_api.ApiResult(ok=True, status=200, body="raw text", actor="bot",
                expected_actor="bot", headers={"etag": '"text"'}) for _ in range(2)]
            for _ in range(2):
                github_raw_read.read(["api", "/repos/example/app"], gh_cmd="fake-gh", actor="bot", host="github.com")
                self.assertNotIn("If-None-Match", transport.call_args.kwargs["extra_headers"])
            self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_304_restores_only_same_identity_body_and_failure_never_serves_offline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_HTTP_CACHE_DIR": directory}), \
                patch.object(github_api, "_call_gh_uncached") as transport:
            def response(status, actor="bot"):
                return github_api.ApiResult(ok=status == 200, status=status, body={"number": 7} if status == 200 else None,
                    actor=actor, expected_actor=actor, headers={"etag": '"v1"'},
                    failure=None if status == 200 else github_api.FailureDetail(cause="http_error", message="failed", retryable=False, fallback_eligible=False, disposition="stop"))
            transport.side_effect = [response(200), response(304), response(200, "other"), response(503)]
            def read(actor="bot"):
                result = github_raw_read.read(["api", "/repos/example/app/pulls/7"], gh_cmd="fake-gh", actor=actor, host="github.com")
                assert result is not None
                return result
            self.assertEqual(read().body, {"number": 7})
            cached = read()
            self.assertTrue(cached.ok)
            self.assertEqual((cached.status, cached.body), (304, {"number": 7}))
            self.assertEqual(transport.call_args.kwargs["extra_headers"].get("If-None-Match"), '"v1"')
            read("other")
            self.assertNotIn("If-None-Match", transport.call_args.kwargs["extra_headers"])
            self.assertFalse(read().ok)
            self.assertEqual(transport.call_count, 4)


if __name__ == "__main__":
    unittest.main()
