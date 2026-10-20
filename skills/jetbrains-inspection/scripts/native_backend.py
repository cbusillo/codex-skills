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
import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

_spec = importlib.util.spec_from_file_location("native_common", Path(__file__).with_name("qodana_backend.py"))
if _spec is None or _spec.loader is None:
    raise ImportError("Native shared backend unavailable")
common: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)
Blocked = common.Blocked
LOAD_LIMIT = common.LOAD_LIMIT
HEAP_LIMIT_MIB = 1536
ACTIVE_PROCESSORS = 2
NICE = 10
NAMED_PYTHON = frozenset({"DuplicatedCode", "HttpUrlsUsage", "PyUnresolvedReferencesInspection", "PyTypeCheckerInspection"})
NAMED_ODOO = frozenset({"OdooPy", "OdooXml"})
LICENSE_PROMPT = re.compile(r"^(?:no valid licen[cs]e\b|checking trial availability\b|(?:start|activate|try|begin|offer) (?:a |your |free )?trial\b|(?:your |the |a )?(?:trial|evaluation) (?:is |has )?(?:expired|period|available|active|licen[cs]e|expires|ends)\b|you are using (?:a )?trial\b|licen[cs]e.{0,40}(?:expired|invalid|missing|required)|(?:activate|purchase|buy|log.?in).{0,40}licen[cs]e|third.party privacy notice.*not accepted)", re.I)


def license_boundary(line):
    # Native's brief stops even on a trial offer or prompt, before activation.
    return any(LICENSE_PROMPT.search(event.rsplit(" - ", 1)[-1].strip()) for event in line.splitlines())


def is_odoo(repo):
    paths = common.checked(["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*__manifest__.py"])
    fixture_roots = {"tests", "fixtures", "test-fixtures", "testdata", "assets"}
    return any(not fixture_roots.intersection(Path(path).parts) for path in paths.decode().split("\0") if path)


def select_backend(args, host, host_default=None):
    if args.qodana_recover_only or args.qodana_image or args.qodana_license_file or args.qodana_plugin_zip or args.qodana_uv:
        return "qodana", "qodana_options_supplied"
    if args.native_worker or args.native_recover_only or args.native_framework_root:
        return "native", "native_options_supplied"
    if host_default:
        return host_default, "host_default"
    if sys.platform != "darwin":
        return common.select_backend(args, host)
    try:
        repo, config, _scope, selected = common.scope_selection(args, host)
        if not eligible(args, host, repo, config):
            return common.select_backend(args, host)
        if selected and is_odoo(repo):
            return "native", "odoo_native_route"
        if selected and not common.lane_gaps(host, config, selected):
            if all(f.endswith(".py") for f in selected):
                return "native", "python_native_route"
    except (Blocked, host.InspectError, OSError, subprocess.SubprocessError, ValueError):
        pass
    return common.select_backend(args, host)


def eligible(args, host, repo, settings):
    """Public configuration checks only; never inspect activation or start an IDE."""
    try:
        worker, _app, roots = worker_settings(args, host)
        launcher_paths(worker)
        if is_odoo(repo) and (not roots or not any((root / "odoo/release.py").is_file() for root in roots) or any(not root.is_dir() for root in roots)):
            return False
        if not (repo / ".venv/bin/python").is_file() and not settings.get("prepare"):
            return False
        common.profile_for(repo, args.profile or settings.get("profile", ""))
        return True
    except (Blocked, host.InspectError, OSError, ValueError):
        return False


@contextlib.contextmanager
def profile_lock(worker):
    import fcntl
    path = worker / "worker.lock"
    if not worker.is_dir() or worker.is_symlink():
        raise Blocked("native_worker_missing")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Blocked("native_worker_lock_unsafe") from None
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("native_worker_busy") from None
        yield lock


def coverage(requested: set[str], registered: set[str], executed: set[str]) -> dict[str, Any]:
    missing = sorted(set(requested) - set(registered))
    unproven = sorted(set(requested) - set(executed))
    return {"requested": sorted(requested), "registered": sorted(set(requested) & set(registered)),
            "execution_proved": sorted(set(requested) & set(executed)),
            "unavailable": missing, "unproven": unproven, "complete": not missing and not unproven}


