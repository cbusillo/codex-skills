#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline regression tests for the Codex manual helper."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).with_name("fetch-codex-manual.mjs").resolve()


class FetchCodexManualTests(unittest.TestCase):
    def test_cli_runs_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / "manual.mjs"
            alias.symlink_to(HELPER)
            unknown_argument = "--invalid-test-argument"
            result = subprocess.run(
                ["node", str(alias), unknown_argument],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn(unknown_argument, result.stderr)

    def run_cache_scenario(self, scenario: dict) -> dict:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for the manual-helper tests")
        script = r"""
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
const { fetchCodexManual } = await import(process.argv[2]);
const scenario = JSON.parse(process.argv[3]);
const cacheDir = process.argv[1];
const calls = [];
let response = scenario.initial;
globalThis.fetch = async (_url, options) => {
  calls.push(options.method);
  const digest = response.headHash ?? createHash('sha256').update(response.body).digest('hex');
  const header = options.method === 'HEAD' ? digest : (response.getHash ?? digest);
  const headers = header === 'missing' ? {} : { 'x-content-sha256': header };
  return new Response(options.method === 'HEAD' ? null : response.body, { status: 200, headers });
};
const fetchManual = () => fetchCodexManual({ cacheDir, manualUrl: 'https://manual.example.invalid/manual' });
const snapshot = async () => {
  const files = {};
  for (const name of await readdir(cacheDir)) files[name] = await readFile(path.join(cacheDir, name), 'utf8');
  return files;
};
await mkdir(cacheDir, { recursive: true });
const first = await fetchManual();
let domainErrorType;
try {
  await fetchCodexManual({ cacheDir: first.status.manualPath, manualUrl: 'https://manual.example.invalid/manual' });
} catch (failure) { domainErrorType = failure.constructor; }
if (!domainErrorType) throw new Error('A cache file must be refused as a directory');
const before = await snapshot();
calls.length = 0;
if (scenario.corrupt) await writeFile(first.status.manualPath, scenario.corrupt);
response = scenario.next ?? response;
let second, error, domainError;
try { second = await fetchManual(); } catch (failure) {
  error = failure.message;
  domainError = failure.constructor === domainErrorType;
}
process.stdout.write(JSON.stringify({ first, second, error, domainError, calls, before, after: await snapshot() }));
"""
        environment = {
            key: value for key, value in os.environ.items()
            if key.lower() not in {"http_proxy", "https_proxy"}
        }
        environment["PATH"] = ""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [node, "--input-type=module", "-e", script, directory, HELPER.as_uri(), json.dumps(scenario)],
                env=environment, capture_output=True, text=True, timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_verified_manual_is_reused_without_get(self) -> None:
        body = "# Fixture manual\n\n## Commands\nBody\n"
        result = self.run_cache_scenario({"initial": {"body": body}})
        self.assertEqual(result["first"]["status"]["cacheStatus"], "updated")
        self.assertEqual(result["second"]["status"]["cacheStatus"], "hit")
        self.assertEqual(result["calls"], ["HEAD"])
        self.assertEqual(result["before"], result["after"])
        status = result["second"]["status"]
        self.assertEqual(status["fetchedManualSha256"], hashlib.sha256(body.encode()).hexdigest())
        self.assertEqual(result["after"][Path(status["manualPath"]).name], body)

    def test_corrupt_cached_body_is_refetched(self) -> None:
        body = "# Fixture manual\n\n## Current\nVerified body\n"
        result = self.run_cache_scenario({"initial": {"body": body}, "corrupt": "corrupt cache"})
        self.assertEqual(result["second"]["status"]["cacheStatus"], "updated")
        self.assertEqual(result["calls"], ["HEAD", "GET"])
        self.assertEqual(result["before"], result["after"])

    def test_unverified_downloads_preserve_cached_artifacts(self) -> None:
        old = "# Old manual\n\n## Prior\nPreserve this\n"
        new = "# New manual\n\n## New\nReplacement\n"
        old_hash = hashlib.sha256(old.encode()).hexdigest()
        new_hash = hashlib.sha256(new.encode()).hexdigest()
        cases = [
            ({"body": new, "headHash": "missing"}, ["HEAD"]),
            ({"body": new, "headHash": "invalid hash"}, ["HEAD"]),
            ({"body": new, "getHash": "missing"}, ["HEAD", "GET"]),
            ({"body": new, "getHash": "invalid hash"}, ["HEAD", "GET"]),
            ({"body": new, "getHash": old_hash}, ["HEAD", "GET"]),
            ({"body": old, "headHash": new_hash}, ["HEAD", "GET"]),
        ]
        for response, calls in cases:
            with self.subTest(response=response):
                result = self.run_cache_scenario({"initial": {"body": old}, "next": response})
                self.assertTrue(result.get("error"), result)
                self.assertTrue(result.get("domainError"), result)
                self.assertNotIn("second", result)
                self.assertEqual(result["calls"], calls)
                self.assertEqual(result["after"], result["before"])

    def test_curl_transport_uses_final_headers_and_cleans_response_files(self) -> None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("Node.js is required for the manual-helper tests")
        body = "# Curl fixture\n\n## Verified section\nBody\n"
        curl_script = r"""
import hashlib, os, pathlib, sys
arguments = sys.argv[1:]
method = 'HEAD' if '--head' in arguments else arguments[arguments.index('--request') + 1]
body = os.environ['CURL_FIXTURE_BODY']
digest = hashlib.sha256(body.encode()).hexdigest()
headers = ('HTTP/1.1 200 Connection established\r\n\r\n'
           'HTTP/2 302 Found\r\nLocation: https://redirect.example.invalid\r\n\r\n'
           f'HTTP/2 200 OK\r\nX-Content-SHA256: {digest}\r\n\r\n')
pathlib.Path(arguments[arguments.index('--dump-header') + 1]).write_text(headers)
pathlib.Path(arguments[arguments.index('--output') + 1]).write_text('' if method == 'HEAD' else body)
with pathlib.Path(os.environ['CURL_FIXTURE_CALLS']).open('a') as log:
    log.write(method + '\n')
"""
        script = r"""
import { readFile, readdir } from 'node:fs/promises';
const { fetchCodexManual } = await import(process.argv[2]);
globalThis.fetch = () => { throw new Error('Unexpected native-fetch fallback'); };
const options = { cacheDir: process.argv[1], manualUrl: 'https://manual.example.invalid/manual' };
const first = await fetchCodexManual(options);
const second = await fetchCodexManual(options);
process.stdout.write(JSON.stringify({ first, second, body: await readFile(second.status.manualPath, 'utf8'),
                                     files: await readdir(options.cacheDir) }));
"""
        environment = {
            key: value for key, value in os.environ.items()
            if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "curl"
            executable.write_text(f"#!{sys.executable}\n" + curl_script)
            executable.chmod(0o700)
            calls = root / "calls"
            environment.update(PATH=str(root), HTTP_PROXY="http://proxy.example.invalid",
                               CURL_FIXTURE_BODY=body, CURL_FIXTURE_CALLS=str(calls))
            result = subprocess.run(
                [node, "--input-type=module", "-e", script, str(root / "cache"), HELPER.as_uri()],
                env=environment, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(calls.read_text().splitlines(), ["HEAD", "GET", "HEAD"])
        self.assertEqual(payload["first"]["status"]["cacheStatus"], "updated")
        self.assertEqual(payload["second"]["status"]["cacheStatus"], "hit")
        self.assertEqual(payload["body"], body)
        status = payload["second"]["status"]
        self.assertEqual(set(payload["files"]), {Path(status["manualPath"]).name, Path(status["outlinePath"]).name})

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
import { fileURLToPath } from 'node:url';
const { fetchCodexManual } = await import(process.argv[2]);
const nativeFetch = globalThis.fetch;
const nativeSetTimeout = globalThis.setTimeout;
const nativeClearTimeout = globalThis.clearTimeout;
const body = Buffer.from('# Manual\\n\\n## Section\\nBody\\n');
let bodyStarted = false;
let bodyReadStarted = false;
let helperAbortFired = false;
let proofDeadline;
let resolveProofDeadline;
const proofDeadlinePromise = new Promise(resolve => {
  resolveProofDeadline = resolve;
});
const helperTimers = new Set();
globalThis.setTimeout = (callback, delay, ...args) => {
  if (delay !== 200) return nativeSetTimeout(callback, delay, ...args);
  const timer = { active: true, args, callback };
  helperTimers.add(timer);
  return timer;
};
globalThis.clearTimeout = timer => {
  if (helperTimers.has(timer)) {
    timer.active = false;
    return;
  }
  nativeClearTimeout(timer);
};
globalThis.fetch = async (...args) => {
  const response = await nativeFetch(...args);
  const method = args[1]?.method ?? 'GET';
  if (method !== 'GET') return response;
  return new Proxy(response, {
    get(target, property) {
      if (property === 'text') {
        return async () => {
          bodyReadStarted = true;
          const bodyPromise = target.text();
          proofDeadline = nativeSetTimeout(
            () => resolveProofDeadline({ kind: 'proof-deadline' }),
            1000,
          );
          const timer = [...helperTimers].find(candidate => candidate.active);
          if (timer) {
            helperAbortFired = true;
            timer.callback(...timer.args);
          }
          return bodyPromise;
        };
      }
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
};
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
const manualUrl = 'http://127.0.0.1:' + server.address().port + '/manual.md';
let outcome;
try {
  let domainErrorType;
  try {
    await fetchCodexManual({ cacheDir: fileURLToPath(process.argv[2]), manualUrl });
  } catch (failure) { domainErrorType = failure.constructor; }
  if (!domainErrorType) throw new Error('A cache file must be refused as a directory');
  outcome = await Promise.race([
    fetchCodexManual({
      cacheDir: process.argv[1],
      manualUrl,
      timeoutMs: 200,
    }).then(
      () => ({ kind: 'resolved' }),
      error => ({
        kind: 'rejected',
        expectedError: error.constructor === domainErrorType,
      }),
    ),
    proofDeadlinePromise,
  ]);
} finally {
  nativeClearTimeout(proofDeadline);
  globalThis.fetch = nativeFetch;
  globalThis.setTimeout = nativeSetTimeout;
  globalThis.clearTimeout = nativeClearTimeout;
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
if (
  bodyStarted &&
  bodyReadStarted &&
  helperAbortFired &&
  outcome.kind === 'rejected' &&
  outcome.expectedError
) {
  process.stdout.write('timed_out');
} else {
  process.stderr.write(JSON.stringify({
    bodyStarted,
    bodyReadStarted,
    helperAbortFired,
    outcome,
  }));
  process.exitCode = 1;
}
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
