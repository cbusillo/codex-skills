#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Framing protects event identities without storing engine messages."""
import importlib.util
import errno
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from typing import Any

spec = importlib.util.spec_from_file_location("observer_test", Path(__file__).parents[1] / "scripts/native_observer.py")
if spec is None or spec.loader is None:
    raise ImportError("Observer fixture unavailable")
observer: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observer)


class ObserverTests(unittest.TestCase):
    def test_determiners_and_split_prompts_keep_their_line_identity(self):
        for text in ("Your license has expired", "The license is missing", "This license is invalid"):
            frame = observer.LineFramer()
            _, prefix = frame.feed(text[:7].encode())
            self.assertFalse(observer.license_boundary(prefix))
            _, prefix = frame.feed(text[7:].encode())
            self.assertTrue(observer.license_boundary(prefix))

    def test_midline_tail_never_becomes_a_prompt(self):
        frame = observer.LineFramer()
        first = b"x" * 5000 + b"Start trial " + b"x" * 4084
        _, prefix = frame.feed(first)
        self.assertFalse(observer.license_boundary(prefix))
        _, prefix = frame.feed(b"")
        self.assertFalse(observer.license_boundary(prefix))
        lines, prefix = frame.feed(b"\nStart trial")
        self.assertFalse(observer.license_boundary(lines[0]))
        self.assertTrue(observer.license_boundary(prefix))

    def test_paths_build_messages_and_quoted_examples_are_not_prompts(self):
        for text in ("Running local inspections on /repo/trial.py", "building license-required-package", 'Example: "Start trial"', "example - Start trial"):
            self.assertFalse(observer.license_boundary(text))

    def test_logged_physical_traversal_is_framed_and_relative(self):
        receipt = {}
        events = observer.EngineObserver(Path("/private/project"), receipt)
        line = "2026-10-20 16:00:00,000 [  100] FINE - #c.i.Inspection - Running local inspections on /private/project/source.py"
        events.observe(line, partial=True)
        self.assertNotIn("inspected_files", receipt)
        events.observe(line)
        self.assertEqual(receipt["inspected_files"], ["source.py"])

    def test_engine_errors_retain_only_the_logger_category(self):
        receipt = {}
        events = observer.EngineObserver(Path("/private/project"), receipt)
        with self.assertRaises(observer.ObservationError):
            events.observe("2026-10-20 16:00:00,000 [  100] ERROR - #c.i.vfs.Store - private customer message")
        self.assertEqual(receipt["engine_error_category"], "#c.i.vfs.Store")
        self.assertNotIn("private customer", str(receipt))

    def test_overlong_line_fails_without_reinterpreting_a_suffix(self):
        frame = observer.LineFramer(limit=8)
        with self.assertRaises(observer.ObservationError):
            frame.feed(b"prefix Start trial")

    def test_license_scanner_checks_later_lines_after_an_oversized_line(self):
        scanner = observer.LicenseScanner(limit=32)
        scanner.feed(b"x" * 40 + b"\nNo valid license found\n")
        self.assertTrue(scanner.observed)
        self.assertFalse(scanner.complete)

    def test_license_scanner_does_not_reinterpret_a_discarded_line_suffix(self):
        scanner = observer.LicenseScanner(limit=32)
        scanner.feed(b"x" * 40)
        scanner.feed(b"Start trial")
        self.assertFalse(scanner.observed)
        scanner.feed(b"\nStart trial")
        self.assertTrue(scanner.observed)
        self.assertFalse(scanner.complete)

    def test_license_scanner_preserves_partial_line_identity_across_chunks(self):
        scanner = observer.LicenseScanner()
        scanner.feed(b"prefix ")
        scanner.feed(b"No valid license found\nYour lic")
        self.assertFalse(scanner.observed)
        scanner.feed(b"ense has expired")
        self.assertTrue(scanner.observed)
        self.assertTrue(scanner.complete)


