#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run the hermetic Launchplane agent/operator contract conformance gate."""

from __future__ import annotations

import json

from launchplane_contract import ContractError, load_contract, validate_contract


def main() -> int:
    try:
        artifact = load_contract()
        summary = validate_contract(artifact)
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
                    "message": "Vendored contract and local consumers are consistent.",
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
