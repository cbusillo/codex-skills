#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Offline validation for LM Studio API helper semantics."""

from __future__ import annotations

import argparse
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

from lm_studio_api import (
    LocalLLMError,
    derive_lm_studio_native_base_url,
    load_lm_studio_model,
    normalize_endpoint,
    public_endpoint,
    resolve_endpoint,
    unload_lm_studio_model,
)
from lm_studio_chat import LocalLLMChatError, chat


def main() -> int:
    test_cli_base_url_provider_inference()
    test_native_url_derivation()
    test_public_redaction()
    test_unload_requires_instance_id()
    test_native_load_omits_ttl()
    test_explicit_chat_verifies_config_and_retains_usage()
    test_explicit_chat_rejects_unverified_config_before_private_prompt()
    test_explicit_chat_rejects_invalid_requested_config_before_load()
    test_explicit_chat_requires_usable_instance_id()
    test_explicit_chat_requires_typed_loaded_status()
    test_explicit_chat_cleans_up_after_warmup_failure()
    test_explicit_chat_retains_usage_after_empty_content()
    test_explicit_chat_preserves_cleanup_on_request_error()
    test_explicit_chat_reports_unload_only_failure()
    test_explicit_chat_reports_request_and_cleanup_errors()
    test_explicit_chat_wraps_raw_load_transport_error()
    test_explicit_chat_wraps_raw_transport_error()
    test_explicit_chat_preserves_raw_primary_and_cleanup_errors()
    test_explicit_chat_retains_usage_after_raw_cleanup_error()
    test_chat_omits_absent_usage()
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
    def fake_post_json(
        url: str, request_payload: dict[str, object], *_args: object, **_kwargs: object
    ) -> dict[str, object]:
        captured["url"] = url
        captured["payload"] = request_payload
        return {"status": "loaded", "instance_id": "example"}

    with mock.patch("lm_studio_api.post_json", side_effect=fake_post_json):
        load_lm_studio_model(endpoint, "example-model", 1, context_length=8192, flash_attention=True, ttl=300)
    payload = captured.get("payload")
    if not isinstance(payload, dict):
        raise AssertionError("native load payload was not captured")
    assert payload == {"model": "example-model", "context_length": 8192, "flash_attention": True}, payload


def test_explicit_chat_verifies_config_and_retains_usage() -> None:
    private_prompt = "synthetic private prompt success"
    with fake_endpoint("success") as (endpoint, observed):
        result = chat(endpoint, "example-model", private_prompt, "system", chat_args(warmup=True), {})

    assert result["ok"] is True
    assert result["usage"] == {
        "prompt_tokens": 21,
        "completion_tokens": 8,
        "total_tokens": 29,
        "prompt_tokens_details": {"cached_tokens": 13},
        "completion_tokens_details": {"reasoning_tokens": 5},
    }
    lifecycle = result["lifecycle"]
    assert lifecycle["load_config_verification"]["status"] == "verified"
    assert lifecycle["warmup_usage"] == {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}
    assert lifecycle["unload_response"] == {"status": "unloaded"}
    assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]
    chat_prompts = observed["chat_prompts"]
    assert chat_prompts == ["Reply with exactly: OK", private_prompt]
    chat_models = [
        request["payload"]["model"]
        for request in observed["requests"]
        if request["path"] == "/v1/chat/completions"
    ]
    assert chat_models == ["instance-fixture", "instance-fixture"]
    assert result["model"] == "example-model"
    assert result["served_model"] == "example-model"


def test_explicit_chat_rejects_unverified_config_before_private_prompt() -> None:
    private_prompt = "synthetic private prompt must remain local"
    for mode, expected_status in (
        ("mismatch", "mismatch"),
        ("wrong_type", "mismatch"),
        ("null_value", "mismatch"),
        ("null_config", "unknown"),
        ("missing_config", "unknown"),
    ):
        with fake_endpoint(mode) as (endpoint, observed):
            try:
                chat(endpoint, "example-model", private_prompt, "system", chat_args(warmup=True), {})
            except LocalLLMChatError as exc:
                lifecycle = exc.lifecycle
            else:
                raise AssertionError(f"{mode} load config should fail before chat")
        assert lifecycle["load_config_verification"]["status"] == expected_status
        assert observed["chat_prompts"] == []
        assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]
        assert private_prompt not in json.dumps(observed["requests"])