class ProcessDrainTests(unittest.TestCase):
    backend: Any
    process_receipt: dict[str, Any]

    def setUp(self):
        backend_spec = importlib.util.spec_from_file_location("drain_test_backend", Path(__file__).parents[1] / "scripts/native_backend.py")
        if backend_spec is None or backend_spec.loader is None:
            raise ImportError("Drain fixture unavailable")
        self.backend = importlib.util.module_from_spec(backend_spec)
        backend_spec.loader.exec_module(self.backend)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private = Path(temporary.name).resolve()
        (self.private / "logs").mkdir()

    def simulated_process(self):
        reader, writer = os.pipe()
        os.close(writer)
        stream = os.fdopen(reader, "rb")
        self.addCleanup(stream.close)
        process = Mock(pid=123456, stdout=stream, returncode=None)
        process.wait.return_value = 0
        return process

    def test_cleanup_signal_exit_races_preserve_abort_and_run_finalizers(self):
        for error in (PermissionError(errno.EPERM, "exiting"), ProcessLookupError(errno.ESRCH, "gone")):
            with self.subTest(error=type(error).__name__):
                process = self.simulated_process()
                receipt = {"load_pause_count": 0}
                exited = Mock()

                def abort(_pid):
                    raise self.backend.Blocked("native_timeout")

                with patch.object(self.backend.subprocess, "Popen", return_value=process), \
                        patch.object(self.backend.os, "killpg", side_effect=error), \
                        patch.object(self.backend, "group_gone", return_value=True) as proof, \
                        (self.private / "worker.lock").open("w") as lock:
                    with self.assertRaises(self.backend.Blocked) as outcome:
                        self.backend.execute(["fixture"], {}, self.private, time.monotonic() + 3, None,
                                             receipt, lock_fd=lock.fileno(), on_spawn=abort, on_exit=exited)
                self.assertEqual(str(outcome.exception), "native_timeout")
                self.assertGreater(process.wait.call_args.kwargs["timeout"], 0)
                proof.assert_called_once()
                exited.assert_called_once()
                self.assertTrue(receipt["process_custody_closed"])
                self.assertTrue(receipt["license_observation_complete"])

    def test_unreaped_child_keeps_custody_and_still_drains_licence_events(self):
        for error in (subprocess.TimeoutExpired("fixture", 2), OSError(errno.EIO, "unavailable")):
            with self.subTest(error=type(error).__name__):
                process = self.simulated_process()
                process.wait.side_effect = error
                receipt = {"load_pause_count": 0}
                exited = Mock()
                (self.private / "logs/late.log").write_text("No valid license found\n")

                def abort(_pid):
                    raise self.backend.Blocked("native_timeout")

                with patch.object(self.backend.subprocess, "Popen", return_value=process), \
                        patch.object(self.backend.os, "killpg", side_effect=PermissionError(errno.EPERM, "denied")), \
                        patch.object(self.backend, "group_gone", return_value=True) as proof, \
                        (self.private / "worker.lock").open("w") as lock:
                    with self.assertRaises(self.backend.Blocked) as outcome:
                        self.backend.execute(["fixture"], {}, self.private, time.monotonic() + 3, None,
                                             receipt, lock_fd=lock.fileno(), on_spawn=abort, on_exit=exited)
                self.assertEqual(str(outcome.exception), "native_process_group_unproven")
                self.assertGreater(process.wait.call_args.kwargs["timeout"], 0)
                proof.assert_called_once()
                exited.assert_not_called()
                self.assertFalse(receipt["process_custody_closed"])
                self.assertFalse(receipt["license_observation_complete"])
                self.assertTrue(receipt["license_boundary_observed"])
                self.assertTrue(process.stdout.closed)

    def load_signal_case(self, failed_signal, *, exited):
        process = self.simulated_process()
        receipt = {"load_pause_count": 0}
        polls = 0
        exit_poll = 2 if failed_signal == signal.SIGSTOP else 3

        def poll():
            nonlocal polls
            polls += 1
            if exited and polls >= exit_poll:
                process.returncode = 0
            return process.returncode

        def send(_pid, operation):
            if operation == failed_signal:
                raise PermissionError(errno.EPERM, "signal unavailable")

        def spawn(*_args, **kwargs):
            # Model inherited descriptors so the guardian's gate has a reader.
            for descriptor in kwargs.get("pass_fds", ()):
                self.addCleanup(os.close, os.dup(descriptor))
            return process

        loads = iter((65, 1)) if failed_signal == signal.SIGCONT else iter((65,))
        process.poll.side_effect = poll
        outcome = None
        with patch.object(self.backend.subprocess, "Popen", side_effect=spawn), \
                patch.object(self.backend.os, "killpg", side_effect=send), \
                patch.object(self.backend.os, "getloadavg", side_effect=lambda: (next(loads, 1), 1, 1)), \
                patch.object(self.backend, "group_gone", return_value=True), \
                (self.private / "worker.lock").open("w") as lock:
            try:
                self.backend.execute(["fixture"], {}, self.private, time.monotonic() + 3, None,
                                     receipt, lock_fd=lock.fileno(), require_completion=False)
            except self.backend.Blocked as error:
                outcome = str(error)
        return outcome, receipt

    def test_load_signal_exit_races_return_to_normal_completion(self):
        for operation in (signal.SIGSTOP, signal.SIGCONT):
            with self.subTest(operation=operation):
                outcome, receipt = self.load_signal_case(operation, exited=True)
                self.assertIsNone(outcome)
                self.assertTrue(receipt["process_custody_closed"])

    def test_live_load_signal_failure_aborts_instead_of_continuing(self):
        for operation in (signal.SIGSTOP, signal.SIGCONT):
            with self.subTest(operation=operation):
                outcome, receipt = self.load_signal_case(operation, exited=False)
                self.assertEqual(outcome, "native_process_signal_failed")
                self.assertTrue(receipt["process_custody_closed"])

    def run_process(self, code, *, lifetime=None):
        receipt: dict[str, Any] = {"load_pause_count": 0}
        self.process_receipt = receipt
        with (self.private / "worker.lock").open("w") as lock, \
                patch.object(self.backend.os, "getloadavg", return_value=(1, 1, 1)):
            with self.assertRaises(self.backend.Blocked) as error:
                if lifetime is None:
                    self.backend.execute([sys.executable, "-c", code], {}, self.private,
                                         time.monotonic() + 3, None, receipt, lock_fd=lock.fileno())
                else:
                    with patch.object(self.backend, "check_lifetime", side_effect=lifetime):
                        self.backend.execute([sys.executable, "-c", code], {}, self.private,
                                             time.monotonic() + 3, None, receipt, lock_fd=lock.fileno())
        return receipt, str(error.exception)

    def test_error_then_license_in_one_received_chunk_preserves_boundary(self):
        code = "import os,time;os.write(1,b'ERROR: plugin startup failure\\nNo valid license found\\n');time.sleep(30)"
        receipt, _ = self.run_process(code)
        self.assertTrue(receipt.get("license_boundary_observed"))
        self.assertTrue(receipt.get("license_observation_complete"))

    def test_timeout_drains_unread_unterminated_prompt_after_group_exit(self):
        written = self.private / "written"
        code = ("import os,time;from pathlib import Path;"
                "os.write(1,b'Your license has expired');"
                f"Path({str(written)!r}).touch();time.sleep(30)")

        def timeout_before_read(*_args):
            if not self.process_receipt.get("launch_gate_released"):
                return
            limit = time.monotonic() + 2
            while not written.exists() and time.monotonic() < limit:
                time.sleep(0.005)
            self.assertTrue(written.exists())
            raise self.backend.Blocked("native_timeout")

        receipt, reason = self.run_process(code, lifetime=timeout_before_read)
        self.assertEqual(reason, "native_timeout")
        self.assertTrue(receipt.get("license_boundary_observed"))
        self.assertTrue(receipt.get("license_observation_complete"))

    def test_stdout_error_does_not_skip_an_unread_log_prompt(self):
        log = self.private / "logs/idea.log"
        code = ("import os,time;from pathlib import Path;"
                f"Path({str(log)!r}).write_text('The license is missing\\n');"
                "os.write(1,b'ERROR: plugin startup failure\\n');time.sleep(30)")
        receipt, _ = self.run_process(code)
        self.assertTrue(receipt.get("license_boundary_observed"))
        self.assertTrue(receipt.get("license_observation_complete"))

    def test_unsafe_log_does_not_attest_complete_license_observation(self):
        synthetic = self.private / "synthetic.log"
        synthetic.write_text("ordinary fixture log\n")
        (self.private / "logs/idea.log").symlink_to(synthetic)
        receipt, _ = self.run_process("import time;time.sleep(30)")
        self.assertFalse(receipt.get("license_observation_complete", False))

    def test_oversized_line_keeps_later_prompt_and_marks_scan_incomplete(self):
        code = "import os,time;os.write(1,b'x'*70000+b'\\nNo valid license found\\n');time.sleep(30)"
        receipt, _ = self.run_process(code)
        self.assertTrue(receipt.get("license_boundary_observed"))
        self.assertFalse(receipt.get("license_observation_complete", False))
        self.assertIn("line_over_limit", receipt["license_observation_failures"])

    def test_replaced_log_is_scanned_from_start_without_attesting_lost_bytes(self):
        log = self.private / "logs/idea.log"
        log.write_text("ordinary fixture log\n")
        observe = self.backend.observations.EngineObserver.observe

        def rotate_then_fail(events, line, **kwargs):
            if line == "ordinary fixture log":
                replacement = self.private / "replacement.log"
                replacement.write_text("No valid license found\n")
                replacement.replace(log)
                raise self.backend.observations.ObservationError("native_engine_error")
            return observe(events, line, **kwargs)

        with patch.object(self.backend.observations.EngineObserver, "observe", new=rotate_then_fail):
            receipt, _ = self.run_process("import time;time.sleep(30)")
        self.assertTrue(receipt.get("license_boundary_observed"))
        self.assertFalse(receipt.get("license_observation_complete", False))
        self.assertIn("log_disappeared", receipt["license_observation_failures"])

    def test_rotation_preserves_partial_prompt_and_complete_observation(self):
        log = self.private / "logs/idea.log"
        log.write_text("ordinary fixture log\nYour lic")
        observe = self.backend.observations.EngineObserver.observe

        def rotate_then_fail(events, line, **kwargs):
            if line == "Your lic":
                rotated = log.with_name("idea.1.log")
                log.rename(rotated)
                with rotated.open("a") as output:
                    output.write("ense has expired\n")
                log.write_text("new ordinary fixture log\n")
                raise self.backend.observations.ObservationError("native_engine_error")
            return observe(events, line, **kwargs)

        with patch.object(self.backend.observations.EngineObserver, "observe", new=rotate_then_fail):
            receipt, _ = self.run_process("import time;time.sleep(30)")
        self.assertTrue(receipt.get("license_boundary_observed"))
        self.assertTrue(receipt.get("license_observation_complete"))

    def test_normal_log_rotation_does_not_leave_an_unknown_outcome_hold(self):
        log = self.private / "logs/idea.log"
        log.write_text("ordinary fixture log\n")
        observe = self.backend.observations.EngineObserver.observe

        def rotate_then_fail(events, line, **kwargs):
            if line == "ordinary fixture log":
                log.rename(log.with_name("idea.1.log"))
                log.write_text("new ordinary fixture log\n")
                raise self.backend.observations.ObservationError("native_engine_error")
            return observe(events, line, **kwargs)

        with patch.object(self.backend.observations.EngineObserver, "observe", new=rotate_then_fail):
            receipt, _ = self.run_process("import time;time.sleep(30)")
        self.assertFalse(receipt.get("license_boundary_observed", False))
        self.assertTrue(receipt.get("license_observation_complete"))

    def test_rotation_between_enumeration_and_open_waits_for_the_next_tick(self):
        log = self.private / "logs/idea.log"
        log.write_text("ordinary fixture log\n")
        original_open = Path.open
        rotated = False

        def rotate_at_open(path, *args, **kwargs):
            nonlocal rotated
            if path == log and args == ("rb",) and not rotated:
                rotated = True
                log.rename(log.with_name("idea.1.log"))
                log.write_text("new ordinary fixture log\n")
                raise FileNotFoundError("fixture rotation race")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=rotate_at_open):
            receipt, reason = self.run_process("import time;time.sleep(.3);print('ERROR: artificial end',flush=True);time.sleep(30)")
        self.assertTrue(rotated)
        self.assertEqual(reason, "native_engine_error")
        self.assertTrue(receipt.get("license_observation_complete"))


if __name__ == "__main__":
    unittest.main()
