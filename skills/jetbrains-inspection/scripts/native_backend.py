#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Stock PyCharm inspection using an independently activated profile in place.

Only public launcher properties and the non-secret SDK table are read from
the worker. Account, activation and credential files are never enumerated,
opened, staged or transferred. The profile lock is shared with its UI launcher.
"""
from __future__ import annotations

import contextlib
import ast
import hashlib
import importlib.util
import json
import os
import re
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, NamedTuple
from xml.etree import ElementTree

_spec = importlib.util.spec_from_file_location("native_common", Path(__file__).with_name("native_common.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("Native shared backend unavailable")
common: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
Blocked = common.Blocked
LOAD_LIMIT = common.LOAD_LIMIT
HEAP_LIMIT_MIB = 1536
ACTIVE_PROCESSORS = 2
NICE = 10
DRAIN_SECONDS = 2
DRAIN_BYTES = 4 * 1024 * 1024
PYTHON_PRODUCT_KEY = "pycharm"
WEBSTORM_PRODUCT_KEY = "webstorm"
PREPARATION_CACHES = {PYTHON_PRODUCT_KEY: {"UV_CACHE_DIR": "uv-cache", "UV_PYTHON_INSTALL_DIR": "uv-python"},
                      WEBSTORM_PRODUCT_KEY: {"npm_config_cache": "npm-cache"}}
CONTROL_ASSETS = Path(__file__).parents[1] / "assets/native-controls"


def sibling_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise ImportError("Native component unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observations: Any = sibling_module("native_observations", "native_observer.py")
proof: Any = sibling_module("native_proof", "native_coverage.py")
NATIVE_SUFFIXES = proof.NATIVE_PROVEN_SUFFIXES
lifecycle: Any = sibling_module("native_custody", "native_lifecycle.py")
NAMED_ODOO = frozenset(tool for tool in proof.CANARY_FILES if tool.startswith("Odoo"))
NAMED_PYTHON = frozenset(proof.CANARY_FILES) - NAMED_ODOO
license_boundary = observations.license_boundary

is_odoo = common.is_odoo


def select_backend(args, host, host_default=None):
    if common.native_options_supplied(args):
        return "native", "native_options_supplied"
    if host_default:
        return host_default, "host_default"
    if sys.platform != "darwin":
        return "api", "native_ineligible=native_macos_not_available"
    try:
        repo, config, _scope, selected = common.scope_selection(args, host)
        product_key = native_product_key(host, config, selected)
        if not eligible(args, host, repo, config, product_key):
            return "api", "native_ineligible=" + str(eligibility_reason(args, host, repo, config, product_key))
        if not selected:
            return "api", "selection_empty"
        if product_key == WEBSTORM_PRODUCT_KEY:
            return "native", "webstorm_native_route"
        if any(Path(f).suffix not in NATIVE_SUFFIXES for f in selected):
            return "api", "selection_file_type_unsupported"
        if common.lane_gaps(host, config, selected):
            return "api", "selection_outside_root_pycharm_lane"
        if is_odoo(repo):
            return "native", "odoo_native_route"
        if all(f.endswith(".py") for f in selected):
            return "native", "python_native_route"
        if all(Path(f).suffix in {".py", ".yaml", ".yml"} for f in selected):
            return "native", "configuration_native_route"
        return "api", "xml_selection_requires_odoo"
    except (Blocked, host.InspectError, OSError, subprocess.SubprocessError, ValueError):
        return "api", "native_selection_unavailable"


def native_product_key(host, config, selected):
    if (selected and all(Path(f).suffix in proof.WEBSTORM_PROVEN_SUFFIXES for f in selected)
            and common.configured_product_key(host, config, selected) == WEBSTORM_PRODUCT_KEY):
        return WEBSTORM_PRODUCT_KEY
    return PYTHON_PRODUCT_KEY


def requested_product_key(args, host):
    if args.native_recover_only:
        return PYTHON_PRODUCT_KEY
    try:
        _repo, config, _scope, selected = common.scope_selection(args, host)
    except (Blocked, host.InspectError, OSError, subprocess.SubprocessError, ValueError):
        return PYTHON_PRODUCT_KEY
    return native_product_key(host, config, selected)


def eligible(args, host, repo, settings, product_key=PYTHON_PRODUCT_KEY):
    return eligibility_reason(args, host, repo, settings, product_key) is None


def framework_available(roots):
    return bool(roots) and all(root.is_dir() for root in roots) and any((root / "odoo/release.py").is_file() for root in roots)


def eligibility_reason(args, host, repo, settings, product_key=PYTHON_PRODUCT_KEY):
    """Public configuration checks only; never inspect activation or start an IDE."""
    try:
        configured_worker = worker_settings(args, host, product_key)
        roots = configured_worker.framework_roots
        launcher_paths(configured_worker.worker)
        if product_key == WEBSTORM_PRODUCT_KEY:
            node_executable(host)
            if settings.get("prepare"):
                return "native_preparation_shape_unsupported"
            common.npm_lockfile(repo)
            common.profile_for(repo, args.profile or settings.get("profile", ""))
            return None
        if settings.get("prepare") and common.requested_python_version(settings) is None:
            return "native_preparation_shape_unsupported"
        if is_odoo(repo) and not framework_available(roots):
            return "native_odoo_framework_required"
        if is_odoo(repo):
            framework_context(repo, common.scope_selection(args, host)[3], roots)
        if not (repo / ".venv/bin/python").is_file() and common.requested_python_version(settings) is None:
            return "native_python_environment_required"
        common.profile_for(repo, args.profile or settings.get("profile", ""))
        return None
    except Blocked as error:
        return str(error)
    except (host.InspectError, OSError, ValueError):
        return "native_configuration_unavailable"


def framework_context(repo, selected, roots):
    """Compare declared tenant/addon series to public framework source, without executing it."""
    core = [root for root in roots if (root / "odoo/release.py").is_file()]
    if len(core) != 1:
        raise Blocked("native_odoo_framework_ambiguous")
    try:
        release = ast.parse((core[0] / "odoo/release.py").read_text())
        version = None
        for statement in release.body:
            if isinstance(statement, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "version_info" for target in statement.targets):
                    version = statement.value
                    break
        if not isinstance(version, ast.Tuple) or len(version.elts) < 2:
            raise Blocked("native_odoo_version_unproven")
        series = ".".join(str(ast.literal_eval(value)) for value in version.elts[:2])
        declared, dependencies = {}, {}
        workspace = repo / "workspace.toml"
        if workspace.is_file():
            workspace_config = tomllib.loads(workspace.read_text())
            value = workspace_config.get("build", {}).get("odoo_version")
            if value:
                declared["workspace.toml"] = str(value)
            shared = workspace_config.get("repos", {}).get("shared_addons")
            if shared:
                def git_common(root):
                    return Path(common.checked(["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"]).decode().strip()).resolve()
                try:
                    primary = git_common(repo).parent
                    candidates = {(repo / shared["path"]).resolve(), (primary / shared["path"]).resolve()}
                    expected_repositories = {git_common(path) for path in candidates if path.is_dir()}
                    matching = [root for root in roots if git_common(root) in expected_repositories]
                    if len(matching) != 1:
                        raise Blocked("native_odoo_dependency_unproven")
                    dependency = matching[0]
                    expected_commit = common.checked(["git", "-C", str(dependency), "rev-parse", "--verify", "--end-of-options", str(shared["ref"]) + "^{commit}"]).decode().strip()
                    actual_commit = common.checked(["git", "-C", str(dependency), "rev-parse", "HEAD"]).decode().strip()
                except common.Blocked:
                    raise Blocked("native_odoo_dependency_unproven") from None
                if expected_commit != actual_commit:
                    raise Blocked("native_odoo_dependency_revision_mismatch")
                dependencies["shared_addons"] = {"declared_ref": shared["ref"], "resolved_commit": expected_commit, "checkout": str(dependency)}
        for source in selected:
            directory = (repo / source).parent
            while directory.is_relative_to(repo):
                manifest = directory / "__manifest__.py"
                if manifest.is_file():
                    common.require_public_input(manifest)
                    expression = ast.parse(manifest.read_text()).body[0]
                    if not isinstance(expression, ast.Expr) or not isinstance(expression.value, ast.Dict):
                        raise Blocked("native_odoo_version_unproven")
                    value = ast.literal_eval(expression.value).get("version")
                    if value:
                        declared[manifest.relative_to(repo).as_posix()] = str(value)
                    break
                if directory == repo:
                    break
                directory = directory.parent
        if not declared or not re.fullmatch(r"\d+\.\d+", series):
            raise Blocked("native_odoo_version_unproven")
        for value in declared.values():
            match = re.match(r"^(\d+\.\d+)(?:\.|$)", value)
            if not match:
                raise Blocked("native_odoo_version_unproven")
            if match.group(1) != series:
                raise Blocked("native_odoo_version_mismatch")
        return {"framework_series": series, "declared_versions": declared, "dependency_revisions": dependencies}
    except (SyntaxError, ValueError, AttributeError, IndexError, StopIteration, TypeError):
        raise Blocked("native_odoo_version_unproven") from None


def native_worker_config(host: Any, product_key: str) -> dict[str, Any]:
    jetbrains = host.read_global_config().get("jetbrains", {})
    workers = jetbrains.get("nativeWorkers", {}) if isinstance(jetbrains, dict) else None
    if not isinstance(workers, dict):
        raise Blocked("native_configuration_invalid")
    if "nativeWorker" in jetbrains and workers:
        raise Blocked("native_configuration_invalid")
    legacy = product_key == PYTHON_PRODUCT_KEY and "nativeWorker" in jetbrains
    configured = jetbrains["nativeWorker"] if legacy else workers.get(product_key, {})
    if not isinstance(configured, dict):
        raise Blocked("native_configuration_invalid")
    return configured


def runtime_storage(host, worker, product_key=PYTHON_PRODUCT_KEY):
    configured = native_worker_config(host, product_key)
    raw = configured.get("cacheRoot")
    if raw is not None:
        shared = Path(raw).expanduser()
        if not shared.is_absolute() or shared.resolve() != shared or not shared.is_dir() or shared.is_relative_to(worker):
            raise Blocked("native_cache_root_unavailable")
        expected_identity = configured.get("cacheStorageIdentity")
        if expected_identity is not None and common.storage_identity(shared) != expected_identity:
            raise Blocked("native_cache_storage_changed")
    else:
        shared = host.cache_dir() / "native"
        shared.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = host.cache_dir() / "native" if os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR") else shared
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    caches = {name: shared / directory for name, directory in PREPARATION_CACHES[product_key].items()}
    for path in caches.values():
        if path.is_symlink():
            raise Blocked("native_cache_path_unsafe")
        path.mkdir(exist_ok=True, mode=0o700)
    return state, {name: str(path) for name, path in caches.items()}


def node_executable(host):
    raw = native_worker_config(host, WEBSTORM_PRODUCT_KEY).get("node")
    node = Path(raw).expanduser() if isinstance(raw, str) and raw else None
    if node is None or not node.is_absolute():
        raise Blocked("native_node_not_configured")
    if not node.is_file() or not os.access(node, os.X_OK) or not os.access(node.parent / "npm", os.X_OK):
        raise Blocked("native_node_unavailable")
    return node


def npm_preparation(node, project, private, caches):
    argv = [str(node.parent / "npm"), "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(project)]
    env = {"PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(private / "prepare-home"), "TMPDIR": str(private / "tmp"),
           "npm_config_update_notifier": "false", **caches}
    return argv, env


def wait_for_host_capacity(wait_until, on_wait, receipt):
    started = time.monotonic()
    try:
        while os.getloadavg()[0] > LOAD_LIMIT:
            if time.monotonic() >= wait_until:
                raise Blocked("native_host_load_paused")
            on_wait()
            time.sleep(max(0.0, min(1.0, wait_until - time.monotonic())))
    finally:
        receipt["load_wait_ms"] = round((time.monotonic() - started) * 1000)


@contextlib.contextmanager
def profile_lock(worker, name="worker.lock", wait_until=None, on_wait=None):
    """Exclusive kernel lock; an admitted request waits until `wait_until` for its owner to release it."""
    import fcntl
    path = worker / name
    if not worker.is_dir() or worker.is_symlink():
        raise Blocked("native_worker_missing")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Blocked("native_worker_lock_unsafe") from None
    with os.fdopen(fd, "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if wait_until is None or time.monotonic() >= wait_until:
                    raise Blocked("native_worker_busy") from None
            if on_wait is not None:
                on_wait()
            time.sleep(max(0.0, min(0.25, wait_until - time.monotonic())))
        yield lock


class WorkerSettings(NamedTuple):
    worker: Path
    app: Path
    framework_roots: list[Path]
    product: Any


def worker_settings(args: Any, host: Any, product_key: str = PYTHON_PRODUCT_KEY) -> WorkerSettings:
    product = host.IDE_PRODUCTS[product_key]
    configured = native_worker_config(host, product_key)
    raw = args.native_worker or os.environ.get("JETBRAINS_INSPECTION_NATIVE_WORKER") or configured.get("path")
    if not isinstance(raw, str) or not raw:
        raise Blocked("native_worker_not_configured")
    worker = Path(raw).expanduser()
    if not worker.is_absolute() or worker.is_symlink() or worker.resolve() != worker:
        raise Blocked("native_worker_path_unsafe")
    app = args.ide_app or configured.get("app")
    if args.native_recover_only:
        return WorkerSettings(worker, Path(app).expanduser().resolve() if isinstance(app, str) else worker, [], product)
    if not isinstance(app, str) or not app:
        raise Blocked("native_app_not_configured")
    candidate = Path(app).expanduser()
    if not candidate.is_absolute():
        candidates = [item.path for item in host.discover_ide_app_candidates()
                      if item.name == app or item.path.name == app or item.path.stem == app]
        if len(candidates) != 1:
            raise Blocked("native_app_selection_ambiguous_or_missing")
        candidate = candidates[0]
    app = candidate.resolve()
    launcher = app / "Contents/bin/inspect.sh"
    if not launcher.is_file():
        raise Blocked("native_inspector_unavailable")
    distribution = json.loads((app / "Contents/Resources/product-info.json").read_text())
    if distribution.get("productCode") not in product.product_codes or distribution.get("envVarBaseName") != product.env_var_base_name:
        raise Blocked(f"native_{product.key}_distribution_required")
    roots = args.native_framework_root or configured.get("frameworkRoots", [])
    roots = [Path(p).expanduser().resolve() for p in roots]
    return WorkerSettings(worker, app, roots, product)


def launcher_paths(worker):
    # Read exactly the public properties file, never glob/scan the config tree.
    properties = worker / "idea.properties"
    if properties.is_symlink():
        raise Blocked("native_worker_properties_unsafe")
    values = {}
    for line in properties.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    expected = {"idea.config.path": worker / "config", "idea.plugins.path": worker / "plugins"}
    if any(values.get(key) != str(path) for key, path in expected.items()):
        raise Blocked("native_independent_profile_required")
    if any(not path.is_dir() or path.is_symlink() for path in expected.values()):
        raise Blocked("native_independent_profile_required")
    home = worker / "home"
    if not home.is_dir() or home.is_symlink():
        raise Blocked("native_independent_home_required")
    return expected["idea.config.path"], expected["idea.plugins.path"], home


def check_lifetime(parent_fd, deadline):
    if time.monotonic() >= deadline:
        raise Blocked("native_timeout")
    if parent_fd is not None:
        ready = selectors.DefaultSelector()
        try:
            ready.register(parent_fd, selectors.EVENT_READ)
            if ready.select(0) and os.read(parent_fd, 1) == b"":
                raise Blocked("native_parent_exited")
        finally:
            ready.close()


def state_dir():
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/jetbrains-inspection/native"


def atomic_write(path, data):
    """Replace a non-secret owned file in its existing directory, durably."""
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_record(path, record):
    atomic_write(path, json.dumps(record).encode())


def sdk_tree(path):
    try:
        return ElementTree.parse(path, parser=ElementTree.XMLParser(target=ElementTree.TreeBuilder(insert_comments=True)))
    except ElementTree.ParseError:
        raise Blocked("native_sdk_table_malformed") from None


RECOVERY_LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d\s+[\d:, .]+\[\s*\d+]\s+\w+\s+-\s+(\S+)\s+-\s+(.*)$")
RECOVERY_LICENSING_WORD = re.compile(r"licen[cs]|trial|evaluation", re.I)
HEALTHY_LICENSING_LINES = (("#c.i.p.trialPromotion", "IDE is not managed"),
                           ("#c.i.p.i.b.AppStarter", "Will skip the config import to directory "))
LOCAL_INSPECTION_PREFIX = "Running local inspections on "
RECOVERY_LOG_LIMIT = 64 * 1024 * 1024


def logged_licensed_inspection(private: Path) -> bool:
    logs, project = private / "logs", private / "project"
    if logs.is_symlink() or not logs.is_dir():
        return False
    inspected = False
    try:
        for path in sorted(logs.glob("*.log")):
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as log:
                status = os.fstat(log.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_size > RECOVERY_LOG_LIMIT:
                    return False
                data = log.read()
            scanner = observations.LicenseScanner()
            scanner.feed(data, final=True)
            if scanner.observed or not scanner.complete:
                return False
            for line in data.decode(errors="replace").splitlines():
                framed = RECOVERY_LOG_LINE.match(line)
                logger, message = framed.groups() if framed else ("", line)
                if logger == "#c.i.c.e.GlobalInspectionContextImpl" and message.startswith(LOCAL_INSPECTION_PREFIX):
                    inspected = inspected or Path(message[len(LOCAL_INSPECTION_PREFIX):]).is_relative_to(project)
                elif RECOVERY_LICENSING_WORD.search(line) and not any(
                        logger == healthy_logger and message.startswith(healthy_message)
                        for healthy_logger, healthy_message in HEALTHY_LICENSING_LINES):
                    return False
    except OSError:
        return False
    return inspected


def recover_run(worker, receipt=None, result_path=None, release_run=None, process_proof=None):
    current = receipt if receipt is not None else {"private_deleted": True, "sdk_removed": True}
    try:
        lifecycle.recover(worker, current, common, write_record, remove_sdk, result_path=result_path,
                          release_run=release_run, process_proof=process_proof,
                          licensed_inspection_logged=logged_licensed_inspection)
    except lifecycle.LifecycleError as error:
        raise Blocked(str(error)) from None


PROCESS_BIRTH_CLOCK = "ps-lstart-utc"


def process_identity(pid, private):
    result = common.command(["ps", "-p", str(pid), "-o", "pid=,pgid=,lstart=,stat=,command="],
                            timeout=2, env={"PATH": os.defpath, "LC_ALL": "C", "TZ": "UTC"})
    if result.returncode or not result.stdout.strip():
        return None
    fields = result.stdout.decode(errors="replace").strip().split(None, 8)
    if len(fields) != 9:
        raise Blocked("native_recovery_process_identity_unproven")
    if fields[7].startswith("Z"):
        return None
    if int(fields[0]) != pid:
        raise Blocked("native_recovery_process_identity_unproven")
    private_match = bool(re.search(re.escape(str(private)) + r"(?=/|\s|$)", fields[8]))
    return {"pid": pid, "pgid": int(fields[1]), "started": " ".join(fields[2:7]),
            "birth_clock": PROCESS_BIRTH_CLOCK, "private": str(private) if private_match else None}


def group_gone(pid, seconds=2, *, receipt=None):
    probe = {"pgid": pid, "last_errno": None}
    if receipt is not None:
        receipt["process_group_probe"] = probe
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError as error:
            probe["last_errno"] = error.errno
            return True
        except PermissionError as error:
            # macOS can briefly retain an exiting group without signalable
            # members. EPERM is not exit proof; keep checking within the bound.
            probe["last_errno"] = error.errno
        except OSError as error:
            probe["last_errno"] = error.errno
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def send_group_signal(pid, operation, receipt):
    """A failed signal is an observation, never proof of group exit."""
    event = receipt.setdefault("process_signals", {}).setdefault(signal.Signals(operation).name, {"attempts": 0})
    event.update(pgid=pid, attempts=event["attempts"] + 1, last_errno=None)
    try:
        os.killpg(pid, operation)
    except OSError as error:
        event.update(sent=False, last_errno=error.errno)
        return False
    event["sent"] = True
    return True


def stop_orphan(worker, receipt):
    """The control lock proves no live guardian owns this journal."""
    journal = worker / "native-run.json"
    if not journal.exists():
        return None
    receipt.update(private_deleted=False, sdk_removed=False, custody_retained=True)
    if journal.is_symlink():
        raise Blocked("native_recovery_ownership_unproven")
    try:
        record = json.loads(journal.read_text())
        identity = lifecycle.identity_from(record)
        if common.storage_identity(Path(identity["storage_anchor"])) != identity["storage_identity"]:
            raise Blocked("native_recovery_storage_changed")
        lifecycle.prove_private(identity, record.get("private_materialized", False))
        process = lifecycle.active_process(record)
        custody_proof = {"run_id": identity["run_id"], "active_process": process}
        if process is None:
            return custody_proof
        engine = process["identity"]
        pid = engine["pid"]
        receipt["recovery_process"] = {key: engine.get(key) if type(engine.get(key)) is int and engine[key] > 0 else None for key in ("pid", "pgid")}
        if type(pid) is not int or pid < 1 or engine["pgid"] != pid or engine["private"] != identity["private"]:
            raise Blocked("native_recovery_process_identity_unproven")
        current = process_identity(pid, Path(identity["private"]))
        if current is None:
            if group_gone(pid, receipt=receipt):
                return custody_proof
            raise Blocked("native_recovery_process_group_unproven")
        if (engine.get("birth_clock") == current.get("birth_clock") == PROCESS_BIRTH_CLOCK
                and current["started"] != engine["started"]):
            # POSIX forbids reuse of a PID while its matching process group
            # exists. A different birth proves the old group is gone; never
            # signal the unrelated replacement. The outcome hold remains.
            receipt["orphan_pid_reused"] = pid
            return custody_proof
        if current != engine:
            raise Blocked("native_recovery_process_identity_changed")
        if send_group_signal(pid, signal.SIGKILL, receipt):
            receipt["orphan_group_stopped"] = pid
        receipt["recovered_process_kind"] = process["kind"]
        if not group_gone(pid, receipt=receipt):
            raise Blocked("native_recovery_process_group_unproven")
        return custody_proof
    except lifecycle.LifecycleError as error:
        raise Blocked(str(error)) from None
    except (KeyError, TypeError, ValueError):
        raise Blocked("native_recovery_journal_malformed") from None


LAUNCH_BARRIER = "import os,sys; fd=int(sys.argv[1]); allowed=os.read(fd,1)==b'1'; os.close(fd); sys.exit(125) if not allowed else os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"


class ChildExitObserver:
    """Observe exit without releasing the child PID that pins our group."""
    def __init__(self, pid):
        self.pid, self.complete, self.queue = pid, False, None
        self.kind = "waitid" if all(hasattr(os, name) for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")) else "kqueue"
        if self.kind == "kqueue":
            if not hasattr(select, "kqueue"):
                raise Blocked("native_exit_observer_unavailable")
            self.queue = select.kqueue()
            try:
                self.queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                   flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                   fflags=select.KQ_NOTE_EXIT)], 0, 0)
            except OSError:
                self.queue.close()
                raise

    def exited(self):
        if not self.complete:
            if self.queue is None:
                event = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                self.complete = event is not None and event.si_pid == self.pid
            else:
                for event in self.queue.control(None, 1, 0):
                    if event.flags & select.KQ_EV_ERROR:
                        raise OSError(event.data, "process exit observation failed")
                    if event.ident == self.pid and event.filter == select.KQ_FILTER_PROC and event.fflags & select.KQ_NOTE_EXIT:
                        self.complete = True
        return self.complete

    def close(self):
        if self.queue is not None:
            self.queue.close()


def execute(argv, env, private: Path, deadline: float, parent_fd: int | None, receipt: dict[str, Any], *, lock_fd: int, global_lock_fd: int | None = None, require_completion=True, on_spawn=None, on_exit=None):
    """One guarded process group; preparation never uses IDE event parsing."""
    process = None
    exit_watch = None
    receipt["worker_exited"] = True
    pid_key, exited_key = ("worker_pid", "worker_exited") if require_completion else ("preparation_pid", "preparation_exited")
    gate_read, gate_write = os.pipe()
    stdout = None
    paused = False
    log_offsets, log_framers = {}, {}
    log_license_scanners = {}
    log_identities = set()
    observation_failures = set()
    output_framer = observations.LineFramer()
    output_license_scanner = observations.LicenseScanner()
    observer = observations.EngineObserver(private / "project", receipt)
    selector = selectors.DefaultSelector()
    receipt["license_observation_complete"] = not require_completion

    def leader_exited():
        try:
            return exit_watch.exited()
        except OSError as observation_error:
            receipt["exit_observer_errno"] = observation_error.errno
            raise Blocked("native_exit_observation_unproven") from None

    def scan_license(scanner, data, final=False):
        if scanner.feed(data, final=final):
            receipt["license_boundary_observed"] = True

    def consume(framer, scanner, data, final=False):
        if not require_completion:
            return
        # Scan the whole received chunk before ERROR, cancellation, oversized
        # lines or any other ordinary observation can abort processing it.
        # Leader exit does not end streams inherited by descendants. Only the
        # post-group drain may finalize the independent license-line scanner.
        scan_license(scanner, data)
        if receipt.get("license_boundary_observed"):
            raise Blocked("native_owner_activation_required")
        lines, partial = framer.feed(data, final=final)
        for line in lines:
            observer.observe(line)
        if partial:
            observer.observe(partial, partial=True)

    def ordinary_read_size(read_budget, requested: int) -> int:
        if read_budget is None:
            return requested
        if read_budget["remaining"] <= 0 or time.monotonic() >= read_budget["deadline"]:
            receipt["ordinary_drain_bound"] = True
            raise Blocked("native_final_drain_limit")
        return min(requested, int(read_budget["remaining"]))

    def read_logs(final=False, budget=None):
        if not require_completion:
            return
        for path in (private / "logs").glob("*.log"):
            ordinary_read_size(budget, 1)
            if path.is_symlink():
                raise Blocked("native_log_path_unsafe")
            try:
                stream = path.open("rb")
            except FileNotFoundError:
                # Rotation may rename a globbed path before open. Judge any
                # lost observed inode in the post-exit drain, not this tick.
                continue
            with stream:
                status = os.fstat(stream.fileno())
                identity = status.st_dev, status.st_ino
                log_identities.add(identity)
                offset = log_offsets.get(identity, 0)
                if status.st_size < offset:
                    observation_failures.add("log_truncated")
                    raise Blocked("native_log_truncated")
                framer = log_framers.setdefault(identity, observations.LineFramer())
                scanner = log_license_scanners.setdefault(identity, observations.LicenseScanner())
                stream.seek(offset)
                while True:
                    check_lifetime(parent_fd, deadline)
                    log_read_size = ordinary_read_size(budget, 1024 * 1024)
                    data = stream.read(log_read_size)
                    if budget is not None:
                        budget["remaining"] -= len(data)
                    log_offsets[identity] = stream.tell()
                    consume(framer, scanner, data)
                    if not final or len(data) < log_read_size:
                        break
                log_offsets[identity] = stream.tell()
            if final:
                consume(framer, scanner, b"", final=True)

    def drain_licenses(stream):
        """Bounded post-exit observation, independent of the execution deadline.

        No ordinary observer runs here. Any unreadable, truncated or oversized
        source leaves the terminal outcome unproved as non-licensing.
        """
        limit = time.monotonic() + DRAIN_SECONDS
        remaining = DRAIN_BYTES
        complete = not observation_failures
        failures = set(observation_failures)

        def incomplete(reason):
            nonlocal complete
            complete = False
            failures.add(reason)

        def allowed():
            return remaining > 0 and time.monotonic() < limit

        try:
            if stream is None:
                incomplete("stdout_unavailable")
            else:
                os.set_blocking(stream.fileno(), False)
                while allowed():
                    try:
                        data = os.read(stream.fileno(), min(65536, remaining))
                    except BlockingIOError:
                        incomplete("stdout_eof_unproved")
                        break
                    if not data:
                        break
                    remaining -= len(data)
                    scan_license(output_license_scanner, data)
                else:
                    incomplete("drain_bound")
                scan_license(output_license_scanner, b"", final=True)
        except Exception:
            incomplete("stdout_read_failed")

        try:
            paths = []
            for path in (private / "logs").glob("*.log"):
                if len(paths) == 64:
                    incomplete("log_count_bound")
                    break
                paths.append(path)
            seen = set()
            for path in paths:
                try:
                    if not allowed():
                        incomplete("drain_bound")
                        break
                    if path.is_symlink():
                        incomplete("log_path_unsafe")
                        continue
                    with path.open("rb") as log:
                        status = os.fstat(log.fileno())
                        identity = status.st_dev, status.st_ino
                        seen.add(identity)
                        offset = log_offsets.get(identity, 0)
                        scanner = log_license_scanners.setdefault(identity, observations.LicenseScanner())
                        if status.st_size < offset:
                            incomplete("log_truncated")
                            offset = 0
                            scanner = observations.LicenseScanner()
                            log_license_scanners[identity] = scanner
                        log.seek(offset)
                        while allowed():
                            data = log.read(min(65536, remaining))
                            if not data:
                                break
                            remaining -= len(data)
                            log_offsets[identity] = log.tell()
                            scan_license(scanner, data)
                        else:
                            incomplete("drain_bound")
                    scan_license(scanner, b"", final=True)
                except Exception:
                    incomplete("log_read_failed")
            if log_identities - seen:
                incomplete("log_disappeared")
        except Exception:
            incomplete("logs_unavailable")
        if not output_license_scanner.complete or any(not scanner.complete for scanner in log_license_scanners.values()):
            incomplete("line_over_limit")
        receipt["license_observation_complete"] = complete
        if failures:
            receipt["license_observation_failures"] = sorted(failures)

    try:
        check_lifetime(parent_fd, deadline)
        inherited = (lock_fd,) if global_lock_fd is None else (lock_fd, global_lock_fd)
        inherited += (gate_read,)
        launch = [sys.executable, "-c", LAUNCH_BARRIER, str(gate_read), *argv]
        process = subprocess.Popen(launch, cwd=private, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True,
                                   pass_fds=inherited)
        receipt[pid_key] = process.pid
        receipt[exited_key] = False
        receipt["process_custody_closed"] = False
        stdout = process.stdout
        if gate_read is not None:
            os.close(gate_read)
            gate_read = None
        try:
            exit_watch = ChildExitObserver(process.pid)
            receipt["exit_observer"] = exit_watch.kind
        except OSError as error:
            receipt["exit_observer_errno"] = error.errno
            raise Blocked("native_exit_observer_unavailable") from None
        if on_spawn is not None:
            on_spawn(process.pid)
        check_lifetime(parent_fd, deadline)
        if gate_write is not None:
            os.write(gate_write, b"1")
            receipt["launch_gate_released" if require_completion else "preparation_gate_released"] = True
            os.close(gate_write)
            gate_write = None
        if stdout is None:
            raise Blocked("native_output_pipe_unavailable")
        os.set_blocking(stdout.fileno(), False)
        selector.register(stdout, selectors.EVENT_READ)
        while True:
            check_lifetime(parent_fd, deadline)
            for _key, _event in selector.select(0.1):
                chunk = os.read(stdout.fileno(), 65536)
                consume(output_framer, output_license_scanner, chunk)
                if not chunk:
                    selector.unregister(stdout)
            read_logs()
            if leader_exited():
                final_budget = {"deadline": time.monotonic() + DRAIN_SECONDS, "remaining": DRAIN_BYTES}
                while True:
                    check_lifetime(parent_fd, deadline)
                    size = ordinary_read_size(final_budget, 65536)
                    try:
                        chunk = os.read(stdout.fileno(), size)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    final_budget["remaining"] -= len(chunk)
                    consume(output_framer, output_license_scanner, chunk)
                consume(output_framer, output_license_scanner, b"", final=True)
                read_logs(final=True, budget=final_budget)
                break
            load = os.getloadavg()[0]
            if load > LOAD_LIMIT and not paused:
                if not send_group_signal(process.pid, signal.SIGSTOP, receipt):
                    if leader_exited():
                        continue
                    raise Blocked("native_process_signal_failed")
                paused = True
                receipt["load_pause_count"] += 1
            elif load <= LOAD_LIMIT and paused:
                if not send_group_signal(process.pid, signal.SIGCONT, receipt):
                    if leader_exited():
                        continue
                    raise Blocked("native_process_signal_failed")
                paused = False
        receipt["completion_observed"] = observer.completion if require_completion else False
    except observations.ObservationError as error:
        raise Blocked(str(error)) from None
    finally:
        for gate in (gate_read, gate_write):
            if isinstance(gate, int):
                os.close(gate)
        selector.close()
        if exit_watch is not None:
            try:
                exit_watch.close()
            except OSError as error:
                receipt["exit_observer_close_errno"] = error.errno
        if process is not None:
            if process.returncode is None:
                # The non-reaping observer keeps this PID pinned even on exit.
                send_group_signal(process.pid, signal.SIGKILL, receipt)
            try:
                process.wait(timeout=2)
                reaped = True
            except (subprocess.TimeoutExpired, OSError) as error:
                reaped = False
                receipt["process_reap_failure"] = {"kind": type(error).__name__, "errno": getattr(error, "errno", None)}
            receipt["process_reaped"] = reaped
            receipt["engine_exit_code" if require_completion else "preparation_exit_code"] = process.returncode
            absent = group_gone(process.pid, receipt=receipt)
            closed = reaped and absent
            receipt["process_custody_closed"] = closed
            if require_completion:
                drain_licenses(stdout if stdout is not None else process.stdout)
                if not closed:
                    receipt["license_observation_complete"] = False
                    receipt.setdefault("license_observation_failures", []).append("process_group_unproven")
            if stdout is not None:
                stdout.close()
            receipt[exited_key] = closed
            if closed and on_exit is not None:
                on_exit()
            if not closed:
                raise Blocked("native_process_group_unproven")
        elif require_completion:
            # Popen never created a licensed process, hence no output can have
            # been lost. The pre-armed hold can be reconciled as a failed spawn.
            receipt["license_observation_complete"] = True
    if process.returncode != 0 or require_completion and not observer.completion:
        raise Blocked("native_execution_incomplete" if require_completion else "native_preparation_failed")


def write_selection_scope(idea: Path, selected: set[str]):
    scope = ElementTree.Element("component", name="DependencyValidationManager")
    if any(re.search(r'[\s"!&|()*?:\[\]\\]', path) for path in selected):
        raise Blocked("native_scope_path_unsupported")
    ElementTree.SubElement(scope, "scope", name="JBNativeSelection", pattern="||".join("file:" + path for path in sorted(selected)))
    (idea / "scopes").mkdir(exist_ok=True)
    ElementTree.ElementTree(scope).write(idea / "scopes/JBNativeSelection.xml", encoding="unicode")


def write_module_manager(idea: Path, module: ElementTree.Element):
    ElementTree.ElementTree(module).write(idea / "Native.iml", encoding="unicode")
    modules = ElementTree.Element("project", version="4")
    component = ElementTree.SubElement(modules, "component", name="ProjectModuleManager")
    ElementTree.SubElement(ElementTree.SubElement(component, "modules"), "module", fileurl=(idea / "Native.iml").as_uri(), filepath=str(idea / "Native.iml"))
    ElementTree.ElementTree(modules).write(idea / "modules.xml", encoding="unicode")


def prepare_web_model(project: Path, selected: set[str]):
    idea = project / ".idea"
    idea.mkdir(exist_ok=True)
    if not (idea / "modules.xml").is_file():
        module = ElementTree.Element("module", type="WEB_MODULE", version="4")
        manager = ElementTree.SubElement(module, "component", name="NewModuleRootManager")
        content = ElementTree.SubElement(manager, "content", url=project.as_uri())
        ElementTree.SubElement(content, "excludeFolder", url=(project / ".git").as_uri())
        ElementTree.SubElement(manager, "orderEntry", type="inheritedJdk")
        ElementTree.SubElement(manager, "orderEntry", type="sourceFolder", forTests="false")
        write_module_manager(idea, module)
    write_selection_scope(idea, selected)


def prepare_model(project: Path, sdk_name: str, framework_roots: list[Path], selected: set[str]):
    idea = project / ".idea"
    idea.mkdir(exist_ok=True)
    # A private snapshot's module model only; original IDE state is untouched.
    module = ElementTree.Element("module", type="PYTHON_MODULE", version="4")
    manager = ElementTree.SubElement(module, "component", name="NewModuleRootManager")
    for root in [project, *framework_roots]:
        content = ElementTree.SubElement(manager, "content", url=root.as_uri())
        ElementTree.SubElement(content, "sourceFolder", url=root.as_uri(), isTestSource="false")
        if root == project:
            if (project / "addons").is_dir():
                ElementTree.SubElement(content, "sourceFolder", url=(project / "addons").as_uri(), isTestSource="false")
            for excluded in (".venv", ".git"):
                ElementTree.SubElement(content, "excludeFolder", url=(root / excluded).as_uri())
    ElementTree.SubElement(manager, "orderEntry", type="jdk", jdkName=sdk_name, jdkType="Python SDK")
    ElementTree.SubElement(manager, "orderEntry", type="sourceFolder", forTests="false")
    write_module_manager(idea, module)
    misc = ElementTree.Element("project", version="4")
    ElementTree.SubElement(misc, "component", name="ProjectRootManager", version="2", attrib={"project-jdk-name": sdk_name, "project-jdk-type": "Python SDK"})
    ElementTree.ElementTree(misc).write(idea / "misc.xml", encoding="unicode")
    pyproject_model = ElementTree.Element("project", version="4")
    component = ElementTree.SubElement(pyproject_model, "component", name="PyProjectModelSettings")
    ElementTree.SubElement(component, "option", name="usePyprojectToml", value="false")
    ElementTree.ElementTree(pyproject_model).write(idea / "pyProjectModel.xml", encoding="unicode")
    write_selection_scope(idea, selected)


def plant_controls(project, run_id, odoo, product_key=PYTHON_PRODUCT_KEY):
    fixtures = CONTROL_ASSETS
    prefix = "jb_native_controls_" + run_id.replace("-", "")
    destination = project / prefix
    destination.mkdir()
    names = ["coverage.py", "duplicates.py", "python_types.py"]
    if odoo:
        names += ["__init__.py", "__manifest__.py", "models.py", "views/defect.xml"]
    if product_key == WEBSTORM_PRODUCT_KEY:
        fixtures = CONTROL_ASSETS / WEBSTORM_PRODUCT_KEY
        names = sorted({*proof.WEBSTORM_CANARY_FILES.values(), *proof.WEBSTORM_CLEAN_TWINS.values()})
    for name in names:
        target = destination / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(fixtures / name, target)
    return {(Path(prefix) / name).as_posix() for name in names}


def register_sdk(config: Path, interpreter: Path, version: str, sdk_name: str, *, on_attempt=None):
    """Register the run's non-secret entry; its custody context owns removal."""
    table = config / "options/jdk.table.xml"
    if table.is_symlink() or table.parent.is_symlink():
        raise Blocked("native_sdk_table_unsafe")
    existed = table.exists()
    tree = sdk_tree(table) if existed else ElementTree.ElementTree(ElementTree.Element("application"))
    root = tree.getroot()
    component = root.find("component[@name='ProjectJdkTable']")
    if component is None:
        component = ElementTree.SubElement(root, "component", name="ProjectJdkTable")
    for entry in component.findall("jdk"):
        name = entry.find("name")
        if name is not None and name.get("value") == sdk_name:
            raise Blocked("native_sdk_identity_conflict")
    jdk = ElementTree.SubElement(component, "jdk", version="2")
    for tag, value in (("name", sdk_name), ("type", "Python SDK"), ("version", "Python " + version), ("homePath", str(interpreter))):
        ElementTree.SubElement(jdk, tag, value=value)
    output = common.checked([str(interpreter), "-c", "import sysconfig,json; print(json.dumps([sysconfig.get_path('stdlib'),sysconfig.get_path('purelib')]))"])
    roots = ElementTree.SubElement(jdk, "roots")
    for tag in ("classPath", "sourcePath"):
        composite = ElementTree.SubElement(ElementTree.SubElement(roots, tag), "root", type="composite")
        if tag == "classPath":
            for path in json.loads(output):
                ElementTree.SubElement(composite, "root", url=Path(path).as_uri(), type="simple")
    ElementTree.SubElement(jdk, "additional")
    table.parent.mkdir(exist_ok=True)
    if on_attempt is not None:
        on_attempt()
    atomic_write(table, ElementTree.tostring(tree.getroot(), encoding="utf-8"))


