# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1", "websockets==17.3"]
# ///
import argparse
import hashlib
import io
import json
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import Mock, patch

import launchplane_migrated_upload as upload
import launchplane_testing_rebuild as rebuild
import pytest
from test_launchplane_helpers import write_action as helper


class UploadFixture(NamedTuple):
    args: argparse.Namespace
    data: dict[str, bytes]
    manifest: dict[str, Any]


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


def fixture(tmp_path: Path) -> UploadFixture:
    data = {"database": b"database PRIVATE_SENTINEL", "filestore": b"filestore PRIVATE_SENTINEL"}
    manifest = {
        "kind": "migrated",
        "product": "example",
        "context": "example-testing",
        "source_backup": "PRIVATE_SENTINEL snapshot",
        "producer_run": "PRIVATE_SENTINEL run",
        "target_image": "sha256:" + "a" * 64,
        "filestore_files": 1,
        "filestore_expanded_bytes": 100,
        **{part: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()} for part, value in data.items()},
    }
    file = tmp_path / "manifest.json"
    file.write_text(json.dumps(manifest))
    for part, value in data.items():
        (tmp_path / part).write_bytes(value)
    args = argparse.Namespace(
        command=upload.COMMAND,
        product="example",
        operation_id="original",
        private_address="10.0.0.2",
        migrated_source_file=file,
        database=tmp_path / "database",
        filestore=tmp_path / "filestore",
        timeout=30,
    )
    return UploadFixture(args, data, manifest)


def state(
    manifest: dict[str, Any], data: dict[str, bytes], offsets: dict[str, int] | None = None, receipt: bool = False
) -> dict[str, Any]:
    offsets = offsets or {}
    return {
        "product": "example",
        "operation_id": "original",
        "source_binding_sha256": helper.metadata_review_digest(manifest),
        "receipt_recorded": receipt,
        "parts": {
            part: {
                "size": offsets.get(part, 0),
                "sha256": hashlib.sha256(value[: offsets.get(part, 0)]).hexdigest(),
                "complete": offsets.get(part, 0) == len(value),
            }
            for part, value in data.items()
        },
    }


