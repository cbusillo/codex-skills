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
    def test_unsupported_cli_options_and_mutations_are_left_to_gh(self):
        for args in (["api", "repos/example/app", "--jq", ".name"],
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

    def test_304_restores_only_same_identity_body_and_failure_never_serves_offline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_HTTP_CACHE_DIR": directory}), \
                patch.object(github_api, "_call_gh_uncached") as transport:
            def response(status, actor="bot"):
                return github_api.ApiResult(ok=status == 200, status=status, body={"number": 7} if status == 200 else None,
                    actor=actor, expected_actor=actor, headers={"etag": '"v1"'},
                    failure=None if status == 200 else github_api.FailureDetail(cause="http_error", message="failed", retryable=False, fallback_eligible=False, disposition="stop"))
            transport.side_effect = [response(200), response(304), response(200, "other"), response(503)]
            def read(actor="bot"):
                return github_raw_read.read(["api", "/repos/example/app/pulls/7"], gh_cmd="fake-gh", actor=actor, host="github.com")
            self.assertEqual(read().body, {"number": 7})
            cached = read()
            self.assertTrue(cached.ok)
            self.assertEqual((cached.status, cached.body), (304, {"number": 7}))
            self.assertEqual(transport.call_args.kwargs["extra_headers"], {"If-None-Match": '"v1"'})
            read("other")
            self.assertEqual(transport.call_args.kwargs["extra_headers"], {})
            self.assertFalse(read().ok)
            self.assertEqual(transport.call_count, 4)


if __name__ == "__main__":
    unittest.main()
