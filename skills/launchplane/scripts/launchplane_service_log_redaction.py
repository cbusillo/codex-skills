"""Bounded record parsing and secret-value projection for private service logs."""
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import ast
import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote

MAX_MESSAGE_BYTES = 4096
MAX_DEPTH = 8
MAX_DECODES = 3
MAX_LITERAL_ATTEMPTS = 32
MASK = "[redacted]"
PAIR_NAMES = {"name", "key", "variable"}
PAIR_VALUES = {"value", "data", "default", "val"}
CONTROL = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]"
)
ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
FIELD = re.compile(
    r"""(?P<name>--?[A-Za-z_][A-Za-z0-9_.-]{0,127}|["']?[A-Za-z_][A-Za-z0-9_.-]{0,127}["']?)\s*(?P<sep>[=:]|(?<=\w)\s+)\s*"""
)
KEY_BLOCK = re.compile(
    r"-----((?:BEGIN|END)) ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----"
)
PROVIDER_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|[sr]k[-_][A-Za-z0-9_-]+|(?:AKIA|ASIA)[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{35}|xox[baprs]-[A-Za-z0-9-]+|glpat-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
AUTH_VALUE = re.compile(
    r"(?i)\b(?:bearer\s+[^\s,;\"']+|digest\s+(?:username|realm|nonce|response)\s*=[^\n]+|basic\s+(?P<basic>[A-Za-z0-9+/]+={0,2}))"
)
USERINFO = re.compile(
    r"(\b[A-Za-z][A-Za-z0-9+.-]*://)(?:[^\s/?#:@]+:[^\s]+|[^\s/?#]+)@"
)
OPAQUE = re.compile(r"[A-Za-z0-9_+/=-]{32,}")
DIAGNOSTIC_VALUE = re.compile(
    r"""(?ix)(?:\b(?:deploying|commit(?:_sha)?|sha|digest|revision|image(?:_id)?|head|trace(?:_id)?|operation(?:_id)?|idempotency_key)\s*(?:[=:]\s*|\s+)|sha(?:1|256):)([A-Za-z0-9_./:-]+)|\bFile\s+["']([^"']+)["']"""
)
SENSITIVE_WORDS = {
    "password",
    "pass",
    "cred",
    "creds",
    "passwd",
    "passphrase",
    "pwd",
    "token",
    "secret",
    "credential",
    "credentials",
    "cookie",
    "cookies",
    "authorization",
    "auth",
    "sid",
    "session",
    "sessionid",
    "apikey",
}
DIAGNOSTIC_KEYS = {
    "idempotency",
    "cache",
    "primary",
    "foreign",
    "partition",
    "sort",
    "object",
    "routing",
    "dedup",
    "public",
    "request",
    "operation",
    "product",
    "context",
    "instance",
    "record",
}
METADATA_SUFFIXES = {
    "count",
    "type",
    "usage",
    "limit",
    "expiry",
    "present",
    "configured",
    "label",
    "name",
}
DIAGNOSTIC_NAMES = {
    "id",
    "trace",
    "span",
    "sha",
    "digest",
    "commit",
    "revision",
    "image",
    "etag",
    "operation",
    "path",
    "file",
    "idempotencykey",
}


class RedactionError(ValueError):
    """A fixed safe code, never private input text."""


class LiteralBudgetExceeded(Exception):
    """Internal signal to withhold one complete bounded record."""


@dataclass(frozen=True)
class SafeRecord:
    message: str
    matched: bool
    redacted: bool


def words(name: str) -> list[str]:
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [part for part in re.split(r"[^a-z0-9]+", name.casefold()) if part]


def sensitive(name: str) -> bool:
    parts = words(name)
    if not parts:
        return False
    if parts[-1] in METADATA_SUFFIXES:
        return False
    canonical = [part.removesuffix("s") for part in parts]
    long_stems = (
        "password",
        "passwd",
        "passphrase",
        "secret",
        "token",
        "credential",
        "apikey",
        "cookie",
    )
    return any(
        part in SENSITIVE_WORDS or part.endswith(long_stems) or part == "privatekey"
        for original, singular in zip(parts, canonical, strict=True)
        for part in (original, singular)
    ) or (
        canonical[-1] == "key" and (len(parts) == 1 or parts[-2] not in DIAGNOSTIC_KEYS)
    )


def diagnostic(name: str) -> bool:
    parts = words(name)
    return bool(parts) and (
        parts[-1] in DIAGNOSTIC_NAMES or "".join(parts) in DIAGNOSTIC_NAMES
    )


def normalize(text: str) -> str:
    def decode_escape(match: re.Match) -> str:
        return json.loads('"' + match[0] + '"')

    for _ in range(MAX_DECODES):
        decoded = ESCAPE.sub(decode_escape, unquote(text))
        if decoded == text:
            return text
        text = decoded
    if ESCAPE.search(text) or unquote(text) != text:
        raise RedactionError("log_encoding_limit")
    return text


def shape_redact(text: str, *, diagnostic_value: bool = False) -> str:
    text = PROVIDER_TOKEN.sub(MASK, text)

    def auth(match: re.Match) -> str:
        if match["basic"] is not None:
            try:
                if b":" not in base64.b64decode(match["basic"], validate=True):
                    return match[0]
            except (ValueError, binascii.Error):
                return match[0]
        return MASK

    text = AUTH_VALUE.sub(auth, text)
    text = USERINFO.sub(r"\1" + MASK + "@", text)
    protected = [
        match.span(group)
        for match in DIAGNOSTIC_VALUE.finditer(text)
        for group in (1, 2)
        if match[group] is not None
    ]

    def opaque(match: re.Match) -> str:
        value = match[0]
        if diagnostic_value or any(
            start <= match.start() and match.end() <= end for start, end in protected
        ):
            return value
        is_hex = re.fullmatch(r"[0-9a-fA-F]{32,}", value)
        mixed = (
            any(char.isupper() for char in value)
            and any(char.islower() for char in value)
            and any(char.isdigit() for char in value)
        )
        return MASK if is_hex or mixed else value

    return OPAQUE.sub(opaque, text)


def balanced_end(text: str, start: int = 0) -> int | None:
    if start >= len(text) or text[start] not in "[{(\"'":
        return None
    stack = []
    quote = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "[{(":
            stack.append({"[": "]", "{": "}", "(": ")"}[char])
        elif char in "]})" and (not stack or stack.pop() != char):
            return None
        if not quote and not stack:
            return index + 1
    return None


def leading_literal(text: str, start: int = 0) -> tuple[object, int] | None:
    try:
        return json.JSONDecoder().raw_decode(text, start)
    except (ValueError, RecursionError):
        pass
    end = balanced_end(text, start)
    if end is None:
        return None
    span = text[start:end]
    try:
        return ast.literal_eval(span), end
    except (ValueError, SyntaxError, RecursionError, TypeError, MemoryError):
        # Unknown repr calls are data, never executable. A quoted container
        # whose semantics cannot be projected is withheld as one closed value.
        if any(char in span for char in "\"'"):
            return MASK, end
        return None


@dataclass(frozen=True)
class PlainAnalysis:
    cut: int | None = None
    continuation: str | None = None
    whole: bool = False
    pair_value: bool = False


@dataclass(frozen=True)
class CleanRecord:
    message: str
    continuation: str | None = None


def continuation_kind(rest: str) -> str | None:
    rest = rest.strip()
    if not rest:
        return "next"
    if re.fullmatch(r"(?:&[A-Za-z0-9_-]+\s+)?[|>][0-9+-]*(?:\s+#.*)?", rest):
        return "block"
    if rest[0] in "[{(\"'" and balanced_end(rest) is None:
        return "block"
    return None


def analyze_plain(text: str) -> PlainAnalysis:
    fields = list(FIELD.finditer(text))
    descriptors = []
    for field in fields:
        if field["name"].strip("\"'").casefold() in PAIR_NAMES:
            value = re.match(
                r"[\"']?([A-Za-z_][A-Za-z0-9_.-]{0,127})", text[field.end() :]
            )
            if value:
                descriptors.append((field, sensitive(value[1])))
    secret_pair = any(secret for _, secret in descriptors)
    pair_value = any(
        field["name"].strip("\"'").casefold() in PAIR_VALUES for field in fields
    )
    public_pair = bool(descriptors) and not secret_pair and pair_value
    cuts = []
    continuation = None
    for field in fields:
        name = field["name"].strip("\"'")
        if public_pair and name.casefold() in PAIR_NAMES:
            continue
        secret = sensitive(name) and (
            field["sep"] in {"=", ":"} or name.startswith("--")
        )
        if secret or secret_pair and name.casefold() in PAIR_VALUES:
            cuts.append(field.end())
            continuation = continuation or continuation_kind(text[field.end() :])
    if secret_pair:
        return PlainAnalysis(0, continuation, whole=True, pair_value=pair_value)
    return PlainAnalysis(
        min(cuts) if cuts else None, continuation, pair_value=pair_value
    )


def parse_literal(text: str) -> tuple[str, object, str] | None:
    positions = [index for index, char in enumerate(text) if char in "{[("]
    if text.lstrip().startswith(('"', "'")):
        positions.append(len(text) - len(text.lstrip()))
    attempts = resume = 0
    for start in sorted(set(positions)):
        if start < resume:
            continue
        if attempts >= MAX_LITERAL_ATTEMPTS:
            raise LiteralBudgetExceeded
        attempts += 1
        prefix = CONTROL.sub("", normalize(text[:start]))
        if analyze_plain(prefix).cut is not None:
            return None
        parsed = leading_literal(text, start)
        if parsed is None:
            end = balanced_end(text, start)
            if end is not None:
                resume = end
            continue
        value, end = parsed
        if (
            isinstance(value, str)
            and value != MASK
            and (prefix.strip() or text[end:].strip())
        ):
            continue
        suffix = CONTROL.sub("", normalize(text[end:]))
        outside = analyze_plain(prefix + " " + suffix)
        if (
            outside.whole
            or outside.pair_value
            and analyze_plain(CONTROL.sub("", normalize(text))).whole
        ):
            return None
        return prefix, value, text[end:]
    return None


def project(value: object, depth: int, *, field: str = "") -> object:
    if depth >= MAX_DEPTH:
        return MASK
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            return MASK
        normalized = {normalize(key): item for key, item in value.items()}
        named_pair = any(key.casefold() in PAIR_VALUES for key in normalized) and any(
            key.casefold() in PAIR_NAMES and isinstance(item, str)
            for key, item in normalized.items()
        )
        pair = any(
            key.casefold() in PAIR_NAMES
            and isinstance(item, str)
            and sensitive(normalize(item))
            for key, item in normalized.items()
        )
        return {
            key: MASK
            if (sensitive(key) and not (named_pair and key.casefold() in PAIR_NAMES))
            or pair
            and key.casefold() in PAIR_VALUES
            else project(item, depth + 1, field=key)
            for key, item in normalized.items()
        }
    if isinstance(value, (list, tuple)):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and sensitive(normalize(value[0]))
        ):
            return [value[0], MASK]
        output = []
        redact_next = False
        for item in value:
            output.append(MASK if redact_next else project(item, depth + 1))
            redact_next = (
                isinstance(item, str) and item.startswith("--") and sensitive(item)
            )
        return output
    if isinstance(value, str):
        return clean_record(
            value, depth + 1, diagnostic_value=diagnostic(field)
        ).message
    return value if value is None or type(value) in {bool, int, float} else MASK


