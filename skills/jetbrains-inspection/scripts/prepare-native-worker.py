#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import native_backend as native


def inspection_host() -> Any:
    spec = importlib.util.spec_from_file_location("native_worker_setup_host", Path(__file__).with_name("jb-inspect.py"))
    if spec is None or spec.loader is None:
        raise ImportError("Inspection helper unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


host: Any = inspection_host()
WORKER_DIRECTORIES = ("config", "plugins", "home", "system", "log", "tmp")


def absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path != path.resolve() or any(character in str(path) for character in "\n\r\\"):
        raise ValueError("Use an absolute path without symlinks, line breaks or backslashes.")
    return path


def worker_entry(args: argparse.Namespace, destination: Path) -> dict[str, str]:
    cache = absolute_path(args.cache_root)
    if not cache.is_dir() or cache.is_relative_to(destination):
        raise ValueError("cacheRoot must be an existing directory outside the new worker.")
    entry = {"path": str(destination), "app": str(Path(args.app).expanduser().resolve()), "cacheRoot": str(cache)}
    for name in ("node", "jdk"):
        value = getattr(args, name)
        if value:
            entry[name] = str(Path(value).expanduser())
    return entry


def validate_entry(product_key: str, entry: dict[str, str]) -> Any:
    config = {"jetbrains": {"nativeWorkers": {product_key: entry}}}
    configured_host = SimpleNamespace(IDE_PRODUCTS=host.IDE_PRODUCTS, read_global_config=lambda: config)
    args = host.build_parser().parse_args(["agent-inspect"])
    settings = native.worker_settings(args, configured_host, product_key)
    launcher = settings.app / "Contents/MacOS" / settings.product.launcher_names[0]
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError("The product's macOS launcher is missing or not executable.")
    if product_key == "webstorm":
        native.node_executable(configured_host)
    if product_key == "intellij":
        native.java_home(configured_host)
    return settings


def gui_launcher(worker: Path, app: Path, product: Any) -> str:
    environment = {
        "PATH": os.defpath,
        "HOME": str(worker / "home"),
        "TMPDIR": str(worker / "tmp"),
        f"{product.env_var_base_name}_PROPERTIES": str(worker / "idea.properties"),
        f"{product.env_var_base_name}_VM_OPTIONS": str(worker / product.vm_options_name),
    }
    executable = str(app / "Contents/MacOS" / product.launcher_names[0])
    return f"""import fcntl
import os

lock = os.open({str(worker / "worker.lock")!r}, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit('Worker busy. Quit its GUI or let its inspection finish first.')
os.set_inheritable(lock, True)
os.execve({executable!r}, [{executable!r}], {environment!r})
"""


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    destination = Path(args.destination).expanduser()
    if os.path.lexists(destination):
        raise FileExistsError("Destination already exists; choose a new worker path.")
    destination = absolute_path(str(destination))
    if not destination.parent.is_dir():
        raise FileNotFoundError("Destination parent must already exist on the intended storage.")
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Install uv before preparing a worker.")
    entry = worker_entry(args, destination)
    settings = validate_entry(args.product, entry)
    product = settings.product
    defaults = (settings.app / "Contents/bin" / product.vm_options_name).read_text().splitlines()
    excluded = ("-Duser.home", "-Djava.io.tmpdir", "-Didea.", "-XX:ErrorFile", "-XX:HeapDump", "-XX:+HeapDump")
    vm = [line for line in defaults if not line.startswith(excluded)]
    vm += [
        f"-Duser.home={destination / 'home'}",
        f"-Djava.io.tmpdir={destination / 'tmp'}",
        f"-XX:ErrorFile={destination / 'log'}/java-error-%p.log",
        "-XX:-HeapDumpOnOutOfMemoryError",
    ]
    destination.mkdir(mode=0o700)
    for name in WORKER_DIRECTORIES:
        (destination / name).mkdir(mode=0o700)
    properties = "".join(f"idea.{name}.path={destination / name}\n" for name in ("config", "plugins", "system", "log"))
    write_private(destination / "idea.properties", properties + "idea.initially.ask.config=never\n")
    write_private(destination / product.vm_options_name, "\n".join(vm) + "\n")
    write_private(destination / "worker.lock", "")
    write_private(destination / "launch.py", gui_launcher(destination, settings.app, product))
    launcher = destination / f"Activate {product.display_name} worker.command"
    command = shlex.join([uv, "run", "--no-project", "--no-config", "python", "-I", str(destination / "launch.py")])
    write_private(launcher, f"#!/bin/sh\nexec {command}\n", mode=0o700)
    native.launcher_paths(destination)
    return {
        "config": {"jetbrains": {"nativeWorkers": {args.product: entry}}},
        "launcher": str(launcher),
        "activation_required": True,
        "next_action": "Merge config.jetbrains.nativeWorkers into the shared helper config. Open launcher once, sign in with an existing entitlement, and quit. No IDE was launched.",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a fresh isolated macOS worker without reading or creating account or licence data."
    )
    parser.add_argument("--product", choices=host.IDE_PRODUCTS, required=True)
    parser.add_argument("--destination", required=True, help="New absolute worker path; its parent must exist.")
    parser.add_argument("--app", required=True, help="Installed product's macOS application bundle.")
    parser.add_argument("--cache-root", required=True, help="Existing host cache directory outside the worker.")
    parser.add_argument("--node", help="WebStorm: absolute executable node path with npm beside it.")
    parser.add_argument("--jdk", help="IntelliJ IDEA: absolute Java home with bin/java and release.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Native worker preparation supports macOS only.")
    try:
        result = prepare(args)
    except (OSError, ValueError, native.Blocked) as error:
        print(json.dumps({"error": native.common.safe_text(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
