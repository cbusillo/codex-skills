#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused tests for install_just_every_plugins.py."""

from __future__ import annotations

import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("install_just_every_plugins.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("install_just_every_plugins", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load install_just_every_plugins.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_main(module: Any, *args: str) -> tuple[int, str]:
    stdout = io.StringIO()
    old_argv = sys.argv
    try:
        sys.argv = ["install_just_every_plugins.py", *args]
        with redirect_stdout(stdout):
            exit_code = module.main()
    finally:
        sys.argv = old_argv
    return exit_code, stdout.getvalue()


def test_install_dry_run_prints_marketplace_and_plugin_commands() -> None:
    module = load_module()

    exit_code, stdout = run_main(module, "install", "--dry-run")

    marketplace = module.marketplace_root()
    assert exit_code == 0
    assert stdout == (
        f"codex plugin marketplace add {marketplace}\n"
        "codex plugin add ultracode@codex-skills-just-every\n"
        "codex plugin add auto-review@codex-skills-just-every\n"
    )


def test_install_dry_run_can_filter_plugins() -> None:
    module = load_module()

    exit_code, stdout = run_main(
        module,
        "install",
        "--dry-run",
        "--plugin",
        "auto-review",
    )

    assert exit_code == 0
    assert "codex plugin add auto-review@codex-skills-just-every\n" in stdout
    assert "codex plugin add ultracode@codex-skills-just-every\n" not in stdout


def test_status_dry_run_prints_inspection_commands() -> None:
    module = load_module()

    exit_code, stdout = run_main(module, "status", "--dry-run")

    assert exit_code == 0
    assert stdout == (
        "codex plugin marketplace list\n"
        "codex plugin list --marketplace codex-skills-just-every --available --json\n"
    )


def main() -> int:
    test_install_dry_run_prints_marketplace_and_plugin_commands()
    test_install_dry_run_can_filter_plugins()
    test_status_dry_run_prints_inspection_commands()
    print("ok test-install-just-every-plugins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