def clean_record(
    text: str, depth: int = 0, *, diagnostic_value: bool = False
) -> CleanRecord:
    if depth >= MAX_DEPTH:
        return CleanRecord(MASK)
    try:
        parsed = parse_literal(text)
        if parsed is None:
            text = CONTROL.sub("", normalize(text))
            parsed = parse_literal(text)
    except LiteralBudgetExceeded:
        normalized = CONTROL.sub("", normalize(text))
        return CleanRecord(MASK, analyze_plain(normalized).continuation)
    if parsed is not None:
        prefix, value, suffix = parsed
        if not prefix and not suffix and isinstance(value, str):
            # A complete serialized record cannot open a stream continuation.
            nested = clean_record(value, depth + 1, diagnostic_value=diagnostic_value)
            return CleanRecord(nested.message)
        left = clean_record(CONTROL.sub("", normalize(prefix)), depth + 1)
        right = clean_record(
            CONTROL.sub("", normalize(suffix)),
            depth + 1,
            diagnostic_value=diagnostic_value,
        )
        return CleanRecord(
            left.message
            + json.dumps(project(value, depth + 1), ensure_ascii=True)
            + right.message,
            right.continuation,
        )
    plain = clean_plain(text, diagnostic_value=diagnostic_value)
    if text.lstrip().startswith(('{"', "{'", '["', "['", '("', "('")):
        return CleanRecord(MASK, plain.continuation)
    return plain


