#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Custody and independent finalization for one isolated native run.

The durable running phase is armed before launch. An unrecorded outcome never
permits another licensed process merely because resource recovery succeeded.
"""
import contextlib
import json
import os
import re
import uuid
from pathlib import Path


class LifecycleError(Exception):
    pass


IDENTITY_FIELDS = ("run_id", "private", "sdk_name", "interpreter", "table_existed",
                   "storage_anchor", "storage_identity", "backend_revision")


def identity_from(record):
    try:
        if record["schema_version"] != 2:
            raise ValueError()
        identity = {key: record[key] for key in IDENTITY_FIELDS}
        run_id = str(uuid.UUID(identity["run_id"]))
        private = Path(identity["private"])
        anchor = Path(identity["storage_anchor"])
        if (identity["run_id"] != run_id or identity["sdk_name"] != "jb-native-" + run_id
                or private.name != "private-" + run_id or not private.is_absolute()
                or private.resolve() != private or not anchor.is_absolute()
                or not private.is_relative_to(anchor) or not isinstance(identity["table_existed"], bool)
                or not Path(identity["interpreter"]).is_absolute()
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", identity["backend_revision"])):
            raise ValueError()
        return identity
    except (ValueError, KeyError, TypeError):
        raise LifecycleError("native_recovery_journal_malformed") from None


def write_owner(path, identity):
    # A write-once marker has no unjournaled temporary basename. The durable
    # reservation permits recovery of this exact partial file before any source
    # is written, and O_EXCL never truncates an existing marker.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(identity).encode())
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def prove_private(identity, materialized):
    private = Path(identity["private"])
    if not private.exists():
        return
    if private.is_symlink() or not private.is_dir():
        raise LifecycleError("native_recovery_ownership_unproven")
    children = list(private.iterdir())
    marker = private / "owner.json"
    if not children:
        return
    if marker.is_symlink():
        raise LifecycleError("native_recovery_ownership_unproven")
    if not materialized and children == [marker] and marker.is_file():
        # No source write is allowed until the materialized phase is durable.
        return
    try:
        if not marker.is_file() or json.loads(marker.read_text()) != identity:
            raise LifecycleError("native_recovery_ownership_unproven")
    except (ValueError, OSError):
        raise LifecycleError("native_recovery_ownership_unproven") from None


def reconcile_result(path, worker, record, common):
    source = Path(path).expanduser().resolve()
    common.require_public_input(source)
    if source.is_relative_to(worker) or source.suffix != ".json":
        raise LifecycleError("native_reconciliation_result_unsafe")
    try:
        result = json.loads(source.read_text())
        native = result["diagnostic"]["native"]
        identity = result["identity"]
        if (result["backend"] != "native" or not result["agent_result"]["terminal"]
                or identity["inspection_run_id"] != record["run_id"]
                or identity["native_backend_revision"] != record["backend_revision"]
                or native["run_id"] != record["run_id"]
                or native["terminal_outcome_known"] is not True
                or native["license_boundary_observed"] is not False
                or native["worker_exited"] is not True
                or not isinstance(native["terminal_reason"], str)
                or not re.fullmatch(r"native_[a-z_]+", native["terminal_reason"])
                or native["terminal_reason"] in {"native_owner_activation_required", "native_activation_stop_recorded", "native_internal_error"}
                or record.get("license_boundary") is True
                or (worker / "native-activation-stop.json").exists()):
            raise ValueError()
        return {**record, "phase": "terminal", "license_boundary": False,
                "outcome": native["terminal_reason"]}
    except (ValueError, KeyError, TypeError):
        raise LifecycleError("native_reconciliation_result_unproven") from None


def recover(worker, receipt, common, write_record, remove_sdk, *, result_path=None):
    journal = worker / "native-run.json"
    if not journal.exists():
        if result_path:
            raise LifecycleError("native_reconciliation_no_run")
        return
    receipt.update(private_deleted=False, sdk_removed=False, custody_retained=True)
    try:
        if journal.is_symlink():
            raise LifecycleError("native_recovery_ownership_unproven")
        record = json.loads(journal.read_text())
        identity = identity_from(record)
        receipt["recovered_run_id"] = identity["run_id"]
        if common.storage_identity(Path(identity["storage_anchor"])) != identity["storage_identity"]:
            raise LifecycleError("native_recovery_storage_changed")
        phase = record.get("phase")
        if phase not in {"reserved", "materialized", "sdk_attempted", "engine_running", "terminal"}:
            raise LifecycleError("native_recovery_phase_unproven")
        prove_private(identity, record.get("private_materialized", False))
        if result_path:
            record = reconcile_result(result_path, worker, record, common)
            write_record(journal, record)
        if record.get("sdk_attempted"):
            remove_sdk(worker / "config", Path(identity["interpreter"]), identity["sdk_name"],
                       identity["table_existed"], missing_ok=True)
        receipt["sdk_removed"] = True
        private = Path(identity["private"])
        if private.exists():
            common.delete_private(private)
        receipt["private_deleted"] = True
        if record.get("license_boundary") is True:
            write_record(worker / "native-activation-stop.json",
                         {"run_id": identity["run_id"], "boundary_event_count": 1})
        elif record["phase"] == "engine_running":
            raise LifecycleError("native_interrupted_run_unresolved")
        journal.unlink()
        receipt["custody_retained"] = False
    except LifecycleError:
        raise
    except Exception as error:
        # Recovery cannot attest absence after any failed operation. Never use
        # an exception-name prefix to infer which resources were removed.
        code = str(error)
        receipt["recovery_failure"] = code if re.fullmatch(r"native_[a-z_]+", code) else type(error).__name__
        raise LifecycleError("native_recovery_failed") from None


class RunContext:
    def __init__(self, receipt, common, write_record, remove_sdk):
        self.receipt = receipt
        self.common = common
        self.write_record = write_record
        self.remove_sdk = remove_sdk
        self.locks = contextlib.ExitStack()
        self.worker = None
        self.private = None
        self.journal = None
        self.record = None
        self.identity = None
        self.sdk_lease = None
        self.finalized = False
        self.failures = []

    def phase(self, phase, **fields):
        updated = {**self.record, "phase": phase, **fields}
        self.write_record(self.journal, updated)
        self.record = updated

    def reserve(self, state, worker, config, interpreter):
        self.worker = worker
        self.private = state / ("private-" + self.receipt["run_id"])
        anchor = self.common.storage_anchor(state)
        self.identity = {
            "run_id": self.receipt["run_id"], "private": str(self.private),
            "sdk_name": "jb-native-" + self.receipt["run_id"], "interpreter": str(interpreter),
            "table_existed": (config / "options/jdk.table.xml").exists(),
            "storage_anchor": str(anchor), "storage_identity": self.common.storage_identity(anchor),
            "backend_revision": self.receipt["backend_revision"],
        }
        self.journal = worker / "native-run.json"
        record = {**self.identity, "schema_version": 2, "phase": "reserved", "sdk_attempted": False, "private_materialized": False}
        self.write_record(self.journal, record)
        self.record = record
        self.receipt.update(private_deleted=False, custody_retained=True)
        self.private.mkdir(mode=0o700)
        write_owner(self.private / "owner.json", self.identity)
        self.phase("materialized", private_materialized=True)

    def sdk_attempt(self, config, interpreter, sdk_name):
        self.phase("sdk_attempted", sdk_attempted=True)
        self.sdk_lease = (config, interpreter, sdk_name, self.identity["table_existed"])
        self.receipt["sdk_removed"] = False

    def attempt(self, operation, callback):
        try:
            callback()
            return True
        except Exception:
            # Each finalization action is independent, including under disk
            # exhaustion. Only a bounded operation name leaves this boundary.
            self.failures.append(operation)
            return False

    def finish(self, findings, reason, *, known_terminal):
        if self.finalized:
            return reason, self.receipt.get("findings_artifact")
        artifact = None
        licensing = bool(self.receipt.get("license_boundary_observed"))
        self.receipt.update(license_boundary_observed=licensing,
                            terminal_outcome_known=known_terminal, terminal_reason=reason)
        stop_durable = False
        try:
            if self.record is not None and known_terminal:
                self.attempt("terminal_record", lambda: self.phase("terminal", outcome=reason, license_boundary=licensing))
            if licensing and self.worker is not None:
                stop_durable = self.attempt("activation_stop", lambda: self.write_record(
                    self.worker / "native-activation-stop.json",
                    {"run_id": self.receipt["run_id"], "boundary_event_count": 1}))
            if findings and self.private is not None:
                output = self.private.parent / (self.receipt["run_id"] + ".findings.json")
                if self.attempt("findings_artifact", lambda: self.write_record(output, findings)):
                    artifact = str(output)
            if self.sdk_lease is not None:
                self.receipt["sdk_removed"] = self.attempt("sdk_removal", lambda: self.remove_sdk(*self.sdk_lease, missing_ok=True))
            if self.private is not None and self.record is not None:
                def dispose_private():
                    prove_private(self.identity, self.record.get("private_materialized", False))
                    if self.private.exists():
                        self.common.delete_private(self.private)
                self.receipt["private_deleted"] = self.attempt("private_removal", dispose_private)
            if self.record is not None and self.receipt["private_deleted"] and self.receipt["sdk_removed"]:
                held_outcome = self.record["phase"] == "engine_running"
                held_license = licensing and not stop_durable
                if not held_outcome and not held_license or stop_durable:
                    removed = self.attempt("journal_removal", lambda: self.journal.unlink(missing_ok=True))
                    self.receipt["custody_retained"] = not removed
        finally:
            self.attempt("lock_release", self.locks.close)
            self.finalized = True
        self.receipt["persistence_failures"] = list(self.failures)
        self.receipt["findings_artifact"] = artifact
        if not licensing:
            if self.record is not None and (not self.receipt["private_deleted"] or not self.receipt["sdk_removed"]):
                reason = "native_cleanup_failed"
            elif self.failures:
                reason = "native_persistence_failed"
        return reason, artifact