def applicable_rules(requested, selected):
    python = any(path.endswith(".py") for path in selected)
    xml = any(path.endswith(".xml") for path in selected)
    return {rule for rule in requested if (python or not rule.startswith("Py"))
            and (not common.OTHER_LANGUAGE_RULE_RE.match(rule) or xml and rule.startswith("Xml"))}


def applicable_files(tool, selected):
    if tool.startswith("Py") or tool == "OdooPy":
        return {path for path in selected if path.endswith(".py")}
    if tool.startswith("Xml") or tool == "OdooXml":
        return {path for path in selected if path.endswith(".xml")}
    return set(selected)


def worker_settings(args: Any, host: Any) -> tuple[Path, Path, list[Path]]:
    global_config = host.read_global_config()
    jetbrains = global_config.get("jetbrains", {})
    configured = jetbrains.get("nativeWorker", {}) if isinstance(jetbrains, dict) else None
    if not isinstance(configured, dict):
        raise Blocked("native_configuration_invalid")
    raw = args.native_worker or os.environ.get("JETBRAINS_INSPECTION_NATIVE_WORKER") or configured.get("path")
    if not isinstance(raw, str) or not raw:
        raise Blocked("native_worker_not_configured")
    worker = Path(raw).expanduser()
    if not worker.is_absolute() or worker.is_symlink() or worker.resolve() != worker:
        raise Blocked("native_worker_path_unsafe")
    app = args.ide_app or configured.get("app")
    if args.native_recover_only:
        return worker, Path(app).expanduser().resolve() if isinstance(app, str) else worker, []
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
    product = json.loads((app / "Contents/Resources/product-info.json").read_text())
    if product.get("productCode") not in {"PY", "PC"} or product.get("envVarBaseName") != "PYCHARM":
        raise Blocked("native_pycharm_distribution_required")
    roots = args.native_framework_root or configured.get("frameworkRoots", [])
    roots = [Path(p).expanduser().resolve() for p in roots]
    return worker, app, roots


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
    # Qodana and native must exclude each other, not just their own backend.
    return common.worker_lock_path().parent


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
        raise Blocked("native_recovery_sdk_table_malformed") from None


def recover_run(worker):
    """Recover only a journaled private directory and its exact SDK entry."""
    journal = worker / "native-run.json"
    if not journal.exists():
        return
    if journal.is_symlink():
        raise Blocked("native_recovery_ownership_unproven")
    try:
        record = json.loads(journal.read_text())
        run_id = str(uuid.UUID(record["run_id"]))
        required = {"private", "sdk_name", "interpreter", "table_existed", "storage_anchor", "storage_identity"}
        if not required <= record.keys():
            raise ValueError("missing journal fields")
    except (ValueError, KeyError, TypeError):
        raise Blocked("native_recovery_journal_malformed") from None
    private = Path(record["private"])
    anchor = Path(record["storage_anchor"])
    if record["sdk_name"] != "jb-native-" + run_id or private.is_symlink() or not private.is_absolute() or private.resolve() != private or not private.name.startswith("private-") or not private.is_relative_to(anchor):
        raise Blocked("native_recovery_ownership_unproven")
    if common.storage_identity(Path(record["storage_anchor"])) != record["storage_identity"]:
        raise Blocked("native_recovery_storage_changed")
    if private.exists():
        marker = private / "owner.json"
        if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != record:
            raise Blocked("native_recovery_ownership_unproven")
    if record["interpreter"] is not None:
        remove_sdk(worker / "config", Path(record["interpreter"]), record["sdk_name"], record["table_existed"], missing_ok=True)
    if private.exists():
        # Marker remains until every other owned file was deleted.
        common.delete_private(private)
    journal.unlink()


