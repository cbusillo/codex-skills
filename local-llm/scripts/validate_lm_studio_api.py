#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Offline validation for LM Studio API helper semantics."""

from __future__ import annotations

from lm_studio_api import (
    LocalLLMError,
    derive_lm_studio_native_base_url,
    load_lm_studio_model,
    normalize_endpoint,
    public_endpoint,
    resolve_endpoint,
    unload_lm_studio_model,
)


def main() -> int:
    test_cli_base_url_provider_inference()
    test_native_url_derivation()
    test_public_redaction()
    test_unload_requires_instance_id()
    test_native_load_omits_ttl()
    print("ok: lm_studio_api offline validation passed")
    return 0


def test_cli_base_url_provider_inference() -> None:
    default_endpoint = resolve_endpoint({}, None, None)
    assert default_endpoint["provider"] == "lm_studio"
    assert default_endpoint["native_base_url"] == "http://127.0.0.1:1234/api/v1"

    lm_studio_override = resolve_endpoint({}, None, "http://127.0.0.1:1234/v1")
    assert lm_studio_override["provider"] == "lm_studio"
    assert lm_studio_override["locality"] == "localhost"
    assert lm_studio_override["native_base_url"] == "http://127.0.0.1:1234/api/v1"

    generic_override = resolve_endpoint({}, None, "http://localhost:8000/v1")
    assert generic_override["provider"] == "openai_compatible"
    assert generic_override["locality"] == "localhost"
    assert "native_base_url" not in generic_override


def test_native_url_derivation() -> None:
    cases = {
        "http://127.0.0.1:1234/v1": "http://127.0.0.1:1234/api/v1",
        "http://127.0.0.1:1234/prefix/v1": "http://127.0.0.1:1234/prefix/api/v1",
        "http://127.0.0.1:1234": "http://127.0.0.1:1234/api/v1",
    }
    for source, expected in cases.items():
        actual = derive_lm_studio_native_base_url(source)
        assert actual == expected, f"{source}: expected {expected}, got {actual}"


def test_public_redaction() -> None:
    endpoint = normalize_endpoint(
        {
            "id": "private-lan",
            "provider": "lm_studio",
            "base_url": "http://example-lmstudio.local:1234/v1",
            "locality": "trusted_lan",
            "trust": "private_local",
            "token_env": "EXAMPLE_TOKEN",
        }
    )
    public = public_endpoint(endpoint)
    assert public["base_url"] == "[redacted:trusted_lan]"
    assert public["native_base_url"] == "[redacted:trusted_lan]"
    assert public["uses_token_env"] is True


def test_unload_requires_instance_id() -> None:
    endpoint = normalize_endpoint({"provider": "lm_studio", "base_url": "http://127.0.0.1:1234/v1"})
    try:
        unload_lm_studio_model(endpoint, " ", 1)
    except LocalLLMError as exc:
        assert "instance_id is required" in str(exc)
    else:
        raise AssertionError("blank instance_id should fail before any HTTP request")


def test_native_load_omits_ttl() -> None:
    endpoint = normalize_endpoint({"provider": "lm_studio", "base_url": "http://127.0.0.1:1234/v1"})
    captured: dict[str, object] = {}
    original = load_lm_studio_model.__globals__["post_json"]

    def fake_post_json(url: str, payload: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
        captured["url"] = url
        captured["payload"] = payload
        return {"status": "loaded", "instance_id": "example"}

    load_lm_studio_model.__globals__["post_json"] = fake_post_json
    try:
        load_lm_studio_model(endpoint, "example-model", 1, context_length=8192, flash_attention=True, ttl=300)
    finally:
        load_lm_studio_model.__globals__["post_json"] = original
    payload = captured.get("payload")
    if not isinstance(payload, dict):
        raise AssertionError("native load payload was not captured")
    assert payload == {"model": "example-model", "context_length": 8192, "flash_attention": True}, payload


if __name__ == "__main__":
    raise SystemExit(main())
