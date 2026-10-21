#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run the hermetic Launchplane local caller-binding gate."""

from __future__ import annotations

import json

from launchplane_contract import ContractError, validate_local_bindings


def main() -> int:
    try:
        summary = validate_local_bindings()
    except ContractError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "status": "invalid",
                    "error": {"code": exc.code},
                    "summary": {
                        "hermetic_only": True,
                        "upstream_freshness_proven": False,
                    },
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "ok",
                "contract": summary,
                "summary": {
                    "message": "Local caller bindings are valid; producer compatibility is not proven.",
                    "hermetic_only": True,
                    "upstream_freshness_proven": False,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