def remove_sdk(config: Path, interpreter: Path, sdk_name: str, table_existed: bool, missing_ok=False):
    table = config / "options/jdk.table.xml"
    if table.is_symlink() or table.parent.is_symlink():
        raise Blocked("native_sdk_table_unsafe")
    if not table.exists() and missing_ok:
        return
    current = sdk_tree(table)
    component = current.getroot().find("component[@name='ProjectJdkTable']")
    if component is None:
        if missing_ok:
            return
        raise Blocked("native_sdk_cleanup_unproven")
    matches = []
    for entry in component:
        name = entry.find("name")
        if name is not None and name.get("value") == sdk_name:
            matches.append(entry)
    if len(matches) > 1 or not matches and not missing_ok:
        raise Blocked("native_sdk_cleanup_unproven")
    for entry in matches:
        home = entry.find("homePath")
        if home is None or home.get("value") != str(interpreter):
            raise Blocked("native_sdk_cleanup_identity_changed")
        component.remove(entry)
    if not table_existed and len(component) == 0 and len(current.getroot()) == 1:
        table.unlink()
    elif matches:
        atomic_write(table, ElementTree.tostring(current.getroot(), encoding="utf-8"))


def read_results(results: Path, project: Path) -> tuple[list[dict[str, Any]], set[str], dict[str, str]]:
    findings: list[dict[str, Any]] = []
    languages: dict[str, str] = {}
    registered: set[str] = set()
    descriptions = results / ".descriptions.xml"
    if descriptions.is_file():
        root = ElementTree.parse(descriptions).getroot()
        for node in root.iter("inspection"):
            short_name = node.get("shortName")
            if short_name:
                registered.add(short_name)
                if language := node.get("language"):
                    languages[short_name] = language
    for path in sorted(results.glob("*.xml")):
        if path.is_symlink():
            raise Blocked("native_report_path_unsafe")
        root = ElementTree.parse(path).getroot()
        if root.tag != "problems":
            continue
        for problem in root.findall("problem"):
            raw = urllib.parse.unquote(problem.findtext("file", "")).removeprefix("file://")
            raw = raw.replace("$PROJECT_DIR$", str(project))
            source = Path(raw)
            if not source.is_absolute() or not source.is_relative_to(project):
                continue
            relative = source.relative_to(project).as_posix()
            problem_class = problem.find("problem_class")
            rule = problem_class.get("id", path.stem) if problem_class is not None else path.stem
            line = int(problem.findtext("line", "0"))
            if not isinstance(rule, str) or not rule.strip() or line < 1:
                raise Blocked("native_finding_identity_unproven")
            if relative.startswith("jb_native_controls_") and relative.endswith("/python_types.py") and rule == "PyTypeHintsInspection":
                raise Blocked("native_python_language_level_mismatch")
            findings.append({"file": relative, "line": line, "rule_id": rule,
                             "severity": problem_class.get("severity", "WARNING") if problem_class is not None else "WARNING",
                             "message": common.safe_text(problem.findtext("description", ""))})
    return findings, registered, languages



