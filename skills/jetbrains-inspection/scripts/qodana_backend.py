#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Local, disposable Qodana runner used only by agent-inspect's explicit backend.

Raw engine output and licensing stay in disposable state. Only selected findings
and count/hash/resource/cleanup receipts leave it. No language is qualified for
GREEN yet (jetbrains-inspection-api#503).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree

PYTHON_IMAGE = "jetbrains/qodana-python-community@sha256:f96231d12dbc0b8aa63dccb994f5c53870e9de0c3ea82b7a13cddf672a966cca"
IMAGE_RE = re.compile(r"jetbrains/qodana-(python|jvm)-community@sha256:[0-9a-f]{64}\Z")
LICENSE_RE = re.compile(r"(?:pycharm\.key|idea\.key|plugin_[A-Za-z0-9_-]+\.license)\Z")
BOUNDARY_RE = re.compile(r"(?:start(?:ing|ed)?|activat(?:e[ds]?|ing|ion)|obtain(?:ing|ed)?|issu(?:e[ds]?|ing))\b.{0,100}\btrial|\btrial\b.{0,100}\b(?:start(?:ing|ed)?|activat(?:e[ds]?|ing|ion)|obtain(?:ing|ed)?|issu(?:e[ds]?|ing))|purchas|buy.{0,30}licen", re.I)
LOAD_LIMIT = 64
FINDING_LIMIT = 25
# An effective profile and a successful exit are not execution/completeness proof.
PARITY_GAPS = {
    "python": ["python_parity_unqualified", "measured_python_DuplicatedCode_HttpUrlsUsage_gap_unqualified", "python_clean_completeness_unproven"],
    "jvm": ["jvm_module_source_roots_parity_unproven", "jvm_intended_feature_coverage_unproven"],
}


class Blocked(Exception):
    """A reason code only; never propagate raw process/credential diagnostics."""


def command(argv, *, cwd=None, env=None, timeout=30):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)


def checked(argv, **kwargs):
    result = command(argv, **kwargs)
    if result.returncode:
        raise Blocked("qodana_prerequisite_failed")
    return result.stdout


def remove_container(name, env):
    try:
        removed = command(["docker", "rm", "-f", name], env=env)
        if removed.returncode == 0:
            return True
        absent = command(["docker", "container", "inspect", name], env=env)
        return absent.returncode != 0 and b"No such" in absent.stderr
    except (OSError, subprocess.TimeoutExpired):
        return False


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_text(value):
    text = str(value)
    if re.search(r"license.?key|confirmation.?stamp|activation.?code|authorization|\btoken\b", text, re.I):
        return "[credential-bearing text omitted]"
    text = re.sub(r"[A-Za-z0-9+/_=-]{60,}", "[opaque data omitted]", text)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email omitted]", text)
    return text[:2000]


def boundary_event(line):
    # Q261: availability/dry-run refusal is permitted, issuance is not.
    message = line.split(" - ", 1)[-1]
    return bool(BOUNDARY_RE.search(message))


def safe_env():
    # Do not forward QODANA_TOKEN, QODANA_LICENSE, IDE/account/Keychain settings,
    # Docker context overrides or provider secrets into the child/container.
    return {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}


def worker_lock_path():
    return Path("/tmp") / f"codex-skills-qodana-{os.getuid()}.lock"


def state_dir():
    # Shared across task caches and outside cache/30-day task retention.
    return Path.home() / ".local/state/jetbrains-inspection/qodana"


def save_run_record(private, receipt):
    record = {"run_id": receipt["run_id"], "private": str(private),
              "container_name": "jb-qodana-" + receipt["run_id"]}
    (private / "owner.json").write_text(json.dumps(record))
    path = state_dir() / (receipt["run_id"] + ".json")
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return path


