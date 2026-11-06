# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

COMMAND = "testing-rebuild-migrated-upload"
PARTS = ("database", "filestore")
SHA256 = re.compile(r"[a-f0-9]{64}")


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(COMMAND, help="Stream the original completed migrated pair into its existing testing rebuild.")
    for name in ("product", "operation-id", "private-address"):
        parser.add_argument("--" + name, required=True)
    for name in ("migrated-source-file", "database", "filestore"):
        parser.add_argument("--" + name, required=True, type=Path)


def load_manifest(path: Path, *, product: str) -> dict[str, Any]:
    if path.stat().st_size > 16 * 1024:
        raise ValueError("invalid_migrated_manifest")
    source = json.loads(path.read_text())
    expected = {
        "kind",
        "product",
        "context",
        "source_backup",
        "producer_run",
        "target_image",
        *PARTS,
        "filestore_expanded_bytes",
        "filestore_files",
    }
    if not isinstance(source, dict):
        raise ValueError("invalid_migrated_manifest")
    source.setdefault("kind", "migrated")
    if set(source) != expected or source["kind"] != "migrated" or source["product"] != product:
        raise ValueError("invalid_migrated_manifest")
    if any(
        not isinstance(source[key], str) or not 0 < len(source[key]) <= 1024
        for key in ("product", "context", "source_backup", "producer_run")
    ):
        raise ValueError("invalid_migrated_manifest")
    if not isinstance(source["target_image"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", source["target_image"]):
        raise ValueError("invalid_migrated_manifest")
    for key in ("filestore_expanded_bytes", "filestore_files"):
        if type(source[key]) is not int or not 0 < source[key] < 2**63:
            raise ValueError("invalid_migrated_manifest")
    for part in PARTS:
        item = source[part]
        if (
            not isinstance(item, dict)
            or set(item) != {"size", "sha256"}
            or type(item["size"]) is not int
            or not 0 < item["size"] < 2**63
            or not isinstance(item["sha256"], str)
            or SHA256.fullmatch(item["sha256"]) is None
        ):
            raise ValueError("invalid_migrated_manifest")
    return source


class PrivateHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, private_address: str, *, timeout: float) -> None:
        address = ipaddress.ip_address(private_address)
        if not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError("private_operator_address_required")
        self.private_address = str(address)
        self.tls_context = ssl.create_default_context()
        super().__init__(hostname, port, timeout=timeout, context=self.tls_context, blocksize=1024 * 1024)

    def connect(self) -> None:
        self.sock = socket.create_connection((self.private_address, self.port), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.sock.close()
            raise


def private_request(
    h: Any,
    settings: dict[str, str],
    address: str,
    path: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    origin = urlsplit(h.build_url(settings["service_url"], path))
    if origin.scheme != "https" or not origin.hostname:
        raise ValueError("private_operator_https_required")
    connection = PrivateHTTPSConnection(origin.hostname, origin.port or 443, address, timeout=timeout)
    try:
        connection.request(
            "GET",
            origin.path,
            headers={"Accept": "application/json", "Authorization": "Bearer " + settings["token"]},
        )
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024 or response.status != 200:
            raise ValueError("migrated_upload_unavailable")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("invalid_migrated_upload_response")
        return result
    finally:
        connection.close()


def private_send(
    h: Any,
    settings: dict[str, str],
    address: str,
    path: str,
    *,
    timeout: float,
    handle: BinaryIO,
    offset: int,
    prefix: str,
    remaining: int,
) -> dict[str, Any]:
    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    origin = urlsplit(h.build_url(settings["service_url"], path))
    if origin.scheme != "https" or not origin.hostname:
        raise ValueError("private_operator_https_required")
    connection = PrivateHTTPSConnection(origin.hostname, origin.port or 443, address, timeout=timeout)
    logger = logging.Logger("migrated-upload")
    logger.disabled = True
    with socket.create_connection((connection.private_address, connection.port), timeout) as raw:
        try:
            with connect(
                origin._replace(scheme="wss").geturl(),
                sock=raw,
                ssl=connection.tls_context,
                server_hostname=origin.hostname,
                additional_headers={"Authorization": "Bearer " + settings["token"]},
                proxy=None,
                compression=None,
                logger=logger,
                open_timeout=timeout,
                ping_timeout=timeout,
                legacy=True,
            ) as websocket:
                websocket.send(json.dumps({"offset": offset, "prefix_sha256": prefix, "size": remaining}))
                if json.loads(websocket.recv(timeout=timeout)) != {"ready": True, "offset": offset}:
                    raise ValueError("migrated_prefix_unconfirmed")
                while remaining:
                    data = handle.read(min(remaining, 1024 * 1024))
                    if not data:
                        raise ValueError("original_migrated_input_truncated")
                    websocket.send(data)
                    remaining -= len(data)
                websocket.send('{"complete": true}')
                result = json.loads(websocket.recv(timeout=timeout))
                if not isinstance(result, dict):
                    raise ValueError("invalid_migrated_upload_response")
                return result
        except WebSocketException as error:
            raise ValueError("migrated_upload_incomplete") from error


def prefix_digest(handle: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        data = handle.read(min(remaining, 1024 * 1024))
        if not data:
            raise ValueError("original_migrated_input_truncated")
        digest.update(data)
        remaining -= len(data)
    return digest.hexdigest()


def check_state(state: dict[str, Any], *, product: str, operation: str, binding: str, source: dict[str, Any]) -> None:
    if (state.get("product"), state.get("operation_id"), state.get("source_binding_sha256")) != (
        product,
        operation,
        binding,
    ) or type(state.get("receipt_recorded")) is not bool:
        raise ValueError("migrated_upload_binding_changed")
    parts = state.get("parts")
    if not isinstance(parts, dict) or set(parts) != set(PARTS):
        raise ValueError("invalid_migrated_upload_response")
    for part in PARTS:
        item = parts[part]
        if (
            not isinstance(item, dict)
            or type(item.get("complete")) is not bool
            or type(item.get("size")) is not int
            or not 0 <= item["size"] <= source[part]["size"]
            or item["complete"]
            and item["size"] != source[part]["size"]
        ):
            raise ValueError("invalid_migrated_upload_response")
    if state["receipt_recorded"] and not all(parts[part]["complete"] for part in PARTS):
        raise ValueError("incomplete_migrated_receipt")


def execute(h: Any, args: argparse.Namespace) -> int:
    product, operation = (h._required_argument(args, key) for key in ("product", "operation_id"))
    if any(h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(item) is None for item in (product, operation)):
        raise ValueError("invalid_request")
    source = load_manifest(args.migrated_source_file, product=product)
    binding = h.metadata_review_digest(source)
    request = {
        "product": h.public_identifier(product),
        "operation_id": h.public_identifier(operation),
        "source_binding_sha256": binding,
    }
    paths = {part: getattr(args, part).resolve(strict=True) for part in PARTS}
    for part, path in paths.items():
        if not path.is_file() or path.stat().st_size != source[part]["size"]:
            raise ValueError("original_migrated_input_changed")
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != source[part]["sha256"]:
                raise ValueError("original_migrated_input_changed")
    settings = h.prepare_operator_settings(args=args, operation=COMMAND, request=request)
    if settings is None:
        return 2
    timeout = max(args.timeout, sum(source[part]["size"] for part in PARTS) / (4 * 1024 * 1024))
    route = h.internal_helper_path("testing-rebuild-migrated-state").format(product=product, operation_id=operation)
    try:
        state = private_request(h, settings, args.private_address, route, timeout=timeout)
        check_state(state, product=product, operation=operation, binding=binding, source=source)
        for part, path in paths.items():
            saved = state["parts"][part]
            if saved["complete"] and (state["receipt_recorded"] or part != "filestore"):
                continue
            with path.open("rb") as handle:
                prefix = prefix_digest(handle, saved["size"])
                remaining = source[part]["size"] - saved["size"]
                upload = h.internal_helper_path("testing-rebuild-migrated-upload").format(
                    product=product, operation_id=operation, part=part
                )
                state = private_send(
                    h,
                    settings,
                    args.private_address,
                    upload,
                    timeout=timeout,
                    handle=handle,
                    offset=saved["size"],
                    prefix=prefix,
                    remaining=remaining,
                )
            check_state(state, product=product, operation=operation, binding=binding, source=source)
        if not state["receipt_recorded"]:
            raise ValueError("migrated_receipt_unconfirmed")
        output = h.base_payload(status="accepted", operation=COMMAND, request=request)
        output["result"] = {
            "receipt_recorded": True,
            "parts": {part: {key: state["parts"][part][key] for key in ("complete", "size")} for part in PARTS},
        }
        output["summary"] = {
            "recommendation": "Read the original testing rebuild operation. Uploaded bytes are not a completed restore."
        }
        h.assert_public_safe_shape(output["result"])
        h.emit(output)
        return 0
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
        output = h.unavailable_payload(
            operation=COMMAND,
            request=request,
            status="unavailable",
            code="migrated_upload_incomplete",
            message="Retain the original pair. Read the original rebuild and repeat this same upload command to inspect and resume its exact prefix.",
        )
        h.emit(output)
        return 1
