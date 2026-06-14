#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Synthesize a verified work brief from GitHub work evidence."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
LOCAL_LLM_SCRIPT_DIR = ROOT / "local-llm" / "scripts"
LOCAL_LLM_API_PATH = LOCAL_LLM_SCRIPT_DIR / "lm_studio_api.py"
DEFAULT_LOCAL_LLM_CONFIG = ROOT / ".local" / "local-llm.yaml"
MODEL_INDEX = ROOT / "local-llm" / "references" / "model-index.yaml"
CONTRACT_PATH = SCRIPT_DIR.parent / "references" / "prompt-contract.md"
LM_CHAT_PATH = LOCAL_LLM_SCRIPT_DIR / "lm_studio_chat.py"
VERIFY_PATH = SCRIPT_DIR / "verify_work_brief.py"
DEFAULT_ROLE = "work_brief_writer"
DEFAULT_MAX_INPUT_CHARS = 200_000
DEFAULT_RESPONSE_TOKENS = 900
CONTEXT_CHAR_RATIO = 4
CONTEXT_TOKEN_RESERVE = 1024
BRIEF_STYLES = {"standard", "conversation"}

Runner = Callable[..., subprocess.CompletedProcess[str]]


class SynthesisError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize a work brief with a direct local LLM call and verify it against evidence."
    )
    parser.add_argument("--evidence", required=True, type=Path, help="GitHub work evidence JSON file.")
    parser.add_argument(
        "--plan-context",
        action="append",
        default=[],
        type=Path,
        help="Optional durable plan context JSON file. May be repeated.",
    )
    parser.add_argument("--audience", choices=("operator", "manager", "executive"), default="manager")
    parser.add_argument(
        "--brief-style",
        choices=sorted(BRIEF_STYLES),
        help="Writing style. Defaults to conversation for executive briefs and standard otherwise.",
    )
    parser.add_argument("--report-recipient", help="Human-facing recipient label for the brief.")
    parser.add_argument(
        "--brief-output",
        "--output",
        dest="brief_output",
        type=Path,
        help="Write brief Markdown here. Prints to stdout when omitted.",
    )
    parser.add_argument("--prompt-output", type=Path, help="Optional path for the exact user prompt sent to the local LLM.")
    parser.add_argument("--llm-result-output", type=Path, help="Optional path for the local LLM JSON result.")
    parser.add_argument("--role", default=DEFAULT_ROLE, help="local-llm model role to use.")
    parser.add_argument("--local-llm-config", type=Path, help="Optional .local/local-llm.yaml path.")
    parser.add_argument("--endpoint")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--load-policy", choices=("none", "jit_chat", "api_explicit"))
    parser.add_argument("--ttl", type=int)
    parser.add_argument("--context-length", type=int)
    parser.add_argument("--flash-attention", action="store_true")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--unload-after", action="store_true")
    parser.add_argument(
        "--max-input-chars",
        type=int,
        help=(
            "Prompt preflight limit. Defaults to the smaller of the script default "
            "and the configured model context budget."
        ),
    )
    parser.add_argument(
        "--allow-truncate",
        action="store_true",
        help="Allow lm_studio_chat.py to truncate prompts longer than --max-input-chars.",
    )
    parser.add_argument("--no-verify", action="store_true", help="Skip verify_work_brief.py.")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable synthesis summary.")
    args = parser.parse_args()
    if args.max_input_chars is not None and args.max_input_chars <= 0:
        parser.error("--max-input-chars must be positive")
    args.brief_style = args.brief_style or ("conversation" if args.audience == "executive" else "standard")
    return args


def main() -> int:
    args = parse_args()
    try:
        summary = synthesize(args)
    except SynthesisError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif not args.brief_output:
        print(summary["brief"])
    else:
        print(f"ok synthesize-work-brief: {summary['brief_path']}")
    return 0