def test_explicit_chat_rejects_invalid_requested_config_before_load() -> None:
    with fake_endpoint("success") as (endpoint, observed):
        try:
            chat(endpoint, "example-model", "private invalid-config prompt", "system", chat_args(context_length=0), {})
        except LocalLLMChatError as exc:
            assert "context_length must be positive" in str(exc)
        else:
            raise AssertionError("invalid requested context length should fail before load")
    assert "load_payload" not in observed
    assert observed["chat_prompts"] == []
    assert observed["unload_payloads"] == []


def test_explicit_chat_requires_usable_instance_id() -> None:
    for mode, expected_error in (
        ("missing_instance", "usable instance_id"),
        ("padded_instance", "surrounding whitespace"),
    ):
        with fake_endpoint(mode) as (endpoint, observed):
            try:
                chat(endpoint, "example-model", "private identity prompt", "system", chat_args(), {})
            except LocalLLMChatError as exc:
                assert expected_error in str(exc)
                assert exc.lifecycle["instance_id_verified"] is False
                assert exc.lifecycle["cleanup_skipped"] == {
                    "reason": "load response did not provide a usable instance_id",
                    "loaded_state": "unknown",
                }
            else:
                raise AssertionError(f"{mode} load instance_id should fail before chat")
        assert observed["chat_prompts"] == []
        assert observed["unload_payloads"] == []


def test_explicit_chat_requires_typed_loaded_status() -> None:
    private_prompt = "private load-status prompt"
    for mode in ("wrong_status_type", "missing_status"):
        with fake_endpoint(mode) as (endpoint, observed):
            try:
                chat(endpoint, "example-model", private_prompt, "system", chat_args(warmup=True), {})
            except LocalLLMChatError as exc:
                assert "typed status=loaded" in str(exc)
                assert exc.lifecycle["load_status_verification"]["verified"] is False
            else:
                raise AssertionError(f"{mode} should fail before chat")
        assert observed["chat_prompts"] == []
        assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]
        assert private_prompt not in json.dumps(observed["requests"])


def test_explicit_chat_cleans_up_after_warmup_failure() -> None:
    private_prompt = "private prompt after failed warmup"
    with fake_endpoint("warmup_error") as (endpoint, observed):
        try:
            chat(endpoint, "example-model", private_prompt, "system", chat_args(warmup=True), {})
        except LocalLLMChatError as exc:
            assert "warmup chat request failed" in str(exc)
            assert exc.lifecycle["unload_response"] == {"status": "unloaded"}
        else:
            raise AssertionError("warmup HTTP error should fail")
    assert observed["chat_prompts"] == ["Reply with exactly: OK"]
    assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]
    assert private_prompt not in json.dumps(observed["requests"])


def test_explicit_chat_retains_usage_after_empty_content() -> None:
    with fake_endpoint("empty_content") as (endpoint, observed):
        try:
            chat(endpoint, "example-model", "empty-content prompt", "system", chat_args(), {})
        except LocalLLMChatError as exc:
            assert "no assistant content" in str(exc)
            assert exc.usage is not None
            assert exc.usage["completion_tokens_details"] == {"reasoning_tokens": 5}
            assert exc.lifecycle["unload_response"] == {"status": "unloaded"}
        else:
            raise AssertionError("empty assistant content should fail")
    assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]


def test_explicit_chat_preserves_cleanup_on_request_error() -> None:
    with fake_endpoint("chat_error") as (endpoint, observed):
        try:
            chat(endpoint, "example-model", "private failing prompt", "system", chat_args(), {})
        except LocalLLMChatError as exc:
            assert "chat request failed" in str(exc)
            assert exc.lifecycle["unload_response"] == {"status": "unloaded"}
        else:
            raise AssertionError("chat HTTP error should fail")
    assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]

    with fake_endpoint("load_error") as (endpoint, observed):
        try:
            chat(endpoint, "example-model", "private load-error prompt", "system", chat_args(), {})
        except LocalLLMChatError as exc:
            assert "model load failed" in str(exc)
        else:
            raise AssertionError("load HTTP error should fail")
    assert observed["chat_prompts"] == []
    assert observed["unload_payloads"] == []