def execute(argv, env, private: Path, deadline: float, parent_fd: int | None, receipt: dict[str, Any], *, lock_fd: int, global_lock_fd: int | None = None, require_completion=True):
    """One process group; continuously guard license events and parent lifetime."""
    process = None
    stdout = None
    paused = False
    log_offsets = {}
    log_tails = {}
    pending = b""
    selector = selectors.DefaultSelector()
    completion = False
    try:
        process = subprocess.Popen(argv, cwd=private, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True,
                                   pass_fds=(lock_fd,) if global_lock_fd is None else (lock_fd, global_lock_fd))
        receipt["worker_pid"] = process.pid
        stdout = process.stdout
        if stdout is None:
            raise Blocked("native_output_pipe_unavailable")
        os.set_blocking(stdout.fileno(), False)
        selector.register(stdout, selectors.EVENT_READ)

        def observe(data):
            nonlocal completion
            if require_completion and license_boundary(data):
                receipt["license_boundary_observed"] = True
                raise Blocked("native_owner_activation_required")
            plugin = re.search(r"Odoo IDE \(([0-9][\w.+-]*)\)", data)
            if plugin:
                receipt["odoo_plugin_version"] = plugin.group(1)
            if re.search(r"^Done\.?\s*$", data, re.M):
                completion = True
            if re.search(r"Using default project profile|Cannot load project|No files to inspect|No modules", data, re.I):
                raise Blocked("native_project_or_profile_unprepared")
            if re.search(r"^ERROR:|\sERROR\s+-", data, re.M):
                receipt["engine_error_observed"] = True
                raise Blocked("native_engine_error")
            if require_completion and re.search(r"Cancelling inspection progress|\b(?:ProcessCanceledException|CancellationException)\b", data):
                raise Blocked("native_execution_cancelled")
            for path in re.findall(r"Running local inspections on ([^\r\n]+)", data):
                source = Path(path.strip())
                project = private / "project"
                if source.is_absolute() and source.is_relative_to(project):
                    relative = source.relative_to(project).as_posix()
                    if relative not in receipt.setdefault("inspected_files", []):
                        receipt["inspected_files"].append(relative)

        def read_logs():
            for path in (private / "logs").glob("*.log"):
                if path.is_symlink():
                    raise Blocked("native_log_path_unsafe")
                with path.open("rb") as stream:
                    stream.seek(log_offsets.get(path, 0))
                    data = stream.read(1024 * 1024)
                    log_offsets[path] = stream.tell()
                combined = log_tails.get(path, b"") + data
                observe(combined.decode(errors="replace"))
                log_tails[path] = combined[-4096:]

        while True:
            check_lifetime(parent_fd, deadline)
            for _key, _event in selector.select(0.1):
                chunk = os.read(stdout.fileno(), 65536)
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()[-65536:]
                observe(b"\n".join(lines).decode(errors="replace"))
                if not chunk:
                    selector.unregister(stdout)
            observe(pending.decode(errors="replace"))
            read_logs()
            if process.poll() is not None:
                observe(pending.decode(errors="replace"))
                read_logs()
                break
            load = os.getloadavg()[0]
            if load > LOAD_LIMIT and not paused:
                os.killpg(process.pid, signal.SIGSTOP)
                paused = True
                receipt["load_pause_count"] += 1
            elif load <= LOAD_LIMIT and paused:
                os.killpg(process.pid, signal.SIGCONT)
                paused = False
        receipt["engine_exit_code"] = process.returncode
        receipt["completion_observed"] = completion
        if process.returncode != 0 or require_completion and not completion:
            raise Blocked("native_execution_incomplete")
    finally:
        selector.close()
        if process is not None:
            # Also kill descendants after a shell/launcher exits unexpectedly.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if stdout is not None:
                stdout.close()
            receipt["worker_exited"] = True


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
            for excluded in (".venv", ".git"):
                ElementTree.SubElement(content, "excludeFolder", url=(root / excluded).as_uri())
    ElementTree.SubElement(manager, "orderEntry", type="jdk", jdkName=sdk_name, jdkType="Python SDK")
    ElementTree.SubElement(manager, "orderEntry", type="sourceFolder", forTests="false")
    ElementTree.ElementTree(module).write(idea / "Native.iml", encoding="unicode")
    modules = ElementTree.Element("project", version="4")
    component = ElementTree.SubElement(modules, "component", name="ProjectModuleManager")
    ElementTree.SubElement(ElementTree.SubElement(component, "modules"), "module", fileurl=(idea / "Native.iml").as_uri(), filepath=str(idea / "Native.iml"))
    ElementTree.ElementTree(modules).write(idea / "modules.xml", encoding="unicode")
    misc = ElementTree.Element("project", version="4")
    ElementTree.SubElement(misc, "component", name="ProjectRootManager", version="2", attrib={"project-jdk-name": sdk_name, "project-jdk-type": "Python SDK"})
    ElementTree.ElementTree(misc).write(idea / "misc.xml", encoding="unicode")
    pyproject_model = ElementTree.Element("project", version="4")
    component = ElementTree.SubElement(pyproject_model, "component", name="PyProjectModelSettings")
    ElementTree.SubElement(component, "option", name="usePyprojectToml", value="false")
    ElementTree.ElementTree(pyproject_model).write(idea / "pyProjectModel.xml", encoding="unicode")
    scope = ElementTree.Element("component", name="DependencyValidationManager")
    if any(re.search(r'[\s"!&|()*?:\[\]\\]', path) for path in selected):
        raise Blocked("native_scope_path_unsupported")
    ElementTree.SubElement(scope, "scope", name="JBNativeSelection", pattern="||".join("file:" + path for path in sorted(selected)))
    (idea / "scopes").mkdir(exist_ok=True)
    ElementTree.ElementTree(scope).write(idea / "scopes/JBNativeSelection.xml", encoding="unicode")


