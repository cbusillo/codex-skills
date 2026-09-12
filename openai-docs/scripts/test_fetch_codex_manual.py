#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline regression tests for the Codex manual helper."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).with_name("fetch-codex-manual.mjs").resolve()


class FetchCodexManualTests(unittest.TestCase):
    def test_cli_runs_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "manual.mjs"
            alias.symlink_to(HELPER)
            result = subprocess.run(
                ["node", str(alias), "--invalid-test-argument"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Unknown argument: --invalid-test-argument", result.stderr)

    def test_timeout_covers_stalled_response_body(self) -> None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for the manual-helper tests")
        env = {
            key: value for key, value in os.environ.items()
            if key.lower() not in {"http_proxy", "https_proxy"}
        }
        # Exercise native fetch without a curl fallback masking a hang.
        env["PATH"] = ""
        script = """
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
const { fetchCodexManual } = await import(process.argv[2]);
const body = Buffer.from('# Manual\\n\\n## Section\\nBody\\n');
let bodyStarted = false;
const server = createServer((request, response) => {
  response.writeHead(200, {
    'x-content-sha256': createHash('sha256').update(body).digest('hex'),
    'Content-Length': body.length,
  });
  if (request.method === 'HEAD') { response.end(); return; }
  bodyStarted = true;
  response.write(body.subarray(0, 1));
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
let rejected = false;
try {
  await fetchCodexManual({
    cacheDir: process.argv[1],
    manualUrl: 'http://127.0.0.1:' + server.address().port + '/manual.md',
    timeoutMs: 200,
  });
} catch (error) {
  rejected = error.message.includes('could not be fetched');
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
if (bodyStarted && rejected) process.stdout.write('timed_out');
else process.exitCode = 1;
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [node, "--input-type=module", "-e", script, directory, HELPER.as_uri()],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "timed_out")


if __name__ == "__main__":
    unittest.main()
