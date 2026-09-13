#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run an explicitly selected Codex host/model against a trusted Responses endpoint.

Only the child process receives the temporary homes and filtered environment.
Timeout cleanup covers the owned POSIX process group, not detached services.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from lm_studio_api import (
    DEFAULT_CONFIG,
    LOCALITIES,
    LocalLLMError,
    load_yaml,
    public_base_url,
    resolve_endpoint,
    resolve_role,
    role_model,
)


HOST_VERSION_PREFIXES = {"codex": "codex-cli ", "codex-lab": "codex-lab "}
TOKEN_KEY = "LOCAL_CODEX_AGENT_ENDPOINT_TOKEN"
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_PROMPT_BYTES = 4 * 1024 * 1024
PROBE_SECONDS = 5.0
CLEANUP_SECONDS = 0.5
RUNTIME_ENV_KEYS = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
}


class LocalCodexAgentError(RuntimeError):
    pass


class RunInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="Prompt text; omitted or '-' reads stdin.")
    parser.add_argument("--host", required=True, choices=tuple(HOST_VERSION_PREFIXES))
    parser.add_argument("--host-bin", help="Executable for the selected host (default: its name on PATH).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Private local-llm YAML config.")
    endpoint = parser.add_mutually_exclusive_group()
    endpoint.add_argument("--endpoint", help="Endpoint id in private config.")
    endpoint.add_argument("--base-url", help="Explicit loopback Responses API base URL; LAN URLs require config.")
    parser.add_argument("--role", help="Role in private config; the public model index is not consulted.")
    parser.add_argument("--model", help="Explicit local model id; overrides the selected role's model.")
    parser.add_argument("--workdir", "-C", type=Path, default=Path.cwd())
    parser.add_argument("--sandbox", default="read-only", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--max-seconds", type=float, default=120, help="Agent execution deadline (default: 120s); wrapper enforced.")
    parser.add_argument("--context-window", type=int, help="Explicit context metadata; no wrapper default.")
    parser.add_argument("--output-last-message", type=Path, help="Write the validated final message here on success.")
    parser.add_argument("--json", action="store_true", help="Emit captured exec JSONL instead of only the final message.")
    args = parser.parse_args(argv)
    if not args.model and not args.role:
        parser.error("choose --model or a --role configured in private local-llm YAML")
    if not math.isfinite(args.max_seconds) or args.max_seconds <= 0:
        parser.error("--max-seconds must be finite and positive")
    if args.context_window is not None and args.context_window <= 0:
        parser.error("--context-window must be positive")
    return args


def build_settings(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    try:
        config = load_yaml(args.config)
    except (LocalLLMError, OSError) as exc:
        # YAML parser diagnostics may contain config values, including secrets.
        raise LocalCodexAgentError("unable to read local LLM config as a YAML mapping") from exc
    role = resolve_role(config, {}, args.role)
    endpoints = config.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        raise LocalCodexAgentError("endpoints must be a mapping")
    selected = args.endpoint or role.get("endpoint") or config.get("default_endpoint")
    if selected is not None and not isinstance(selected, str):
        raise LocalCodexAgentError("selected endpoint id must be a string")
    if not args.base_url and selected in endpoints:
        raw_endpoint = endpoints[selected]
        if not isinstance(raw_endpoint, dict):
            raise LocalCodexAgentError("selected endpoint must be a mapping")
        if not isinstance(raw_endpoint.get("base_url"), str) or not raw_endpoint["base_url"].strip():
            raise LocalCodexAgentError("selected endpoint requires an explicit base_url")
        if "locality" in raw_endpoint and (
            not isinstance(raw_endpoint["locality"], str) or raw_endpoint["locality"] not in LOCALITIES
        ):
            raise LocalCodexAgentError("selected endpoint has an unknown locality")
        if "enabled" in raw_endpoint and raw_endpoint["enabled"] is not True:
            raise LocalCodexAgentError("selected endpoint is disabled or has an invalid enabled value")
    try:
        endpoint = resolve_endpoint(config, args.endpoint, args.base_url, role)
        validate_endpoint(endpoint)
    except (ValueError, TypeError) as exc:
        raise LocalCodexAgentError("selected endpoint has an invalid URL or configuration") from exc
    model = args.model or role_model(role)
    if not isinstance(model, str) or not model.strip() or not model.isprintable():
        raise LocalCodexAgentError("choose a nonblank --model or configure a model in the selected private role")
    model = model.strip()
    token = endpoint_token(endpoint)
    context_window = args.context_window or context_window_from_role(role)
    provider = f"local_codex_agent_{uuid.uuid4().hex}"
    settings: dict[str, Any] = {
        "model_provider": provider,
        "model": model,
        "review_model": model,
        "approval_policy": "never",
        "cli_auth_credentials_store": "file",
        "web_search": "disabled",
        "history.persistence": "none",
        "analytics.enabled": False,
        "feedback.enabled": False,
        "otel.exporter": "none",
        "otel.trace_exporter": "none",
        "otel.metrics_exporter": "none",
        "otel.log_user_prompt": False,
        "allow_login_shell": False,
        "shell_environment_policy.ignore_default_excludes": False,
        "shell_environment_policy.exclude": [TOKEN_KEY],
        "features.apps": False,
        "features.plugins": False,
        "features.remote_plugin": False,
        "features.remote_models": False,
        "features.memories": False,
        "features.multi_agent": False,
        "features.multi_agent_v2": False,
        f"model_providers.{provider}.name": "Isolated local Responses endpoint",
        f"model_providers.{provider}.base_url": endpoint["base_url"],
        f"model_providers.{provider}.wire_api": "responses",
        f"model_providers.{provider}.requires_openai_auth": False,
        f"model_providers.{provider}.supports_websockets": False,
        f"model_providers.{provider}.request_max_retries": 0,
        f"model_providers.{provider}.stream_max_retries": 0,
    }
    if token is not None:
        settings[f"model_providers.{provider}.env_key"] = TOKEN_KEY
    if context_window is not None:
        settings["model_context_window"] = context_window
    summary = {
        "host": args.host,
        "provider": provider,
        "wire_api": "responses",
        "base_url": public_base_url(endpoint),
        "locality": endpoint["locality"],
        "trust": endpoint.get("trust"),
        "uses_token_env": token is not None,
        "configured_model": model,
        "configured_context_window": context_window,
        "served_model": None,
        "served_model_evidence": "exec JSONL does not expose the server's model identity",
        "sandbox": args.sandbox,
        "max_seconds": args.max_seconds,
    }
    return settings, summary, token


def validate_endpoint(endpoint: dict[str, Any]) -> None:
    if any(character.isspace() for character in endpoint["base_url"]):
        raise LocalCodexAgentError("endpoint URL must not contain whitespace")
    parsed = urlsplit(endpoint["base_url"])
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port == 0:
        raise LocalCodexAgentError("endpoint must be an HTTP(S) API base URL")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise LocalCodexAgentError("endpoint URLs cannot contain credentials, query parameters, or fragments; use token_env")
    host = parsed.hostname.casefold()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    locality = endpoint["locality"]
    if locality == "localhost":
        if not loopback or endpoint.get("trust") not in {"private_local", "private", "cli_override"}:
            raise LocalCodexAgentError("localhost endpoints require a loopback URL and private trust")
    elif locality in {"trusted_lan", "remote_private"}:
        if endpoint.get("trust") not in {"private_local", "private"}:
            raise LocalCodexAgentError("network endpoints require private trust in local config")
    else:
        raise LocalCodexAgentError("local agent execution refuses cloud or unknown endpoints; there is no cloud fallback")


def endpoint_token(endpoint: dict[str, Any]) -> str | None:
    name = endpoint.get("token_env")
    if name is None:
        return None
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise LocalCodexAgentError("token_env must name an environment variable")
    token = os.environ.get(name)
    if token is None or not token.strip():
        raise LocalCodexAgentError("the configured endpoint token_env is unset or empty")
    if "\r" in token or "\n" in token:
        raise LocalCodexAgentError("the configured endpoint token contains an invalid newline")
    return token


def context_window_from_role(role: dict[str, Any]) -> int | None:
    load = role.get("load")
    raw = role.get("context_length")
    if raw is None and isinstance(load, dict):
        raw = load.get("context_length")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise LocalCodexAgentError("role context_length must be a positive integer")
    return raw


def isolated_environment(run_dir: Path, token: str | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in RUNTIME_ENV_KEYS or key.startswith("LC_")}
    for dirname in ("home", "host", "tmp", "config", "cache", "data"):
        (run_dir / dirname).mkdir(mode=0o700, exist_ok=True)
    env.update({
        "HOME": str(run_dir / "home"),
        "CODEX_HOME": str(run_dir / "host"),
        "CODEX_LAB_HOME": str(run_dir / "host"),
        "CODE_HOME": str(run_dir / "host"),
        "TMPDIR": str(run_dir / "tmp"),
        "XDG_CONFIG_HOME": str(run_dir / "config"),
        "XDG_CACHE_HOME": str(run_dir / "cache"),
        "XDG_DATA_HOME": str(run_dir / "data"),
        "NO_PROXY": "*",
    })
    if token is not None:
        env[TOKEN_KEY] = token
    return env


def resolve_host_binary(args: argparse.Namespace) -> str:
    resolved = shutil.which(args.host_bin or args.host)
    if resolved is None:
        raise LocalCodexAgentError("selected Codex host is unavailable; provide its installed executable with --host-bin")
    return str(Path(resolved).absolute())


def config_overrides(settings: dict[str, Any]) -> list[str]:
    # Keep Unicode literal: JSON surrogate-pair escapes are invalid in TOML.
    return [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in settings.items()]


def build_command(args: argparse.Namespace, executable: str, settings: dict[str, Any], final_path: Path) -> list[str]:
    command = [
        executable, "exec", "--ephemeral", "--ignore-user-config",
        "--skip-git-repo-check", "-C", str(args.workdir.resolve()),
        "-s", args.sandbox, "--json", "--color", "never",
        "--output-last-message", str(final_path),
    ]
    for override in config_overrides(settings):
        command.extend(["-c", override])
    command.append("-")
    return command


def stop_process_group(process: subprocess.Popen[bytes]) -> None:
    """Signal only the new session's process group, including surviving children."""
    # macOS can return EPERM when a process group contains only an unreaped
    # zombie. Reap our leader before signaling, and retry if it just exited.
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except PermissionError:
        if process.poll() is None:
            raise
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.025)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_process(
    command: list[str], env: dict[str, str], run_dir: Path, *, input_text: str = "", seconds: float,
    extra_output: Path | None = None,
) -> tuple[int, str, str]:
    """Use bounded file captures so a noisy host cannot fill wrapper memory."""
    with tempfile.TemporaryFile(dir=run_dir) as stdin, tempfile.TemporaryFile(dir=run_dir) as stdout, tempfile.TemporaryFile(dir=run_dir) as stderr:
        stdin.write(input_text.encode("utf-8"))
        stdin.seek(0)
        process = subprocess.Popen(
            command, stdin=stdin, stdout=stdout, stderr=stderr,
            cwd=run_dir, env=env, start_new_session=True,
        )
        deadline = time.monotonic() + seconds
        try:
            while True:
                capture_size = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
                if extra_output is not None and extra_output.exists():
                    capture_size += extra_output.stat().st_size
                if capture_size > MAX_CAPTURE_BYTES:
                    raise LocalCodexAgentError("host exceeded the 16 MiB output limit")
                returncode = process.poll()
                if returncode is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LocalCodexAgentError(f"host timed out after {seconds:g}s; owned process group stopped")
                time.sleep(min(0.025, remaining))
        finally:
            stop_process_group(process)
        stdout.seek(0)
        stderr.seek(0)
        return returncode, stdout.read(MAX_CAPTURE_BYTES).decode("utf-8", errors="replace"), stderr.read(MAX_CAPTURE_BYTES).decode("utf-8", errors="replace")


def verify_host(args: argparse.Namespace, executable: str, env: dict[str, str], run_dir: Path) -> str:
    # Probes do not receive endpoint credentials or prompts.
    probe_env = {key: value for key, value in env.items() if key != TOKEN_KEY}
    status, version, _ = run_process([executable, "--version"], probe_env, run_dir, seconds=PROBE_SECONDS)
    version = version.strip()
    if status or not version.startswith(HOST_VERSION_PREFIXES[args.host]) or "\n" in version:
        raise LocalCodexAgentError("executable does not identify as the explicitly selected Codex host")
    status, help_text, _ = run_process([executable, "exec", "--help"], probe_env, run_dir, seconds=PROBE_SECONDS)
    required = ("--ephemeral", "--ignore-user-config", "--sandbox", "--cd", "--config", "--json", "--output-last-message", "--skip-git-repo-check", "--color")
    if status or any(flag not in help_text for flag in required):
        raise LocalCodexAgentError("selected host lacks required isolated exec flags")
    return version


def redact(text: str, token: str | None) -> str:
    if not token:
        return text
    for representation in (json.dumps(token)[1:-1], token):
        text = text.replace(representation, "[redacted-endpoint-token]")
    return text


def redacted_json(value: Any, token: str | None) -> str:
    def clean(item: Any) -> Any:
        if isinstance(item, str):
            return redact(item, token)
        if isinstance(item, list):
            return [clean(element) for element in item]
        if isinstance(item, dict):
            return {clean(key): clean(element) for key, element in item.items()}
        return item

    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True)