def plant_controls(project, run_id, odoo):
    fixtures = Path(__file__).parents[1] / "assets/native-controls"
    prefix = "jb_native_controls_" + run_id.replace("-", "")
    destination = project / prefix
    destination.mkdir()
    names = ["coverage.py", "duplicates.py", "python_types.py"]
    if odoo:
        names += ["__init__.py", "__manifest__.py", "models.py", "views/defect.xml"]
    for name in names:
        target = destination / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(fixtures / name, target)
    return {(Path(prefix) / name).as_posix() for name in names}


@contextlib.contextmanager
def registered_sdk(config: Path, interpreter: Path, version: str, sdk_name: str):
    """Add/remove only this run's non-secret SDK entry, in the same profile."""
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
    atomic_write(table, ElementTree.tostring(tree.getroot(), encoding="utf-8"))
    try:
        yield
    finally:
        remove_sdk(config, interpreter, sdk_name, existed)


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


def read_results(results: Path, project: Path, selected: set[str]) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    findings: list[dict[str, Any]] = []
    executed: set[str] = set()
    registered: set[str] = set()
    descriptions = results / ".descriptions.xml"
    if descriptions.is_file():
        root = ElementTree.parse(descriptions).getroot()
        registered = {name for node in root.iter("inspection") if isinstance(name := node.get("shortName"), str) and name}
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
            executed.add(rule)
            if relative not in selected:
                continue
            findings.append({"file": relative, "line": line, "rule_id": rule,
                             "severity": problem_class.get("severity", "WARNING") if problem_class is not None else "WARNING",
                             "message": common.safe_text(problem.findtext("description", ""))})
    return findings, registered, executed


UNUSED_SPLIT = frozenset({'PyUnusedLocalVariableInspection', 'PyUnusedParameterInspection', 'PyUnusedFunctionInspection'})


def migrate_expected(requested: set[str], registered: set[str]) -> tuple[set[str], dict[str, list[str]]]:
    expected = set(requested)
    migrations = {}
    if 'PyUnusedLocalInspection' in expected and 'PyUnusedLocalInspection' not in registered and UNUSED_SPLIT <= set(registered):
        expected.remove('PyUnusedLocalInspection')
        expected.update(UNUSED_SPLIT)
        migrations['PyUnusedLocalInspection'] = sorted(UNUSED_SPLIT)
    return expected, migrations