def test_explicit_chat_reports_request_and_cleanup_errors() -> None:
    with fake_endpoint("chat_and_unload_error") as (endpoint, _observed):
        try:
            chat(endpoint, "example-model", "private dual-error prompt", "system", chat_args(), {})
        except LocalLLMChatError as exc:
            message = str(exc)
            assert "chat request failed" in message
            assert "cleanup also failed" in message
            assert "model unload failed" in message
        else:
            raise AssertionError("request and cleanup errors should both be reported")


def test_explicit_chat_reports_unload_only_failure() -> None:
    with fake_endpoint("unload_error") as (endpoint, _observed):
        try:
            chat(endpoint, "example-model", "cleanup-only prompt", "system", chat_args(), {})
        except LocalLLMChatError as exc:
            assert "model unload failed" in str(exc)
            assert exc.lifecycle["unload_error"] == str(exc)
            assert exc.usage is not None
            assert exc.usage["total_tokens"] == 29
        else:
            raise AssertionError("unload failure should fail a requested-cleanup run")


def test_explicit_chat_wraps_raw_load_transport_error() -> None:
    transport_error = IncompleteRead(b"partial", 7)
    with fake_endpoint("success") as (endpoint, observed):
        with mock.patch("lm_studio_chat.load_lm_studio_model", side_effect=transport_error):
            try:
                chat(endpoint, "example-model", "raw load prompt", "system", chat_args(), {})
            except LocalLLMChatError as exc:
                assert exc.__cause__ is transport_error
                assert exc.lifecycle["load_policy"] == "api_explicit"
                assert "cleanup_skipped" not in exc.lifecycle
            else:
                raise AssertionError("raw load transport error should be wrapped")
    assert observed["requests"] == []


def test_explicit_chat_wraps_raw_transport_error() -> None:
    transport_error = IncompleteRead(b"partial", 7)
    with fake_endpoint("success") as (endpoint, observed):
        with mock.patch("lm_studio_chat.post_json", side_effect=transport_error):
            try:
                chat(endpoint, "example-model", "raw transport prompt", "system", chat_args(), {})
            except LocalLLMChatError as exc:
                assert exc.__cause__ is transport_error
                assert exc.lifecycle["unload_response"] == {"status": "unloaded"}
                assert exc.usage is None
            else:
                raise AssertionError("raw transport error should be wrapped")
    assert observed["unload_payloads"] == [{"instance_id": "instance-fixture"}]


def test_explicit_chat_preserves_raw_primary_and_cleanup_errors() -> None:
    transport_error = IncompleteRead(b"partial", 7)
    cleanup_error = OSError("synthetic raw cleanup failure")
    with fake_endpoint("success") as (endpoint, _observed):
        with (
            mock.patch("lm_studio_chat.post_json", side_effect=transport_error),
            mock.patch("lm_studio_chat.unload_lm_studio_model", side_effect=cleanup_error),
        ):
            try:
                chat(endpoint, "example-model", "dual raw failure prompt", "system", chat_args(), {})
            except LocalLLMChatError as exc:
                assert exc.__cause__ is transport_error
                assert "cleanup also failed: synthetic raw cleanup failure" in str(exc)
                assert exc.lifecycle["unload_error"] == "synthetic raw cleanup failure"
            else:
                raise AssertionError("dual raw failures should be wrapped together")


def test_explicit_chat_retains_usage_after_raw_cleanup_error() -> None:
    cleanup_error = OSError("synthetic post-success cleanup failure")
    with fake_endpoint("success") as (endpoint, _observed):
        with mock.patch("lm_studio_chat.unload_lm_studio_model", side_effect=cleanup_error):
            try:
                chat(endpoint, "example-model", "post-success cleanup prompt", "system", chat_args(), {})
            except LocalLLMChatError as exc:
                assert exc.__cause__ is cleanup_error
                assert exc.usage is not None
                assert exc.usage["total_tokens"] == 29
                assert exc.lifecycle["unload_error"] == "synthetic post-success cleanup failure"
            else:
                raise AssertionError("raw cleanup failure should preserve completed chat usage")


