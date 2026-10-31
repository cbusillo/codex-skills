# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Iterator
import urllib.error
import urllib.request

LAUNCHPLANE = Path(__file__).resolve().parents[2] / "launchplane/scripts"
sys.path.insert(0, str(LAUNCHPLANE))
spec = importlib.util.spec_from_file_location("attention_operator", LAUNCHPLANE / "launchplane-write-action.py")
assert spec is not None and spec.loader is not None
operator: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)
MAX_EVENT_LINE_BYTES = 8192


class ConfigurationError(ValueError):
    pass


def messages(args: Any) -> Iterator[dict]:
    try:
        settings = operator.resolve_settings(args)
        operator.validate_service_url(settings["service_url"])
        if not settings["token"]:
            raise ValueError("existing Launchplane operator configuration is required")
    except ValueError as error:
        raise ConfigurationError(str(error)) from error
    request = urllib.request.Request(
        operator.build_url(settings["service_url"], operator.helper_command_path("attention-events-read")),
        headers={"Accept": "application/x-ndjson", "Authorization": f"Bearer {settings['token']}"},
    )
    try:
        with operator.safe_urlopen(request, timeout=20) as response:
            while line := response.readline(MAX_EVENT_LINE_BYTES + 1):
                if len(line) > MAX_EVENT_LINE_BYTES or not line.endswith(b"\n"):
                    raise ValueError("attention event exceeded the bounded message size")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("attention event must be an object")
                yield row
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise PermissionError(f"Launchplane attention subscription refused: HTTP {error.code}") from None
        raise OSError(f"Launchplane attention subscription unavailable: HTTP {error.code}") from None