def trace_evidence(payload: Any, project: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """Return successful physical tool/file pairs; errors cannot borrow success."""
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        raise ValueError('native_trace_invalid')
    project = Path(project)
    success: dict[str, set[str]] = {}
    failed: set[tuple[str, str]] = set()
    for batch in payload['data']:
        if not isinstance(batch, dict) or not isinstance(batch.get('spans'), list):
            raise ValueError('native_trace_invalid')
        for span in batch['spans']:
            if not isinstance(span, dict):
                raise ValueError('native_trace_invalid')
            if span.get('operationName') != 'inspectionRun':
                continue
            tags = {}
            for tag in span.get('tags', []):
                if not isinstance(tag, dict) or not isinstance(tag.get('key'), str) or tag['key'] in tags:
                    raise ValueError('native_trace_invalid')
                tags[tag['key']] = tag.get('value')
            tool, raw = tags.get('inspectionId'), tags.get('file')
            if not isinstance(tool, str) or not tool:
                raise ValueError('native_trace_invalid')
            # Synthetic PSI copies such as /factories.py do not prove physical
            # source execution. Global spans without a file cannot satisfy local
            # or scoped tool/file requirements either.
            if not isinstance(raw, str):
                continue
            source = Path(raw)
            if not source.is_absolute() or '..' in source.parts or not source.is_relative_to(project):
                continue
            relative = source.relative_to(project).as_posix()
            key = tool, relative
            if tags.get('otel.status_code') == 'ERROR' or str(tags.get('error')).lower() == 'true':
                failed.add(key)
                continue
            duration = span.get('durationNano', span.get('duration'))
            if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
                raise ValueError('native_trace_invalid')
            success.setdefault(tool, set()).add(relative)
    for tool, relative in failed:
        success.get(tool, set()).discard(relative)
    return success, failed


def read_trace(path: Path, project: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    path = Path(path)
    if path.is_symlink():
        raise Blocked('native_trace_unsafe')
    return trace_evidence(json.loads(path.read_text()), project)


def envelope(host: Any, receipt: dict[str, Any], findings: list[dict[str, Any]], reason: str, gaps: list[str], artifact: str | None = None) -> dict[str, Any]:
    clean = reason == "native_completed" and not gaps
    verdict = "GREEN" if clean and not findings else "RED" if clean else "UNKNOWN"
    action = "Fix current findings." if verdict == "RED" else "None." if verdict == "GREEN" else "Resolve the reported native prerequisite or execution-coverage gap before a fresh assessment."
    if reason in {"native_owner_activation_required", "native_activation_stop_recorded"}:
        action = "Owner question: the Director must restore JBTEST-D1 activation through the product UI using existing entitlements. Stop licensed runs; never start a trial or copy credentials."
    elif reason in {"native_worker_not_configured", "native_app_not_configured"}:
        action = "Configure jetbrains.nativeWorker.path/app in the shared helper config for the independently Director-activated worker, or supply --native-worker and --ide-app. See references/native-backend.md."
    elif reason == "native_trusted_root_required":
        action = "Use the intended repository under jetbrains.trustedAutoOpenRoots in the standard helper config; native preparation executes only for a trusted source root."
    elif reason == "native_python_environment_required":
        action = "Run the repository's documented Python setup in the exact worktree; native needs its .venv or declared structured prepare.python."
    elif reason == "native_scope_unsupported":
        action = "Select .py and Odoo .xml files assigned to the root PyCharm lane; other language or nested-project models require their existing qualified backend."
    elif reason.startswith("native_recovery_") and reason != "native_recovery_only_no_inspection":
        action = "Restore the journaled storage and use --backend native --native-recover-only with the same independent worker. Preserve unproved state."
    elif reason == "native_worker_busy":
        action = "Let the current isolated worker's owner finish; use the same profile and lock after it exits."
    report = f"{verdict}: native PyCharm; {reason}; {len(findings)} findings; {len(receipt.get('coverage', {}).get('unproven', []))} inspections unproven."
    return {"schema_version": host.AGENT_RESULT_SCHEMA_VERSION, "command": "agent-inspect", "backend": "native",
            "status": {"GREEN": "clean", "RED": "findings"}.get(verdict, "error"),
            "agent_result": {"verdict": verdict, "bucket": {"GREEN": "clean", "RED": "findings"}.get(verdict, "environment_blocked"),
                             "retry_policy": {"retry": False, "max_attempts": 0, "wait_ms": 0}, "terminal": True,
                             "next_action": action, "agent_report": report},
            "scope": receipt.get("scope"), "selected_file_count": receipt.get("selected_file_count"),
            "finding_count": len(findings), "findings": findings[:common.FINDING_LIMIT], "findings_artifact": artifact,
            "findings_truncated": len(findings) > common.FINDING_LIMIT, "proof_failures": gaps,
            "coverage": receipt.get("coverage", {}), "uncovered_inspections": receipt.get("coverage", {}).get("unproven", []),
            "inspection_outcome": {"verdict": verdict, "reason": reason},
            "cleanup": {"status": "closed" if receipt.get("private_deleted") and receipt.get("sdk_removed") and receipt.get("worker_exited", True) else "failed"},
            "identity": {"helper_revision": receipt.get("helper_revision"), "native_backend_revision": receipt.get("backend_revision"), "inspection_run_id": receipt["run_id"]},
            "diagnostic": {"error_reason": reason, "native": receipt}}


def _run(args: Any, host: Any, parent_fd: int | None) -> dict[str, Any]:
    receipt: dict[str, Any] = {"run_id": str(uuid.uuid4()), "private_deleted": True, "sdk_removed": True, "load_pause_count": 0,
               "nice": NICE, "active_processors": ACTIVE_PROCESSORS, "heap_limit_mib": HEAP_LIMIT_MIB,
               "helper_revision": host.helper_revision(), "backend_revision": "sha256:" + common.digest(Path(__file__))}
    findings, gaps, reason, artifact = [], [], "native_prerequisite_failed", None
    private: Path | None = None
    journal: Path | None = None
    worker: Path | None = None
    held_lock = contextlib.ExitStack()
    started = time.monotonic()
    deadline = started + max(1, args.timeout_ms / 1000)
    try:
        if sys.platform != "darwin":
            raise Blocked("native_macos_not_available")
        worker, app, framework = worker_settings(args, host)
        if not args.native_recover_only:
            product = json.loads((app / "Contents/Resources/product-info.json").read_text())
            receipt.update(ide_version=product.get("version"), ide_build=product.get("buildNumber"))
        state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        global_lock = held_lock.enter_context(profile_lock(state_dir()))
        lock = held_lock.enter_context(profile_lock(worker))
        config, plugins, home = launcher_paths(worker)
        recover_run(worker)
        if args.native_recover_only:
            raise Blocked("native_recovery_only_no_inspection")
        state = host.cache_dir() / "native"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        stop = worker / "native-activation-stop.json"
        if stop.exists():
            raise Blocked("native_activation_stop_recorded")
        if os.getloadavg()[0] > LOAD_LIMIT:
            raise Blocked("native_host_load_paused")
        repo, settings, scope, selected = common.scope_selection(args, host)
        if not any(repo.is_relative_to(Path(root)) for root in host.trusted_auto_open_roots()):
            raise Blocked("native_trusted_root_required")
        if not is_odoo(repo):
            framework = []
        elif framework and (any(not root.is_dir() for root in framework) or not any((root / "odoo/release.py").is_file() for root in framework)):
            raise Blocked("native_odoo_framework_unavailable")
        receipt.update(scope=scope, selected_file_count=len(selected), repo_head_sha=host.git_head_sha(repo))
        receipt["framework_revisions"] = [host.git_head_sha(root) for root in framework]
        if any(common.checked(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"]).strip() for root in framework):
            raise Blocked("native_framework_inputs_unproven")
        if not selected:
            raise Blocked("native_scope_empty")
        if any(Path(f).suffix not in {".py", ".xml"} for f in selected) or common.lane_gaps(host, settings, selected):
            raise Blocked("native_scope_unsupported")
        if args.max_files is not None and len(selected) > args.max_files:
            raise Blocked("native_scope_too_large")
        if args.problem_type != "all" or args.file_pattern != "all" or args.offset or args.include_stale or args.allow_text_only_coverage:
            raise Blocked("native_filter_unsupported")
        profile, expected = common.profile_for(repo, args.profile or settings.get("profile", ""))
        profile_requested = set(expected)
        expected = applicable_rules(expected, selected)
        if any(f.endswith(".py") for f in selected):
            expected |= NAMED_PYTHON
        if is_odoo(repo):
            expected |= NAMED_ODOO
        restricted = {name for tool in ElementTree.parse(profile).iter("inspection_tool")
                      if isinstance(name := tool.get("class"), str) and name in expected and any(scope.get("enabled") == "false" for scope in tool.findall("scope"))}
        receipt["profile_sha256"] = common.digest(profile)
        if is_odoo(repo) and not framework:
            raise Blocked("native_odoo_framework_required")
        private = state / ("private-" + receipt["run_id"])
        sdk_name = "jb-native-" + receipt["run_id"]
        source_interpreter = repo / ".venv/bin/python"
        interpreter = private / "project/.venv/bin/python" if settings.get("prepare") else source_interpreter
        anchor = common.storage_anchor(state)
        record = {"run_id": receipt["run_id"], "private": str(private), "sdk_name": sdk_name,
                  "interpreter": str(interpreter), "table_existed": (config / "options/jdk.table.xml").exists(),
                  "storage_anchor": str(anchor), "storage_identity": common.storage_identity(anchor)}
        journal = worker / "native-run.json"
        # Custody precedes source snapshotting or environment preparation.
        write_record(journal, record)
        private.mkdir(mode=0o700)
        write_record(private / "owner.json", record)
        receipt["private_deleted"] = False
        for name in ("project", "system", "logs", "tmp", "results", "registry"):
            (private / name).mkdir(mode=0o700)
        project = private / "project"
        hashes = common.snapshot(repo, project)
        if not selected <= hashes.keys():
            raise Blocked("native_scope_snapshot_incomplete")
        if any((project / path).stat().st_size > common.MAX_ANALYZED_FILE_KB * 1024 for path in selected):
            raise Blocked("native_selected_file_over_analysis_limit")
        receipt["source_manifest_sha256"] = common.hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        controls = plant_controls(project, receipt["run_id"], is_odoo(repo))
        check_lifetime(parent_fd, deadline)
        # Use the declared version, or the exact worktree's prepared venv.
        requested = common.requested_python_version(settings)
        if settings.get("prepare"):
            prepared = common.preparation(settings, private, "python", repo, uv_supplied=True)
            preparation_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(private / "prepare-home"), "TMPDIR": str(private / "tmp")}
            (private / "prepare-home").mkdir()
            for command in prepared["commands"]:
                argv = [arg.replace(common.WORK + "/", str(private) + "/") for arg in command]
                execute(argv, preparation_env, private, deadline, parent_fd, receipt, lock_fd=lock.fileno(), global_lock_fd=global_lock.fileno(), require_completion=False)
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
        vm = (app / "Contents/bin/pycharm.vmoptions").read_text().splitlines()
        vm = [line for line in vm if not line.startswith(("-Xmx", "-Xms", "-XX:HeapDump", "-XX:+HeapDump", "-XX:ErrorFile", "-Duser.home", "-Didea.", "-XX:ActiveProcessorCount"))]
        vm += ["-Xms256m", f"-Xmx{HEAP_LIMIT_MIB}m", f"-XX:ActiveProcessorCount={ACTIVE_PROCESSORS}", "-XX:-HeapDumpOnOutOfMemoryError", "-Djava.awt.headless=true", f"-Duser.home={home}", "-Didea.headless.enable.statistics=false", "-Didea.analyze.scope=JBNativeSelection", "-Didea.log.debug.categories=#com.intellij.codeInspection.ex.GlobalInspectionContextImpl", f"-Didea.max.intellisense.filesize={common.MAX_ANALYZED_FILE_KB}"]
        vm += [f"-Didea.{name}.path={path}" for name, path in properties.items()]
        vm += [f"-Didea.diagnostic.opentelemetry.file={private}/inspection-trace.json",
               "-Didea.diagnostic.opentelemetry.detailed.scopes=inspection"]
        vm += [f"-XX:ErrorFile={private}/logs/java-error-%p.log", f"-XX:HeapDumpPath={private}/logs"]
        (private / "native.vmoptions").write_text("\n".join(vm) + "\n")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(private / "tmp"),
               "PYCHARM_PROPERTIES": str(private / "idea.properties"), "PYCHARM_VM_OPTIONS": str(private / "native.vmoptions"),
               "JETBRAINS_INSPECTION_REGISTRY_DIR": str(private / "registry")}
        argv = ["/usr/bin/nice", "-n", str(NICE), str(app / "Contents/bin/inspect.sh"), str(project), str(private / "profile.xml"), str(private / "results"), "-v2"]
        receipt["sdk_removed"] = False
        try:
            with registered_sdk(config, interpreter, version, sdk_name):
                execute(argv, env, private, deadline, parent_fd, receipt, lock_fd=lock.fileno(), global_lock_fd=global_lock.fileno())
            receipt["sdk_removed"] = True
        except Blocked as error:
            # registered_sdk completed its finally before propagating a run failure.
            # A cleanup-specific failure stays explicitly unresolved.
            if str(error) not in {"native_sdk_cleanup_identity_changed", "native_sdk_cleanup_unproven", "native_recovery_sdk_table_malformed"}:
                receipt["sdk_removed"] = True
            raise
        findings, registered, executed = read_results(private / "results", project, selected)
        trace_tools, trace_failed = read_trace(private / "inspection-trace.json", project)
        allowed = selected | controls
        executed.update(tool for tool, paths in trace_tools.items() if paths & allowed)
        effective, migrations = migrate_expected(expected, registered)
        receipt["coverage"] = coverage(effective, registered, executed)
        receipt["coverage"]["vendor_migrations"] = migrations
        receipt["coverage"]["physical_execution"] = {tool: sorted(paths & allowed) for tool, paths in trace_tools.items() if tool in effective and paths & allowed}
        receipt["coverage"]["profile_requested"] = sorted(profile_requested)
        receipt["coverage"]["not_applicable"] = sorted(profile_requested - expected)
        gaps = ["execution_unproven:" + name for name in receipt["coverage"]["unproven"]]
        gaps += ["inspection_execution_failed:" + tool + ":" + path for tool, path in sorted(trace_failed) if path in allowed]
        if set(receipt.get("inspected_files", [])) - allowed:
            gaps.append("native_unselected_file_traversal")
        restricted, _ = migrate_expected(restricted, registered)
        gaps += ["scoped_profile_execution_unproven:" + name for name in sorted(restricted)
                 if not applicable_files(name, selected) <= trace_tools.get(name, set())]
        if not selected <= set(receipt.get("inspected_files", [])):
            gaps.append("native_selected_file_traversal_unproven")
        receipt["coverage"]["scope_failures"] = [gap for gap in gaps if not gap.startswith("execution_unproven:")]
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
        if any(common.checked(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"]).strip() for root in framework):
            raise Blocked("native_framework_inputs_changed")
        if args.severity != "all":
            findings = [finding for finding in findings if finding["severity"].lower() == args.severity.lower()]
        reason = "native_completed" if not gaps else "native_coverage_unproven"
    except Blocked as error:
        reason = str(error).replace("qodana_", "native_")
        if reason.startswith("native_recovery_") and reason != "native_recovery_only_no_inspection":
            receipt.update(private_deleted=False, sdk_removed=False)
    except (host.InspectError, OSError, ValueError, ElementTree.ParseError, subprocess.SubprocessError):
        reason = "native_prerequisite_or_report_failed"
    finally:
        if receipt.get("license_boundary_observed") and worker is not None:
            # Count-only persistent stop, never preserve raw licensing output.
            write_record(worker / "native-activation-stop.json", {"run_id": receipt["run_id"], "boundary_event_count": 1})
        if private is not None and private.exists():
            if findings:
                artifact_path = private.parent / (receipt["run_id"] + ".findings.json")
                artifact_path.write_text(json.dumps(findings))
                artifact = str(artifact_path)
            try:
                if journal is not None:
                    common.delete_private(private)
                else:
                    shutil.rmtree(private)
                receipt["private_deleted"] = True
            except OSError:
                reason = "native_cleanup_failed"
        if journal is not None and receipt["private_deleted"] and receipt["sdk_removed"]:
            journal.unlink(missing_ok=True)
        held_lock.close()
        receipt["wall_seconds"] = round(time.monotonic() - started, 3)
    return envelope(host, receipt, findings, reason, gaps, artifact)


def run(args, host):
    # A child owns the complete lifecycle and keeps the profile lock after the
    # invoking harness dies. The pipe signals parent death even during a pause.
    if not hasattr(os, "fork"):
        return envelope(host, {"run_id": str(uuid.uuid4()), "private_deleted": True, "sdk_removed": True}, [], "native_platform_unsupported", [])
    lifetime_read, lifetime_write = os.pipe()
    result_read, result_write = os.pipe()
    child = os.fork()
    if child == 0:
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
                result = _run(args, host, lifetime_read)
            except Exception:
                result = envelope(host, {"run_id": str(uuid.uuid4()), "private_deleted": False, "sdk_removed": False}, [], "native_internal_error", [])
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
                return envelope(host, {"run_id": str(uuid.uuid4()), "private_deleted": False, "sdk_removed": False}, [], "native_guardian_exited_without_result", [])
            return json.loads(data)
    finally:
        os.close(lifetime_write)
        os.waitpid(child, 0)