ADVISORY_SEVERITIES = frozenset({"TYPO", "GRAMMAR_ERROR", "STYLE_SUGGESTION"})
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def changed_line_map(repo: Path, selected: set[str]) -> dict[str, set[int] | None] | None:
    def git(*argv: str) -> str | None:
        result = common.command(["git", "-c", "core.quotePath=false", "-C", str(repo), *argv])
        return None if result.returncode else result.stdout.decode(errors="replace")

    upstream = git("rev-parse", "--symbolic-full-name", "origin/HEAD")
    base = git("merge-base", "HEAD", upstream.strip()) if upstream else None
    if not base or not selected:
        return None
    paths = sorted(selected)
    present = git("ls-tree", "-r", "-z", "--name-only", base.strip(), "--", *paths)
    differing = git("diff", "-z", "--name-only", "--no-renames", base.strip(), "--", *paths)
    diff = git("diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/",
               base.strip(), "--", *paths)
    if present is None or differing is None or diff is None:
        return None
    edited: dict[str, set[int]] = {}
    current: str | None = None
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header, current = True, None
        elif in_header and line.startswith("+++ "):
            name = line[len("+++ "):].rstrip("\t")
            current = name[len("b/"):] if name.startswith("b/") else None
        elif hunk := HUNK.match(line):
            in_header = False
            if current is not None:
                start, count = int(hunk[1]), int(hunk[2] if hunk[2] is not None else 1)
                edited.setdefault(current, set()).update(range(start, start + count))
    changed: dict[str, set[int] | None] = {path: None for path in selected - set(present.split("\0"))}
    for path in selected & set(differing.split("\0")):
        changed.setdefault(path, edited.get(path))
    return changed