def redacted_jsonl(stdout: str, token: str | None) -> str:
    try:
        return "\n".join(redacted_json(json.loads(line), token) for line in stdout.splitlines() if line.strip())
    except json.JSONDecodeError as exc:
        raise LocalCodexAgentError("host emitted invalid exec JSONL") from exc


def validated_final(stdout: str, final_path: Path) -> str:
    try:
        events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise LocalCodexAgentError("host emitted invalid exec JSONL") from exc
    if any(not isinstance(event, dict) for event in events):
        raise LocalCodexAgentError("host emitted invalid exec events")
    if any(event.get("type") in {"turn.failed", "error"} for event in events):
        raise LocalCodexAgentError("host reported a failed turn")
    if not any(event.get("type") == "turn.completed" for event in events):
        raise LocalCodexAgentError("host exited without a completed turn")
    if final_path.is_symlink() or not final_path.is_file():
        raise LocalCodexAgentError("host did not produce a final message file")
    if final_path.stat().st_size > MAX_CAPTURE_BYTES:
        raise LocalCodexAgentError("host final message exceeds the output limit")
    final = final_path.read_text(encoding="utf-8")
    if not final.strip():
        raise LocalCodexAgentError("host completed without a nonblank final message")
    return final


def write_final(path: Path, text: str) -> None:
    # Publish only validated output. A failed run never truncates an existing result.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".local-codex-final-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(text)
            output.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def interrupt(signum: int, _frame: Any) -> None:
    raise RunInterrupted(signum)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = None
    previous_sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        if os.name != "posix":
            raise LocalCodexAgentError("this wrapper requires POSIX process-group cleanup")
        if not args.workdir.is_dir():
            raise LocalCodexAgentError("--workdir must be an existing directory")
        settings, summary, token = build_settings(args)
        executable = resolve_host_binary(args)
        with tempfile.TemporaryDirectory(prefix="local-codex-agent-") as temporary:
            run_dir = Path(temporary).resolve()
            env = isolated_environment(run_dir, token)
            summary["host_version"] = verify_host(args, executable, env, run_dir)
            # --ignore-user-config skips even this private per-run file, so pass
            # the same settings again as explicit -c overrides.
            config_path = run_dir / "host" / "config.toml"
            config_path.touch(mode=0o600)
            config_path.write_text("\n".join(config_overrides(settings)) + "\n", encoding="utf-8")
            prompt = args.prompt if args.prompt is not None and args.prompt != "-" else sys.stdin.read(MAX_PROMPT_BYTES + 1)
            if not prompt.strip():
                raise LocalCodexAgentError("prompt is empty")
            if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                raise LocalCodexAgentError("prompt exceeds the 4 MiB input limit")
            final_path = run_dir / "last-message.txt"
            command = build_command(args, executable, settings, final_path)
            print(redacted_json({"local_codex_agent": summary}, token), file=sys.stderr)
            status, stdout, stderr = run_process(command, env, run_dir, input_text=prompt, seconds=args.max_seconds, extra_output=final_path)
            if stderr:
                print(redact(stderr, token), end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
            if status:
                if args.json and stdout:
                    print(redacted_jsonl(stdout, token))
                raise LocalCodexAgentError(f"host exited unsuccessfully (status {status})")
            final = redact(validated_final(stdout, final_path), token)
            if args.output_last_message is not None:
                write_final(args.output_last_message, final)
            output = redacted_jsonl(stdout, token) if args.json else final
            print(output, end="" if output.endswith("\n") else "\n")
            return 0
    except (LocalCodexAgentError, LocalLLMError, OSError, UnicodeError) as exc:
        print(f"error: {redact(str(exc), token)}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, RunInterrupted) as exc:
        signum = exc.signum if isinstance(exc, RunInterrupted) else signal.SIGINT
        print("error: local agent execution interrupted; owned process group stopped", file=sys.stderr)
        return 128 + signum
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
