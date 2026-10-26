#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import hashlib
import errno
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

LOAD_LIMIT = 64
FINDING_LIMIT = 25
MAX_ANALYZED_FILE_KB = 2500
REQUIRED_PLUGINS_FILE = ".idea/externalDependencies.xml"
NATIVE_OPTIONS = ("native_worker", "native_recover_only", "native_framework_root", "native_reconcile_result", "native_release_run")


class Blocked(Exception):
    def __init__(self, reason, *, input_path=None):
        super().__init__(reason)
        self.input_path = input_path


def command(argv, *, cwd=None, env=None, timeout=30):
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout)


def checked(argv, **kwargs):
    result = command(argv, **kwargs)
    if result.returncode:
        raise Blocked("native_prerequisite_failed")
    return result.stdout


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_text(value):
    text = str(value)
    if re.search(r"license.?key|confirmation.?stamp|activation.?code|authorization|\btoken\b", text, re.I):
        return "[credential-bearing text omitted]"
    text = re.sub(r"[A-Za-z0-9+/_=-]{60,}", "[opaque data omitted]", text)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email omitted]", text)
    return text[:2000]


def record_private_rescan(receipt: dict[str, Any] | None, stage: str) -> None:
    if receipt is not None:
        rescans = receipt.setdefault("private_removal_rescans", {})
        rescans[stage] = rescans.get(stage, 0) + 1


def same_directory(path: Path, original: os.stat_result) -> bool:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(observed.st_mode) and (observed.st_dev, observed.st_ino) == (original.st_dev, original.st_ino)


def remove_private_subtree(entry: Path, receipt: dict[str, Any] | None) -> None:
    original = entry.stat(follow_symlinks=False)
    try:
        shutil.rmtree(entry)
    except OSError as error:
        if error.errno != errno.ENOTEMPTY or not same_directory(entry, original):
            raise
        record_private_rescan(receipt, "subtree")
        shutil.rmtree(entry)


def restore_private_owner(directory_fd: int, owner: bytes | None, receipt: dict[str, Any] | None) -> None:
    if owner is None:
        return
    try:
        descriptor = os.open("owner.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb") as output:
            output.write(owner)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory_fd)
    except FileExistsError:
        return
    except OSError as error:
        if receipt is not None:
            receipt["private_owner_restore_errno"] = error.errno


def remove_private_contents(private: Path, receipt: dict[str, Any] | None, progress: dict[str, str]) -> None:
    progress["stage"] = "enumeration"
    entries = list(private.iterdir())
    for entry in entries:
        if entry.name == "owner.json":
            continue
        if entry.is_dir() and not entry.is_symlink():
            progress["stage"] = "subtree"
            remove_private_subtree(entry, receipt)
        else:
            progress["stage"] = "entry"
            entry.unlink()


