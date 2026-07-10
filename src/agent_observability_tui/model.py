"""Canonical, source-neutral trace event records.

The model deliberately contains no framework types.  Events are immutable once built and
serialize to the stable ``agenttrace.event/1`` envelope used by storage and exports.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, TypeAlias, cast

from .redaction import minimize_sensitive_content, sanitize_payload, sanitize_text

SCHEMA_VERSION = "agenttrace.event/1"
MAX_TOKEN_COUNT = 2**63 - 1
MAX_COST_USD = Decimal("1000000000000")

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | Sequence["JsonValue"]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"canonical event values must be JSON-compatible, got {type(value).__name__}")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _format_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, field_name: str, required: bool) -> datetime | None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from error
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO 8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_string(value: object) -> str | None:
    return None if value is None else _safe_string(value)


def _safe_string(value: object, *, max_bytes: int = 512) -> str:
    return cast(str, sanitize_text(str(value), max_bytes=max_bytes).value)


def _non_negative(value: int | None, *, field_name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{field_name} must be a non-negative integer or null")


@dataclass(frozen=True, slots=True)
class CorrelationIds:
    """Identifiers used to join events without imposing one tracing vendor's vocabulary."""

    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    request_id: str | None = None
    tool_call_id: str | None = None
    run_id: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "trace_id",
            "span_id",
            "parent_span_id",
            "request_id",
            "tool_call_id",
            "run_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _safe_string(value, max_bytes=256))
        sanitized_extra = sanitize_payload(self.extra, max_payload_bytes=16 * 1024).value
        safe_extra = sanitized_extra if isinstance(sanitized_extra, Mapping) else {}
        object.__setattr__(
            self,
            "extra",
            MappingProxyType({str(key): str(value) for key, value in safe_extra.items()}),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for name in (
            "trace_id",
            "span_id",
            "parent_span_id",
            "request_id",
            "tool_call_id",
            "run_id",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.extra:
            result["extra"] = dict(self.extra)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> CorrelationIds:
        if not value:
            return cls()
        known = {
            "trace_id",
            "span_id",
            "parent_span_id",
            "request_id",
            "tool_call_id",
            "run_id",
        }
        supplied_extra = value.get("extra")
        extra = (
            {str(key): str(item) for key, item in supplied_extra.items()}
            if isinstance(supplied_extra, Mapping)
            else {}
        )
        extra.update(
            {str(key): str(item) for key, item in value.items() if key not in known | {"extra"}}
        )
        return cls(
            trace_id=_optional_string(value.get("trace_id")),
            span_id=_optional_string(value.get("span_id")),
            parent_span_id=_optional_string(value.get("parent_span_id")),
            request_id=_optional_string(value.get("request_id")),
            tool_call_id=_optional_string(value.get("tool_call_id")),
            run_id=_optional_string(value.get("run_id")),
            extra=extra,
        )


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported token counters with explicit provenance."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    provenance: str = "source-reported"

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
        ):
            _non_negative(getattr(self, name), field_name=name)
            if (value := getattr(self, name)) is not None and value > MAX_TOKEN_COUNT:
                raise ValueError(f"{name} exceeds the supported maximum")
        if not self.provenance:
            raise ValueError("token provenance must not be empty")
        object.__setattr__(self, "provenance", _safe_string(self.provenance, max_bytes=128))

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"provenance": self.provenance}
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> TokenUsage | None:
        if value is None:
            return None

        def integer(name: str) -> int | None:
            item = value.get(name)
            if item is None:
                return None
            if isinstance(item, int) and not isinstance(item, bool):
                return item
            if isinstance(item, str) and item.isdecimal():
                return int(item)
            raise ValueError(f"{name} must be a non-negative integer or null")

        return cls(
            input_tokens=integer("input_tokens"),
            output_tokens=integer("output_tokens"),
            reasoning_tokens=integer("reasoning_tokens"),
            cache_read_tokens=integer("cache_read_tokens"),
            cache_write_tokens=integer("cache_write_tokens"),
            total_tokens=integer("total_tokens"),
            provenance=str(value.get("provenance", "source-reported")),
        )