def test_chat_omits_absent_usage() -> None:
    with fake_endpoint("no_usage") as (endpoint, _observed):
        result = chat(endpoint, "example-model", "usage-free prompt", "system", chat_args(warmup=False), {})
    assert "usage" not in result
    assert "warmup_usage" not in result["lifecycle"]


def chat_args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "max_tokens": None,
        "timeout": 2,
        "temperature": 0,
        "load_policy": "api_explicit",
        "ttl": None,
        "context_length": 32768,
        "flash_attention": True,
        "warmup": False,
        "unload_after": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@contextmanager
def fake_endpoint(mode: str) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    observed: dict[str, Any] = {
        "mode": mode,
        "requests": [],
        "chat_prompts": [],
        "unload_payloads": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode())
            observed["requests"].append({"path": self.path, "payload": payload})
            if self.path == "/api/v1/models/load":
                observed["load_payload"] = payload
                if mode == "load_error":
                    self.reply(500, {"error": {"message": "synthetic load failure"}})
                    return
                effective: Any = {
                    "context_length": payload.get("context_length"),
                    "flash_attention": payload.get("flash_attention"),
                }
                if mode == "mismatch":
                    effective = {"context_length": 262144, "flash_attention": True, "parallel": 4}
                elif mode == "wrong_type":
                    effective = {"context_length": "32768", "flash_attention": True}
                elif mode == "null_value":
                    effective = {"context_length": None, "flash_attention": True}
                response: dict[str, Any] = {"status": "loaded", "instance_id": "instance-fixture"}
                if mode == "missing_instance":
                    response["instance_id"] = " "
                elif mode == "padded_instance":
                    response["instance_id"] = " instance-fixture "
                if mode == "wrong_status_type":
                    response["status"] = True
                elif mode == "missing_status":
                    del response["status"]
                if mode == "null_config":
                    response["load_config"] = None
                elif mode != "missing_config":
                    response["load_config"] = effective
                self.reply(200, response)
                return
            if self.path == "/v1/chat/completions":
                prompt = payload["messages"][-1]["content"]
                observed["chat_prompts"].append(prompt)
                if mode in {"chat_error", "chat_and_unload_error"} or (
                    mode == "warmup_error" and prompt == "Reply with exactly: OK"
                ):
                    self.reply(500, {"error": {"message": "synthetic chat failure"}})
                    return
                warmup = prompt == "Reply with exactly: OK"
                response: dict[str, Any] = {
                    "model": "example-model",
                    "choices": [{"message": {"content": "" if mode == "empty_content" else "OK"}}],
                }
                if mode != "no_usage":
                    response["usage"] = (
                        {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}
                        if warmup
                        else {
                            "prompt_tokens": 21,
                            "completion_tokens": 8,
                            "total_tokens": 29,
                            "prompt_tokens_details": {"cached_tokens": 13},
                            "completion_tokens_details": {"reasoning_tokens": 5},
                        }
                    )
                self.reply(200, response)
                return
            if self.path == "/api/v1/models/unload":
                observed["unload_payloads"].append(payload)
                if mode in {"chat_and_unload_error", "unload_error"}:
                    self.reply(500, {"error": {"message": "synthetic unload failure"}})
                else:
                    self.reply(200, {"status": "unloaded"})
                return
            self.reply(404, {"error": {"message": "unknown synthetic route"}})

        def reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    def handler_factory(
        request: Any, client_address: Any, fixture_server: ThreadingHTTPServer
    ) -> BaseHTTPRequestHandler:
        return Handler(request, client_address, fixture_server)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    endpoint = normalize_endpoint(
        {"id": "synthetic", "provider": "lm_studio", "base_url": f"http://127.0.0.1:{port}/v1"}
    )
    try:
        yield endpoint, observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
