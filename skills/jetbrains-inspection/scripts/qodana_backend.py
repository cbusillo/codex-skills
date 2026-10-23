#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Local, disposable Qodana runner behind agent-inspect.

Raw engine output stays in disposable state; credentials are never imported.
Only selected findings and count/hash/resource/cleanup receipts leave it. Python is the one language
qualified for GREEN, through a planted canary (codex-skills#2155); others stay
UNKNOWN without findings (jetbrains-inspection-api#503).
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
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
BOUNDARY_RE = re.compile(r"(?:start(?:ing|ed)?|activat(?:e[ds]?|ing|ion)|obtain(?:ing|ed)?|issu(?:e[ds]?|ing))\b.{0,100}\btrial|\btrial\b.{0,100}\b(?:start(?:ing|ed)?|activat(?:e[ds]?|ing|ion)|obtain(?:ing|ed)?|issu(?:e[ds]?|ing))|purchas|buy.{0,30}licen", re.I)
LOAD_LIMIT = 64
FINDING_LIMIT = 25
DRIFT_PATH_LIMIT = 25
RUN_LABEL = "codex-skills.qodana.run"
# Engine state lives in the container's own layer under WORK, copied in and
# out through the Docker API. Docker Desktop's file sharing keeps host handles
# for every bind-mounted path the engine touches, even after the container
# exits (shiny-infra-ops#320), so only the small log directory is shared.
WORK = "/work"
SHARED_LOGS = "/private/logs"
COPIED_IN = ("project", "config", "home", "cache", "data", "bin",
             "qodana.yaml", "profile.xml", "probe.vmoptions", "prepare-python-project.py")
UV_SPEC_RE = re.compile(r"[0-9A-Za-z.,<>=!~* ]{0,100}\Z")
# An effective profile and a successful exit are not execution/completeness
# proof. Python proves execution with a planted canary instead (below).
PARITY_GAPS = {
    "python": [],
    "jvm": ["jvm_module_source_roots_parity_unproven", "jvm_intended_feature_coverage_unproven"],
}
# A canary is planted in the private snapshot in every directory holding a
# selected Python file. Its unresolved name must be reported, which proves the
# engine inspected that directory with a loaded Python model. A type-hint
# finding on its valid 3.10+ annotations is what a module without the attached
# interpreter reports. Qodana's results show neither stub version gates nor
# version-specific syntax, so the requested version itself is proved by the
# prepared venv that QODANA_PYTHON_PATH names.
CANARY_SOURCE = ("def _jb_qodana_canary(value: int | None = None) -> dict[str, int] | None:\n"
                 "    unused_probe = 1\n"
                 "    return {\"value\": value or 0, \"probe\": _jb_qodana_unresolved_probe}\n")
CANARY_UNRESOLVED_LINE = 3
# The engine gives no code insight to larger files, so no inspection runs on
# them. Set explicitly so the engine and the GREEN check share one limit.
MAX_ANALYZED_FILE_KB = 2500
CANARY_REQUIRED_RULE = "PyUnresolvedReferencesInspection"
CANARY_LANGUAGE_LEVEL_RULE = "PyTypeHintsInspection"
# Enabled in common profiles but not run by the Python Community image
# (jetbrains-inspection-api#503). Results always name them and block GREEN.
# Q481 is retired; the qualified native route supplies these inspections.
PYTHON_COMMUNITY_UNCOVERED = frozenset({"DuplicatedCode", "HttpUrlsUsage"})
# Tools for other file types cannot apply to a selection of only .py files.
OTHER_LANGUAGE_RULE_RE = re.compile(r"(?:Css|Sass|Scss|Less|Eslint|Stylelint|Tslint|JS|Js|TypeScript|Html|Vue|Angular|Kotlin|Java|Groovy|Gradle|Xml|Json|Yaml|Sql)(?:[A-Z_]|\Z)")


class Blocked(Exception):
    """A reason code only; never propagate raw process/credential diagnostics."""

    def __init__(self, reason, *, input_path=None):
        super().__init__(reason)
        self.input_path = input_path


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
    return state_dir() / "worker.lock"


def state_dir():
    # Shared across task caches and outside cache/30-day task retention.
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/jetbrains-inspection/qodana"


def ignore_cleanup_signals():
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)


def delete_private(private):
    # Keep the ownership marker through partial deletion and delete it last.
    for entry in private.iterdir():
        if entry.name == "owner.json":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    marker = private / "owner.json"
    if marker.exists():
        marker.unlink()
    private.rmdir()


def storage_anchor(path):
    anchor = path
    while anchor.parent != anchor and anchor.parent.stat().st_dev == anchor.stat().st_dev:
        anchor = anchor.parent
    return anchor


def storage_identity(anchor):
    if not anchor.is_dir():
        raise Blocked("qodana_recovery_storage_unavailable")
    if sys.platform == "darwin":
        info = command(["/usr/sbin/diskutil", "info", "-plist", str(anchor)])
        if info.returncode:
            # APFS firmlinks such as /Users are not diskutil volume names.
            # Resolve their actual device through df without parsing mount paths.
            rows = checked(["df", "-P", str(anchor)]).decode().splitlines()
            device = rows[-1].split()[0]
            info = command(["/usr/sbin/diskutil", "info", "-plist", device])
        if info.returncode:
            raise Blocked("qodana_recovery_storage_unavailable")
        data = plistlib.loads(info.stdout)
        mount = Path(data.get("MountPoint", ""))
        if not data.get("VolumeUUID") or not mount.is_dir() or mount.stat().st_dev != anchor.stat().st_dev:
            raise Blocked("qodana_recovery_storage_unavailable")
        return {"volume_uuid": data["VolumeUUID"]}
    mount_info = command(["findmnt", "--noheadings", "--output", "UUID", "--target", str(anchor)]) if shutil.which("findmnt") else None
    if mount_info is not None and mount_info.returncode == 0 and mount_info.stdout.strip():
        return {"volume_uuid": mount_info.stdout.decode().strip()}
    return {"device": anchor.stat().st_dev}


def save_run_record(private, receipt, env=None):
    anchor = storage_anchor(private.parent)
    record = {"run_id": receipt["run_id"], "private": str(private),
              "container_name": "jb-qodana-" + receipt["run_id"],
              "storage_anchor": str(anchor), "storage_identity": storage_identity(anchor),
              "docker_endpoint": (env or {}).get("DOCKER_HOST", "")}
    (private / "owner.json").write_text(json.dumps(record))
    path = state_dir() / (receipt["run_id"] + ".json")
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return path


def recover_record(path, env):
    record = json.loads(path.read_text())
    run_id = str(uuid.UUID(record["run_id"]))
    private = Path(record["private"])
    name = "jb-qodana-" + run_id
    if path.is_symlink() or path.stem != run_id or record["container_name"] != name or not private.is_absolute() or not private.name.startswith("private-") or private.is_symlink():
        raise Blocked("qodana_recovery_ownership_unproven")
    if storage_identity(Path(record["storage_anchor"])) != record["storage_identity"]:
        raise Blocked("qodana_recovery_storage_changed")
    recorded_endpoint = record.get("docker_endpoint")
    if recorded_endpoint:
        if not recorded_endpoint.startswith("unix://"):
            raise Blocked("qodana_recovery_ownership_unproven")
        env = {**env, "DOCKER_HOST": recorded_endpoint}
    if private.exists():
        marker = private / "owner.json"
        if marker.is_symlink() or (marker.is_file() and json.loads(marker.read_text()) != record):
            raise Blocked("qodana_recovery_ownership_unproven")
        if not marker.is_file() and any(private.iterdir()):
            raise Blocked("qodana_recovery_ownership_unproven")
    inspection = command(["docker", "container", "inspect", name], env=env)
    if inspection.returncode == 0:
        info = json.loads(inspection.stdout)[0]
        labels = info.get("Config", {}).get("Labels", {})
        mounts = info.get("Mounts", [])
        if labels.get(RUN_LABEL) != run_id or not any(m.get("Destination") == SHARED_LOGS and m.get("Source") == str(private / "logs") for m in mounts):
            raise Blocked("qodana_recovery_ownership_unproven")
        if not remove_container(name, env):
            raise Blocked("qodana_recovery_incomplete")
    elif b"No such" not in inspection.stderr:
        raise Blocked("qodana_recovery_unavailable")
    if private.exists():
        delete_private(private)
    path.unlink()


def recover_runs(env):
    """Called only under the kernel-held worker lock, so prior helpers are gone.

    Recover exact journaled state after SIGKILL/crash, never by age/PID/path alone.
    The per-run marker and container run label/mount must agree before removal.
    """
    count, unresolved = 0, []
    for path in state_dir().glob("*.json"):
        if path.name == "boundary-stop.json":
            continue
        try:
            recover_record(path, env)
            count += 1
        except Blocked as error:
            if str(error) == "qodana_interrupted":
                raise
            unresolved.append(str(error))
        except Exception:
            unresolved.append("qodana_recovery_unavailable")
    return {"recovered_run_count": count, "unresolved_record_count": len(unresolved), "reasons": sorted(set(unresolved))}


def profile_for(repo, name):
    candidates = list((repo / ".idea/inspectionProfiles").glob("*.xml"))
    if name:
        explicit = repo / name
        if explicit.is_file() and explicit.resolve().is_relative_to(repo):
            candidates = [explicit]
    matches = []
    for path in candidates:
        require_public_input(path.relative_to(repo))
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


def require_public_input(path):
    """Reject credential locations by name before reading or copying any bytes.

    This is a location guard, not a secret scanner. Arbitrarily named secrets
    must stay outside the project snapshot and public plugin packages.
    """
    path = Path(path)
    name = path.name.lower()
    if (path.suffix.lower() in {".license", ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}
            or name == ".env" or name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"}
            or name in {"credentials", "credentials.json", "auth.json", "accounts.json", "account.json",
                        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
            or any(part.lower() in {".ssh", ".aws", ".gnupg"} for part in path.parts)):
        # Project/archive paths stay relative; external inputs expose only their
        # basename, never the caller's home or credential-store location.
        refused = path.name if path.is_absolute() else path.as_posix()
        raise Blocked("qodana_credential_input_refused", input_path=safe_text(refused))


def unpack_plugins(paths, cache):
    for index, raw in enumerate(paths):
        source = Path(raw).expanduser()
        require_public_input(source)
        require_public_input(source.resolve())
        destination = cache / f"bundle-{index}"
        destination.mkdir(parents=True)
        with zipfile.ZipFile(source) as package:
            for member in package.infolist():
                require_public_input(member.filename)
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(destination.resolve()) or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise Blocked("qodana_plugin_package_invalid")
            package.extractall(destination)
        if not all(child.is_dir() for child in destination.iterdir()):
            raise Blocked("qodana_plugin_package_invalid")


def docker_argv(name, image, private, language, uv_binary=None):
    """Create, not run: inputs are copied in by copy_in before the container starts."""
    argv = ["docker", "create", "--pull", "never", "--name", name,
            "--label", "codex-skills.qodana=local", "--label", RUN_LABEL + "=" + name.removeprefix("jb-qodana-"),
            "--log-driver", "none", "--cpus", "2",
            "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "512",
            "--entrypoint", "/usr/bin/nice", "--workdir", WORK,
            "-v", f"{private / 'logs'}:{SHARED_LOGS}",
            "-e", f"HOME={WORK}/home", "-e", f"QODANA_CONF={WORK}/config", "-e", f"QODANA_DATA={WORK}/data",
            "-e", f"{'PYCHARM' if language == 'python' else 'IDEA'}_VM_OPTIONS={WORK}/probe.vmoptions"]
    if sys.platform != "darwin":
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    if language == "python":
        configured = "bootstrap" in json.loads((private / "qodana.yaml").read_text())
        argv += ["-e", "QODANA_PYTHON_PATH=" + (f"{WORK}/project/.venv/bin/python" if configured else "/usr/bin/python3"),
                 "-e", f"UV_CACHE_DIR={WORK}/cache/uv"]
    if uv_binary:
        argv += ["-e", f"PATH={WORK}/bin:/usr/local/bin:/usr/bin:/bin"]
    argv += [image, "-n", "10", "/opt/idea/bin/qodana", "scan", "--project-dir", f"{WORK}/project",
             "--results-dir", f"{WORK}/results", "--cache-dir", f"{WORK}/cache",
             "--config", f"{WORK}/qodana.yaml", "--save-report=false",
             "--property", "idea.headless.enable.statistics=false"]
    return argv


COPY_SECONDS = 600
# Qodana rewrites this when it applies the configured profile. That choice is
# verified against the effective profile, so it is not an analysis input.
ENGINE_WRITTEN = frozenset({".idea/inspectionProfiles/profiles_settings.xml"})


def check_lifetime(parent_fd, deadline):
    if parent_gone(parent_fd):
        raise Blocked("qodana_parent_gone")
    if time.monotonic() > deadline:
        raise Blocked("qodana_timeout")


def bounded(argv, env, parent_fd=None, stdout=None):
    """One Docker client step, stopped at its deadline or when the parent exits.

    The step is killed and reaped before returning, so container cleanup never
    races a copy that is still streaming.
    """
    process = subprocess.Popen(argv, stdout=stdout or subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    deadline = time.monotonic() + COPY_SECONDS
    try:
        while process.poll() is None:
            check_lifetime(parent_fd, deadline)
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    if process.returncode:
        raise Blocked("qodana_prerequisite_failed")


def copy_in(name, private, env, parent_fd=None):
    """Stream inputs into the created container through the Docker API."""
    for path in private.rglob("*"):
        require_public_input(path.relative_to(private))
    for entry in COPIED_IN:
        if (private / entry).exists():
            bounded(["docker", "cp", "--archive", str(private / entry), f"{name}:{WORK}/{entry}"], env, parent_fd)
    for index, plugin in enumerate(sorted((private / "plugins").glob("bundle-*/*"))):
        # A run-specific folder name never shadows the image's built-in plugins
        # or uses a build-number cache.
        bounded(["docker", "cp", "--archive", str(plugin), f"{name}:/opt/idea/plugins/{name}-{index}"], env, parent_fd)


def start_argv(name):
    return ["nice", "-n", "10", "docker", "start", "--attach", name]


def copy_out(name, private, env, parent_fd=None):
    """Results, the prepared interpreter's version and inspected-source hashes come back after exit."""
    bounded(["docker", "cp", f"{name}:{WORK}/results", str(private / "results")], env, parent_fd)
    try:
        bounded(["docker", "cp", f"{name}:{WORK}/project/.venv/pyvenv.cfg", str(private / "pyvenv.cfg")], env, parent_fd)
    except Blocked as error:
        # Absent when no preparation ran; interpreter_version then reports None.
        if str(error) != "qodana_prerequisite_failed":
            raise
    # Preparation runs inside the container and can rewrite sources; hash what
    # the engine saw so findings are checked against the requested snapshot.
    archive = private / "inspected-project.tar"
    with archive.open("wb") as output:
        bounded(["docker", "cp", f"{name}:{WORK}/project", "-"], env, parent_fd, stdout=output)
    hashes = archive_hashes(archive, parent_fd, time.monotonic() + COPY_SECONDS)
    (private / "inspected-sha256.json").write_text(json.dumps(hashes))
    archive.unlink()


def archive_hashes(archive, parent_fd=None, deadline=float("inf")):
    """Hashes of regular files under the exported project, outside .venv and .git.

    Large tracked files must not hold the container past
    the deadline or the parent's exit, so both are checked between chunks.
    """
    import tarfile
    hashes = {}
    with tarfile.open(archive) as exported:
        for member in exported:
            check_lifetime(parent_fd, deadline)
            relative = member.name.partition("/")[2]
            if not member.isfile() or relative.split("/", 1)[0] in {".venv", ".git"}:
                continue
            stream = exported.extractfile(member)
            if stream is None:
                continue
            content = hashlib.sha256()
            while chunk := stream.read(1 << 20):
                check_lifetime(parent_fd, deadline)
                content.update(chunk)
            hashes[relative] = content.hexdigest()
    return hashes


def snapshot(repo, project):
    paths = checked(["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]).decode().split("\0")
    for relative in filter(None, paths):
        require_public_input(relative)
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


def uv_requirement(repo):
    """The repository's declared uv version range, or "" when it declares none."""
    import tomllib
    sources = ((repo / "uv.toml", ()), (repo / "pyproject.toml", ("tool", "uv")))
    for path, keys in sources:
        if not path.is_file():
            continue
        table = tomllib.loads(path.read_text())
        for key in keys:
            table = table.get(key, {})
        spec = table.get("required-version")
        if spec is None:
            continue
        if not isinstance(spec, str) or not UV_SPEC_RE.fullmatch(spec):
            raise Blocked("qodana_uv_requirement_invalid")
        return spec
    return ""


def preparation(config, private, language, repo=None, uv_supplied=False):
    """Bootstrap commands in order, and a directory to put first on PATH."""
    prepare = config.get("prepare")
    if not prepare:
        return None
    if language != "python" or not isinstance(prepare, dict) or set(prepare) != {"python"}:
        raise Blocked("qodana_preparation_unsupported")
    py = prepare["python"]
    if "projectPath" in py:
        raise Blocked("qodana_preparation_unsupported")
    # Reuse the maintained generator, on Linux in the snapshot, without reading
    # any desktop SDK table. Existing config schema is validated by the host.
    generator = Path(__file__).with_name("prepare-python-project.py")
    shutil.copyfile(generator, private / generator.name)
    # Qodana registers its own SDK from QODANA_PYTHON_PATH. A generated desktop
    # model names an SDK Qodana never registers, leaving the module without one,
    # so valid modern annotations were reported as findings.
    argv = ["uv", "run", "--no-project", f"{WORK}/prepare-python-project.py", "--repo", f"{WORK}/project",
            "--python", py["version"], "--module-name", py.get("moduleName", "qodana-project"),
            "--no-project-model"]
    if py.get("sync"):
        argv += ["--sync"]
    for script in py.get("pep723Scripts", []):
        argv += ["--pep723-script", script]
    for root in py.get("testRoots", []):
        argv += ["--test-root", root]
    for extra in py.get("extras", []):
        argv += ["--extra", extra]
    if uv_supplied:
        return {"commands": [argv], "path": None}
    # The image ships no uv: install the declared range into the disposable
    # profile so a run needs no host-supplied Linux binary.
    install = ["python3", "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet",
               "--target", f"{WORK}/tools/uv", "uv" + uv_requirement(repo)]
    return {"commands": [install, argv], "path": f"{WORK}/tools/uv/bin"}


def bootstrap_script(prepared):
    first, *rest = prepared["commands"]
    steps = [shlex.join(first)]
    if prepared["path"]:
        steps.append("export PATH=" + shlex.quote(prepared["path"]) + ":$PATH")
    return " && ".join(steps + [shlex.join(step) for step in rest])



def interpreter_version(config):
    """The prepared venv's major.minor, or None when bootstrap left no venv."""
    if config.is_symlink() or not config.is_file():
        return None
    for line in config.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"version", "version_info"}:
            match = re.match(r"\s*(\d+\.\d+)", value)
            return match.group(1) if match else None
    return None


def result_uri(result):
    locations = result.get("locations")
    if not locations:
        return None
    uri = urllib.parse.unquote(locations[0].get("physicalLocation", {}).get("artifactLocation", {}).get("uri", ""))
    for prefix in (f"file://{WORK}/project/", f"{WORK}/project/"):
        if uri.startswith(prefix):
            return uri[len(prefix):]
    return uri


def canary_gaps(results, canaries, effective):
    """Execution and interpreter proof from the planted canaries.

    Returns blocking gaps, the rule IDs seen on canaries (safe receipts) and
    whether the module was analyzed without the attached interpreter.
    """
    reports = list(results.rglob("qodana.sarif.json"))
    if len(reports) != 1 or reports[0].is_symlink():
        return ["python_canary_execution_unproven"], [], False
    runs = json.loads(reports[0].read_text()).get("runs") or [{}]
    seen = {canary: set() for canary in canaries}
    for result in runs[0].get("results") or []:
        uri = result_uri(result) if isinstance(result, dict) else None
        if uri in seen:
            line = result["locations"][0].get("physicalLocation", {}).get("region", {}).get("startLine")
            seen[uri].add((safe_text(result.get("ruleId")), line))
    rules = sorted({rule for hits in seen.values() for rule, _line in hits})
    missing = [rule for rule in (CANARY_REQUIRED_RULE, CANARY_LANGUAGE_LEVEL_RULE) if rule not in effective]
    if missing:
        # Without both, a missing finding cannot tell clean code from an engine that skipped it.
        return ["python_canary_rule_not_enabled:" + rule for rule in missing], rules, False
    gaps = []
    if not canaries or any((CANARY_REQUIRED_RULE, CANARY_UNRESOLVED_LINE) not in hits for hits in seen.values()):
        gaps.append("python_canary_execution_unproven")
    wrong_level = any(rule == CANARY_LANGUAGE_LEVEL_RULE for hits in seen.values() for rule, _line in hits)
    return gaps, rules, wrong_level


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
        uri = result_uri(result) or ""
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


def effective_rules(results):
    profiles = list(results.rglob("effective.profile.xml"))
    if len(profiles) != 1:
        return None
    tools = ElementTree.parse(profiles[0]).getroot().iter("inspection_tool")
    return {t.get("class") for t in tools if t.get("enabled") == "true"}


def coverage_gaps(language, selected, expected, results, odoo_project=False):
    """Gaps that block GREEN, plus profile inspections GREEN names as uncovered."""
    gaps = list(PARITY_GAPS[language])
    uncovered = []
    actual = effective_rules(results)
    python_only = language == "python" and all(Path(f).suffix == ".py" for f in selected)
    if actual is None:
        gaps.append("effective_profile_unavailable")
    else:
        for rule in sorted(expected - actual):
            if python_only and OTHER_LANGUAGE_RULE_RE.match(str(rule)):
                continue
            if python_only and rule in PYTHON_COMMUNITY_UNCOVERED:
                uncovered.append(rule)
            gaps.append("effective_profile_missing:" + str(rule))
    if odoo_project or any(Path(f).name == "__manifest__.py" for f in selected) or expected & {"OdooPy", "OdooXml"}:
        gaps += ["odoo_effective_default_unproven", "odoo_model_roots_interpreter_unproven", "OdooXml_execution_unproven",
                 "odoo_optional_integrations_unqualified"]
    supported = {".py"} if language == "python" else {".java", ".kt", ".kts", ".gradle"}
    if any(Path(f).suffix not in supported for f in selected):
        gaps.append("selected_language_coverage_unqualified")
    return gaps, uncovered


def lane_gaps(host, config, selected):
    """Lanes route to IDEs; Qodana's Python model covers only files a PyCharm lane owns."""
    if not config.get("lanes"):
        return []
    lanes = host.parse_inspection_lanes(config)
    for path in selected:
        owners = [candidate for candidate in lanes
                  if any(host.inspection_lane_pattern_matches(path, pattern) for pattern in candidate.include)]
        # The first matching lane owns a file, as in the API lane router.
        if not owners or owners[0].ide.strip().lower() != "pycharm" or owners[0].project_path:
            return ["qodana_lane_models_unqualified"]
    return []


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
        # The shared log directory live; results once copied out after exit.
        for parent in (self.private / "results", self.private / "logs"):
            for path in parent.rglob("*.log"):
                if path.is_symlink() or not path.is_file():
                    continue
                with path.open("rb") as stream:
                    stream.seek(self.offsets.get(path, 0))
                    chunk = stream.read(65536)
                    self.offsets[path] = stream.tell()
                if chunk:
                    self.observe(chunk)


def parent_gone(parent_fd):
    if parent_fd is None:
        return False
    try:
        return os.read(parent_fd, 1) == b""
    except BlockingIOError:
        return False


def execute_worker(argv, name, private, env, timeout, receipt, *, parent_fd=None):
    monitor = Monitor(private)
    started = time.monotonic()
    process = None
    paused = False
    try:
        checked(argv, env=env)
        copy_in(name, private, env, parent_fd)
        process = subprocess.Popen(start_argv(name), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if parent_gone(parent_fd):
                    raise Blocked("qodana_parent_gone")
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
        copy_out(name, private, env, parent_fd)
        monitor.logs()
        if monitor.boundary:
            raise Blocked("qodana_trial_or_purchase_stop")
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
        ignore_cleanup_signals()
        # Remove owned container/writable layer before deleting its profile. This
        # also stops paused workers; never kill by executable name or desktop PID.
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
        # The client is now gone and cannot create a late container.
        receipt["container_removed"] = remove_container(name, env)


def record_boundary(receipt):
    receipt["boundary_event"] = True
    try:
        path = state_dir() / "boundary-stop.json"
        path.write_text(json.dumps({"run_id": receipt["run_id"], "reason": "qodana_trial_or_purchase_stop"}))
        path.chmod(0o600)
        receipt["boundary_recorded"] = True
    except OSError:
        receipt["boundary_recorded"] = False


def finish_guardian(guardian):
    pid, writer = guardian
    os.close(writer)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if finished:
            return True
        time.sleep(0.05)
    return False


def execute(argv, name, private, env, timeout, receipt, *, lock_fd=None):
    """A detached local monitor retains the lock and deletes copies on parent EOF.

    The pipe proves parent lifetime without PID reuse assumptions. The monitor
    stays alive after delivering the safe receipt until collection finishes, so
    SIGKILL at any parent stage still cleans the profile, including paused runs.
    """
    life_read, life_write = os.pipe()
    result_read, result_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(life_write)
        os.close(result_read)
        os.setsid()
        null_fd = os.open(os.devnull, os.O_RDWR)
        for inherited in (0, 1, 2):
            os.dup2(null_fd, inherited)
        os.close(null_fd)
        os.set_blocking(life_read, False)
        reason = "qodana_completed"
        try:
            execute_worker(argv, name, private, env, timeout, receipt, parent_fd=life_read)
        except Blocked as error:
            reason = str(error)
            if error.input_path is not None:
                receipt["refused_input"] = error.input_path
        except BaseException:
            reason = "qodana_worker_failed"
        finally:
            ignore_cleanup_signals()
            if reason == "qodana_trial_or_purchase_stop":
                record_boundary(receipt)
            try:
                with os.fdopen(result_write, "w") as output:
                    json.dump({"reason": reason, "receipt": receipt}, output)
            except (BrokenPipeError, OSError):
                pass
            while not parent_gone(life_read):
                time.sleep(0.05)
            if receipt.get("container_removed"):
                try:
                    if private.exists():
                        delete_private(private)
                    (state_dir() / (receipt["run_id"] + ".json")).unlink(missing_ok=True)
                except OSError:
                    pass  # The ownership journal/marker keeps cleanup resumable.
            os.close(life_read)
            if lock_fd is not None:
                os.close(lock_fd)
            os._exit(0)
    os.close(life_read)
    os.close(result_write)
    guardian = (pid, life_write)
    try:
        chunks = []
        while chunk := os.read(result_read, 65536):
            chunks.append(chunk)
        result = json.loads(b"".join(chunks))
        receipt.update(result["receipt"])
        if result["reason"] != "qodana_completed":
            raise Blocked(result["reason"])
        return guardian
    except BaseException:
        ignore_cleanup_signals()
        # Close the result reader first: a large receipt cannot keep the monitor
        # blocked writing while we wait for its private cleanup.
        os.close(result_read)
        result_read = None
        if not finish_guardian(guardian):
            receipt["cleanup_pending"] = True
        else:
            receipt["container_removed"] = remove_container(name, env)
        raise
    finally:
        if result_read is not None:
            os.close(result_read)


def envelope(host, receipt, findings, reason, gaps, artifact, uncovered=()):
    completed = reason == "qodana_completed"
    verdict = "RED" if findings and completed else "GREEN" if completed and not gaps else "UNKNOWN"
    action = {"RED": "Fix current findings; retain the recorded coverage gaps.",
              "GREEN": "None; the selected Python scope is clean apart from the named uncovered inspections."}.get(
        verdict, "Resolve the reported prerequisite or coverage gap and run a fresh assessment.")
    if receipt.get("boundary_event") or reason == "qodana_trial_or_purchase_stop":
        action = "Stop Qodana work and report the trial/purchase boundary to the Director; do not activate or purchase."
    if reason in {"qodana_license_unavailable", "qodana_license_import_removed"}:
        action = ("Owner question: use an independently Director-activated headless profile; "
                  "never copy credentials. See jetbrains-inspection/references/qodana-backend.md#director-activation. "
                  "The Director activates it through the product UI using existing entitlements.")
    elif reason in {"qodana_source_inputs_changed", "qodana_source_selection_changed"}:
        action = ("Await every writer in this worktree, including a running Anthropic or Google model review, whose "
                  "`.model-review-*` scratch lives in the checkout until it finishes; check that the paths in "
                  "diagnostic.qodana.source_input_drift are stable, then run one fresh assessment.")
    elif reason == "qodana_credential_input_refused":
        action = ("Remove the refused input from the snapshot or public plugin package; "
                  "keep credentials outside project inputs or ignored by Git. "
                  "For public files with ambiguous credential names, use --backend api without copying them. "
                  "See diagnostic.qodana.refused_input. No activation is needed for this refusal.")
    cleanup_clean = receipt["profile_deleted"] and receipt["container_removed"] and not receipt.get("unresolved_record_count") and not receipt.get("cleanup_pending") and not receipt.get("journal_cleanup_failed")
    coverage = "coverage gaps recorded" if gaps else "execution proved" if completed else "execution unproved"
    if uncovered:
        coverage += "; not run: " + ", ".join(uncovered)
    return {"schema_version": host.AGENT_RESULT_SCHEMA_VERSION, "command": "agent-inspect", "backend": "qodana",
            "status": {"RED": "findings", "GREEN": "clean"}.get(verdict, "error"), "helper_exit_code": host.classify_run_exit({"verdict": verdict}),
            "agent_result": {"verdict": verdict, "bucket": {"RED": "findings", "GREEN": "clean"}.get(verdict, "environment_blocked"),
                             "retry_policy": {"retry": False, "max_attempts": 0, "wait_ms": 0}, "terminal": True,
                             "next_action": action, "agent_report": f"{verdict}: {reason}; {len(findings)} findings; {coverage}."},
            "uncovered_inspections": list(uncovered),
            "scope": receipt.get("scope"), "selected_file_count": receipt.get("selected_file_count"),
            "finding_count": len(findings), "findings": findings[:FINDING_LIMIT], "findings_limit": FINDING_LIMIT,
            "findings_truncated": len(findings) > FINDING_LIMIT, "findings_artifact": artifact,
            "proof_failures": gaps, "inspection_outcome": {"verdict": verdict, "reason": reason},
            "lifecycle_outcome": {"status": "clean" if cleanup_clean else "unknown"},
            "cleanup": {"status": "closed" if cleanup_clean else "deferred" if receipt.get("cleanup_pending") else "failed"},
            "identity": {"helper_revision": receipt["helper_revision"], "qodana_backend_revision": receipt["backend_revision"],
                         "inspection_run_id": receipt["run_id"], "qodana_image": receipt.get("image")},
            "diagnostic": {"error_reason": reason, "qodana": receipt}}


def bounded_paths(paths):
    paths = sorted(paths)
    return {"paths": paths[:DRIFT_PATH_LIMIT], "count": len(paths), "omitted_count": max(0, len(paths) - DRIFT_PATH_LIMIT)}


def source_input_drift(repo, hashes, selected, current_selected):
    missing = [f for f in hashes if not (repo / f).is_file()]
    changed = [f for f, h in hashes.items() if (repo / f).is_file() and digest(repo / f) != h]
    added, removed = current_selected - selected, selected - current_selected
    if not (missing or changed or added or removed):
        return None
    drift = {"kind": "inputs" if missing or changed else "selection", "attribution": "unattributed",
             "changed_inputs": bounded_paths(changed), "missing_inputs": bounded_paths(missing),
             "selection_added": bounded_paths(added), "selection_removed": bounded_paths(removed)}
    return {key: value for key, value in drift.items() if not isinstance(value, dict) or value["count"]}


def requested_python_version(config):
    """The structured preparation's Python version, the only declared interpreter."""
    prepare = config.get("prepare")
    python = prepare.get("python") if isinstance(prepare, dict) and set(prepare) == {"python"} else None
    return python.get("version") if isinstance(python, dict) and "projectPath" not in python else None


def has_odoo_manifest(paths):
    fixture_roots = {"tests", "fixtures", "test-fixtures", "testdata", "assets"}
    return any(Path(path).name == "__manifest__.py" and not fixture_roots.intersection(Path(path).parts) for path in paths if path)


def is_odoo(repo):
    paths = checked(["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*__manifest__.py"])
    return has_odoo_manifest(paths.decode().split("\0"))


def plant_canaries(project, selected, language, receipt):
    """Snapshot-only canaries, one per directory holding a selected Python file."""
    if language != "python":
        return []
    stem = "_jb_qodana_canary_" + str(receipt["run_id"]).replace("-", "")[:12] + ".py"
    canaries = sorted({(Path(f).parent / stem).as_posix() for f in selected if f.endswith(".py")})
    for canary in canaries:
        (project / canary).write_text(CANARY_SOURCE)
    if canaries:
        checked(["git", "-C", str(project), "add", "--", *canaries])
    return canaries


def python_proof_gaps(private, hashes, selected, canaries, requested, receipt):
    """Blocking gaps for a Python GREEN; raises when findings cannot be decisive."""
    gaps = []
    # A sibling canary cannot vouch for a file the engine skips for its size.
    if any((private / "project" / f).stat().st_size > MAX_ANALYZED_FILE_KB * 1024 for f in selected):
        gaps.append("qodana_selected_file_over_analysis_limit")
    version = interpreter_version(private / "pyvenv.cfg")
    receipt["python_interpreter_version"] = version
    if requested is None:
        gaps.append("python_interpreter_unprepared")
    elif version != requested:
        raise Blocked("qodana_python_interpreter_mismatch")
    # A tracked module can exclude files by pattern, which no directory canary detects.
    if any(f.endswith(".iml") and "excludePattern" in (private / "project" / f).read_text(errors="replace") for f in hashes):
        gaps.append("qodana_tracked_module_exclusions_unproven")
    failures, receipt["canary_rules"], wrong_level = canary_gaps(
        private / "results", canaries, effective_rules(private / "results") or set())
    if wrong_level:
        # Findings from a model at the wrong language level are not decisive.
        raise Blocked("qodana_python_language_level_mismatch")
    return gaps + failures


def scope_selection(args, host):
    repo = Path(host.git_root(Path(args.repo).expanduser().resolve()) or args.repo).resolve()
    repository_config = host.read_repo_config(repo)
    config = repository_config.get("qualityGate", {}).get("inspection", {})
    host.parse_repository_preparation(config)
    scope = args.scope or host.first_scope(config.get("scopePreference")) or host.first_scope(repository_config.get("jetbrains", {}).get("scopePreference")) or "changed_files"
    selected = set()
    for raw in host.inspection_scope_paths(args, repo, scope):
        path = host.normalize_inspection_lane_path(raw, repo)
        if path.is_file():
            selected.add(path.relative_to(repo).as_posix())
    if config.get("lanes") and scope != "files":
        selection = host.resolve_inspection_lane_selection(args, {
            "worktree_root": str(repo), "scope": scope,
            "_inspection_lanes": host.parse_inspection_lanes(config),
        })
        selected -= {entry["file"] for entry in selection["excluded_files"]}
    return repo, config, scope, selected


def backend_options(args, backend):
    fields = {
        "qodana": ("qodana_recover_only", "qodana_image", "qodana_license_file", "qodana_plugin_zip", "qodana_uv"),
        "native": ("native_worker", "native_recover_only", "native_framework_root", "native_reconcile_result", "native_release_run"),
    }
    return any(getattr(args, name, None) for name in fields[backend])


def select_backend(args, host, host_default=None):
    """Default backend for one agent-inspect run: (backend, reason).

    Python is the qualified language (codex-skills#2155): a selection of only
    Python files, outside Odoo and owned by PyCharm lanes, runs headless when
    the pinned image is already local. Everything else keeps the API. A host
    may pin the choice with JB_INSPECT_DEFAULT_BACKEND.
    """
    if backend_options(args, "qodana"):
        return "qodana", "qodana_options_supplied"
    if host_default:
        return host_default, "host_default"
    try:
        repo, config, _scope, selected = scope_selection(args, host)
        if not selected or any(not f.endswith(".py") for f in selected):
            return "api", "selection_not_python_only"
        if requested_python_version(config) is None:
            return "api", "python_preparation_not_declared"
        if is_odoo(repo):
            return "api", "odoo_not_yet_qualified"
        if lane_gaps(host, config, selected):
            return "api", "lane_not_pycharm"
        if command(["docker", "image", "inspect", PYTHON_IMAGE], env=safe_env()).returncode != 0:
            return "api", "qodana_image_unavailable"
    except (Blocked, host.InspectError, OSError, subprocess.SubprocessError, ValueError):
        return "api", "qodana_selection_unavailable"
    return "qodana", "python_qualified"


def run(args, host):
    receipt = {"run_id": str(uuid.uuid4()), "profile_deleted": True,
               "container_removed": True, "load_pause_count": 0, "cpu_limit": 2, "memory_limit_bytes": 4 * 1024**3,
               "nice": 10, "docker_logging": "none", "helper_revision": host.helper_revision(),
               "backend_revision": "sha256:" + digest(Path(__file__))}
    findings, gaps, uncovered, reason, artifact = [], [], [], "qodana_prerequisite_failed", None
    private = None
    root = None
    journal = None
    guardian = None
    lock = None
    env = safe_env()
    old_handlers = {}
    try:
        # Deprecated input is recognized only to explain the supported route.
        # Refuse before stat/read, Docker, recovery or private-profile creation.
        if args.qodana_license_file:
            raise Blocked("qodana_license_import_removed")
        for raw in [args.qodana_uv, *args.qodana_plugin_zip]:
            if raw:
                source = Path(raw).expanduser()
                require_public_input(source)
                require_public_input(source.resolve())
        if os.name != "posix":
            raise Blocked("qodana_local_platform_unsupported")
        import fcntl
        def interrupt(_signum, _frame):
            raise Blocked("qodana_interrupted")
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, interrupt)
        state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = worker_lock_path().open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("qodana_worker_busy") from None
        endpoint = checked(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], env=env).decode().strip()
        if not endpoint.startswith("unix://"):
            raise Blocked("qodana_local_docker_required")
        # Pin every subsequent command to this verified local socket, even if
        # another task changes Docker's current context while this run executes.
        env["DOCKER_HOST"] = endpoint
        recovery = recover_runs(env)
        receipt.update(recovery)
        if recovery["unresolved_record_count"]:
            raise Blocked("qodana_recovery_incomplete")
        if (state_dir() / "boundary-stop.json").exists():
            receipt["boundary_event"] = True
            raise Blocked("qodana_stop_recorded")
        if args.qodana_recover_only:
            raise Blocked("qodana_recovery_only_no_inspection")
        if os.getloadavg()[0] > LOAD_LIMIT:
            raise Blocked("qodana_host_load_paused")
        repo, config, scope, selected = scope_selection(args, host)
        receipt["scope"] = scope
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
        journal = save_run_record(private, receipt, env)
        for name in ("project", "config", "home", "cache", "logs", "data", "bin", "plugins"):
            (private / name).mkdir(mode=0o700)
        hashes = snapshot(repo, private / "project")
        if not selected <= hashes.keys():
            raise Blocked("qodana_scope_snapshot_incomplete")
        receipt["source_manifest_sha256"] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        requested_python = requested_python_version(config)
        canaries = plant_canaries(private / "project", selected, language, receipt)
        shutil.copyfile(profile, private / "profile.xml")
        ignored = sorted(set(hashes) - selected)
        yaml = {"version": "1.0", "profile": {"base": {"path": f"{WORK}/profile.xml"},
                "inspections": [{"group": "ALL", "ignore": ignored}] if ignored else []}}
        prepared = preparation(config, private, language, repo, bool(args.qodana_uv))
        receipt["uv_source"] = None if not prepared else "supplied" if args.qodana_uv else "provisioned"
        if prepared:
            yaml["bootstrap"] = bootstrap_script(prepared)
        uv_binary = args.qodana_uv
        if uv_binary:
            shutil.copyfile(Path(uv_binary).expanduser(), private / "bin/uv")
            (private / "bin/uv").chmod(0o700)
        (private / "qodana.yaml").write_text(json.dumps(yaml))
        (private / "probe.vmoptions").write_text("-Xmx1536m\n-XX:ActiveProcessorCount=2\n-XX:-HeapDumpOnOutOfMemoryError\n-Djava.awt.headless=true\n-Didea.headless.enable.statistics=false\n"
                                                  f"-Duser.home={WORK}/home\n-Didea.log.path={SHARED_LOGS}\n"
                                                  f"-Didea.max.intellisense.filesize={MAX_ANALYZED_FILE_KB}\n")
        unpack_plugins(args.qodana_plugin_zip, private / "plugins")
        if os.getloadavg()[0] > LOAD_LIMIT:
            raise Blocked("qodana_host_load_paused")
        name = "jb-qodana-" + receipt["run_id"]
        receipt["container_name"] = name
        argv = docker_argv(name, image, private, language, uv_binary)
        if scope == "directory":
            argv += ["--only-directory", host.normalize_inspection_lane_path(args.directory, repo).relative_to(repo).as_posix()]
        receipt["repo_head_sha"] = host.git_head_sha(repo)
        receipt["container_removed"] = False
        argv += ["--timeout", str(max(1, args.timeout_ms))]
        guardian = execute(argv, name, private, env, max(1, args.timeout_ms / 1000), receipt, lock_fd=lock.fileno())
        gaps, uncovered = coverage_gaps(language, selected, expected, private / "results", has_odoo_manifest(hashes))
        gaps += lane_gaps(host, config, selected)
        if language == "python":
            gaps += python_proof_gaps(private, hashes, selected, canaries, requested_python, receipt)
        findings = collect_findings(private / "results", selected, args)
        inspected = private / "inspected-sha256.json"
        seen = json.loads(inspected.read_text()) if inspected.is_file() else {}
        # Every requested input, selected or imported by one, must be what the
        # engine saw; files the run added, such as canaries, are allowed.
        changed = sorted(f for f, h in hashes.items() if seen.get(f) != h and f not in ENGINE_WRITTEN)
        if changed:
            receipt["changed_inputs"] = changed[:DRIFT_PATH_LIMIT]
            receipt["changed_input_count"] = len(changed)
            receipt["changed_input_omitted_count"] = max(0, len(changed) - DRIFT_PATH_LIMIT)
            raise Blocked("qodana_snapshot_inputs_changed")
        drift = source_input_drift(repo, hashes, selected, scope_selection(args, host)[3])
        if drift:
            receipt["source_input_drift"] = drift
            raise Blocked("qodana_source_inputs_changed" if drift["kind"] == "inputs" else "qodana_source_selection_changed")
        if digest(profile) != receipt["profile_sha256"]:
            raise Blocked("qodana_profile_inputs_changed")
        reason = "qodana_completed" if findings or not gaps else "qodana_coverage_unproven"
    except Blocked as error:
        reason = str(error)
        if error.input_path is not None:
            receipt["refused_input"] = error.input_path
    except host.InspectError as error:
        reason = error.payload.get("error_reason", "qodana_scope_invalid")
    except KeyboardInterrupt:
        reason = "qodana_interrupted"
    except Exception:
        # Engine output, filesystem errors and exception strings can contain
        # activation values or raw private paths; only the reason code escapes.
        reason = "qodana_backend_failed"
    finally:
        if old_handlers:
            ignore_cleanup_signals()
        if reason == "qodana_trial_or_purchase_stop":
            record_boundary(receipt)
        if guardian is not None and not finish_guardian(guardian):
            receipt["cleanup_pending"] = True
        if private is not None and receipt["container_removed"] and not receipt.get("cleanup_pending"):
            try:
                if private.exists():
                    delete_private(private)
                receipt["profile_deleted"] = not private.exists()
            except OSError:
                receipt["profile_deleted"] = False
        if receipt.get("cleanup_pending"):
            reason = "qodana_cleanup_pending"
        elif not receipt["profile_deleted"] or not receipt["container_removed"]:
            reason = "qodana_cleanup_incomplete"
        elif journal is not None:
            try:
                journal.unlink(missing_ok=True)
            except OSError:
                receipt["journal_cleanup_failed"] = True
                reason = "qodana_recovery_record_cleanup_failed"
        if lock is not None:
            lock.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    if old_handlers and (state_dir() / "boundary-stop.json").exists():
        receipt["boundary_event"] = True
    if findings and reason in {"qodana_completed", "qodana_coverage_unproven"}:
        try:
            artifact_path = root / (receipt["run_id"] + ".json")
            artifact_path.write_text(json.dumps({"backend": "qodana", "receipt": receipt, "problems": findings,
                                                 "proof_failures": gaps}, indent=2))
            artifact_path.chmod(0o600)
            artifact = {"status": "complete", "path": str(artifact_path), "sha256": digest(artifact_path), "finding_count": len(findings)}
        except OSError:
            reason = "qodana_findings_artifact_unavailable"
    return envelope(host, receipt, findings, reason, gaps, artifact, uncovered)