def attribute_to_change(receipt: dict[str, Any], findings: list[dict[str, Any]], repo: Path, scope: str, selected: set[str]) -> None:
    if scope not in {"changed_files", "files"}:
        return
    changed = changed_line_map(repo, selected)
    receipt["changed_lines"] = "unavailable" if changed is None else "computed"
    for finding in findings:
        if changed is None:
            finding["introduced"] = None
        elif finding["file"] not in changed:
            finding["introduced"] = False
        else:
            lines = changed[finding["file"]]
            finding["introduced"] = lines is None or finding["line"] in lines


def envelope(host: Any, receipt: dict[str, Any], findings: list[dict[str, Any]], reason: str, gaps: list[str], artifact: str | None = None) -> dict[str, Any]:
    product = host.IDE_PRODUCTS[receipt.get("product", PYTHON_PRODUCT_KEY)]
    preexisting = [finding for finding in findings if finding.get("introduced") is False]
    introduced = [finding for finding in findings if finding.get("introduced") is not False]
    advisory = [finding for finding in introduced if finding["severity"] in ADVISORY_SEVERITIES]
    findings = [finding for finding in introduced if finding["severity"] not in ADVISORY_SEVERITIES]
    clean = reason == "native_completed" and not gaps
    verdict = "GREEN" if clean and not findings else "RED" if clean else "UNKNOWN"
    action = "Fix current findings." if verdict == "RED" else "None." if verdict == "GREEN" else "Resolve the reported native prerequisite or execution-coverage gap before a fresh assessment."
    if advisory and verdict != "UNKNOWN":
        action += (" Review the advisory spelling and grammar findings: fix real mistakes, and add real tool, library"
                   " or product names to the repository's .idea/dictionaries/project.xml.")
    if reason in {"native_owner_activation_required", "native_activation_stop_recorded"}:
        action = "Owner question: the Director must restore the configured independent worker's activation through the product UI using existing entitlements. Stop licensed runs; never start a trial or copy credentials."
    elif reason in {"native_worker_not_configured", "native_app_not_configured"}:
        action = f"Configure jetbrains.nativeWorkers.{product.key}.path/app in the shared helper config for the independently Director-activated worker, or supply --native-worker and --ide-app. See references/native-backend.md."
    elif reason in {"native_node_not_configured", "native_node_unavailable"}:
        action = "Set jetbrains.nativeWorkers.webstorm.node in the shared helper config to the absolute path of an executable node with npm in the same directory. See references/native-backend.md."
    elif reason == "native_npm_lockfile_required":
        action = "Native WebStorm needs a root package.json and package-lock.json; it runs only npm ci --ignore-scripts and never npm install. Commit the lockfile or keep this selection UNKNOWN."
    elif reason == "native_pnpm_unsupported":
        action = "Native WebStorm does not prepare pnpm projects yet. Keep this selection UNKNOWN and rely on the repository's own lint and tests."
    elif reason == "native_trusted_root_required":
        action = "Use the intended repository under jetbrains.trustedAutoOpenRoots in the standard helper config; native preparation executes only for a trusted source root."
    elif reason == "native_python_environment_required":
        action = "Run the repository's documented Python setup in the exact worktree; native needs its .venv or declared structured prepare.python."
    elif reason == "native_preparation_shape_unsupported":
        action = "Keep the declared command or nested preparation on its existing backend, or prepare the exact root .venv and select --backend native explicitly. Native runs only structured root prepare.python inside its snapshot."
    elif reason.startswith("native_odoo_"):
        action = "Run the workspace's documented dependency sync so its declared addon refs resolve locally. Match the selected addon's version and workspace build.odoo_version to one Odoo core checkout plus the declared addon dependency roots. Supply those clean roots with --native-framework-root; preserve source version declarations."
    elif reason == "native_scope_too_large":
        action = "Reduce the selected scope or set --max-files to the intended file limit before reassessing."
    elif reason == "native_scope_unsupported":
        action = "Select .py, .yaml, .yml and Odoo .xml files assigned to the root PyCharm lane; other language or nested-project models require their existing qualified backend."
    elif reason in {"native_recovery_process_group_unproven", "native_recovery_process_identity_changed", "native_recovery_process_identity_unproven"}:
        identity = receipt.get("recovery_process", {})
        pid = identity.get("pid") or "<journaled-pid>"
        pgid = receipt.get("process_group_probe", {}).get("pgid") or identity.get("pgid") or "<journaled-pgid>"
        action = f"Preserve the SDK, private directory and journal. Run the read-only census ps -axo pid=,pgid=,stat=,command= and compare journaled PID {pid} / PGID {pgid}. Let unrelated processes finish. Bring the Director one recommendation before ending any owned process whose identity is unproved; never signal an unproved identity automatically. Once the journaled PID and group are absent, use --backend native --native-recover-only with the same independent worker. This does not release an unknown licensed outcome."
    elif reason.startswith("native_recovery_") and reason != "native_recovery_only_no_inspection":
        action = "Restore the journaled storage and use --backend native --native-recover-only with the same independent worker. Preserve unproved state."
    elif reason == "native_interrupted_run_unresolved":
        action = "Resources were recovered but the licensed run's outcome is unproved. Preserve the journal. Reconcile the original saved terminal JSON with no licensing event with --native-recover-only --native-reconcile-result; without that evidence, escalate the missing outcome for review. Never infer expired activation or delete the hold to retry."
    elif reason == "native_license_observation_unproven":
        action = "The engine exited but licence observation was incomplete. Preserve its run hold and use --native-recover-only for resources. Escalate the missing outcome; exact-run release requires the Director's recorded independent-UI confirmation. No activation expiry is inferred."
    elif reason.startswith("native_reconciliation_"):
        action = "Use --native-recover-only with the original terminal native result for this exact run/backend. Results with licensing events, unknown outcomes and activation stops cannot be reconciled as results without a licensing event. See references/native-backend.md."
    elif reason.startswith("native_release_"):
        action = "Preserve unmatched helper metadata. After the Director's recorded confirmation through the independent product UI, use --native-recover-only --native-release-run with the exact journal/stop run UUID. That command never inspects."
    elif reason == "native_sdk_table_malformed":
        action = "Preserve the independent profile's malformed non-secret SDK table and repair that metadata under the worker lock before reassessing. No SDK was registered; never reset the profile or inspect activation files."
    elif reason.startswith("native_cache_"):
        action = "Restore the configured existing host cache directory and recorded storage identity, or configure its supported replacement outside the activated worker. Never create a replacement mount directory."
    elif reason == "native_worker_busy":
        action = "Another owner held the isolated worker past --lifecycle-lock-timeout-ms; no engine launched. Let the live holder or independent GUI finish, then request one fresh assessment. If its guardian died, use --backend native --native-recover-only with the same independent worker to reconcile the journaled process; never kill another session's worker."
    elif reason == "native_process_group_unproven":
        action = "Preserve the SDK, private directory and journal until the process group is proved gone. Use --backend native --native-recover-only with the same independent worker; resource recovery does not clear an unknown licensed outcome."
    elif reason == "native_process_signal_failed":
        action = "The load-control signal failed while its leader was still live. Bounded finalization attempted termination, reaping, group proof and licence drain. Check process_signals in the receipt before a fresh assessment; retained custody requires --native-recover-only first."
    elif reason == "native_final_drain_limit":
        action = "Final stdout and logs exceeded their shared read bound. Narrow the inspection scope or diagnose the noisy helper or log writer before a fresh assessment. Finalization still stops the owned group and independently scans for licensing events; preserve any resulting hold and use --native-recover-only for retained custody."
    elif reason in {"native_exit_observer_unavailable", "native_exit_observation_unproven"}:
        action = "Restore non-reaping child-exit observation in the documented native Python setup (waitid/WNOWAIT or kqueue/NOTE_EXIT). Registration is required before the launch gate opens. Preserve any held custody and use --native-recover-only first; never substitute a reaping poll before stopping the owned group."
    report = (f"{verdict}: native {product.display_name}; {reason}; {len(findings)} findings; {len(advisory)} advisory spelling or grammar findings; "
              f"{len(preexisting)} preexisting findings not counted; "
              f"{len(gaps)} proof gaps; {len(receipt.get('coverage', {}).get('unproven', []))} inspections unproven.")
    return {"schema_version": host.AGENT_RESULT_SCHEMA_VERSION, "command": "agent-inspect", "backend": "native",
            "status": {"GREEN": "clean", "RED": "findings"}.get(verdict, "error"),
            "agent_result": {"verdict": verdict, "bucket": {"GREEN": "clean", "RED": "findings"}.get(verdict, "environment_blocked"),
                             "retry_policy": {"retry": False, "max_attempts": 0, "wait_ms": 0}, "terminal": True,
                             "next_action": action, "agent_report": report},
            "scope": receipt.get("scope"), "selected_file_count": receipt.get("selected_file_count"),
            "finding_count": len(findings), "preexisting_finding_count": len(preexisting),
            "advisory_finding_count": len(advisory), "advisory_findings": advisory[:common.FINDING_LIMIT],
            "advisory_findings_truncated": len(advisory) > common.FINDING_LIMIT,
            "changed_lines": receipt.get("changed_lines"), "findings": findings[:common.FINDING_LIMIT], "findings_artifact": artifact,
            "findings_truncated": len(findings) > common.FINDING_LIMIT, "proof_failures": gaps,
            "coverage": receipt.get("coverage", {}), "uncovered_inspections": receipt.get("coverage", {}).get("unproven", []),
            "inspection_outcome": {"verdict": verdict, "reason": reason},
            "cleanup": {"status": "closed" if receipt.get("private_deleted") and receipt.get("sdk_removed") and receipt.get("worker_exited", True) and receipt.get("process_custody_closed", True) else "failed"},
            "identity": {"helper_revision": receipt.get("helper_revision"), "native_backend_revision": receipt.get("backend_revision"), "inspection_run_id": receipt["run_id"]},
            "diagnostic": {"error_reason": reason, "native": receipt}}


