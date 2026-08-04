#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Preview-gated, read-back-verified Part-DB part-lot quantity updates."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


READ_HELPER = Path(__file__).with_name("partdb-read.py")
SPEC = importlib.util.spec_from_file_location("partdb_read", READ_HELPER)
assert SPEC and SPEC.loader
partdb_read = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = partdb_read
SPEC.loader.exec_module(partdb_read)

PLAN_KIND = "partdb-write-plan.v1"
APPROVAL_KIND = "partdb-write-approval.v1"
RECEIPT_KIND = "partdb-write-receipt.v1"
LOT_PATH = "/api/part_lots/{id}"


class WriteError(RuntimeError):
    pass


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise WriteError(f"{description} is invalid") from None


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def is_amount(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def environment_values(private_repo: Path, env_file: str) -> dict[str, str]:
    candidate = (private_repo / env_file).resolve()
    try:
        candidate.relative_to(private_repo.resolve())
    except ValueError:
        raise WriteError("private Part-DB environment file is invalid") from None
    values = dict(os.environ)
    if not candidate.is_file():
        return values
    try:
        lines = candidate.read_text().splitlines()
    except (PermissionError, UnicodeDecodeError):
        raise WriteError("Part-DB credentials are unavailable") from None
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def write_environment(private_repo: Path, config: dict[str, Any]) -> tuple[str, str]:
    api = config.get("api")
    policy = config.get("policy")
    if not isinstance(api, dict) or not isinstance(policy, dict) or policy.get("allow_mutations") is not True:
        raise WriteError("private Part-DB context does not permit mutations")
    base_key = api.get("base_url_env")
    read_token_key = api.get("read_token_env")
    token_key = api.get("write_token_env")
    env_file = api.get("env_file")
    if not all(isinstance(value, str) and value for value in (base_key, read_token_key, token_key, env_file)):
        raise WriteError("private Part-DB write context is incomplete")
    if read_token_key == token_key:
        raise WriteError("private Part-DB read and write credentials must be separate")
    values = environment_values(private_repo, env_file)
    base_url = values.get(base_key, "").rstrip("/")
    token = values.get(token_key, "")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not token:
        raise WriteError("Part-DB write credentials are unavailable")
    return base_url, token


def verify_lot_patch_schema(base_url: str, read_token: str) -> None:
    schema = partdb_read.request(base_url, read_token, "/api/docs.json", accept="application/vnd.openapi+json")
    paths = schema.get("paths") if isinstance(schema, dict) else None
    endpoint = paths.get(LOT_PATH) if isinstance(paths, dict) else None
    patch = endpoint.get("patch") if isinstance(endpoint, dict) else None
    content = patch.get("requestBody", {}).get("content") if isinstance(patch, dict) else None
    payload = content.get("application/merge-patch+json") if isinstance(content, dict) else None
    payload_schema = payload.get("schema") if isinstance(payload, dict) else None
    if not isinstance(payload_schema, dict):
        raise WriteError("installed Part-DB schema does not support part-lot PATCH")
    if isinstance(payload_schema.get("properties"), dict) and "amount" in payload_schema["properties"]:
        return
    reference = payload_schema.get("$ref")
    component_name = reference.rsplit("/", 1)[-1] if isinstance(reference, str) else ""
    components = schema.get("components", {}).get("schemas") if isinstance(schema, dict) else None
    component = components.get(component_name) if isinstance(components, dict) else None
    if not isinstance(component, dict) or "amount" not in component.get("properties", {}):
        raise WriteError("installed Part-DB schema does not support part-lot amount updates")


def read_lot(base_url: str, token: str, lot_id: int) -> dict[str, Any]:
    value = partdb_read.request(base_url, token, LOT_PATH.format(id=lot_id))
    if not isinstance(value, dict) or not is_amount(value.get("amount")):
        raise WriteError("Part-DB part-lot response has an unsupported shape")
    return value


def patch_lot(base_url: str, token: str, lot_id: int, amount: int | float) -> None:
    request = urllib.request.Request(
        f"{base_url}{LOT_PATH.format(id=lot_id)}",
        data=json.dumps({"amount": amount}).encode(),
        method="PATCH",
        headers={
            "Accept": "application/ld+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/merge-patch+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            return
    except urllib.error.HTTPError as exc:
        raise WriteError(f"Part-DB write failed with HTTP {exc.code}; response body redacted") from None
    except (urllib.error.URLError, TimeoutError):
        raise WriteError("Part-DB write failed; connection details redacted") from None


def validate_intent(intent: Any) -> dict[str, int | float | str]:
    if not isinstance(intent, dict) or set(intent) != {"op", "lot_id", "amount"} or intent.get("op") != "part-lot-amount-set":
        raise WriteError("intent must contain only op, lot_id, and amount")
    lot_id = intent.get("lot_id")
    amount = intent.get("amount")
    if not isinstance(lot_id, int) or isinstance(lot_id, bool) or lot_id <= 0 or not is_amount(amount):
        raise WriteError("lot_id and amount are invalid")
    return {"op": "part-lot-amount-set", "lot_id": lot_id, "amount": amount}


def validate_plan(artifact: Any) -> dict[str, Any]:
    if not isinstance(artifact, dict) or set(artifact) != {"digest", "kind", "nonce", "operation"} or artifact.get("kind") != PLAN_KIND:
        raise WriteError("plan artifact is invalid")
    expected = artifact.get("digest")
    unsigned = {key: value for key, value in artifact.items() if key != "digest"}
    if not isinstance(expected, str) or digest(unsigned) != expected:
        raise WriteError("plan artifact digest is invalid")
    if not isinstance(artifact.get("nonce"), str) or len(artifact["nonce"]) != 32:
        raise WriteError("plan artifact is invalid")
    operation = artifact.get("operation")
    if not isinstance(operation, dict) or set(operation) != {"lot_id", "prior_amount", "target_amount"}:
        raise WriteError("plan operation is invalid")
    lot_id = operation.get("lot_id")
    prior_amount = operation.get("prior_amount")
    target_amount = operation.get("target_amount")
    if not isinstance(lot_id, int) or isinstance(lot_id, bool) or lot_id <= 0 or not is_amount(prior_amount) or not is_amount(target_amount):
        raise WriteError("plan operation is invalid")
    return artifact


def validate_approval(approval: Any, plan_digest: str) -> None:
    if not isinstance(approval, dict) or approval != {"kind": APPROVAL_KIND, "plan_digest": plan_digest}:
        raise WriteError("approval does not match the exact plan")


def receipt(plan_digest: str, outcome: str) -> dict[str, str]:
    return {"kind": RECEIPT_KIND, "outcome": outcome, "plan_digest": plan_digest}


def receipt_path(plan_path: Path, plan_digest: str) -> Path:
    return plan_path.parent / ".partdb-write-receipts" / f"{plan_digest}.json"


def reserve_receipt(path: Path, plan_digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt(plan_digest, "applying"), indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x") as handle:
            handle.write(payload)
    except FileExistsError:
        raise WriteError("this approval was already used; inspect its receipt or create a new plan") from None


def amounts_equal(left: int | float, right: int | float) -> bool:
    return math.isclose(left, right, rel_tol=0, abs_tol=1e-9)


def plan(args: argparse.Namespace) -> None:
    intent = validate_intent(load_json(Path(args.intent), "intent"))
    private_repo, config = partdb_read.context()
    base_url, read_token = partdb_read.environment(private_repo, config)
    verify_lot_patch_schema(base_url, read_token)
    current = read_lot(base_url, read_token, int(intent["lot_id"]))
    artifact = {
        "kind": PLAN_KIND,
        "nonce": secrets.token_hex(16),
        "operation": {
            "lot_id": intent["lot_id"],
            "prior_amount": current["amount"],
            "target_amount": intent["amount"],
        },
    }
    artifact["digest"] = digest(artifact)
    save_json(Path(args.output), artifact)
    print(json.dumps({"digest": artifact["digest"], "operation": artifact["operation"], "status": "ready" if not amounts_equal(current["amount"], intent["amount"]) else "noop"}, sort_keys=True))


def approve(args: argparse.Namespace) -> None:
    artifact = validate_plan(load_json(Path(args.plan), "plan artifact"))
    expected = artifact["digest"]
    if args.approve != expected:
        raise WriteError("typed approval does not match the plan digest")
    save_json(Path(args.output), {"kind": APPROVAL_KIND, "plan_digest": expected})
    print(json.dumps({"approved": True, "plan_digest": expected}, sort_keys=True))


def apply(args: argparse.Namespace) -> None:
    if not args.apply:
        raise WriteError("refusing mutation without --apply")
    artifact = validate_plan(load_json(Path(args.plan), "plan artifact"))
    plan_digest = artifact["digest"]
    validate_approval(load_json(Path(args.approval), "approval artifact"), plan_digest)
    operation = artifact["operation"]
    plan_path = Path(args.plan)
    receipt_file = receipt_path(plan_path, plan_digest)
    reserve_receipt(receipt_file, plan_digest)
    private_repo, config = partdb_read.context()
    try:
        base_url, read_token = partdb_read.environment(private_repo, config)
        verify_lot_patch_schema(base_url, read_token)
        current = read_lot(base_url, read_token, operation["lot_id"])
        if amounts_equal(current["amount"], operation["target_amount"]):
            save_json(receipt_file, receipt(plan_digest, "already-target"))
            print(json.dumps({"applied": False, "receipt": str(receipt_file), "status": "already-target"}, sort_keys=True))
            return
        if not amounts_equal(current["amount"], operation["prior_amount"]):
            raise WriteError("part-lot changed after planning; create a new plan")
        write_base_url, write_token = write_environment(private_repo, config)
        if write_base_url != base_url:
            raise WriteError("Part-DB read and write targets do not match")
        if write_token == read_token:
            raise WriteError("Part-DB read and write credentials must be separate")
        patch_lot(write_base_url, write_token, operation["lot_id"], operation["target_amount"])
        verified = read_lot(base_url, read_token, operation["lot_id"])
        if not amounts_equal(verified["amount"], operation["target_amount"]):
            raise WriteError("post-write verification failed")
    except (WriteError, partdb_read.PartdbError):
        save_json(receipt_file, receipt(plan_digest, "needs-reconciliation"))
        raise
    save_json(receipt_file, receipt(plan_digest, "verified"))
    print(json.dumps({"applied": True, "receipt": str(receipt_file), "status": "verified"}, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview-gated Part-DB part-lot writes")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("plan")
    command.add_argument("--intent", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(handler=plan)
    command = commands.add_parser("approve")
    command.add_argument("--plan", required=True)
    command.add_argument("--approve", required=True)
    command.add_argument("--output", required=True)
    command.set_defaults(handler=approve)
    command = commands.add_parser("apply")
    command.add_argument("--plan", required=True)
    command.add_argument("--approval", required=True)
    command.add_argument("--apply", action="store_true")
    command.set_defaults(handler=apply)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except (WriteError, partdb_read.PartdbError) as exc:
        fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
