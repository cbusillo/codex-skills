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
        for part in canonical
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


def parse_literal(text: str) -> tuple[str, object, str] | None:
    # JSON handles real structured logs; literal_eval handles Python repr data
    # without executing calls. Both see only an already size-bounded record.
    positions = [text.find(char) for char in ("{", "[", "(") if char in text]
    if text.lstrip().startswith(('{"', "{'", '["', "['", '("', "('")):
        positions.append(len(text) - len(text.lstrip()))
    if not positions:
        return None
    start = min(positions)
    if any(
        sensitive(match["name"].strip("\"'"))
        and (match["sep"] in {"=", ":"} or match["name"].startswith("--"))
        for match in FIELD.finditer(CONTROL.sub("", normalize(text[:start])))
    ):
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(text, start)
        if isinstance(value, str) and (text[:start].strip() or text[end:].strip()):
            return None
        return text[:start], value, text[end:]
    except (ValueError, RecursionError):
        pass
    try:
        return text[:start], ast.literal_eval(text[start:]), ""
    except (ValueError, SyntaxError, RecursionError, TypeError, MemoryError):
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
        return clean_record(value, depth + 1, diagnostic_value=diagnostic(field))
    return value if value is None or type(value) in {bool, int, float} else MASK


def clean_record(text: str, depth: int = 0, *, diagnostic_value: bool = False) -> str:
    if depth >= MAX_DEPTH:
        return MASK
    parsed = parse_literal(text)
    if parsed is not None:
        prefix, value, suffix = parsed
        # A lone JSON string is a serialized record, not another quoting layer
        # to preserve. Depth bounds prevent recursive string encodings looping.
        if not prefix and not suffix and isinstance(value, str):
            return clean_record(value, depth + 1, diagnostic_value=diagnostic_value)
        return (
            clean_plain(CONTROL.sub("", normalize(prefix)))
            + json.dumps(project(value, depth + 1), ensure_ascii=True)
            + clean_plain(
                CONTROL.sub("", normalize(suffix)), diagnostic_value=diagnostic_value
            )
        )
    text = CONTROL.sub("", normalize(text))
    parsed = parse_literal(text)
    if parsed is not None:
        prefix, value, suffix = parsed
        return (
            clean_plain(CONTROL.sub("", normalize(prefix)))
            + json.dumps(project(value, depth + 1), ensure_ascii=True)
            + clean_plain(
                CONTROL.sub("", normalize(suffix)), diagnostic_value=diagnostic_value
            )
        )
    if text.lstrip().startswith(('{"', "{'", '["', "['", '("', "('")):
        return MASK
    return clean_plain(text, diagnostic_value=diagnostic_value)


def clean_plain(text: str, *, diagnostic_value: bool = False) -> str:
    for match in FIELD.finditer(text):
        name = match["name"].strip("\"'")
        if name.casefold() in PAIR_NAMES:
            pair_value = re.match(
                r"[\"']?([A-Za-z_][A-Za-z0-9_.-]{0,127})", text[match.end() :]
            )
            if pair_value and sensitive(pair_value[1]):
                return shape_redact(text[: match.end()]) + MASK
            if pair_value and any(
                sibling["name"].strip("\"'").casefold() in PAIR_VALUES
                for sibling in FIELD.finditer(text[match.end() :])
            ):
                continue
        is_flag = name.startswith("--")
        if sensitive(name) and (match["sep"] in {"=", ":"} or is_flag):
            return shape_redact(text[: match.end()]) + MASK
    return shape_redact(text, diagnostic_value=diagnostic_value)


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
        for match in FIELD.finditer(normalized):
            if sensitive(match["name"].strip("\"'")) and match["sep"] in {"=", ":"}:
                rest = normalized[match.end() :].strip()
                incomplete = (
                    rest.startswith(("{", "[", "'", chr(34)))
                    and parse_literal(rest) is None
                )
                if (
                    not rest
                    or incomplete
                    or re.fullmatch(
                        r"(?:&[A-Za-z0-9_-]+\s+)?[|>][0-9+-]*(?:\s+#.*)?", rest
                    )
                ):
                    self.block_indent = indent
                    self.sensitive_indentation = True
                    self.hide_next = not rest
                break
        message = clean_record(text)
        return SafeRecord(message, matched, message != raw)
