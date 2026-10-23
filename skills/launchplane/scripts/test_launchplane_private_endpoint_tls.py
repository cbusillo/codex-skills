#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography==50.0.2", "pytest==9.1.1"]
# ///
"""Qualify scoped public CA handling using disposable in-memory certificates."""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from test_launchplane_helpers import (
    _private_endpoint_payload, _private_endpoint_read, _private_endpoint_record,
    _private_endpoint_response, _reviewed_apply_argv, _run_main, _write_json,
    write_action,
)


def certificate(*, ca=True, constraints=True):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable fixture")])
    now = datetime.now(UTC)
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=1))
               .not_valid_after(now + timedelta(days=1)))
    if constraints:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=None), True)
    return (builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def endpoint(pem):
    return {**_private_endpoint_payload(), "url": "https://10.0.0.106:9443/healthz",
            "tls_ca_pem": pem}


def test_scoped_ca_review_apply_readback_and_redaction():
    pem, _ = certificate()
    other, _ = certificate()
    data = endpoint(pem)
    with TemporaryDirectory(dir=Path.home()) as directory:
        path = _write_json(directory, "endpoint.json", data)
        argv = ["private-health-endpoint-dry-run", "--payload-file", path, "--reason", "r"]
        code, evidence, posts, _ = _run_main(argv, post=_private_endpoint_response(**data))
        assert code == 0, evidence
        assert posts[0]["body"]["endpoint"]["tls_ca_pem"] == pem
        assert evidence["result"]["record"]["tls_ca_sha256"] == hashlib.sha256(pem.encode()).hexdigest()
        assert "BEGIN CERTIFICATE" not in json.dumps(evidence)
        saved = _write_json(directory, "review.json", evidence)
        apply = ["private-health-endpoint-apply", *argv[1:],
                 *_reviewed_apply_argv(evidence["result"]["plan_sha256"], saved)]
        code, result, _, _ = _run_main(
            apply, post=_private_endpoint_response("apply", **data),
            read=_private_endpoint_read(**data))
        assert code == 0, result
        assert result["result"]["read_back_matches"] is True
        assert "BEGIN CERTIFICATE" not in json.dumps(result)

        # An applied or independently stored different issuer cannot verify this review.
        for changed in ({**data, "tls_ca_pem": other}, {k: v for k, v in data.items() if k != "tls_ca_pem"}):
            for post_data, read_data in ((changed, data), (data, changed)):
                code, result, _, _ = _run_main(
                    apply, post=_private_endpoint_response("apply", **post_data),
                    read=_private_endpoint_read(**read_data))
                assert (code, result["status"]) == (1, "accepted_unverified")
                assert "BEGIN CERTIFICATE" not in json.dumps(result)

        # Changing the reviewed CA bytes or removing scoped trust refuses before POST.
        for changed in ({**data, "tls_ca_pem": other}, {k: v for k, v in data.items() if k != "tls_ca_pem"}):
            _write_json(directory, "endpoint.json", changed)
            code, _, posts, _ = _run_main(apply, post=_private_endpoint_response("apply", **changed))
            assert code == 2 and not posts


def test_ca_bundle_record_read_is_redacted_and_unknown_fields_still_refuse():
    first, _ = certificate()
    second, _ = certificate()
    data = endpoint(first + second)
    response = {"status": "ok", "trace_id": "launchplane_req_fixture", "product": "example",
                "context": "example", "instance": "testing", "limit": 25, "count": 1,
                "records": [_private_endpoint_record(**data)]}
    argv = ["private-health-endpoint-read", "--product", "example", "--context", "example", "--instance", "testing"]
    code, result, _, _ = _run_main(argv, read=response)
    assert code == 0, result
    assert "BEGIN CERTIFICATE" not in json.dumps(result)
    response["records"][0]["tls_private_key"] = "unexpected"
    code, _, _, _ = _run_main(argv, read=response)
    assert code != 0


@pytest.mark.parametrize("case", ["prod", "http", "malformed", "leaf", "missing-constraints", "key", "mixed", "oversized", "non-string"])
def test_invalid_scoped_trust_refuses_input_and_readback(case):
    pem, key = certificate()
    data = endpoint(pem)
    if case == "prod":
        data["instance"] = "prod"
    elif case == "http":
        data["url"] = data["url"].replace("https:", "http:")
    else:
        data["tls_ca_pem"] = {
            "malformed": "not a certificate", "leaf": certificate(ca=False)[0],
            "missing-constraints": certificate(constraints=False)[0], "key": key,
            "mixed": pem + key,
            "oversized": pem * (write_action.PRIVATE_HEALTH_ENDPOINT_MAX_CA_LENGTH // len(pem) + 1),
            "non-string": [pem],
        }[case]
    with TemporaryDirectory(dir=Path.home()) as directory:
        path = _write_json(directory, "bad.json", data)
        code, result, posts, _ = _run_main(
            ["private-health-endpoint-dry-run", "--payload-file", path, "--reason", "r"],
            post=_private_endpoint_response(**data))
        assert code == 2 and not posts
        assert "BEGIN" not in json.dumps(result)
    with pytest.raises(write_action.LaunchplaneSafetyError):
        write_action._project_private_health_endpoint_record(_private_endpoint_record(**data))


def test_missing_ca_parser_returns_structured_refusal_before_http():
    pem, _ = certificate()
    real_import = __import__

    def without_crypto(name, *args, **kwargs):
        if name == "cryptography":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    with TemporaryDirectory(dir=Path.home()) as directory:
        path = _write_json(directory, "endpoint.json", endpoint(pem))
        with patch("builtins.__import__", side_effect=without_crypto):
            code, result, posts, _ = _run_main(
                ["private-health-endpoint-dry-run", "--payload-file", path, "--reason", "r"],
                post=_private_endpoint_response(**endpoint(pem)))
        assert code == 2 and not posts
        assert result["warnings"][0]["code"] == "private_health_endpoint_tls_ca_dependency_unavailable"
        assert "BEGIN CERTIFICATE" not in json.dumps(result)


def test_duplicate_ca_extension_returns_structured_refusal_before_http():
    pem, _ = certificate()

    def encode(tag, content):
        size = len(content)
        length = bytes([size]) if size < 128 else bytes([128 + (size.bit_length() + 7) // 8]) + size.to_bytes((size.bit_length() + 7) // 8)
        return bytes([tag]) + length + content

    def children(data):
        while data:
            width = data[1] & 127 if data[1] & 128 else 0
            start = 2 + width
            size = int.from_bytes(data[2:start]) if width else data[1]
            end = start + size
            yield data[0], data[start:end]
            data = data[end:]

    # Duplicate an actual extension in a disposable certificate's DER, retaining
    # the surrounding certificate structure. Parsing extensions must fail safely.
    cert = x509.load_pem_x509_certificate(pem.encode())
    _, content = next(children(cert.public_bytes(serialization.Encoding.DER)))
    fields = list(children(content))
    tbs = []
    for tag, value in children(fields[0][1]):
        if tag == 0xA3:
            seq_tag, extensions = next(children(value))
            value = encode(seq_tag, extensions + extensions)
        tbs.append(encode(tag, value))
    der = encode(0x30, encode(fields[0][0], b"".join(tbs)) + b"".join(encode(*field) for field in fields[1:]))
    bad_pem = "-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(der).decode() + "-----END CERTIFICATE-----\n"
    with pytest.raises(x509.DuplicateExtension):
        _ = x509.load_pem_x509_certificate(bad_pem.encode()).extensions
    with TemporaryDirectory(dir=Path.home()) as directory:
        path = _write_json(directory, "endpoint.json", endpoint(bad_pem))
        code, result, posts, _ = _run_main(
            ["private-health-endpoint-dry-run", "--payload-file", path, "--reason", "r"],
            post=_private_endpoint_response(**endpoint(bad_pem)))
        assert code == 2 and not posts
        assert "BEGIN CERTIFICATE" not in json.dumps(result)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