@dataclass(frozen=True, slots=True)
class Cost:
    """A source-reported or locally estimated cost, serialized exactly as decimal text."""

    amount_usd: str
    provenance: str
    currency: str = "USD"
    price_catalog_version: str | None = None
    price_effective_from: str | None = None

    def __post_init__(self) -> None:
        try:
            amount = Decimal(self.amount_usd)
        except (InvalidOperation, ValueError) as error:
            raise ValueError("amount_usd must be decimal text") from error
        if not amount.is_finite() or amount < 0 or amount > MAX_COST_USD:
            raise ValueError("amount_usd must be a finite non-negative decimal within bounds")
        if not self.provenance:
            raise ValueError("cost provenance must not be empty")
        if self.currency != "USD":
            raise ValueError("only USD costs are supported by agenttrace.event/1")
        object.__setattr__(self, "provenance", _safe_string(self.provenance, max_bytes=128))
        if self.price_catalog_version is not None:
            object.__setattr__(
                self,
                "price_catalog_version",
                _safe_string(self.price_catalog_version, max_bytes=128),
            )
        if self.price_effective_from is not None:
            try:
                date.fromisoformat(self.price_effective_from)
            except ValueError as error:
                raise ValueError("price_effective_from must be YYYY-MM-DD") from error

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "amount_usd": self.amount_usd,
            "currency": self.currency,
            "provenance": self.provenance,
        }
        if self.price_catalog_version is not None:
            result["price_catalog_version"] = self.price_catalog_version
        if self.price_effective_from is not None:
            result["price_effective_from"] = self.price_effective_from
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> Cost | None:
        if value is None:
            return None
        if value.get("amount_usd") is None:
            raise ValueError("cost amount_usd is required")
        if value.get("provenance") is None:
            raise ValueError("cost provenance is required")
        return cls(
            amount_usd=str(value["amount_usd"]),
            provenance=str(value["provenance"]),
            currency=str(value.get("currency", "USD")),
            price_catalog_version=_optional_string(value.get("price_catalog_version")),
            price_effective_from=_optional_string(value.get("price_effective_from")),
        )


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """Immutable canonical event for storage, replay, presentation, and export."""

    event_id: str
    session_id: str
    ingest_sequence: int
    source_timestamp: datetime | None
    observed_timestamp: datetime
    adapter: str
    adapter_version: str
    category: str
    kind: str
    phase: str | None = None
    status: str | None = None
    direction: str | None = None
    transport: str | None = None
    correlation: CorrelationIds = field(default_factory=CorrelationIds)
    token_usage: TokenUsage | None = None
    cost: Cost | None = None
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    raw: JsonValue = None
    schema: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_VERSION:
            raise ValueError(f"unsupported event schema: {self.schema!r}")
        for name in ("event_id", "session_id", "adapter", "adapter_version", "category", "kind"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, _safe_string(getattr(self, name)))
        for name in ("phase", "status", "direction", "transport"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _safe_string(value, max_bytes=128))
        _non_negative(self.ingest_sequence, field_name="ingest_sequence")
        object.__setattr__(
            self,
            "source_timestamp",
            _parse_timestamp(self.source_timestamp, field_name="source_timestamp", required=False),
        )
        object.__setattr__(
            self,
            "observed_timestamp",
            _parse_timestamp(
                self.observed_timestamp, field_name="observed_timestamp", required=True
            ),
        )
        sanitized_attributes = sanitize_payload(self.attributes)
        sanitized_raw = sanitize_payload(self.raw)
        allow_observed_log_text = self.category == "log"
        attributes_value = minimize_sensitive_content(
            sanitized_attributes.value,
            allow_observed_log_text=allow_observed_log_text,
        )
        raw_value = minimize_sensitive_content(
            sanitized_raw.value,
            allow_observed_log_text=allow_observed_log_text,
        )
        if not isinstance(attributes_value, Mapping):
            attributes_value = {"_value": attributes_value}
        attributes_dict = dict(attributes_value)
        was_changed = (
            sanitized_attributes.redacted_count
            or sanitized_attributes.truncated
            or sanitized_raw.redacted_count
            or sanitized_raw.truncated
        )
        # Adapter metadata is authoritative for the original raw input.  Preserve it unchanged on
        # rehydration; direct callers without metadata still receive canonical policy provenance.
        if was_changed and "_sanitization" not in attributes_dict:
            attributes_dict["_sanitization"] = {
                "policy": "agenttrace.redaction/1",
                "attributes": sanitized_attributes.metadata(),
                "raw": sanitized_raw.metadata(),
            }
        attributes_dict.setdefault(
            "_content_capture",
            "observed-log" if allow_observed_log_text else "metadata-only",
        )
        object.__setattr__(self, "attributes", _freeze(attributes_dict))
        object.__setattr__(self, "raw", _freeze(raw_value))

    @property
    def trace_id(self) -> str | None:
        return self.correlation.trace_id

    @property
    def span_id(self) -> str | None:
        return self.correlation.span_id

    @property
    def parent_span_id(self) -> str | None:
        return self.correlation.parent_span_id

    @property
    def request_id(self) -> str | None:
        return self.correlation.request_id

    @property
    def tool_call_id(self) -> str | None:
        return self.correlation.tool_call_id

    @property
    def run_id(self) -> str | None:
        return self.correlation.run_id

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "schema": self.schema,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "ingest_sequence": self.ingest_sequence,
            "source_timestamp": _format_timestamp(self.source_timestamp),
            "observed_timestamp": _format_timestamp(self.observed_timestamp),
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "category": self.category,
            "kind": self.kind,
            "correlation": self.correlation.to_dict(),
            "attributes": _thaw(self.attributes),
            "raw": _thaw(self.raw),
        }
        for name in ("phase", "status", "direction", "transport"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.token_usage is not None:
            result["token_usage"] = self.token_usage.to_dict()
        if self.cost is not None:
            result["cost"] = self.cost.to_dict()
        return result

    def to_json(self) -> str:
        """Return deterministic compact JSON suitable for JSONL and hash input."""

        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TraceEvent:
        schema = str(value.get("schema", SCHEMA_VERSION))
        correlation = value.get("correlation")
        token_usage = value.get("token_usage")
        cost = value.get("cost")
        attributes = value.get("attributes", {})
        if not isinstance(correlation, Mapping) and correlation is not None:
            raise TypeError("correlation must be an object")
        if not isinstance(token_usage, Mapping) and token_usage is not None:
            raise TypeError("token_usage must be an object")
        if not isinstance(cost, Mapping) and cost is not None:
            raise TypeError("cost must be an object")
        if not isinstance(attributes, Mapping):
            raise TypeError("attributes must be an object")
        return cls(
            schema=schema,
            event_id=str(value["event_id"]),
            session_id=str(value["session_id"]),
            ingest_sequence=int(cast(int | str, value["ingest_sequence"])),
            source_timestamp=_parse_timestamp(
                value.get("source_timestamp"), field_name="source_timestamp", required=False
            ),
            observed_timestamp=cast(
                datetime,
                _parse_timestamp(
                    value.get("observed_timestamp"),
                    field_name="observed_timestamp",
                    required=True,
                ),
            ),
            adapter=str(value["adapter"]),
            adapter_version=str(value["adapter_version"]),
            category=str(value["category"]),
            kind=str(value["kind"]),
            phase=_optional_string(value.get("phase")),
            status=_optional_string(value.get("status")),
            direction=_optional_string(value.get("direction")),
            transport=_optional_string(value.get("transport")),
            correlation=CorrelationIds.from_dict(cast(Mapping[str, object] | None, correlation)),
            token_usage=TokenUsage.from_dict(cast(Mapping[str, object] | None, token_usage)),
            cost=Cost.from_dict(cast(Mapping[str, object] | None, cost)),
            attributes=cast(Mapping[str, JsonValue], attributes),
            raw=cast(JsonValue, value.get("raw")),
        )

    @classmethod
    def from_json(cls, value: str | bytes) -> TraceEvent:
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            raise TypeError("canonical event JSON must contain an object")
        return cls.from_dict(decoded)


__all__ = [
    "SCHEMA_VERSION",
    "CorrelationIds",
    "Cost",
    "JsonValue",
    "TokenUsage",
    "TraceEvent",
]
