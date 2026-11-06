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
from urllib.parse import SplitResult, urlsplit

COMMAND = "testing-rebuild-migrated-upload"
PARTS = ("database", "filestore")
SHA256 = re.compile(r"[a-f0-9]{64}")


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(COMMAND, help="Stream the original completed migrated pair into its existing testing rebuild.")
    parser.add_argument(
        "--restart-part",
        choices=PARTS,
        help="Restart one incomplete part from the verified original; omit this flag when resuming an interruption.",
    )
    for name in ("product", "operation-id", "private-address"):
        parser.add_argument("--" + name, required=True)
    for name in ("migrated-source-file", "database", "filestore"):
        parser.add_argument("--" + name, required=True, type=Path)


def read_manifest(path: Path, *, product: str) -> dict[str, Any]:
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


def load_manifest(path: Path, *, product: str) -> dict[str, Any]:
    try:
        return read_manifest(path, product=product)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_migrated_manifest") from error


class UploadRefused(ValueError):
    pass


def validated_private_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise UploadRefused("private_operator_address_required") from error
    address = getattr(address, "ipv4_mapped", None) or address
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    if not any(address.version == network.version and address in network for network in map(ipaddress.ip_network, networks)):
        raise UploadRefused("private_operator_address_required")
    return str(address)


