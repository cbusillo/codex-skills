# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1", "websockets==17.2"]
# ///
import argparse
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import launchplane_migrated_upload as upload
import launchplane_testing_rebuild as rebuild
from test_launchplane_helpers import write_action as helper


def fixture(tmp_path):
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
    return args, data, manifest


def state(manifest, data, offsets=None, receipt=False):
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


def invoke(args, transport):
    output = io.StringIO()
    with (
        patch.object(
            helper,
            "prepare_operator_settings",
            return_value={"service_url": "https://plane.example.invalid", "token": "PRIVATE_SENTINEL token"},
        ),
        patch.object(upload, "private_request", side_effect=transport),
        patch.object(upload, "private_send", side_effect=transport),
        redirect_stdout(output),
    ):
        result = upload.execute(helper, args)
    return result, json.loads(output.getvalue())


def test_resume_streams_only_original_remaining_bytes_and_redacts_private_intent(tmp_path):
    args, data, manifest = fixture(tmp_path)
    offsets = {"database": 4}
    calls = []

    def transport(h, settings, address, path, **kwargs):
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


def test_receiver_prefix_refusal_preserves_original_input_and_does_not_retry(tmp_path):
    args, data, manifest = fixture(tmp_path)
    original = state(manifest, data, {"database": 4})
    transport = Mock(side_effect=[original, ValueError("original prefix changed")])
    code, output = invoke(args, transport)
    assert code == 1
    assert transport.call_count == 2
    assert args.database.read_bytes() == data["database"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_complete_bytes_with_lost_receipt_reply_replay_zero_bytes_on_original_part(tmp_path):
    args, data, manifest = fixture(tmp_path)
    offsets = {p: len(v) for p, v in data.items()}
    calls = []

    def transport(h, settings, address, path, **kwargs):
        calls.append(path)
        if "handle" in kwargs:
            assert path.endswith("/filestore")
            assert kwargs["remaining"] == 0
            assert kwargs["handle"].read(100) == b""
        return state(manifest, data, offsets, receipt="handle" in kwargs)

    code, _ = invoke(args, transport)
    assert code == 0
    assert len(calls) == 2


def test_migrated_source_is_bound_to_review_without_exposing_manifest(tmp_path):
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
    assert body["replacement"]["migrated_source"] == manifest
    assert "PRIVATE_SENTINEL" not in json.dumps(request)
    manifest["producer_run"] = "changed run"
    args.migrated_source_file.write_text(json.dumps(manifest))
    changed, _ = rebuild.intent(helper, original)
    assert changed["payload_digest"] != request["payload_digest"]


def test_transport_uses_private_address_and_original_tls_hostname():
    connection = upload.PrivateHTTPSConnection("plane.example.invalid", 443, "10.0.0.2", timeout=30)
    secured = Mock()
    with (
        patch.object(upload.socket, "create_connection", return_value=Mock()) as connect,
        patch.object(connection.tls_context, "wrap_socket", return_value=secured) as tls,
    ):
        connection.connect()
    assert connect.call_args.args[0] == ("10.0.0.2", 443)
    assert tls.call_args.kwargs["server_hostname"] == "plane.example.invalid"
    assert connection.sock is secured


def test_websocket_stream_keeps_private_socket_tls_identity_and_exact_remaining_bytes():
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
        patch.object(upload.socket, "create_connection", return_value=raw) as tcp,
        patch.object(client, "connect", return_value=websocket) as connect,
    ):
        result = upload.private_send(
            helper,
            {"service_url": "https://plane.example.invalid", "token": "fixture"},
            "10.0.0.2",
            "/v1/fixture",
            timeout=30,
            handle=handle,
            offset=len(prefix),
            prefix=hashlib.sha256(prefix).hexdigest(),
            remaining=len(suffix),
        )
    assert tcp.call_args.args[0] == ("10.0.0.2", 443)
    assert connect.call_args.args[0] == "wss://plane.example.invalid/v1/fixture"
    options = connect.call_args.kwargs
    assert options["sock"] is raw
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


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1"])
def test_public_and_special_addresses_never_receive_the_operator_connection(address):
    with patch.object(upload.socket, "create_connection") as connect, pytest.raises(ValueError):
        upload.PrivateHTTPSConnection("plane.example.invalid", 443, address, timeout=30)
    connect.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
