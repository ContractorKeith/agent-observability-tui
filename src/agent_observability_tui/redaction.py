"""Secret redaction and hostile-text sanitization for untrusted trace input.

This module has no logging side effects.  It returns sanitized values plus explicit metadata so
callers never need to include the original input in an error, diagnostic, or truncation message.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"
CONTENT_OMITTED = "[CONTENT OMITTED]"
VALUE_OMITTED = "[VALUE OMITTED]"
DEFAULT_DISPLAY_BYTES = 4 * 1024
DEFAULT_STRING_BYTES = 16 * 1024
DEFAULT_PAYLOAD_BYTES = 256 * 1024

_SECRET_KEYS = {
    "apikey",
    "authorization",
    "authtoken",
    "clientsecret",
    "cookie",
    "credentials",
    "credential",
    "connectionstring",
    "databaseurl",
    "dsn",
    "idtoken",
    "password",
    "passwd",
    "privatekey",
    "proxyauthorization",
    "pwd",
    "refreshtoken",
    "secret",
    "secretaccesskey",
    "setcookie",
    "token",
}
_SECRET_KEY_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authsecret",
    "clientsecret",
    "privatekey",
    "refreshtoken",
    "secretaccesskey",
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "cookie",
    "credential",
    "credentials",
)

_SECRET_KEY_PREFIXES = (
    "authorization",
    "authsecret",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
)

_CONTENT_KEYS = {
    "action",
    "args",
    "arguments",
    "body",
    "command",
    "content",
    "data",
    "input",
    "message",
    "messages",
    "observation",
    "output",
    "prompt",
    "reasoning",
    "request",
    "response",
    "result",
    "results",
    "stderr",
    "stdin",
    "stdout",
    "systemprompt",
    "text",
    "thought",
    "toolcalls",
    "toolinput",
    "tooloutput",
}

_METADATA_KEYS = {
    "adapter",
    "adapterstability",
    "adapterversion",
    "amountusd",
    "argumentcount",
    "bytecount",
    "cachecreationsinputtokens",
    "cachereadtokens",
    "cachewriteinputtokens",
    "cachewritetokens",
    "category",
    "contentcapture",
    "cpupercent",
    "createdat",
    "currency",
    "direction",
    "durationms",
    "effectivefrom",
    "endedat",
    "endreason",
    "errorcount",
    "errortype",
    "eventcount",
    "eventid",
    "eventtype",
    "evidencecount",
    "executable",
    "exitstatus",
    "finishreason",
    "hermesversion",
    "id",
    "ingestsequence",
    "inputtokencount",
    "inputtokens",
    "jsonrpc",
    "kind",
    "level",
    "linenumber",
    "logger",
    "maxlinebytes",
    "method",
    "model",
    "modelname",
    "name",
    "observedtimestamp",
    "originalbytes",
    "outputbytes",
    "outputtokencount",
    "outputtokens",
    "parentspanid",
    "phase",
    "pid",
    "policy",
    "pricecatalogversion",
    "priceeffectivefrom",
    "pricingversion",
    "progress",
    "progresstoken",
    "provenance",
    "provider",
    "reason",
    "reasoningtokens",
    "redactedcount",
    "requestid",
    "resultcount",
    "retainedbytes",
    "returncode",
    "role",
    "rssbytes",
    "runid",
    "schema",
    "sessionid",
    "signalnumber",
    "source",
    "sourceadapter",
    "sourceingestsequence",
    "sourcename",
    "sourcesessionid",
    "sourcetimestamp",
    "spanid",
    "startedat",
    "status",
    "stream",
    "timestamp",
    "toolcallid",
    "toolname",
    "tokencount",
    "total",
    "totaltokencount",
    "totaltokens",
    "traceid",
    "transport",
    "truncated",
    "truncations",
    "type",
    "version",
    "sanitization",
}

# Terminal escape families are removed before ordinary control characters.  The end-of-string
# alternatives intentionally discard unterminated OSC/DCS sequences instead of exposing payload.
_OSC_RE = re.compile(r"(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)", re.DOTALL)
_DCS_RE = re.compile(r"(?:\x1b[P^_]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x9c|$)", re.DOTALL)
_CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_ESC_RE = re.compile(r"\x1b(?:[ -/]*[@-~])?")

_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|password|passwd|pwd|secret|token|authorization|cookie)"
        r"\s*[:=]\s*)(?:[\"']?)([^\s,;\"']{4,})"
    ),
    re.compile(
        r"\b(?:sk-(?:proj-)?|sk_live_|rk_live_|ghp_|github_pat_|glpat-|npm_|pypi-|"
        r"AIza|hf_|xox[baprs]-)[A-Za-z0-9_-]{12,}\b"
    ),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(r"(?i)([?&](?:api[_-]?key|token|secret|password)=)[^&#\s]+"),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+@"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)


@dataclass(frozen=True, slots=True)
class Truncation:
    """Location and byte counts for a value shortened by policy."""

    path: str
    original_bytes: int
    retained_bytes: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "original_bytes": self.original_bytes,
            "retained_bytes": self.retained_bytes,
        }


@dataclass(frozen=True, slots=True)
class SanitizedPayload:
    """A sanitized value and provenance safe to include in a canonical event."""

    value: Any
    redacted_count: int
    truncations: tuple[Truncation, ...]
    original_bytes: int
    output_bytes: int

    @property
    def truncated(self) -> bool:
        return bool(self.truncations)

    def metadata(self) -> dict[str, Any]:
        return {
            "policy": "agenttrace.redaction/1",
            "redacted_count": self.redacted_count,
            "truncated": self.truncated,
            "original_bytes": self.original_bytes,
            "output_bytes": self.output_bytes,
            "truncations": [item.to_dict() for item in self.truncations],
        }


def _json_bytes(value: Any, *, compact: bool = True) -> bytes:
    separators = (",", ":") if compact else None
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=separators,
        sort_keys=True,
    ).encode("utf-8")


def _source_size(value: Any) -> int:
    try:
        return len(_json_bytes(value))
    except (TypeError, ValueError, RecursionError):
        if isinstance(value, str):
            return len(value.encode("utf-8", errors="replace"))
        if isinstance(value, bytes):
            return len(value)
        return 0


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    if max_bytes <= 0:
        return ""
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _secret_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return (
        normalized in _SECRET_KEYS
        or normalized.endswith(_SECRET_KEY_SUFFIXES)
        or normalized.startswith(_SECRET_KEY_PREFIXES)
    )


def _remove_terminal_controls(value: str) -> str:
    result = _OSC_RE.sub("", value)
    result = _DCS_RE.sub("", result)
    result = _CSI_RE.sub("", result)
    result = _ESC_RE.sub("", result)
    # Newlines, tabs, bidi overrides, zero-width controls, C0/C1, and DEL are all inertly removed.
    return "".join(
        character for character in result if unicodedata.category(character) not in {"Cc", "Cf"}
    )


def _redact_text(value: str) -> tuple[str, int]:
    count = 0
    result = value
    for index, pattern in enumerate(_VALUE_PATTERNS):
        if index in {1, 5, 6}:
            result, replacements = pattern.subn(lambda match: f"{match.group(1)}{REDACTED}", result)
        else:
            result, replacements = pattern.subn(REDACTED, result)
        count += replacements
    return result, count


def _neutralize_markup(value: str) -> str:
    # Rich treats an escaped opening bracket as literal text.  Escaping every opener prevents
    # untrusted tags, links, and closing tags from becoming markup while keeping the text legible.
    # Normalizing any existing slash run makes this operation idempotent across pipeline layers.
    return re.sub(r"\\*\[", r"\\[", value)


def _sanitize_string(value: str, *, max_bytes: int) -> tuple[str, int, int, int]:
    original_bytes = len(value.encode("utf-8", errors="replace"))
    controlled = _remove_terminal_controls(value)
    redacted, count = _redact_text(controlled)
    inert = _neutralize_markup(redacted)
    truncated = _truncate_utf8(inert, max_bytes)
    return truncated, count, original_bytes, len(truncated.encode("utf-8"))


def sanitize_text(
    value: str | bytes, *, max_bytes: int = DEFAULT_DISPLAY_BYTES
) -> SanitizedPayload:
    """Return bounded terminal-inert text with redaction and byte-count metadata."""

    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if isinstance(value, bytes):
        original_bytes = len(value)
        decoded = value.decode("utf-8", errors="replace")
    else:
        original_bytes = len(value.encode("utf-8", errors="replace"))
        decoded = value
    sanitized, count, _, retained = _sanitize_string(decoded, max_bytes=max_bytes)
    truncations = (
        (Truncation(path="$", original_bytes=original_bytes, retained_bytes=retained),)
        if retained
        < len(
            _neutralize_markup(_redact_text(_remove_terminal_controls(decoded))[0]).encode("utf-8")
        )
        else ()
    )
    return SanitizedPayload(
        value=sanitized,
        redacted_count=count,
        truncations=truncations,
        original_bytes=original_bytes,
        output_bytes=retained,
    )


def sanitize_payload(
    value: Any,
    *,
    max_string_bytes: int = DEFAULT_STRING_BYTES,
    max_payload_bytes: int = DEFAULT_PAYLOAD_BYTES,
    max_depth: int = 32,
    max_items: int = 10_000,
) -> SanitizedPayload:
    """Recursively sanitize an untrusted JSON-like value without raising on unknown shapes.

    Oversized complete payloads become a bounded, self-describing preview.  The preview is made
    only after recursive redaction, so truncation cannot reveal a prefix of a credential.
    """

    if max_string_bytes < 1:
        raise ValueError("max_string_bytes must be positive")
    if max_payload_bytes < 96:
        raise ValueError("max_payload_bytes must be at least 96")
    if max_depth < 1 or max_items < 1:
        raise ValueError("max_depth and max_items must be positive")

    original_bytes = _source_size(value)
    redacted_count = 0
    truncations: list[Truncation] = []
    active: set[int] = set()
    visited_items = 0

    def walk(item: Any, path: str, depth: int) -> Any:
        nonlocal redacted_count, visited_items
        visited_items += 1
        if visited_items > max_items:
            truncations.append(Truncation(path=path, original_bytes=0, retained_bytes=0))
            return "[ITEM LIMIT]"
        if depth > max_depth:
            truncations.append(Truncation(path=path, original_bytes=0, retained_bytes=0))
            return "[DEPTH LIMIT]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else "[NON-FINITE NUMBER]"
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        if isinstance(item, str):
            sanitized, count, item_original, retained = _sanitize_string(
                item, max_bytes=max_string_bytes
            )
            redacted_count += count
            cleaned_unbounded, _, _, unbounded_bytes = _sanitize_string(
                item, max_bytes=max(len(item.encode("utf-8", errors="replace")) * 2, 1)
            )
            del cleaned_unbounded
            if retained < unbounded_bytes:
                truncations.append(
                    Truncation(path=path, original_bytes=item_original, retained_bytes=retained)
                )
            return sanitized
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                truncations.append(Truncation(path=path, original_bytes=0, retained_bytes=0))
                return "[CYCLE]"
            active.add(identity)
            result: dict[str, Any] = {}
            try:
                for key, nested in item.items():
                    if visited_items >= max_items:
                        truncations.append(
                            Truncation(path=path, original_bytes=0, retained_bytes=0)
                        )
                        result["_truncated_items"] = "[ITEM LIMIT]"
                        break
                    safe_key, key_count, _, _ = _sanitize_string(
                        str(key), max_bytes=min(max_string_bytes, 512)
                    )
                    redacted_count += key_count
                    child_path = f"{path}.{safe_key}"
                    if _secret_key(key) or _secret_key(safe_key):
                        result[safe_key] = REDACTED
                        redacted_count += 1
                    else:
                        result[safe_key] = walk(nested, child_path, depth + 1)
            except Exception:  # defensive for hostile custom mapping implementations
                result["_mapping_error"] = "[UNREADABLE MAPPING]"
            finally:
                active.discard(identity)
            return result
        if isinstance(item, Sequence):
            identity = id(item)
            if identity in active:
                truncations.append(Truncation(path=path, original_bytes=0, retained_bytes=0))
                return "[CYCLE]"
            active.add(identity)
            try:
                result_items = []
                for index, nested in enumerate(item):
                    if visited_items >= max_items:
                        truncations.append(
                            Truncation(path=path, original_bytes=0, retained_bytes=0)
                        )
                        result_items.append("[ITEM LIMIT]")
                        break
                    result_items.append(walk(nested, f"{path}[{index}]", depth + 1))
                return result_items
            except Exception:  # defensive for hostile custom sequence implementations
                return ["[UNREADABLE SEQUENCE]"]
            finally:
                active.discard(identity)
        return f"[UNSUPPORTED {type(item).__name__}]"

    sanitized = walk(value, "$", 0)
    encoded = _json_bytes(sanitized, compact=False)
    if len(encoded) > max_payload_bytes:
        serialized = _json_bytes(sanitized).decode("utf-8")
        low, high = 0, len(serialized)
        best: dict[str, Any] = {"_truncated": True, "_original_bytes": original_bytes}
        while low <= high:
            midpoint = (low + high) // 2
            preview = _truncate_utf8(serialized, midpoint)
            candidate = {
                "_truncated": True,
                "_original_bytes": original_bytes,
                "_preview": preview,
            }
            if len(_json_bytes(candidate, compact=False)) <= max_payload_bytes:
                best = candidate
                low = midpoint + 1
            else:
                high = midpoint - 1
        retained = len(best.get("_preview", "").encode("utf-8"))
        truncations.append(
            Truncation(path="$", original_bytes=original_bytes, retained_bytes=retained)
        )
        sanitized = best
        encoded = _json_bytes(sanitized, compact=False)
    return SanitizedPayload(
        value=sanitized,
        redacted_count=redacted_count,
        truncations=tuple(truncations),
        original_bytes=original_bytes,
        output_bytes=len(encoded),
    )


def minimize_sensitive_content(value: Any, *, allow_observed_log_text: bool = False) -> Any:
    """Replace prompt/tool/body values while retaining their structural keys.

    The input must already have passed :func:`sanitize_payload`. Plain process log events may
    retain their observed text because capturing stdout/stderr is their explicit product purpose.
    """

    log_keys = {"content", "data", "message", "stderr", "stdout", "text"}

    def walk(item: Any, *, preserve_scalar: bool = False) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if normalized in _CONTENT_KEYS and not (
                    allow_observed_log_text and normalized in log_keys
                ):
                    result[str(key)] = CONTENT_OMITTED
                else:
                    result[str(key)] = walk(
                        nested,
                        preserve_scalar=(
                            normalized in _METADATA_KEYS
                            or (allow_observed_log_text and normalized in log_keys)
                        ),
                    )
            return result
        if isinstance(item, (list, tuple)):
            return [walk(nested, preserve_scalar=preserve_scalar) for nested in item]
        return item if preserve_scalar else VALUE_OMITTED

    return walk(value)


__all__ = [
    "DEFAULT_DISPLAY_BYTES",
    "DEFAULT_PAYLOAD_BYTES",
    "DEFAULT_STRING_BYTES",
    "CONTENT_OMITTED",
    "REDACTED",
    "VALUE_OMITTED",
    "SanitizedPayload",
    "Truncation",
    "minimize_sensitive_content",
    "sanitize_payload",
    "sanitize_text",
]