def invoke(args: argparse.Namespace, transport: Callable[..., dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    output = io.StringIO()
    with (
        patch_attr(
            helper,
            "prepare_operator_settings",
            return_value={"service_url": "https://plane.example.invalid", "token": "PRIVATE_SENTINEL token"},
        ),
        patch_attr(upload, "private_request", side_effect=transport),
        patch_attr(upload, "private_send", side_effect=transport),
        redirect_stdout(output),
    ):
        result = upload.execute(helper, args)
    return result, json.loads(output.getvalue())


def test_resume_streams_only_original_remaining_bytes_and_redacts_private_intent(tmp_path: Path) -> None:
    args, data, manifest = fixture(tmp_path)
    offsets = {"database": 4}
    calls = []

    def transport(_h: Any, _settings: dict[str, str], _address: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        if "handle" in kwargs:
            part = path.rsplit("/", 1)[1]
            sent = b""
            while chunk := kwargs["handle"].read(min(3, kwargs["remaining"] - len(sent))):
                sent += chunk
            assert sent == data[part][offsets.get(part, 0) :]
            assert kwargs["offset"] == offsets.get(part, 0)
            assert kwargs["prefix"] == hashlib.sha256(data[part][: offsets.get(part, 0)]).hexdigest()
            offsets[part] = len(data[part])
        return state(manifest, data, offsets, receipt=len(offsets) == 2 and all(offsets[p] == len(data[p]) for p in data))

    code, output = invoke(args, transport)
    assert code == 0
    assert output["result"]["receipt_recorded"] is True
    assert len(calls) == 3
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_receiver_prefix_refusal_preserves_original_input_and_does_not_retry(tmp_path: Path) -> None:
    args, data, manifest = fixture(tmp_path)
    original = state(manifest, data, {"database": 4})
    transport = Mock(side_effect=[original, upload.UploadRefused("migrated_upload_refused")])
    code, output = invoke(args, transport)
    assert code == 2
    assert output["warnings"][0]["code"] == "migrated_upload_refused"
    assert "repeat" not in json.dumps(output)
    assert transport.call_count == 2
    assert args.database.read_bytes() == data["database"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_complete_bytes_with_lost_receipt_reply_replay_zero_bytes_on_original_part(tmp_path: Path) -> None:
    args, data, manifest = fixture(tmp_path)
    offsets = {p: len(v) for p, v in data.items()}
    calls = []

    def transport(_h: Any, _settings: dict[str, str], _address: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(path)
        if "handle" in kwargs:
            assert path.endswith("/filestore")
            assert kwargs["remaining"] == 0
            assert kwargs["handle"].read(100) == b""
        return state(manifest, data, offsets, receipt="handle" in kwargs)

    code, _ = invoke(args, transport)
    assert code == 0
    assert len(calls) == 2


def test_migrated_source_is_bound_to_review_without_exposing_manifest(tmp_path: Path) -> None:
    args, data, manifest = fixture(tmp_path)
    original = helper.parse_args(
        [
            rebuild.PLAN_COMMAND,
            "--product",
            "example",
            "--copy-source",
            "migrated",
            "--artifact-id",
            "current-build",
            "--source-git-ref",
            "a" * 40,
            "--reason",
            "Restore completed migrated pair",
            "--migrated-source-file",
            str(args.migrated_source_file),
        ]
    )
    request, body = rebuild.intent(helper, original)
    replacement = body["replacement"]
    assert isinstance(replacement, dict)
    assert replacement["migrated_source"] == manifest
    assert "PRIVATE_SENTINEL" not in json.dumps(request)
    manifest["producer_run"] = "changed run"
    args.migrated_source_file.write_text(json.dumps(manifest))
    changed, _ = rebuild.intent(helper, original)
    assert changed["payload_digest"] != request["payload_digest"]


def test_transport_uses_private_address_and_original_tls_hostname() -> None:
    connection = upload.PrivateHTTPSConnection("plane.example.invalid", 443, "10.0.0.2", timeout=30)
    secured = Mock()
    with (
        patch_attr(upload.socket, "create_connection", return_value=Mock()) as connect,
        patch_attr(connection.tls_context, "wrap_socket", return_value=secured) as tls,
    ):
        connection.connect()
    assert connect.call_args.args[0] == ("10.0.0.2", 443)
    assert tls.call_args.kwargs["server_hostname"] == "plane.example.invalid"
    assert connection.sock is secured


def test_websocket_stream_keeps_private_socket_tls_identity_and_exact_remaining_bytes() -> None:
    from websockets.sync import client

    prefix = b"original prefix"
    suffix = b"s" * (1024 * 1024 + 17)
    handle = io.BytesIO(prefix + suffix)
    handle.seek(len(prefix))
    websocket = Mock()
    websocket.recv.side_effect = [json.dumps({"ready": True, "offset": len(prefix)}), '{"receipt_recorded": true}']
    websocket.__enter__ = Mock(return_value=websocket)
    websocket.__exit__ = Mock(return_value=False)
    raw = Mock()
    raw.__enter__ = Mock(return_value=raw)
    raw.__exit__ = Mock(return_value=False)
    with (
        patch_attr(upload.socket, "create_connection", return_value=raw) as tcp,
        patch_attr(client, "connect", return_value=websocket) as connect,
    ):
        result = upload.private_send(
            helper,
            {"service_url": "https://plane.example.invalid", "token": "fixture"},
            "10.0.0.2",
            "/v1/fixture",
            timeout=3000,
            connect_timeout=30,
            handle=handle,
            offset=len(prefix),
            prefix=hashlib.sha256(prefix).hexdigest(),
            remaining=len(suffix),
        )
    assert tcp.call_args.args == (("10.0.0.2", 443), 30)
    assert connect.call_args.args[0] == "wss://plane.example.invalid/v1/fixture"
    options = connect.call_args.kwargs
    assert options["sock"] is raw
    assert options["open_timeout"] == 30
    assert "ping_timeout" not in options
    assert options["ssl"].check_hostname
    assert options["ssl"].verify_mode == upload.ssl.CERT_REQUIRED
    assert options["server_hostname"] == "plane.example.invalid"
    assert options["proxy"] is None
    assert options["additional_headers"] == {"Authorization": "Bearer fixture"}
    messages = [call.args[0] for call in websocket.send.call_args_list]
    assert json.loads(messages[0]) == {
        "offset": len(prefix),
        "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
        "size": len(suffix),
    }
    assert messages[1:-1] == [suffix[: 1024 * 1024], suffix[1024 * 1024 :]]
    assert json.loads(messages[-1]) == {"complete": True}
    assert result == {"receipt_recorded": True}


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::ffff:8.8.8.8", "2001::1"])
def test_public_and_special_addresses_never_receive_the_operator_connection(address: str) -> None:
    with patch_attr(upload.socket, "create_connection") as connect, pytest.raises(ValueError):
        upload.PrivateHTTPSConnection("plane.example.invalid", 443, address, timeout=30)
    connect.assert_not_called()


@pytest.mark.parametrize("content", [b"not JSON PRIVATE_SENTINEL", b"\xffPRIVATE_SENTINEL"])
def test_bad_manifest_never_exposes_private_parse_details(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "PRIVATE_SENTINEL.json"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="^invalid_migrated_manifest$"):
        upload.load_manifest(path, product="example")


def test_missing_private_files_become_fixed_input_errors(tmp_path: Path) -> None:
    args, _, _ = fixture(tmp_path)
    args.database.unlink()
    with (
        patch_attr(
            helper, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid", "token": "fixture"}
        ),
        pytest.raises(ValueError, match="^original_migrated_input_changed$"),
    ):
        upload.execute(helper, args)
    args.migrated_source_file.unlink()
    with pytest.raises(ValueError, match="^invalid_migrated_manifest$"):
        rebuild.intent(
            helper,
            helper.parse_args(
                [
                    rebuild.PLAN_COMMAND,
                    "--product",
                    "example",
                    "--copy-source",
                    "migrated",
                    "--artifact-id",
                    "current-build",
                    "--source-git-ref",
                    "a" * 40,
                    "--reason",
                    "Restore completed migrated pair",
                    "--migrated-source-file",
                    str(args.migrated_source_file),
                ]
            ),
        )


def test_transport_loss_retains_original_operation_and_allows_exact_resume(tmp_path: Path) -> None:
    args, _, _ = fixture(tmp_path)
    transport = Mock(side_effect=OSError("PRIVATE_SENTINEL"))
    code, output = invoke(args, transport)
    assert code == 1
    assert output["warnings"][0]["code"] == "migrated_upload_incomplete"
    assert transport.call_count == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_certificate_mismatch_is_a_refusal_without_resume_advice(tmp_path: Path) -> None:
    args, _, _ = fixture(tmp_path)
    code, output = invoke(args, Mock(side_effect=upload.ssl.SSLCertVerificationError("PRIVATE_SENTINEL")))
    assert code == 2
    assert output["warnings"][0]["code"] == "private_operator_certificate_invalid"
    assert "repeat" not in json.dumps(output)
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("address,url", [("8.8.8.8", "https://plane.example.invalid"), ("10.0.0.2", "http://plane.example.invalid")])
def test_invalid_connection_settings_fail_before_large_input_hashes(tmp_path: Path, address: str, url: str) -> None:
    args, _, _ = fixture(tmp_path)
    args.private_address = address
    with (
        patch_attr(helper, "prepare_operator_settings", return_value={"service_url": url, "token": "fixture"}),
        patch_attr(upload.hashlib, "file_digest") as digest,
        pytest.raises(ValueError),
    ):
        upload.execute(helper, args)
    digest.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