def recover_runs(env):
    """Called only under the kernel-held worker lock, so prior helpers are gone.

    Recover exact journaled state after SIGKILL/crash, never by age/PID/path alone.
    The per-run marker and container run label/mount must agree before removal.
    """
    count = 0
    for path in state_dir().glob("*.json"):
        if path.name == "boundary-stop.json":
            continue
        if path.is_symlink():
            raise Blocked("qodana_recovery_ownership_unproven")
        record = json.loads(path.read_text())
        run_id = str(uuid.UUID(record["run_id"]))
        private = Path(record["private"])
        name = "jb-qodana-" + run_id
        if path.stem != run_id or record["container_name"] != name or not private.is_absolute() or not private.name.startswith("private-") or private.is_symlink():
            raise Blocked("qodana_recovery_ownership_unproven")
        if private.exists():
            marker = private / "owner.json"
            if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != record:
                raise Blocked("qodana_recovery_ownership_unproven")
        inspection = command(["docker", "container", "inspect", name], env=env)
        if inspection.returncode == 0:
            info = json.loads(inspection.stdout)[0]
            labels = info.get("Config", {}).get("Labels", {})
            mounts = info.get("Mounts", [])
            if labels.get("codex-skills.qodana.run") != run_id or not any(m.get("Destination") == "/private" and m.get("Source") == str(private) for m in mounts):
                raise Blocked("qodana_recovery_ownership_unproven")
            if not remove_container(name, env):
                raise Blocked("qodana_recovery_incomplete")
        elif b"No such" not in inspection.stderr:
            raise Blocked("qodana_recovery_unavailable")
        if private.exists():
            shutil.rmtree(private)
        path.unlink()
        count += 1
    return count


def profile_for(repo, name):
    candidates = list((repo / ".idea/inspectionProfiles").glob("*.xml"))
    if name:
        explicit = repo / name
        if explicit.is_file() and explicit.resolve().is_relative_to(repo):
            candidates = [explicit]
    matches = []
    for path in candidates:
        if path.is_symlink():
            continue
        root = ElementTree.parse(path).getroot()
        profile = root if root.tag == "profile" else root.find("profile")
        if profile is None:
            continue
        option = profile.find("option[@name='myName']")
        if name and path != repo / name and (option is None or option.get("value") != name):
            continue
        enabled = {tool.get("class") for tool in profile.findall("inspection_tool") if tool.get("enabled") == "true"}
        if enabled:
            matches.append((path, enabled))
    if len(matches) != 1:
        raise Blocked("qodana_populated_profile_required")
    return matches[0]


def copy_licenses(paths, config, receipt):
    names = set()
    for raw in paths:
        source = Path(raw).expanduser()
        if source.is_symlink() or not source.is_file() or not LICENSE_RE.fullmatch(source.name) or source.name in names:
            raise Blocked("qodana_license_source_invalid")
        names.add(source.name)
        # copyfile reads the source only; no account store, hash or contents in evidence.
        shutil.copyfile(source, config / source.name)
        (config / source.name).chmod(0o400)
        receipt["license_copy_count"] += 1


def unpack_plugins(paths, cache):
    for raw in paths:
        with zipfile.ZipFile(Path(raw).expanduser()) as package:
            for member in package.infolist():
                target = (cache / member.filename).resolve()
                if not target.is_relative_to(cache.resolve()) or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise Blocked("qodana_plugin_package_invalid")
            package.extractall(cache)