def clean_plain(text: str, *, diagnostic_value: bool = False) -> CleanRecord:
    analysis = analyze_plain(text)
    if analysis.cut is not None:
        message = MASK if analysis.whole else shape_redact(text[: analysis.cut]) + MASK
        return CleanRecord(message, analysis.continuation)
    return CleanRecord(shape_redact(text, diagnostic_value=diagnostic_value))


class Redactor:
    def __init__(self, identifiers: tuple[str, ...] = ()) -> None:
        self.patterns = tuple(
            re.compile(
                r"(?<![A-Za-z0-9_-])"
                + re.escape(value)
                + r"(?![A-Za-z0-9_-]|[.][A-Za-z0-9_-])"
            )
            for value in identifiers
            if value
        )
        self.key_block: str | None = None
        self.block_indent: int | None = None
        self.hide_next = False
        self.started = False
        self.public_pair_indent: int | None = None
        self.sensitive_indentation = False

    def feed(self, raw: str) -> SafeRecord:
        if len(raw.encode("utf-8", errors="replace")) > MAX_MESSAGE_BYTES:
            raise RedactionError("log_record_too_large")
        text = CONTROL.sub("", raw)
        normalized = normalize(text)
        matched = all(pattern.search(normalized) for pattern in self.patterns)
        indent = len(text) - len(text.lstrip())
        keys = list(KEY_BLOCK.finditer(normalized))
        for key in keys:
            if key[1] == "BEGIN":
                self.key_block = key[2]
            elif self.key_block != key[2]:
                raise RedactionError("unbound_private_key")
            else:
                self.key_block = None
        if keys:
            self.started = True
            return SafeRecord(MASK, matched, True)
        if self.key_block is not None:
            return SafeRecord(MASK, matched, True)
        if indent and (not self.started or self.sensitive_indentation):
            return SafeRecord(MASK, matched, True)
        self.started = True
        if self.hide_next:
            self.hide_next = False
            return SafeRecord(MASK, matched, True)
        if self.block_indent is not None:
            if not text.strip() or indent > self.block_indent:
                return SafeRecord(MASK, matched, True)
            self.block_indent = None
        pair = re.match(
            r"\s*(?:-\s*)?[\"']?(?:name|key|variable)[\"']?\s*:\s*(.*)",
            normalized,
            re.IGNORECASE,
        )
        if self.public_pair_indent is not None and indent <= self.public_pair_indent:
            self.public_pair_indent = None
        if pair and not sensitive(pair[1].strip("\"' ,")):
            self.public_pair_indent = indent
        value_fragment = re.match(
            r"\s*(?:-\s*)?[\"']?(?:value|data|default|val)[\"']?\s*:",
            normalized,
            re.IGNORECASE,
        )
        if value_fragment and self.public_pair_indent is None:
            self.block_indent = indent
            return SafeRecord(MASK, matched, True)
        if pair and sensitive(pair[1].strip("\"' ,")):
            self.block_indent = indent
        clean = clean_record(text)
        if clean.continuation:
            self.block_indent = indent
            self.sensitive_indentation = True
            self.hide_next = clean.continuation == "next"
        message = clean.message
        return SafeRecord(message, matched, message != raw)