def new_receipt(host):
    components = {name: common.digest(Path(__file__).with_name(name)) for name in
                  ("native_backend.py", "native_coverage.py", "native_observer.py", "native_lifecycle.py", "native_common.py")}
    revision = hashlib.sha256(json.dumps(components, sort_keys=True).encode()).hexdigest()
    return {"run_id": str(uuid.uuid4()), "private_deleted": True, "sdk_removed": True, "load_pause_count": 0, "process_custody_closed": True,
            "nice": NICE, "active_processors": ACTIVE_PROCESSORS, "heap_limit_mib": HEAP_LIMIT_MIB,
            "helper_revision": host.helper_revision(), "backend_revision": "sha256:" + revision,
            "component_revisions": components}


def new_context(host):
    return lifecycle.RunContext(new_receipt(host), common, write_record, remove_sdk)


def _run(args: Any, host: Any, parent_fd: int | None, context=None) -> dict[str, Any]:
    context = context or new_context(host)
    receipt = context.receipt
    findings, gaps, reason, artifact = [], [], "native_prerequisite_failed", None
    known_terminal = False
    held_lock = context.locks
    started = time.monotonic()
    deadline = started + max(1, args.timeout_ms / 1000)
    try:
        if sys.platform != "darwin":
            raise Blocked("native_macos_not_available")
        release_run = getattr(args, "native_release_run", None)
        if (args.native_reconcile_result or release_run) and not args.native_recover_only:
            raise Blocked("native_reconciliation_requires_recovery_only")
        if args.native_reconcile_result and release_run:
            raise Blocked("native_reconciliation_release_conflict")
        worker, app, framework, product = worker_settings(args, host, requested_product_key(args, host))
        receipt["product"] = product.key
        webstorm = product.key == WEBSTORM_PRODUCT_KEY
        context.worker = worker
        config, plugins, home = launcher_paths(worker)
        node = node_executable(host) if webstorm and not args.native_recover_only else None
        if not args.native_recover_only:
            distribution = json.loads((app / "Contents/Resources/product-info.json").read_text())
            receipt.update(ide_version=distribution.get("version"), ide_build=distribution.get("buildNumber"))
        state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        def on_wait():
            check_lifetime(parent_fd, float("inf"))

        admission_started, wait_until = time.monotonic(), None
        if not args.native_recover_only:
            # Ordinary requests queue for the same locks; recovery never waits.
            held_lock.enter_context(host.fifo_turn(state_dir() / "native-queue", "Native inspection", on_wait))
            wait_until = time.monotonic() + max(0, getattr(args, "lifecycle_lock_timeout_ms", host.DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)) / 1000
            wait_for_host_capacity(wait_until, on_wait, receipt)
        held_lock.enter_context(profile_lock(worker, "native-control.lock", wait_until, on_wait))
        process_proof = stop_orphan(worker, receipt)
        global_lock = held_lock.enter_context(profile_lock(state_dir(), wait_until=wait_until, on_wait=on_wait))
        lock = held_lock.enter_context(profile_lock(worker, wait_until=wait_until, on_wait=on_wait))
        receipt["admission_wait_ms"] = round((time.monotonic() - admission_started) * 1000)
        # Waiting for admission does not consume the inspection deadline.
        deadline = time.monotonic() + max(1, args.timeout_ms / 1000)
        recover_run(worker, receipt, getattr(args, "native_reconcile_result", None), release_run, process_proof)
        if (worker / "native-activation-stop.json").exists():
            raise Blocked("native_activation_stop_recorded")
        if args.native_recover_only:
            raise Blocked("native_recovery_only_no_inspection")
        state, preparation_caches = runtime_storage(host, worker, product.key)
        repo, settings, scope, selected = common.scope_selection(args, host, receipt)
        if not any(repo.is_relative_to(Path(root)) for root in host.trusted_auto_open_roots()):
            raise Blocked("native_trusted_root_required")
        if selected and native_product_key(host, settings, selected) != product.key:
            raise Blocked("native_source_selection_changed")
        odoo = not webstorm and is_odoo(repo)
        if not odoo:
            framework = []
        elif not framework:
            raise Blocked("native_odoo_framework_required")
        elif not framework_available(framework):
            raise Blocked("native_odoo_framework_unavailable")
        receipt.update(scope=scope, selected_file_count=len(selected), repo_head_sha=host.git_head_sha(repo))
        receipt["framework_revisions"] = [host.git_head_sha(root) for root in framework]
        if odoo:
            receipt["odoo_context"] = framework_context(repo, selected, framework)
        if any(common.checked(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"]).strip() for root in framework):
            raise Blocked("native_framework_inputs_unproven")
        if not selected:
            raise Blocked("native_scope_empty")
        suffixes = proof.WEBSTORM_PROVEN_SUFFIXES if webstorm else NATIVE_SUFFIXES
        if any(Path(f).suffix not in suffixes for f in selected) or common.lane_gaps(host, settings, selected, product.key):
            raise Blocked("native_scope_unsupported")
        if args.max_files is not None and len(selected) > args.max_files:
            raise Blocked("native_scope_too_large")
        if args.problem_type != "all" or args.file_pattern != "all" or args.offset or args.include_stale or args.allow_text_only_coverage:
            raise Blocked("native_filter_unsupported")
        profile, expected = common.profile_for(repo, args.profile or settings.get("profile", ""))
        profile_requested = set(expected)
        if any(f.endswith(".py") for f in selected):
            expected |= NAMED_PYTHON
        if odoo:
            expected |= NAMED_ODOO
        if webstorm:
            if settings.get("prepare"):
                raise Blocked("native_preparation_shape_unsupported")
            common.npm_lockfile(repo)
            if any(Path(f).suffix in proof.SCRIPT_SUFFIXES for f in selected):
                expected |= set(proof.WEBSTORM_CANARY_FILES)
        receipt["profile_sha256"] = common.digest(profile)
        planned_private = state / ("private-" + receipt["run_id"])
        source_interpreter = repo / ".venv/bin/python"
        requested = None if webstorm else common.requested_python_version(settings)
        interpreter = node if webstorm else planned_private / "project/.venv/bin/python" if requested is not None else source_interpreter
        context.reserve(state, worker, config, interpreter)
        private = context.private
        sdk_name = context.identity["sdk_name"]
        for name in ("project", "system", "logs", "tmp", "results", "registry"):
            (private / name).mkdir(mode=0o700)
        project = private / "project"
        hashes = common.snapshot(repo, project)
        if not selected <= hashes.keys():
            raise Blocked("native_scope_snapshot_incomplete")
        if any((project / path).stat().st_size > common.MAX_ANALYZED_FILE_KB * 1024 for path in selected):
            raise Blocked("native_selected_file_over_analysis_limit")
        receipt["source_manifest_sha256"] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        controls = plant_controls(project, receipt["run_id"], odoo, product.key)
        def record_process(kind, pid):
            identity = process_identity(pid, private)
            if identity is None or identity["pgid"] != pid or identity["private"] != str(private):
                raise Blocked("native_process_identity_unproven")
            context.process_spawned(kind, identity)

        def record_exit():
            context.attempt("process_exit_record", context.process_finished)

        def prepare(preparation_argv, preparation_env):
            (private / "prepare-home").mkdir()
            execute(preparation_argv, preparation_env, private, deadline, parent_fd, receipt, lock_fd=lock.fileno(),
                    global_lock_fd=global_lock.fileno(), require_completion=False,
                    on_spawn=lambda pid: record_process("preparation", pid), on_exit=record_exit)

        check_lifetime(parent_fd, deadline)
        version = None
        if webstorm:
            prepare(*npm_preparation(node, project, private, preparation_caches))
            prepare_web_model(project, selected | controls)
        else:
            if requested is not None:
                prepare(common.preparation(settings, private), {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(private / "prepare-home"),
                                                                "TMPDIR": str(private / "tmp"), **preparation_caches})
                interpreter = project / ".venv/bin/python"
            elif source_interpreter.is_file():
                interpreter = source_interpreter
            else:
                raise Blocked("native_python_environment_required")
            version = common.checked([str(interpreter), "-c", "import sys; print('.'.join(map(str,sys.version_info[:3])))"]).decode().strip()
            if requested and not (version == requested or version.startswith(requested + ".")):
                raise Blocked("native_python_version_mismatch")
            receipt["python_version"] = version
            prepare_model(project, sdk_name, framework, selected | controls)
        shutil.copyfile(profile, private / "profile.xml")
        properties = {"config": config, "plugins": plugins, "system": private / "system", "log": private / "logs"}
        (private / "idea.properties").write_text("".join(f"idea.{name}.path={path}\n" for name, path in properties.items()) + "idea.initially.ask.config=never\n")
        # App defaults are public code; never read the activated worker's VM options.
        vm = (app / "Contents/bin" / product.vm_options_name).read_text().splitlines()
        vm = [line for line in vm if not line.startswith(("-Xmx", "-Xms", "-XX:HeapDump", "-XX:+HeapDump", "-XX:ErrorFile", "-Duser.home", "-Didea.", "-XX:ActiveProcessorCount"))]
        vm += ["-Xms256m", f"-Xmx{HEAP_LIMIT_MIB}m", f"-XX:ActiveProcessorCount={ACTIVE_PROCESSORS}", "-XX:-HeapDumpOnOutOfMemoryError", "-Djava.awt.headless=true", f"-Duser.home={home}", "-Didea.headless.enable.statistics=false", "-Didea.analyze.scope=JBNativeSelection", "-Didea.log.debug.categories=#com.intellij.codeInspection.ex.GlobalInspectionContextImpl", f"-Didea.max.intellisense.filesize={common.MAX_ANALYZED_FILE_KB}"]
        vm += [f"-Didea.{name}.path={path}" for name, path in properties.items()]
        vm += [f"-Didea.diagnostic.opentelemetry.file={private}/inspection-trace.json",
               "-Didea.diagnostic.opentelemetry.detailed.scopes=inspection"]
        vm += [f"-XX:ErrorFile={private}/logs/java-error-%p.log", f"-XX:HeapDumpPath={private}/logs"]
        (private / "native.vmoptions").write_text("\n".join(vm) + "\n")
        engine_path = os.environ.get("PATH", "/usr/bin:/bin")
        env = {"PATH": str(node.parent) + os.pathsep + engine_path if node else engine_path, "HOME": str(home), "TMPDIR": str(private / "tmp"),
               f"{product.env_var_base_name}_PROPERTIES": str(private / "idea.properties"),
               f"{product.env_var_base_name}_VM_OPTIONS": str(private / "native.vmoptions"),
               "JETBRAINS_INSPECTION_REGISTRY_DIR": str(private / "registry")}
        argv = ["/usr/bin/nice", "-n", str(NICE), str(app / "Contents/bin/inspect.sh"), str(project), str(private / "profile.xml"), str(private / "results"), "-v2"]
        if not webstorm:
            register_sdk(config, interpreter, version, sdk_name,
                         on_attempt=lambda: context.sdk_attempt(config, interpreter, sdk_name))
        context.phase("engine_running")
        execute(argv, env, private, deadline, parent_fd, receipt, lock_fd=lock.fileno(), global_lock_fd=global_lock.fileno(),
                on_spawn=lambda pid: record_process("engine", pid), on_exit=record_exit)
        all_findings, registered, languages = read_results(private / "results", project)
        findings = [finding for finding in all_findings if finding["file"] in selected]
        trace_tools, trace_failed = proof.read_trace(private / "inspection-trace.json", project)
        control_root = next(iter(controls)).split("/", 1)[0]
        product_proof = ({"proven_suffixes": proof.WEBSTORM_PROVEN_SUFFIXES, "canary_files": proof.WEBSTORM_CANARY_FILES,
                          "clean_twins": proof.WEBSTORM_CLEAN_TWINS} if webstorm else {})
        receipt["coverage"], gaps = proof.assess(expected, registered, trace_tools, trace_failed, selected,
                                                all_findings, control_root=control_root, tool_languages=languages, scope=scope,
                                                **product_proof)
        receipt["coverage"]["profile_requested"] = sorted(profile_requested)
        allowed = selected | controls
        if set(receipt.get("inspected_files", [])) - allowed:
            gaps.append("native_unselected_file_traversal")
        if not selected <= set(receipt.get("inspected_files", [])):
            gaps.append("native_selected_file_traversal_unproven")
        receipt["coverage"]["scope_failures"] = [gap for gap in gaps if gap.startswith("native_")]
        receipt["coverage"]["complete"] = not gaps
        current_paths = common.checked(["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).decode().split("\0")
        if {path for path in current_paths if path and (repo / path).exists()} != set(hashes):
            raise Blocked("native_source_inputs_changed")
        if any((repo / f).is_symlink() or not (repo / f).resolve().is_relative_to(repo) or not (repo / f).is_file() or common.digest(repo / f) != digest for f, digest in hashes.items()):
            raise Blocked("native_source_inputs_changed")
        if any(not (project / f).is_file() or common.digest(project / f) != digest for f, digest in hashes.items() if not f.startswith(".idea/") and not f.endswith(".iml")):
            raise Blocked("native_snapshot_inputs_changed")
        if common.scope_selection(args, host)[3] != selected:
            raise Blocked("native_source_selection_changed")
        if common.digest(profile) != receipt["profile_sha256"] or common.digest(private / "profile.xml") != receipt["profile_sha256"]:
            raise Blocked("native_profile_inputs_changed")
        if [host.git_head_sha(root) for root in framework] != receipt["framework_revisions"]:
            raise Blocked("native_framework_inputs_changed")
        if odoo and framework_context(repo, selected, framework) != receipt["odoo_context"]:
            raise Blocked("native_odoo_context_changed")
        if any(common.checked(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"]).strip() for root in framework):
            raise Blocked("native_framework_inputs_changed")
        if args.severity != "all":
            findings = [finding for finding in findings if finding["severity"].lower() == args.severity.lower()]
        attribute_to_change(receipt, findings, repo, scope, selected)
        reason = "native_completed" if not gaps else "native_coverage_unproven"
        known_terminal = True
    except Blocked as error:
        reason = str(error)
        known_terminal = True
    except host.InspectError as error:
        reason = "native_scope_too_large" if error.payload.get("error_reason") == "inspection_lane_scope_too_large" else "native_prerequisite_or_report_failed"
        known_terminal = True
    except (OSError, ValueError, ElementTree.ParseError, subprocess.SubprocessError):
        reason = "native_prerequisite_or_report_failed"
        known_terminal = True
    finally:
        if receipt.get("license_boundary_observed"):
            reason, known_terminal = "native_owner_activation_required", True
        elif "worker_pid" in receipt and not receipt.get("license_observation_complete", False):
            reason, known_terminal = "native_license_observation_unproven", False
        reason, artifact = context.finish(findings, reason, known_terminal=known_terminal)
        receipt["wall_seconds"] = round(time.monotonic() - started, 3)
    return envelope(host, receipt, findings, reason, gaps, artifact)


def run(args, host):
    context = new_context(host)
    # A child owns the complete lifecycle and keeps the profile lock after the
    # invoking harness dies. The pipe signals parent death even during a pause.
    if not hasattr(os, "fork"):
        return envelope(host, context.receipt, [], "native_platform_unsupported", [])
    lifetime_read, lifetime_write = os.pipe()
    result_read, result_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.setsid()
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        os.close(lifetime_write)
        os.close(result_read)
        def guardian_signal(_signum, _frame):
            # A caught handler resets to default across exec; SIG_IGN would
            # incorrectly propagate immunity into the inspected process.
            pass
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, guardian_signal)
        try:
            try:
                result = _run(args, host, lifetime_read, context)
            except Exception:
                fallback = "native_owner_activation_required" if context.receipt.get("license_boundary_observed") else "native_internal_error"
                reason, _artifact = context.finish([], fallback, known_terminal=False)
                result = envelope(host, context.receipt, [], reason, [])
            data = json.dumps(result).encode()
            while data:
                data = data[os.write(result_write, data):]
        except (BrokenPipeError, OSError):
            pass
        finally:
            os._exit(0)
    os.close(lifetime_read)
    os.close(result_write)
    try:
        with os.fdopen(result_read, "rb") as stream:
            data = stream.read()
            if not data:
                context.receipt.update(private_deleted=False, sdk_removed=False)
                return envelope(host, context.receipt, [], "native_guardian_exited_without_result", [])
            return json.loads(data)
    finally:
        os.close(lifetime_write)
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            # The invoker may deliberately auto-reap children. The guardian
            # resets its own SIGCHLD, and its result pipe remains authoritative.
            pass