def docker_argv(name, image, private, language, uv_binary=None):
    argv = ["nice", "-n", "10", "docker", "run", "--pull", "never", "--name", name,
            "--label", "codex-skills.qodana=local", "--label", "codex-skills.qodana.run=" + name.removeprefix("jb-qodana-"),
            "--log-driver", "none", "--cpus", "2",
            "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "512",
            "--entrypoint", "/usr/bin/nice", "-v", f"{private}:/private",
            "-v", f"{private / 'project'}:/data/project",
            "-e", "HOME=/private/home", "-e", "QODANA_CONF=/private/config", "-e", "QODANA_DATA=/private/data",
            "-e", f"{'PYCHARM' if language == 'python' else 'IDEA'}_VM_OPTIONS=/private/probe.vmoptions"]
    if sys.platform != "darwin":
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    if language == "python":
        configured = "bootstrap" in json.loads((private / "qodana.yaml").read_text())
        argv += ["-e", "QODANA_PYTHON_PATH=" + ("/data/project/.venv/bin/python" if configured else "/usr/bin/python3")]
    if uv_binary:
        argv += ["-e", "PATH=/private/bin:/usr/local/bin:/usr/bin:/bin", "-e", "UV_CACHE_DIR=/private/cache/uv"]
    for license_copy in (private / "config").iterdir():
        if LICENSE_RE.fullmatch(license_copy.name):
            argv += ["-v", f"{license_copy}:/private/config/{license_copy.name}:ro"]
    argv += [image, "-n", "10", "/opt/idea/bin/qodana", "scan", "--project-dir", "/data/project",
             "--results-dir", "/private/results", "--cache-dir", "/private/cache",
             "--config", "/private/qodana.yaml", "--save-report=false",
             "--property", "idea.headless.enable.statistics=false"]
    return argv