def synthesize(args: argparse.Namespace, *, runner: Runner = subprocess.run) -> dict[str, Any]:
    evidence = read_json(args.evidence, "evidence")
    plan_context = [read_json(path, "plan context") for path in args.plan_context]
    contract = read_text(CONTRACT_PATH)
    if getattr(args, "max_input_chars", None) is None:
        args.max_input_chars = resolve_max_input_chars(args, system_prompt=contract)
    brief_style = resolved_brief_style(args.audience, getattr(args, "brief_style", None))
    user_prompt = build_user_prompt(
        evidence=evidence,
        evidence_path=args.evidence,
        plan_context=plan_context,
        plan_context_paths=args.plan_context,
        audience=args.audience,
        brief_style=brief_style,
        report_recipient=args.report_recipient,
    )
    if args.prompt_output:
        save_text(args.prompt_output, user_prompt)
    if len(user_prompt) > args.max_input_chars and not args.allow_truncate:
        raise SynthesisError(
            f"prompt is {len(user_prompt)} chars, exceeding --max-input-chars {args.max_input_chars}; "
            "narrow evidence or pass --allow-truncate"
        )
    llm_result = run_lm_chat(build_lm_command(args, contract), user_prompt, runner=runner)
    if args.llm_result_output:
        save_text(args.llm_result_output, json.dumps(llm_result, indent=2, sort_keys=True) + "\n")
    brief = str(llm_result.get("content") or "").strip()
    if not brief:
        raise SynthesisError("local LLM returned no brief content")
    brief_path = args.brief_output
    temporary_brief_path: Path | None = None
    try:
        if brief_path:
            save_text(brief_path, brief + "\n")
            verify_target = brief_path
        else:
            fd, temporary_name = tempfile.mkstemp(suffix=".md", text=True)
            temporary_brief_path = Path(temporary_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(brief + "\n")
            verify_target = temporary_brief_path
        verify_result = None
        if not args.no_verify:
            try:
                verify_result = run_verifier(args.evidence, verify_target, args.plan_context, runner=runner)
            except SynthesisError as exc:
                if brief_path:
                    raise SynthesisError(f"{exc}; unverified brief remains at {brief_path}") from exc
                raise
    finally:
        if temporary_brief_path is not None:
            temporary_brief_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "audience": args.audience,
        "brief_style": brief_style,
        "report_recipient": args.report_recipient,
        "derived_context_present": bool(isinstance(evidence, dict) and evidence.get("derived_context")),
        "evidence_path": str(args.evidence),
        "plan_context_paths": [str(path) for path in args.plan_context],
        "brief_path": str(brief_path) if brief_path else None,
        "brief": brief,
        "llm": summarize_llm_result(llm_result),
        "verified": verify_result is not None,
        "verification": verify_result,
    }


def build_user_prompt(
    *,
    evidence: Any,
    evidence_path: Path,
    plan_context: list[Any],
    plan_context_paths: list[Path],
    audience: str,
    brief_style: str,
    report_recipient: str | None,
) -> str:
    derived_context = evidence.get("derived_context") if isinstance(evidence, dict) else None
    evidence_for_prompt = evidence_without_derived_context(evidence)
    parts = [
        "Reader and purpose:",
        f"- audience: {audience}",
        f"- brief style: {brief_style}",
        f"- recipient: {report_recipient or 'unspecified'}",
        "- task: write a concise GitHub work brief as Markdown only",
        "- use the system prompt as the full writing and grounding contract",
    ]
    if brief_style == "conversation":
        parts.extend(
            [
                "- write as a human conversation brief for a busy owner",
                "- lead with what the work gives the reader to talk about with the team",
                "- use light structure; avoid dev-manager status scaffolding and raw metrics as the story",
            ]
        )
    parts.extend(
        [
            "",
            "Evidence source:",
            f"- file: {evidence_path}",
            "",
            "Source limitations to reflect in the brief:",
            *source_limitations(evidence),
            "- Every listed limitation must be reflected in the brief, folded into the relevant point or receipts.",
        ]
    )
    if derived_context:
        parts.extend(
            [
                "",
                "Derived context for human meaning:",
                "- treat this as grounded explanatory context with provenance, not as a new manual source of truth",
                "- do not present standing repo descriptions as changes inside the report window",
                "- preserve confidence/staleness wording when the context is inferred or thin",
                "```json",
                json.dumps(derived_context, indent=2, sort_keys=True),
                "```",
            ]
        )
    parts.extend(
        [
            "",
            "Evidence JSON:",
            "```json",
            json.dumps(evidence_for_prompt, indent=2, sort_keys=True),
            "```",
        ]
    )
    if plan_context:
        parts.extend(
            [
                "",
                "Plan context sources:",
                *[f"- file: {path}" for path in plan_context_paths],
                "",
                "Plan context JSON:",
                "```json",
                json.dumps(plan_context, indent=2, sort_keys=True),
                "```",
            ]
        )
    else:
        parts.extend(["", "Plan context: no plan signal was provided."])
    parts.extend(["", "Return only the Markdown brief. Do not wrap it in a code fence."])
    return "\n".join(parts)


def resolved_brief_style(audience: str, brief_style: str | None) -> str:
    return brief_style or ("conversation" if audience == "executive" else "standard")


def evidence_without_derived_context(evidence: Any) -> Any:
    if not isinstance(evidence, dict) or "derived_context" not in evidence:
        return evidence
    return {key: value for key, value in evidence.items() if key != "derived_context"}


def source_limitations(evidence: Any) -> list[str]:
    if not isinstance(evidence, dict):
        return ["- no structured limitation fields were present"]
    notes: list[str] = []
    for key in ("source_notes", "limitations"):
        value = evidence.get(key)
        if isinstance(value, list):
            notes.extend(str(item).strip() for item in value if str(item).strip())
    return [f"- {note}" for note in notes] or ["- no explicit source limitations were present"]


def resolve_max_input_chars(args: argparse.Namespace, *, system_prompt: str | None = None) -> int:
    explicit_limit = getattr(args, "max_input_chars", None)
    if explicit_limit is not None:
        if explicit_limit <= 0:
            raise SynthesisError("--max-input-chars must be positive")
        return explicit_limit
    context_budget = configured_context_input_chars(args, system_prompt=system_prompt)
    if context_budget is None:
        return DEFAULT_MAX_INPUT_CHARS
    return max(1, min(DEFAULT_MAX_INPUT_CHARS, context_budget))


