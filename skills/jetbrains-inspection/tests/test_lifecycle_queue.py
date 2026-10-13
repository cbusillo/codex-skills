#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts" / "jb-inspect.py"
WORKER = """
import importlib.util, json, os, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('queue_helper', sys.argv[1])
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
cache = Path(os.environ['JETBRAINS_INSPECTION_CACHE_DIR'])
started = time.monotonic()
print('attempting', flush=True)
try:
    with helper.lifecycle_lock(10):
        with (cache / 'entered.jsonl').open('a') as output:
            output.write(json.dumps({'id': sys.argv[2], 'wait_seconds': time.monotonic() - started}) + '\\n')
        print('entered', flush=True)
        sys.stdin.readline()
    print('released', flush=True)
except helper.InspectError as error:
    print(json.dumps(error.payload), flush=True)
    sys.exit(3)
"""


class LifecycleQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def start(self, identity):
        process = subprocess.Popen(
            [sys.executable, '-c', WORKER, str(HELPER), str(identity)],
            env=os.environ | {'JETBRAINS_INSPECTION_CACHE_DIR': str(self.cache)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.processes.append(process)
        self.assertEqual(process.stdout.readline().strip(), 'attempting')
        return process

    def wait_tickets(self, count):
        deadline = time.monotonic() + 10
        while len(list((self.cache / 'lifecycle-queue').glob('*.ticket'))) != count:
            self.assertTrue(all(process.poll() is None for process in self.processes))
            if time.monotonic() >= deadline:
                self.fail('workers did not register their queue tickets')
            time.sleep(0.01)

    def release(self, process):
        stdout, stderr = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        self.assertIn('released', stdout)

    def test_twenty_callers_wait_beyond_lock_budget_and_enter_in_registration_order(self):
        owner = self.start(0)
        self.assertEqual(owner.stdout.readline().strip(), 'entered')
        self.wait_tickets(1)
        for identity in range(1, 20):
            self.start(identity)
            self.wait_tickets(identity + 1)
        # Every waiter has exceeded the acquisition budget before the owner releases.
        time.sleep(0.05)
        for process in self.processes:
            self.release(process)
        rows = [json.loads(line) for line in (self.cache / 'entered.jsonl').read_text().splitlines()]
        self.assertEqual([row['id'] for row in rows], [str(identity) for identity in range(20)])
        self.assertGreater(rows[-1]['wait_seconds'], 0.01)
        self.assertFalse(list((self.cache / 'lifecycle-queue').glob('*.ticket')))

    def test_twenty_callers_remain_pending_then_all_execute_after_owner_releases(self):
        owner = self.start(0)
        self.assertEqual(owner.stdout.readline().strip(), 'entered')
        for identity in range(1, 20):
            self.start(identity)
        time.sleep(0.05)
        self.assertTrue(all(process.poll() is None for process in self.processes),
                        'healthy contention must queue, rather than terminate an assessment')
        # Allow each admitted worker to complete, independent of kernel scheduling order.
        for process in self.processes:
            process.stdin.write('\n')
            process.stdin.flush()
        for process in self.processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr + stdout)
        rows = [json.loads(line) for line in (self.cache / 'entered.jsonl').read_text().splitlines()]
        self.assertCountEqual([row['id'] for row in rows], [str(identity) for identity in range(20)])

    def test_crashed_waiter_is_reaped_without_overtaking_a_live_owner(self):
        owner = self.start('owner')
        self.assertEqual(owner.stdout.readline().strip(), 'entered')
        self.wait_tickets(1)
        abandoned = self.start('abandoned')
        self.wait_tickets(2)
        successor = self.start('successor')
        self.wait_tickets(3)
        abandoned.kill()
        abandoned.communicate(timeout=10)
        time.sleep(0.05)
        self.assertEqual(len((self.cache / 'entered.jsonl').read_text().splitlines()), 1)
        self.release(owner)
        self.release(successor)
        rows = [json.loads(line)['id'] for line in (self.cache / 'entered.jsonl').read_text().splitlines()]
        self.assertEqual(rows, ['owner', 'successor'])
        self.assertFalse(list((self.cache / 'lifecycle-queue').glob('*.ticket')))

    def test_legacy_lock_remains_exclusive_and_timeout_removes_own_ticket(self):
        if os.name == 'nt':
            self.skipTest('POSIX legacy caller control')
        import fcntl
        with (self.cache / 'lifecycle.lock').open('a+') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            process = self.start('blocked')
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 3, stderr + stdout)
            self.assertEqual(json.loads(stdout)['error_reason'], 'lifecycle_lock_timeout')
            self.assertFalse((self.cache / 'entered.jsonl').exists())
            self.assertFalse(list((self.cache / 'lifecycle-queue').glob('*.ticket')))


if __name__ == '__main__':
    unittest.main()
