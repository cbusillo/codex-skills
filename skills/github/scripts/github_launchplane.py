# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Detect optional Launchplane configuration without reading credentials or calling it."""
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path


def configuration_status(metadata: object, *, home: Path, environ: Mapping[str, str]) -> str:
    # Presence is enough: an empty or invalid setting can be a broken configured
    # installation. Repository routing can require a read, never prove enrollment.
    if any(key.startswith("LAUNCHPLANE_") for key in environ):
        return "configured"
    if not isinstance(metadata, dict):
        return "unknown"
    if "launchplane" in metadata:
        return "configured"
    try:
        config_directory = home / ".config" / "launchplane"
        if config_directory.is_symlink() or any(config_directory.iterdir()):
            return "configured"
    except FileNotFoundError:
        if config_directory.parent.is_symlink() and not config_directory.parent.is_dir():
            return "unknown"
    except OSError:
        return "unknown"
    return "not_configured"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Effective repository metadata, when present")
    args = parser.parse_args()
    try:
        metadata = json.loads(Path(args.config).read_text()) if args.config else {}
        status = configuration_status(metadata, home=Path.home(), environ=os.environ)
    except (OSError, ValueError):
        status = "unknown"
    print(json.dumps({"status": status}))


if __name__ == "__main__":
    main()