def configured_context_input_chars(args: argparse.Namespace, *, system_prompt: str | None = None) -> int | None:
    role = local_llm_role_config(args)
    raw_load = role.get("load")
    load_config = raw_load if isinstance(raw_load, dict) else {}
    context_source = role.get("context_length") or load_config.get("context_length")
    if getattr(args, "context_length", None) is None and context_source is None:
        return None
    api = load_local_llm_api()
    context_length = api.parse_int_option(getattr(args, "context_length", None), context_source, 0, "context_length")
    if context_length <= 0:
        return None
    max_tokens = api.parse_int_option(
        getattr(args, "max_tokens", None), role.get("max_tokens"), DEFAULT_RESPONSE_TOKENS, "max_tokens"
    )
    usable_tokens = context_length - max_tokens - CONTEXT_TOKEN_RESERVE
    if usable_tokens <= 0:
        raise SynthesisError(
            f"configured context length {context_length} is too small for max_tokens {max_tokens} "
            f"plus reserve {CONTEXT_TOKEN_RESERVE}"
        )
    usable_chars = usable_tokens * CONTEXT_CHAR_RATIO
    if system_prompt:
        usable_chars -= len(system_prompt)
    if usable_chars <= 0:
        raise SynthesisError(
            f"configured context length {context_length} leaves no prompt room after max_tokens {max_tokens}, "
            f"reserve {CONTEXT_TOKEN_RESERVE}, and system prompt"
        )
    return usable_chars


def local_llm_role_config(args: argparse.Namespace) -> dict[str, Any]:
    config_path = getattr(args, "local_llm_config", None) or DEFAULT_LOCAL_LLM_CONFIG
    try:
        api = load_local_llm_api()
        config = api.load_yaml(Path(config_path))
        index = api.load_yaml(MODEL_INDEX)
        return api.resolve_role(config, index, getattr(args, "role", DEFAULT_ROLE))
    except Exception as exc:
        raise SynthesisError(f"unable to resolve local LLM role for prompt budget: {exc}") from exc


def load_local_llm_api() -> Any:
    spec = importlib.util.spec_from_file_location("local_llm_api_for_work_brief", LOCAL_LLM_API_PATH)
    if spec is None or spec.loader is None:
        raise SynthesisError(f"unable to load local LLM helper from {LOCAL_LLM_API_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_lm_command(args: argparse.Namespace, contract: str) -> list[str]:
    command = [
        "uv",
        "run",
        str(LM_CHAT_PATH),
        "--role",
        args.role,
        "--system",
        contract,
        "--max-input-chars",
        str(args.max_input_chars),
        "--json",
    ]
    optional_flags: list[tuple[str, Any]] = [
        ("--config", args.local_llm_config),
        ("--endpoint", args.endpoint),
        ("--base-url", args.base_url),
        ("--model", args.model),
        ("--temperature", args.temperature),
        ("--max-tokens", args.max_tokens),
        ("--timeout", args.timeout),
        ("--load-policy", args.load_policy),
        ("--ttl", args.ttl),
        ("--context-length", args.context_length),
    ]
    for flag, value in optional_flags:
        if value is not None:
            command.extend([flag, str(value)])
    for flag, enabled in (
        ("--flash-attention", args.flash_attention),
        ("--warmup", args.warmup),
        ("--unload-after", args.unload_after),
    ):
        if enabled:
            command.append(flag)
    return command


def run_lm_chat(command: list[str], prompt: str, *, runner: Runner) -> dict[str, Any]:
    result = runner(command, input=prompt, capture_output=True, text=True)
    if result.returncode != 0:
        raise SynthesisError(f"local LLM chat failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SynthesisError(f"local LLM chat returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SynthesisError("local LLM chat JSON was not an object")
    if not payload.get("ok"):
        raise SynthesisError(f"local LLM chat failed: {payload.get('error') or 'unknown error'}")
    return payload


def run_verifier(evidence_path: Path, brief_path: Path, plan_context_paths: list[Path], *, runner: Runner) -> dict[str, Any]:
    command = ["uv", "run", str(VERIFY_PATH), "--evidence", str(evidence_path), "--brief", str(brief_path)]
    for path in plan_context_paths:
        command.extend(["--plan-context", str(path)])
    result = runner(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SynthesisError(f"brief verification failed: {detail}")
    return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def summarize_llm_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": payload.get("model"),
        "served_model": payload.get("served_model"),
        "role": payload.get("role"),
        "endpoint": payload.get("endpoint"),
        "prompt_chars": payload.get("prompt_chars"),
        "max_tokens": payload.get("max_tokens"),
        "lifecycle": payload.get("lifecycle"),
    }


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(read_text(path))
    except OSError as exc:
        raise SynthesisError(f"unable to read {label} file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SynthesisError(f"invalid JSON in {label} file {path}: {exc}") from exc


def read_text(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise SynthesisError(f"unable to read required file {path}: {exc}") from exc


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


if __name__ == "__main__":
    raise SystemExit(main())