def private_tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class PrivateHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, private_address: str, *, timeout: float) -> None:
        self.private_address = validated_private_address(private_address)
        self.tls_context = private_tls_context()
        super().__init__(hostname, port, timeout=timeout, context=self.tls_context, blocksize=1024 * 1024)

    def connect(self) -> None:
        self.sock = socket.create_connection((self.private_address, self.port), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.sock.close()
            raise


def private_origin(h: Any, settings: dict[str, str], path: str) -> tuple[SplitResult, str]:
    origin = urlsplit(str(h.build_url(settings["service_url"], path)))
    hostname = origin.hostname
    if origin.scheme != "https" or not hostname:
        raise UploadRefused("private_operator_https_required")
    return origin, hostname


def private_request(
    h: Any,
    settings: dict[str, str],
    address: str,
    path: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    origin, hostname = private_origin(h, settings, path)
    connection = PrivateHTTPSConnection(hostname, origin.port or 443, address, timeout=timeout)
    try:
        connection.request(
            "GET",
            origin.path,
            headers={"Accept": "application/json", "Authorization": "Bearer " + settings["token"]},
        )
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if response.status >= 500:
            raise OSError("migrated_upload_unavailable")
        if response.status != 200:
            raise UploadRefused("authorization_denied" if response.status in {401, 403} else "migrated_upload_refused")
        if len(content) > 1024 * 1024:
            raise UploadRefused("invalid_migrated_upload_response")
        try:
            result = json.loads(content)
        except ValueError as error:
            raise UploadRefused("invalid_migrated_upload_response") from error
        if not isinstance(result, dict):
            raise UploadRefused("invalid_migrated_upload_response")
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
    connect_timeout: float,
    handle: BinaryIO,
    offset: int,
    prefix: str,
    remaining: int,
) -> dict[str, Any]:
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, WebSocketException
    from websockets.sync.client import connect

    origin, hostname = private_origin(h, settings, path)
    target_address = validated_private_address(address)
    tls_context = private_tls_context()
    logger = logging.Logger("migrated-upload")
    logger.disabled = True
    with socket.create_connection((target_address, origin.port or 443), connect_timeout) as raw:
        try:
            with connect(
                origin._replace(scheme="wss").geturl(),
                sock=raw,
                ssl=tls_context,
                server_hostname=hostname,
                additional_headers={"Authorization": "Bearer " + settings["token"]},
                proxy=None,
                compression=None,
                logger=logger,
                open_timeout=connect_timeout,
                legacy=True,
            ) as websocket:
                websocket.socket.settimeout(max(connect_timeout, 60))
                websocket.send(json.dumps({"offset": offset, "prefix_sha256": prefix}))
                if json.loads(websocket.recv(timeout=timeout)) != {"ready": True, "offset": offset}:
                    raise UploadRefused("migrated_prefix_unconfirmed")
                while remaining:
                    data = handle.read(min(remaining, 1024 * 1024))
                    if not data:
                        raise UploadRefused("original_migrated_input_truncated")
                    websocket.send(data)
                    remaining -= len(data)
                websocket.send('{"complete": true}')
                result = json.loads(websocket.recv(timeout=timeout))
                if not isinstance(result, dict):
                    raise ValueError("invalid_migrated_upload_response")
                return result
        except InvalidStatus as error:
            if error.response.status_code >= 500:
                raise OSError("migrated_upload_unavailable") from error
            raise UploadRefused("migrated_upload_refused") from error
        except ConnectionClosed as error:
            if error.rcvd is not None and error.rcvd.code == 1008:
                raise UploadRefused("migrated_upload_refused") from error
            raise OSError("migrated_upload_incomplete") from error
        except InvalidHandshake as error:
            raise OSError("migrated_upload_incomplete") from error
        except WebSocketException as error:
            raise UploadRefused("migrated_websocket_refused") from error


def prefix_digest(handle: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        data = handle.read(min(remaining, 1024 * 1024))
        if not data:
            raise UploadRefused("original_migrated_input_truncated")
        digest.update(data)
        remaining -= len(data)
    return digest.hexdigest()


def check_state(state: dict[str, Any], *, product: str, operation: str, binding: str, source: dict[str, Any]) -> None:
    if (state.get("product"), state.get("operation_id"), state.get("source_binding_sha256")) != (
        product,
        operation,
        binding,
    ) or type(state.get("receipt_recorded")) is not bool:
        raise UploadRefused("migrated_upload_binding_changed")
    parts = state.get("parts")
    if not isinstance(parts, dict) or set(parts) != set(PARTS):
        raise UploadRefused("invalid_migrated_upload_response")
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
            raise UploadRefused("invalid_migrated_upload_response")
    if state["receipt_recorded"] and not all(parts[part]["complete"] for part in PARTS):
        raise UploadRefused("incomplete_migrated_receipt")


def execute(h: Any, args: argparse.Namespace) -> int:
    product, operation = (str(getattr(args, key, "") or "").strip() for key in ("product", "operation_id"))
    if any(h.PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(item) is None for item in (product, operation)):
        raise ValueError("invalid_request")
    source = load_manifest(args.migrated_source_file, product=product)
    binding = h.metadata_review_digest(source)
    request = {
        "product": h.public_identifier(product),
        "operation_id": h.public_identifier(operation),
        "source_binding_sha256": binding,
    }
    settings = h.prepare_operator_settings(args=args, operation=COMMAND, request=request)
    if settings is None:
        return 2
    route = h.internal_helper_path("testing-rebuild-migrated-state").format(product=product, operation_id=operation)
    validated_private_address(args.private_address)
    private_origin(h, settings, route)
    try:
        paths = {part: getattr(args, part).resolve(strict=True) for part in PARTS}
        for part, path in paths.items():
            if not path.is_file() or path.stat().st_size != source[part]["size"]:
                raise ValueError("original_migrated_input_changed")
            with path.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != source[part]["sha256"]:
                    raise ValueError("original_migrated_input_changed")
    except OSError as error:
        raise ValueError("original_migrated_input_changed") from error
    timeout = max(args.timeout, sum(source[part]["size"] for part in PARTS) / (4 * 1024 * 1024))
    try:
        state = private_request(h, settings, args.private_address, route, timeout=args.timeout)
        check_state(state, product=product, operation=operation, binding=binding, source=source)
        for part, path in paths.items():
            saved = state["parts"][part]
            restart = getattr(args, "restart_part", None) == part
            if restart and saved["complete"]:
                raise UploadRefused("migrated_artifact_already_complete")
            offset = 0 if restart else int(saved["size"])
            if saved["complete"] and (state["receipt_recorded"] or part != "filestore"):
                continue
            with path.open("rb") as handle:
                prefix = prefix_digest(handle, offset)
                remaining = source[part]["size"] - offset
                upload = h.internal_helper_path("testing-rebuild-migrated-upload").format(
                    product=product, operation_id=operation, part=part
                )
                state = private_send(
                    h,
                    settings,
                    args.private_address,
                    upload,
                    timeout=timeout,
                    connect_timeout=args.timeout,
                    handle=handle,
                    offset=offset,
                    prefix=prefix,
                    remaining=remaining,
                )
            check_state(state, product=product, operation=operation, binding=binding, source=source)
        if not state["receipt_recorded"]:
            raise UploadRefused("migrated_receipt_unconfirmed")
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
    except ssl.SSLCertVerificationError:
        output = h.unavailable_payload(
            operation=COMMAND,
            request=request,
            status="refused",
            code="private_operator_certificate_invalid",
            message="Retain the original operation and verify the documented ingress address and TLS hostname before another upload.",
        )
        output["summary"] = {"recommendation": "Inspect the private ingress certificate and original TLS hostname."}
        h.emit(output)
        return 2
    except ValueError as error:
        output = h.unavailable_payload(
            operation=COMMAND,
            request=request,
            status="refused",
            code=str(error) if isinstance(error, UploadRefused) else "invalid_migrated_upload_response",
            message="Retain the original pair and operation. Inspect its state, original intent and connection settings before another upload.",
        )
        output["summary"] = {"recommendation": "Inspect the original intent and private connection settings."}
        h.emit(output)
        return 2
    except (OSError, http.client.HTTPException):
        output = h.unavailable_payload(
            operation=COMMAND,
            request=request,
            status="unavailable",
            code="migrated_upload_incomplete",
            message="Retain the original pair. Read the original rebuild and repeat this upload command without --restart-part to inspect and resume its exact prefix.",
        )
        h.emit(output)
        return 1