def delete_private(private: Path, *, receipt: dict[str, Any] | None = None) -> None:
    progress = {"stage": "root_open"}
    directory_fd = None
    original = None
    marker = private / "owner.json"
    owner = None
    owner_removed = False
    try:
        directory_fd = os.open(private, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        original = os.fstat(directory_fd)
        progress["stage"] = "owner_read"
        try:
            descriptor = os.open("owner.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            descriptor = None
        if descriptor is not None:
            with os.fdopen(descriptor, "rb") as source:
                owner = source.read()
        remove_private_contents(private, receipt, progress)
        progress["stage"] = "owner"
        if marker.exists():
            marker.unlink()
            owner_removed = True
        progress["stage"] = "root"
        try:
            private.rmdir()
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or not same_directory(private, original) or marker.exists() or marker.is_symlink():
                raise
            record_private_rescan(receipt, "root")
            remove_private_contents(private, receipt, progress)
            progress["stage"] = "root"
            private.rmdir()
    except OSError as error:
        if owner_removed and original is not None and directory_fd is not None and same_directory(private, original):
            restore_private_owner(directory_fd, owner, receipt)
        if receipt is not None:
            receipt["private_removal_error"] = {
                "stage": progress["stage"],
                "errno": error.errno if type(error.errno) is int else None,
            }
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def storage_anchor(path):
    anchor = path
    while anchor.parent != anchor and anchor.parent.stat().st_dev == anchor.stat().st_dev:
        anchor = anchor.parent
    return anchor


def storage_identity(anchor):
    if not anchor.is_dir():
        raise Blocked("native_recovery_storage_unavailable")
    if sys.platform == "darwin":
        info = command(["/usr/sbin/diskutil", "info", "-plist", str(anchor)])
        if info.returncode:
            rows = checked(["df", "-P", str(anchor)]).decode().splitlines()
            device = rows[-1].split()[0]
            info = command(["/usr/sbin/diskutil", "info", "-plist", device])
        if info.returncode:
            raise Blocked("native_recovery_storage_unavailable")
        data = plistlib.loads(info.stdout)
        mount = Path(data.get("MountPoint", ""))
        if not data.get("VolumeUUID") or not mount.is_dir() or mount.stat().st_dev != anchor.stat().st_dev:
            raise Blocked("native_recovery_storage_unavailable")
        return {"volume_uuid": data["VolumeUUID"]}
    mount_info = command(["findmnt", "--noheadings", "--output", "UUID", "--target", str(anchor)]) if shutil.which("findmnt") else None
    if mount_info is not None and mount_info.returncode == 0 and mount_info.stdout.strip():
        return {"volume_uuid": mount_info.stdout.decode().strip()}
    return {"device": anchor.stat().st_dev}


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
        raise Blocked("native_populated_profile_required")
    return matches[0]


def require_public_input(path):
    path = Path(path)
    name = path.name.lower()
    if (path.suffix.lower() in {".license", ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}
            or name == ".env" or name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"}
            or name in {"credentials", "credentials.json", "auth.json", "accounts.json", "account.json",
                        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
            or any(part.lower() in {".ssh", ".aws", ".gnupg"} for part in path.parts)):
        refused = path.name if path.is_absolute() else path.as_posix()
        raise Blocked("native_credential_input_refused", input_path=safe_text(refused))


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
            raise Blocked("native_snapshot_path_unsupported")
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[relative] = digest(source)
        if digest(target) != hashes[relative]:
            raise Blocked("native_snapshot_changed")
    checked(["git", "init", "--quiet", str(project)])
    checked(["git", "-C", str(project), "add", "--all"])
    return hashes


def preparation(config, private):
    prepare = config.get("prepare")
    if not prepare:
        return None
    if not isinstance(prepare, dict) or set(prepare) != {"python"}:
        raise Blocked("native_preparation_unsupported")
    py = prepare["python"]
    if "projectPath" in py:
        raise Blocked("native_preparation_unsupported")
    generator = Path(__file__).with_name("prepare-python-project.py")
    shutil.copyfile(generator, private / generator.name)
    argv = ["uv", "run", "--no-project", str(private / generator.name), "--repo", str(private / "project"),
            "--python", py["version"], "--module-name", py.get("moduleName", "native-project"),
            "--no-project-model"]
    if py.get("sync"):
        argv += ["--sync"]
    for script in py.get("pep723Scripts", []):
        argv += ["--pep723-script", script]
    for root in py.get("testRoots", []):
        argv += ["--test-root", root]
    for extra in py.get("extras", []):
        argv += ["--extra", extra]
    return argv


def lane_gaps(host, config, selected, product_key="pycharm"):
    if not config.get("lanes"):
        return []
    lanes = host.parse_inspection_lanes(config)
    for path in selected:
        owner = lane_owner(host, lanes, path)
        if owner is None or host.product_for_selector(owner.ide) is not host.IDE_PRODUCTS[product_key] or owner.project_path:
            return ["native_lane_models_unqualified"]
    return []


def lane_owner(host, lanes, path) -> Any:
    return next((lane for lane in lanes if any(host.inspection_lane_pattern_matches(path, pattern) for pattern in lane.include)), None)


ALTERNATE_PRODUCT_KEYS = ("webstorm", "intellij")


def configured_product_key(host, config, selected):
    if config.get("lanes"):
        return next((key for key in ALTERNATE_PRODUCT_KEYS if selected and not lane_gaps(host, config, selected, key)), "pycharm")
    product = host.product_for_selector(config.get("ide")) if isinstance(config.get("ide"), str) else None
    return next((key for key in ALTERNATE_PRODUCT_KEYS if product is host.IDE_PRODUCTS[key]), "pycharm")


def npm_lockfile(root):
    if (root / "pnpm-lock.yaml").is_file():
        raise Blocked("native_pnpm_unsupported")
    if not (root / "package.json").is_file() or not (root / "package-lock.json").is_file():
        raise Blocked("native_npm_lockfile_required")
    return root / "package-lock.json"


def requested_python_version(config):
    prepare = config.get("prepare")
    python = prepare.get("python") if isinstance(prepare, dict) and set(prepare) == {"python"} else None
    return python.get("version") if isinstance(python, dict) and "projectPath" not in python else None


def has_odoo_manifest(paths):
    fixture_roots = {"tests", "fixtures", "test-fixtures", "testdata", "assets"}
    return any(Path(path).name == "__manifest__.py" and not fixture_roots.intersection(Path(path).parts) for path in paths if path)


def is_odoo(repo):
    paths = checked(["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*__manifest__.py"])
    return has_odoo_manifest(paths.decode().split("\0"))


def scope_selection(args: Any, host: Any, receipt: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any], str, set[str]]:
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
    if config.get("lanes"):
        selection = host.resolve_inspection_lane_selection(args, {
            "worktree_root": str(repo), "scope": scope,
            "_inspection_lanes": host.parse_inspection_lanes(config),
        })
        selected -= set(selection["unmatched_files"])
        selected -= {entry["file"] for entry in selection["excluded_files"]}
        if receipt is not None:
            receipt.update(unmatched_files=selection["unmatched_files"], excluded_files=selection["excluded_files"])
    return repo, config, scope, selected


PLUGIN_FILE_LIMIT = 4 * 1024 * 1024
UNREADABLE_PLUGIN_INPUT = (OSError, ValueError, ElementTree.ParseError, zipfile.BadZipFile, MemoryError, RecursionError)
UNKNOWN_VERSION = "of unknown version"


def bounded_bytes(path: Path) -> bytes:
    if path.is_symlink() or path.stat().st_size > PLUGIN_FILE_LIMIT:
        raise ValueError(path.name)
    return path.read_bytes()


def plain_xml(data: bytes) -> ElementTree.Element:
    text = data.decode("utf-8")
    if "<!entity" in text.lower() or "<!doctype" in text.lower():
        raise ValueError("XML declarations are refused")
    return ElementTree.fromstring(text)


def required_plugins(repo: Path) -> list[dict[str, str]]:
    path = repo / REQUIRED_PLUGINS_FILE
    if not path.exists() and not path.is_symlink():
        return []
    try:
        root = plain_xml(bounded_bytes(path))
        component = root.find("component[@name='ExternalDependencies']")
        if root.tag != "project" or component is None:
            raise ValueError(REQUIRED_PLUGINS_FILE)
        required = [dict(plugin.attrib) for plugin in component.findall("plugin")]
        if any(not plugin.get("id", "").strip() or any(plugin.get(bound) is not None and version_key(plugin[bound]) is None
                                                       for bound in ("min-version", "max-version")) for plugin in required):
            raise ValueError(REQUIRED_PLUGINS_FILE)
        return required
    except UNREADABLE_PLUGIN_INPUT:
        raise Blocked("native_required_plugins_unreadable") from None


def version_key(version: str) -> tuple[int, ...] | None:
    match = re.match(r"\s*(\d+(?:\.\d+)*)", version)
    if match is None:
        return None
    parts = [int(part) for part in match.group(1).split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def plugin_descriptor(jar: Path) -> tuple[str, str] | None:
    with zipfile.ZipFile(jar) as archive:
        try:
            info = archive.getinfo("META-INF/plugin.xml")
        except KeyError:
            return None
        with archive.open(info) as descriptor:
            data = descriptor.read(PLUGIN_FILE_LIMIT + 1)
        if info.file_size > PLUGIN_FILE_LIMIT or len(data) > PLUGIN_FILE_LIMIT:
            raise ValueError(jar.name)
        root = plain_xml(data)
    plugin_id = (root.findtext("id") or root.findtext("name") or "").strip()
    return (plugin_id, (root.findtext("version") or "").strip()) if plugin_id else None


def contained(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root)


def worker_plugins(app: Path, config: Path, plugins: Path) -> tuple[dict[str, str], set[str]]:
    try:
        distribution = json.loads(bounded_bytes(app / "Contents/Resources/product-info.json"))
        build = str(distribution.get("buildNumber", ""))
        installed: dict[str, str] = {str(plugin_id): build for plugin_id in distribution.get("bundledPlugins", [])}
        root = plugins.resolve()
        found: dict[str, str] = {}
        for entry in sorted(plugins.iterdir()):
            if not contained(entry, root):
                continue
            lib = entry / "lib"
            jars = [entry] if entry.suffix == ".jar" else sorted(lib.glob("*.jar")) if lib.is_dir() and contained(lib, root) else []
            for jar in jars:
                descriptor = plugin_descriptor(jar) if contained(jar, root) and jar.is_file() else None
                if descriptor is not None:
                    plugin_id, version = descriptor
                    found[plugin_id] = version or found.get(plugin_id, "")
        installed.update(found)
        disabled = config / "disabled_plugins.txt"
        lines = bounded_bytes(disabled).decode().splitlines() if disabled.exists() or disabled.is_symlink() else []
        names = [line.strip() for line in lines if line.strip()]
    except UNREADABLE_PLUGIN_INPUT:
        raise Blocked("native_worker_plugins_unreadable") from None
    return installed, set(names)


def outside_bounds(version: str, minimum: str | None, maximum: str | None) -> str | None:
    current = version_key(version)
    if minimum and (current is None or current < (version_key(minimum) or ())):
        return f"needs {minimum} or later"
    if maximum and (current is None or current > (version_key(maximum) or ())):
        return f"needs {maximum} or earlier"
    return None


def plugin_gaps(repo: Path, app: Path, config: Path, plugins: Path) -> tuple[list[str], list[str]]:
    required = required_plugins(repo)
    if not required:
        return [], []
    installed, disabled = worker_plugins(app, config, plugins)
    verified, gaps = [], []
    for plugin in required:
        plugin_id = plugin["id"].strip()
        version = installed.get(plugin_id)
        if version is None:
            gaps.append(f"{plugin_id} (not installed)")
        elif plugin_id in disabled:
            gaps.append(f"{plugin_id} (disabled)")
        elif bound := outside_bounds(version, plugin.get("min-version"), plugin.get("max-version")):
            gaps.append(f"{plugin_id} {version or UNKNOWN_VERSION} ({bound})")
        else:
            verified.append(f"{plugin_id} {version}".strip())
    return verified, gaps


def native_options_supplied(args):
    return any(getattr(args, name, None) for name in NATIVE_OPTIONS)