def snapshot(repo, project):
    paths = checked(["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).decode().split("\0")
    hashes = {}
    for relative in filter(None, paths):
        source = repo / relative
        if not source.exists():
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(repo) or not source.is_file():
            raise Blocked("qodana_snapshot_path_unsupported")
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[relative] = digest(source)
        if digest(target) != hashes[relative]:
            raise Blocked("qodana_snapshot_changed")
    # Independent index only: never copy the linked-worktree .git pointer.
    checked(["git", "init", "--quiet", str(project)])
    checked(["git", "-C", str(project), "add", "--all"])
    return hashes


def preparation(config, private, language):
    prepare = config.get("prepare")
    if not prepare:
        return None
    if language != "python" or not isinstance(prepare, dict) or set(prepare) != {"python"}:
        raise Blocked("qodana_preparation_unsupported")
    py = prepare["python"]
    # Reuse the maintained generator, on Linux in the snapshot, without reading
    # any desktop SDK table. Existing config schema is validated by the host.
    generator = Path(__file__).with_name("prepare-python-project.py")
    shutil.copyfile(generator, private / generator.name)
    argv = ["uv", "run", "--no-project", "/private/prepare-python-project.py", "--repo", "/data/project",
            "--python", py["version"], "--module-name", py.get("moduleName", "qodana-project")]
    if py.get("sync"):
        argv += ["--sync"]
    for script in py.get("pep723Scripts", []):
        argv += ["--pep723-script", script]
    for root in py.get("testRoots", []):
        argv += ["--test-root", root]
    for extra in py.get("extras", []):
        argv += ["--extra", extra]
    return shlex.join(argv)


def collect_findings(results, selected, args):
    sarifs = list(results.rglob("qodana.sarif.json"))
    if len(sarifs) != 1 or sarifs[0].is_symlink():
        raise Blocked("qodana_sarif_missing_or_ambiguous")
    data = json.loads(sarifs[0].read_text())
    runs = data.get("runs")
    if data.get("version") != "2.1.0" or not isinstance(runs, list) or len(runs) != 1:
        raise Blocked("qodana_sarif_invalid")
    sarif_run = runs[0]
    if not isinstance(sarif_run.get("results"), list):
        raise Blocked("qodana_sarif_invalid")
    invocations = sarif_run.get("invocations", [])
    if any(item.get("executionSuccessful") is False for item in invocations):
        raise Blocked("qodana_execution_failed")
    findings = []
    for result in sarif_run["results"]:
        if result.get("suppressions") or result.get("baselineState") == "absent":
            continue
        rule = result.get("ruleId")
        locations = result.get("locations")
        if not isinstance(rule, str) or not locations:
            raise Blocked("qodana_finding_identity_unproven")
        physical = locations[0].get("physicalLocation", {})
        artifact = physical.get("artifactLocation", {})
        uri = urllib.parse.unquote(artifact.get("uri", ""))
        if uri.startswith("file:///data/project/"):
            uri = uri[len("file:///data/project/"):]
        elif uri.startswith("/data/project/"):
            uri = uri[len("/data/project/"):]
        if uri not in selected:
            continue
        region = physical.get("region", {})
        line = region.get("startLine")
        if not isinstance(line, int) or line < 1:
            raise Blocked("qodana_finding_identity_unproven")
        level = result.get("level", "warning")
        severity = {"error": "ERROR", "warning": "WARNING", "note": "WEAK WARNING", "none": "INFORMATION"}.get(level)
        if severity is None:
            raise Blocked("qodana_sarif_invalid")
        if args.severity != "all" and args.severity.lower() != severity.lower():
            continue
        findings.append({"file": uri, "line": line, "column": region.get("startColumn"), "severity": severity,
                         "inspection": safe_text(rule), "description": safe_text(result.get("message", {}).get("text", ""))})
    return findings


def coverage_gaps(language, selected, expected, results, odoo_project=False):
    gaps = list(PARITY_GAPS[language])
    profiles = list(results.rglob("effective.profile.xml"))
    if len(profiles) != 1:
        gaps.append("effective_profile_unavailable")
    else:
        tools = ElementTree.parse(profiles[0]).getroot().iter("inspection_tool")
        actual = {t.get("class") for t in tools if t.get("enabled") == "true"}
        gaps.extend("effective_profile_missing:" + str(rule) for rule in sorted(expected - actual))
    if odoo_project or any(Path(f).name == "__manifest__.py" for f in selected) or expected & {"OdooPy", "OdooXml"}:
        gaps += ["odoo_effective_default_unproven", "odoo_model_roots_interpreter_unproven", "OdooXml_execution_unproven",
                 "odoo_optional_integrations_unqualified"]
    supported = {".py"} if language == "python" else {".java", ".kt", ".kts", ".gradle"}
    if any(Path(f).suffix not in supported for f in selected):
        gaps.append("selected_language_coverage_unqualified")
    return gaps


class Monitor:
    def __init__(self, private):
        self.private = private
        self.offsets = {}
        self.tail = b""
        self.boundary = False
        self.license_refused = False

    def observe(self, chunk):
        # Check overlapping fragments as well as lines: a refusal can precede EOF.
        data = self.tail + chunk
        for text in data.decode(errors="replace").splitlines():
            self.boundary |= boundary_event(text)
            if "Trial dry-run:" not in text:
                self.license_refused |= bool(re.search(r"No valid license found for plugin|please.{0,100}QODANA_TOKEN|license.{0,30}(?:required|expired)", text, re.I))
        self.tail = data[-4096:]

    def logs(self):
        for parent in (self.private / "results", self.private / "logs", self.private / "data"):
            for path in parent.rglob("*.log"):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    stream.seek(self.offsets.get(path, 0))
                    chunk = stream.read(65536)
                    self.offsets[path] = stream.tell()
                if chunk:
                    self.observe(chunk)


def execute(argv, name, private, env, timeout, receipt):
    monitor = Monitor(private)
    started = time.monotonic()
    process = None
    paused = False
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                for _, _ in selector.select(timeout=0.05):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if chunk:
                        monitor.observe(chunk)
                monitor.logs()
                if monitor.boundary:
                    raise Blocked("qodana_trial_or_purchase_stop")
                if time.monotonic() - started > timeout:
                    raise Blocked("qodana_timeout")
                if process.poll() is not None:
                    # Drain buffered console output before accepting completion.
                    monitor.observe(process.stdout.read() or b"")
                    monitor.logs()
                    if monitor.boundary:
                        raise Blocked("qodana_trial_or_purchase_stop")
                    break
                load = os.getloadavg()[0]
                if load > LOAD_LIMIT and not paused:
                    checked(["docker", "pause", name], env=env)
                    paused = True
                    receipt["load_pause_count"] += 1
                elif load <= LOAD_LIMIT and paused:
                    checked(["docker", "unpause", name], env=env)
                    paused = False
        receipt["wall_seconds"] = round(time.monotonic() - started, 3)
        receipt["exit_code"] = process.returncode
        info = json.loads(checked(["docker", "inspect", name], env=env))[0]
        limits = info["HostConfig"]
        if (limits["NanoCpus"] != receipt["cpu_limit"] * 10**9 or
                limits["Memory"] != receipt["memory_limit_bytes"] or
                limits["MemorySwap"] != receipt["memory_limit_bytes"] or
                limits["PidsLimit"] != 512 or limits["LogConfig"]["Type"] != receipt["docker_logging"]):
            raise Blocked("qodana_resource_limits_unproven")
        receipt["resource_limits_verified"] = True
        if info["State"].get("OOMKilled"):
            raise Blocked("qodana_memory_exhausted")
        if monitor.license_refused:
            raise Blocked("qodana_license_unavailable")
        if process.returncode != 0:
            raise Blocked("qodana_execution_failed")
    finally:
        # Remove owned container/writable layer before deleting its profile. This
        # also stops paused workers; never kill by executable name or desktop PID.
        receipt["container_removed"] = remove_container(name, env)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()


def envelope(host, receipt, findings, reason, gaps, artifact):
    verdict = "RED" if findings and reason == "qodana_completed" else "UNKNOWN"
    action = "Fix current findings; retain the recorded coverage gaps." if verdict == "RED" else "Resolve the reported prerequisite or qualify coverage before using Qodana as a gate; the API remains the default."
    if reason == "qodana_trial_or_purchase_stop":
        action = "Stop Qodana work and report the trial/purchase boundary to the Director; do not activate or purchase."
    return {"schema_version": host.AGENT_RESULT_SCHEMA_VERSION, "command": "agent-inspect", "backend": "qodana",
            "status": "findings" if verdict == "RED" else "error", "helper_exit_code": host.classify_run_exit({"verdict": verdict}),
            "agent_result": {"verdict": verdict, "bucket": "findings" if verdict == "RED" else "environment_blocked",
                             "retry_policy": {"retry": False, "max_attempts": 0, "wait_ms": 0}, "terminal": True,
                             "next_action": action, "agent_report": f"{verdict}: {reason}; {len(findings)} findings; coverage remains unqualified."},
            "scope": receipt.get("scope"), "selected_file_count": receipt.get("selected_file_count"),
            "finding_count": len(findings), "findings": findings[:FINDING_LIMIT], "findings_limit": FINDING_LIMIT,
            "findings_truncated": len(findings) > FINDING_LIMIT, "findings_artifact": artifact,
            "proof_failures": gaps, "inspection_outcome": {"verdict": verdict, "reason": reason},
            "lifecycle_outcome": {"status": "clean" if receipt["profile_deleted"] and receipt["container_removed"] else "unknown"},
            "cleanup": {"status": "closed" if receipt["profile_deleted"] and receipt["container_removed"] else "failed"},
            "identity": {"helper_revision": receipt["helper_revision"], "qodana_backend_revision": receipt["backend_revision"],
                         "inspection_run_id": receipt["run_id"], "qodana_image": receipt.get("image")},
            "diagnostic": {"error_reason": reason, "qodana": receipt}}


def run(args, host):
    receipt = {"run_id": str(uuid.uuid4()), "license_copy_count": 0, "profile_deleted": True,
               "container_removed": True, "load_pause_count": 0, "cpu_limit": 2, "memory_limit_bytes": 4 * 1024**3,
               "nice": 10, "docker_logging": "none", "helper_revision": host.helper_revision(),
               "backend_revision": "sha256:" + digest(Path(__file__))}
    findings, gaps, reason, artifact = [], [], "qodana_prerequisite_failed", None
    private = None
    root = None
    journal = None
    lock = None
    env = safe_env()
    old_handlers = {}
    try:
        if os.name != "posix":
            raise Blocked("qodana_local_platform_unsupported")
        import fcntl
        def interrupt(_signum, _frame):
            raise Blocked("qodana_interrupted")
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, interrupt)
        lock = worker_lock_path().open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("qodana_worker_busy") from None
        state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        endpoint = checked(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], env=env).decode().strip()
        if not endpoint.startswith("unix://"):
            raise Blocked("qodana_local_docker_required")
        # Pin every subsequent command to this verified local socket, even if
        # another task changes Docker's current context while this run executes.
        env["DOCKER_HOST"] = endpoint
        receipt["recovered_run_count"] = recover_runs(env)
        if (state_dir() / "boundary-stop.json").exists():
            raise Blocked("qodana_stop_recorded")
        if os.getloadavg()[0] > LOAD_LIMIT:
            raise Blocked("qodana_host_load_paused")
        repo = Path(host.git_root(Path(args.repo).expanduser().resolve()) or args.repo).resolve()
        repository_config = host.read_repo_config(repo)
        config = repository_config.get("qualityGate", {}).get("inspection", {})
        host.parse_repository_preparation(config)
        scope = args.scope or host.first_scope(config.get("scopePreference")) or host.first_scope(repository_config.get("jetbrains", {}).get("scopePreference")) or "changed_files"
        receipt["scope"] = scope
        raw_paths = host.inspection_scope_paths(args, repo, scope)
        selected = set()
        for raw in raw_paths:
            path = host.normalize_inspection_lane_path(raw, repo)
            if path.is_file():
                selected.add(path.relative_to(repo).as_posix())
        receipt["selected_file_count"] = len(selected)
        receipt["selected_files"] = sorted(selected)
        if not selected:
            raise Blocked("qodana_scope_empty")
        if args.max_files is not None and len(selected) > args.max_files:
            raise Blocked("qodana_scope_too_large")
        if args.problem_type != "all" or args.file_pattern != "all" or args.offset or args.include_stale or args.allow_text_only_coverage:
            raise Blocked("qodana_filter_unsupported")
        image = args.qodana_image or PYTHON_IMAGE
        match = IMAGE_RE.fullmatch(image)
        if match is None:
            raise Blocked("qodana_pinned_community_image_required")
        language = match.group(1)
        receipt["image"] = image
        profile, expected = profile_for(repo, args.profile or config.get("profile", ""))
        receipt["profile_sha256"] = digest(profile)
        checked(["docker", "image", "inspect", image], env=env)
        active = checked(["docker", "ps", "--format", "{{.Image}}"], env=env).decode()
        if "qodana" in active.lower():
            raise Blocked("qodana_worker_busy")
        # The shared cache root is overridable for isolated tests and host policy.
        root = host.cache_dir() / "qodana"
        root.mkdir(parents=True, exist_ok=True)
        private = Path(tempfile.mkdtemp(prefix="private-", dir=root))
        private.chmod(0o700)
        receipt["profile_deleted"] = False
        journal = save_run_record(private, receipt)
        for name in ("project", "config", "home", "cache", "results", "logs", "data", "bin"):
            (private / name).mkdir(mode=0o700)
        hashes = snapshot(repo, private / "project")
        if not selected <= hashes.keys():
            raise Blocked("qodana_scope_snapshot_incomplete")
        receipt["source_manifest_sha256"] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        shutil.copyfile(profile, private / "profile.xml")
        ignored = sorted(set(hashes) - selected)
        yaml = {"version": "1.0", "profile": {"base": {"path": "/private/profile.xml"},
                "inspections": [{"group": "ALL", "ignore": ignored}] if ignored else []}}
        bootstrap = preparation(config, private, language)
        if bootstrap:
            yaml["bootstrap"] = bootstrap
        uv_binary = args.qodana_uv
        if uv_binary:
            shutil.copyfile(Path(uv_binary).expanduser(), private / "bin/uv")
            (private / "bin/uv").chmod(0o700)
        (private / "qodana.yaml").write_text(json.dumps(yaml))
        (private / "probe.vmoptions").write_text("-Xmx1536m\n-XX:ActiveProcessorCount=2\n-XX:-HeapDumpOnOutOfMemoryError\n-Djava.awt.headless=true\n-Duser.home=/private/home\n-Didea.headless.enable.statistics=false\n-Didea.log.path=/private/logs\n")
        unpack_plugins(args.qodana_plugin_zip, private / "cache/plugins/262")
        if os.getloadavg()[0] > LOAD_LIMIT:
            raise Blocked("qodana_host_load_paused")
        copy_licenses(args.qodana_license_file, private / "config", receipt)
        name = "jb-qodana-" + receipt["run_id"]
        receipt["container_name"] = name
        argv = docker_argv(name, image, private, language, uv_binary)
        if scope == "directory":
            argv += ["--only-directory", host.normalize_inspection_lane_path(args.directory, repo).relative_to(repo).as_posix()]
        receipt["repo_head_sha"] = host.git_head_sha(repo)
        receipt["container_removed"] = False
        execute(argv, name, private, env, max(1, args.timeout_ms / 1000), receipt)
        gaps = coverage_gaps(language, selected, expected, private / "results", any(Path(f).name == "__manifest__.py" for f in hashes))
        if config.get("lanes"):
            gaps.append("qodana_lane_models_unqualified")
        findings = collect_findings(private / "results", selected, args)
        if any(digest(private / "project" / f) != hashes[f] for f in selected):
            raise Blocked("qodana_snapshot_inputs_changed")
        after_paths = host.inspection_scope_paths(args, repo, scope)
        after_selected = {p.relative_to(repo).as_posix() for raw in after_paths
                          if (p := host.normalize_inspection_lane_path(raw, repo)).is_file()}
        if after_selected != selected or any(not (repo / f).is_file() or digest(repo / f) != h for f, h in hashes.items()):
            raise Blocked("qodana_source_inputs_changed")
        if digest(profile) != receipt["profile_sha256"]:
            raise Blocked("qodana_profile_inputs_changed")
        reason = "qodana_completed" if findings else "qodana_coverage_unproven"
    except Blocked as error:
        reason = str(error)
    except host.InspectError as error:
        reason = error.payload.get("error_reason", "qodana_scope_invalid")
    except KeyboardInterrupt:
        reason = "qodana_interrupted"
    except Exception:
        # Engine output, filesystem errors and exception strings can contain
        # activation values or raw private paths; only the reason code escapes.
        reason = "qodana_backend_failed"
    finally:
        if reason == "qodana_trial_or_purchase_stop":
            receipt["boundary_event"] = True
            try:
                (state_dir() / "boundary-stop.json").write_text(json.dumps({"run_id": receipt["run_id"], "reason": reason}))
                receipt["boundary_recorded"] = True
            except OSError:
                receipt["boundary_recorded"] = False
        if private is not None:
            try:
                shutil.rmtree(private)
                receipt["profile_deleted"] = not private.exists()
            except OSError:
                receipt["profile_deleted"] = False
        if not receipt["profile_deleted"] or not receipt["container_removed"]:
            reason = "qodana_cleanup_incomplete"
        elif journal is not None:
            try:
                journal.unlink()
            except OSError:
                reason = "qodana_recovery_record_cleanup_failed"
        if lock is not None:
            lock.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    if findings and reason in {"qodana_completed", "qodana_coverage_unproven"}:
        try:
            artifact_path = root / (receipt["run_id"] + ".json")
            artifact_path.write_text(json.dumps({"backend": "qodana", "receipt": receipt, "problems": findings,
                                                 "proof_failures": gaps}, indent=2))
            artifact_path.chmod(0o600)
            artifact = {"status": "complete", "path": str(artifact_path), "sha256": digest(artifact_path), "finding_count": len(findings)}
        except OSError:
            reason = "qodana_findings_artifact_unavailable"
    return envelope(host, receipt, findings, reason, gaps, artifact)
