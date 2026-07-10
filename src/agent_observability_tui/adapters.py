"""Inbound adapters from captured agent/framework records to canonical trace events.

Native ``agenttrace.event/1`` is stable.  Framework adapters are intentionally labeled
experimental: they parse captured/exported records and do not attach to live protocol streams.
Every adapter is total over untrusted input—bad records become diagnostic events, never exceptions.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol, cast

from .model import SCHEMA_VERSION, CorrelationIds, Cost, TokenUsage, TraceEvent
from .redaction import SanitizedPayload, sanitize_payload, sanitize_text

ADAPTER_VERSION = "1"
MAX_EXPANDED_EVENTS = 1_000
MAX_RECORD_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Collector-owned facts assigned when a record crosses the inbound seam."""

    session_id: str
    ingest_sequence: int
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_name: str = "import"
    transport: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.ingest_sequence < 0:
            raise ValueError("ingest_sequence must be non-negative")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def at_sequence(self, sequence: int) -> AdapterContext:
        return replace(self, ingest_sequence=sequence)


class EventAdapter(Protocol):
    name: str
    version: str
    stability: str

    def parse(self, record: object, context: AdapterContext) -> tuple[TraceEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class _ExpansionLimit:
    total: int


def _safe_text(value: object, *, limit: int = 512) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return cast(str, sanitize_text(value, max_bytes=limit).value)
    if isinstance(value, (str, int, float, bool)):
        return cast(str, sanitize_text(str(value), max_bytes=limit).value)
    return f"[UNSUPPORTED {type(value).__name__}]"


def _decode(record: object) -> tuple[object | None, str | None, object]:
    if isinstance(record, bytes):
        if len(record) > MAX_RECORD_BYTES:
            return None, f"record exceeds {MAX_RECORD_BYTES}-byte adapter limit", record
        source: object = record.decode("utf-8", errors="replace")
    else:
        source = record
    if isinstance(source, str):
        if len(source.encode("utf-8", errors="replace")) > MAX_RECORD_BYTES:
            return None, f"record exceeds {MAX_RECORD_BYTES}-byte adapter limit", source
        try:
            return json.loads(source), None, source
        except (json.JSONDecodeError, RecursionError):
            return None, "malformed JSON", source
    return source, None, source


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


def _nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _first(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if mapping.get(name) is not None:
            return mapping[name]
    return None


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        converted = value
    elif isinstance(value, str) and value.isdecimal():
        converted = int(value)
    else:
        return None
    return converted if converted >= 0 else None


def _usage(mapping: Mapping[str, Any]) -> TokenUsage | None:
    candidates = (
        mapping.get("token_usage"),
        mapping.get("usage"),
        _nested(mapping, "result", "usage"),
        _nested(mapping, "metrics", "usage"),
        _nested(mapping, "llm_metrics", "token_usage"),
        mapping,
    )
    source = next((item for item in candidates if isinstance(item, Mapping)), None)
    if not isinstance(source, Mapping):
        return None

    def pick(*names: str) -> int | None:
        return _integer(_first(cast(Mapping[str, Any], source), *names))

    values = {
        "input_tokens": pick("input_tokens", "prompt_tokens", "inputTokenCount"),
        "output_tokens": pick("output_tokens", "completion_tokens", "outputTokenCount"),
        "reasoning_tokens": pick("reasoning_tokens", "thinking_tokens"),
        "cache_read_tokens": pick(
            "cache_read_tokens", "cached_input_tokens", "cacheReadInputTokens"
        ),
        "cache_write_tokens": pick("cache_write_tokens", "cacheCreationInputTokens"),
        "total_tokens": pick("total_tokens", "totalTokenCount"),
    }
    if all(item is None for item in values.values()):
        return None
    return TokenUsage(**values, provenance="source-reported")


def _cost(mapping: Mapping[str, Any]) -> Cost | None:
    candidates: tuple[tuple[Any, str], ...] = (
        (mapping.get("actual_cost_usd"), "source-reported"),
        (mapping.get("cost_usd"), "source-reported"),
        (mapping.get("estimated_cost_usd"), "estimated"),
        (_nested(mapping, "usage", "cost_usd"), "source-reported"),
        (_nested(mapping, "llm_metrics", "accumulated_cost"), "source-reported"),
        (_nested(mapping, "metrics", "cost_usd"), "source-reported"),
    )
    pricing_version = _first(mapping, "pricing_version", "price_catalog_version")
    if pricing_version is None:
        pricing_version = _nested(mapping, "usage", "pricing_version")
    effective_from = _first(mapping, "price_effective_from", "pricing_effective_from")
    for value, provenance in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            return Cost(
                amount_usd=str(value),
                provenance=provenance,
                price_catalog_version=(_safe_text(pricing_version, limit=128) or None),
                price_effective_from=_safe_text(effective_from, limit=32) or None,
            )
        except ValueError:
            continue
    return None


def _correlation(mapping: Mapping[str, Any], *, rpc_id: object = None) -> CorrelationIds:
    metadata = mapping.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}

    def pick(*names: str) -> str | None:
        value = _first(mapping, *names)
        if value is None:
            value = _first(cast(Mapping[str, Any], metadata), *names)
        text = _safe_text(value, limit=256)
        return text or None

    request_id = pick("request_id", "requestId")
    if request_id is None and rpc_id is not None:
        request_id = _safe_text(rpc_id, limit=256) or None
    tool_call_id = pick("tool_call_id", "toolCallId", "call_id")
    return CorrelationIds(
        trace_id=pick("trace_id", "traceId"),
        span_id=pick("span_id", "spanId"),
        parent_span_id=pick("parent_span_id", "parentSpanId"),
        request_id=request_id,
        tool_call_id=tool_call_id,
        run_id=pick("run_id", "runId"),
    )


def _event_id(context: AdapterContext, adapter: str, kind: str, supplied: object = None) -> str:
    if supplied is not None:
        candidate = _safe_text(supplied, limit=256)
        if candidate:
            return candidate
    identity = f"{context.session_id}:{context.ingest_sequence}:{adapter}:{kind}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def _sanitized_mapping(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], SanitizedPayload]:
    result = sanitize_payload(value)
    if isinstance(result.value, Mapping):
        return cast(Mapping[str, Any], result.value), result
    return {"_sanitized_value": result.value}, result


def _attributes(
    payload: Mapping[str, Any],
    sanitized: SanitizedPayload,
    *,
    known: set[str],
    selected: Mapping[str, Any] | None = None,
    stability: str,
) -> dict[str, Any]:
    result = dict(selected or {})
    unknown = {key: value for key, value in payload.items() if key not in known}
    if unknown:
        result["unknown_fields"] = unknown
    metadata = sanitized.metadata()
    metadata["adapter_stability"] = stability
    result["_sanitization"] = metadata
    return result


def _diagnostic(
    context: AdapterContext,
    *,
    adapter: str,
    version: str,
    reason: str,
    record: object,
    kind: str = "adapter.invalid",
    stability: str = "experimental",
) -> TraceEvent:
    sanitized = sanitize_payload(record)
    safe_reason = _safe_text(reason, limit=512) or "invalid adapter input"
    sanitization_metadata = sanitized.metadata()
    if "adapter limit" in reason and not sanitization_metadata["truncated"]:
        sanitization_metadata["truncated"] = True
        sanitization_metadata["truncations"] = [
            {
                "path": "$",
                "original_bytes": sanitized.original_bytes,
                "retained_bytes": sanitized.output_bytes,
            }
        ]
    attributes: dict[str, Any] = {
        "reason": safe_reason,
        "source_name": _safe_text(context.source_name, limit=128),
        "_sanitization": {**sanitization_metadata, "adapter_stability": stability},
    }
    return TraceEvent(
        event_id=_event_id(context, adapter, kind),
        session_id=context.session_id,
        ingest_sequence=context.ingest_sequence,
        source_timestamp=None,
        observed_timestamp=context.observed_at,
        adapter=adapter,
        adapter_version=version,
        category="diagnostic",
        kind=kind,
        phase="ingest",
        status="warning",
        transport=context.transport,
        attributes=attributes,
        raw=None,
    )


class _BaseAdapter:
    name = "unknown"
    version = ADAPTER_VERSION
    stability = "experimental"

    def parse(self, record: object, context: AdapterContext) -> tuple[TraceEvent, ...]:
        decoded, error, original = _decode(record)
        if error is not None:
            return (
                _diagnostic(
                    context,
                    adapter=self.name,
                    version=self.version,
                    reason=error,
                    record=original,
                    kind="adapter.malformed",
                    stability=self.stability,
                ),
            )
        expanded = self._expand(decoded)
        events: list[TraceEvent] = []
        for offset, item in enumerate(expanded):
            item_context = context.at_sequence(context.ingest_sequence + offset)
            if isinstance(item, _ExpansionLimit):
                events.append(
                    _diagnostic(
                        item_context,
                        adapter=self.name,
                        version=self.version,
                        reason=f"record expansion exceeded {MAX_EXPANDED_EVENTS} events",
                        record=None,
                        kind="adapter.event_limit",
                        stability=self.stability,
                    )
                )
                continue
            if not isinstance(item, Mapping):
                events.append(
                    _diagnostic(
                        item_context,
                        adapter=self.name,
                        version=self.version,
                        reason="record is not a JSON object",
                        record=item,
                        stability=self.stability,
                    )
                )
                continue
            try:
                event = self._parse_mapping(cast(Mapping[str, Any], item), item_context)
            except Exception:
                # Do not render exception messages: custom inputs can place secrets in them.
                event = _diagnostic(
                    item_context,
                    adapter=self.name,
                    version=self.version,
                    reason="record does not satisfy this adapter contract",
                    record=item,
                    stability=self.stability,
                )
            events.append(event)
        if not events:
            events.append(
                _diagnostic(
                    context,
                    adapter=self.name,
                    version=self.version,
                    reason="record contains no events",
                    record=decoded,
                    stability=self.stability,
                )
            )
        return tuple(events)

    def _expand(self, record: object) -> list[object]:
        if isinstance(record, list):
            items = list(record)
        elif isinstance(record, Mapping) and isinstance(record.get("events"), list):
            items = list(cast(list[object], record["events"]))
        else:
            return [record]
        if len(items) > MAX_EXPANDED_EVENTS:
            return [*items[: MAX_EXPANDED_EVENTS - 1], _ExpansionLimit(len(items))]
        return items

    def _parse_mapping(self, record: Mapping[str, Any], context: AdapterContext) -> TraceEvent:
        raise NotImplementedError


class NativeAdapter(_BaseAdapter):
    name = "native"
    stability = "stable"

    def _parse_mapping(self, record: Mapping[str, Any], context: AdapterContext) -> TraceEvent:
        payload, sanitized = _sanitized_mapping(record)
        if payload.get("schema") != SCHEMA_VERSION:
            return _diagnostic(
                context,
                adapter=self.name,
                version=self.version,
                reason="unsupported native schema",
                record=payload,
                kind="adapter.unsupported_schema",
                stability=self.stability,
            )
        known = {
            "schema",
            "event_id",
            "session_id",
            "ingest_sequence",
            "source_timestamp",
            "observed_timestamp",
            "adapter",
            "adapter_version",
            "category",
            "kind",
            "phase",
            "status",
            "direction",
            "transport",
            "correlation",
            "token_usage",
            "cost",
            "attributes",
            "raw",
        }
        supplied_attributes = payload.get("attributes")
        selected = dict(supplied_attributes) if isinstance(supplied_attributes, Mapping) else {}
        selected["source_ingest_sequence"] = payload.get("ingest_sequence")
        selected["source_session_id"] = payload.get("session_id")
        selected["source_observed_timestamp"] = payload.get("observed_timestamp")
        selected["source_adapter"] = {
            "name": payload.get("adapter"),
            "version": payload.get("adapter_version"),
        }
        attributes = _attributes(
            payload, sanitized, known=known, selected=selected, stability=self.stability
        )
        correlation = payload.get("correlation")
        usage = payload.get("token_usage")
        cost = payload.get("cost")
        return TraceEvent(
            event_id=_event_id(
                context, self.name, _safe_text(payload.get("kind")), payload.get("event_id")
            ),
            session_id=context.session_id,
            ingest_sequence=context.ingest_sequence,
            source_timestamp=_timestamp(payload.get("source_timestamp")),
            observed_timestamp=context.observed_at,
            adapter=self.name,
            adapter_version=self.version,
            category=_safe_text(payload.get("category"), limit=128) or "unknown",
            kind=_safe_text(payload.get("kind"), limit=256) or "unknown",
            phase=_safe_text(payload.get("phase"), limit=128) or None,
            status=_safe_text(payload.get("status"), limit=128) or None,
            direction=_safe_text(payload.get("direction"), limit=128) or None,
            transport=_safe_text(payload.get("transport"), limit=128) or context.transport,
            correlation=CorrelationIds.from_dict(
                cast(
                    Mapping[str, object] | None,
                    correlation if isinstance(correlation, Mapping) else None,
                )
            ),
            token_usage=TokenUsage.from_dict(
                cast(Mapping[str, object] | None, usage if isinstance(usage, Mapping) else None)
            ),
            cost=Cost.from_dict(
                cast(Mapping[str, object] | None, cost if isinstance(cost, Mapping) else None)
            ),
            attributes=attributes,
            raw=cast(Any, payload),
        )


class McpJsonRpcAdapter(_BaseAdapter):
    name = "mcp-jsonrpc"

    def _parse_mapping(self, record: Mapping[str, Any], context: AdapterContext) -> TraceEvent:
        payload, sanitized = _sanitized_mapping(record)
        if payload.get("jsonrpc") != "2.0":
            return _diagnostic(
                context,
                adapter=self.name,
                version=self.version,
                reason="captured MCP record is not JSON-RPC 2.0",
                record=payload,
                stability=self.stability,
            )
        rpc_id = payload.get("id")
        method = _safe_text(payload.get("method"), limit=256)
        if method:
            phase = "request" if rpc_id is not None else "notification"
            status = None
            kind = method
        elif "error" in payload:
            phase, status, kind = "response", "error", "response"
        elif "result" in payload:
            phase, status, kind = "response", "ok", "response"
        else:
            return _diagnostic(
                context,
                adapter=self.name,
                version=self.version,
                reason="JSON-RPC record has no method, result, or error",
                record=payload,
                stability=self.stability,
            )
        category = "tool" if method.startswith("tools/") else "mcp"
        if category == "tool" and phase in {"request", "notification"}:
            phase = "start" if rpc_id is not None else "instant"
        correlation = _correlation(payload, rpc_id=rpc_id)
        if category == "tool" and correlation.tool_call_id is None and rpc_id is not None:
            correlation = replace(correlation, tool_call_id=_safe_text(rpc_id, limit=256))
        known = {
            "jsonrpc",
            "id",
            "method",
            "params",
            "result",
            "error",
            "timestamp",
            "time",
            "direction",
            "transport",
            "usage",
            "token_usage",
            "metadata",
            "trace_id",
            "span_id",
            "parent_span_id",
            "request_id",
            "tool_call_id",
            "run_id",
        }
        selected = {
            "rpc_id": rpc_id,
            "method": method or None,
            "params": payload.get("params"),
            "result": payload.get("result"),
            "error": payload.get("error"),
            "source_name": context.source_name,
        }
        return TraceEvent(
            event_id=_event_id(context, self.name, kind),
            session_id=context.session_id,
            ingest_sequence=context.ingest_sequence,
            source_timestamp=_timestamp(_first(payload, "timestamp", "time")),
            observed_timestamp=context.observed_at,
            adapter=self.name,
            adapter_version=self.version,
            category=category,
            kind=kind,
            phase=phase,
            status=status,
            direction=_safe_text(payload.get("direction"), limit=128) or None,
            transport=_safe_text(payload.get("transport"), limit=128) or context.transport,
            correlation=correlation,
            token_usage=_usage(payload),
            cost=_cost(payload),
            attributes=_attributes(
                payload, sanitized, known=known, selected=selected, stability=self.stability
            ),
            raw=cast(Any, payload),
        )


class HermesAdapter(_BaseAdapter):
    name = "hermes"

    def _expand(self, record: object) -> list[object]:
        if not isinstance(record, Mapping) or not isinstance(record.get("messages"), list):
            return super()._expand(record)
        session = {key: value for key, value in record.items() if key != "messages"}
        session_record = {
            **session,
            "provider": "hermes",
            "_hermes_record_type": "session",
            "type": "session",
        }
        session_context = {
            "session_id": session.get("id") or session.get("session_id"),
            "model": session.get("model"),
        }
        messages: list[object] = []
        source_messages = cast(list[object], record["messages"])
        message_budget = MAX_EXPANDED_EVENTS - 1
        truncated = len(source_messages) > message_budget
        retained_messages = message_budget - 1 if truncated else len(source_messages)
        for message in source_messages[:retained_messages]:
            if isinstance(message, Mapping):
                messages.append(
                    {
                        **message,
                        "provider": "hermes",
                        "_hermes_record_type": "message",
                        "session_id": message.get("session_id") or session_context["session_id"],
                        "model": message.get("model") or session_context["model"],
                    }
                )
            else:
                messages.append(message)
        if truncated:
            messages.append(
                {
                    "provider": "hermes",
                    "_hermes_record_type": "message_limit",
                    "type": "diagnostic",
                    "message_count": len(source_messages),
                }
            )
        return [session_record, *messages]

    def _parse_mapping(self, record: Mapping[str, Any], context: AdapterContext) -> TraceEvent:
        payload, sanitized = _sanitized_mapping(record)
        record_type = _safe_text(payload.get("_hermes_record_type"), limit=64)
        if record_type == "message_limit":
            return _diagnostic(
                context,
                adapter=self.name,
                version=self.version,
                reason="Hermes export exceeded the expanded-event limit",
                record=None,
                kind="adapter.event_limit",
                stability=self.stability,
            )
        role = _safe_text(payload.get("role"), limit=64)
        kind = _safe_text(_first(payload, "type", "event_type", "kind"), limit=256)
        if not kind and record_type == "message":
            if payload.get("tool_calls"):
                kind = "tool_call"
            elif role == "tool" or payload.get("tool_call_id") or payload.get("tool_name"):
                kind = "tool_result"
            else:
                kind = "message"
        if not kind:
            return _diagnostic(
                context,
                adapter=self.name,
                version=self.version,
                reason="Hermes export record has no event type",
                record=payload,
                stability=self.stability,
            )
        lowered = kind.casefold()
        if record_type == "session":
            category = "session"
        elif "tool" in lowered:
            category = "tool"
        elif role == "assistant" or "model" in lowered or "llm" in lowered:
            category = "model"
        else:
            category = "agent"
        phase = (
            "request"
            if any(word in lowered for word in ("call", "request", "start"))
            else "complete"
            if any(word in lowered for word in ("result", "response", "complete", "finish"))
            else None
        )
        status = "error" if "error" in lowered or payload.get("error") else payload.get("status")
        if record_type == "session":
            phase = "complete" if payload.get("ended_at") is not None else "start"
            status = "complete" if payload.get("ended_at") is not None else "running"
        elif record_type == "message" and phase is None:
            phase = "complete"
        if category == "tool":
            phase = "start" if "call" in lowered else "complete"
        known = {
            "provider",
            "session_id",
            "event_id",
            "id",
            "type",
            "event_type",
            "kind",
            "timestamp",
            "created_at",
            "tool",
            "model",
            "message",
            "content",
            "usage",
            "token_usage",
            "cost_usd",
            "estimated_cost_usd",
            "status",
            "error",
            "metadata",
            "trace_id",
            "span_id",
            "parent_span_id",
            "request_id",
            "tool_call_id",
            "run_id",
            "direction",
            "transport",
            "role",
            "tool_calls",
            "tool_name",
            "token_count",
            "finish_reason",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "actual_cost_usd",
            "started_at",
            "ended_at",
            "end_reason",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
            "pricing_version",
            "_hermes_record_type",
        }
        selected = {
            "model": payload.get("model"),
            "tool": payload.get("tool"),
            "message": payload.get("message"),
            "content": payload.get("content"),
            "source_session_id": payload.get("session_id"),
            "source_name": context.source_name,
            "role": role or None,
            "tool_calls": payload.get("tool_calls"),
            "tool_name": payload.get("tool_name"),
            "finish_reason": payload.get("finish_reason"),
            "token_count": payload.get("token_count"),
        }
        source_timestamp = _timestamp(_first(payload, "timestamp", "created_at"))
        if source_timestamp is None and record_type == "session":
            source_timestamp = _timestamp(payload.get("started_at"))
        return TraceEvent(
            event_id=_event_id(context, self.name, kind, _first(payload, "event_id", "id")),
            session_id=context.session_id,
            ingest_sequence=context.ingest_sequence,
            source_timestamp=source_timestamp,
            observed_timestamp=context.observed_at,
            adapter=self.name,
            adapter_version=self.version,
            category=category,
            kind=kind,
            phase=phase,
            status=_safe_text(status, limit=128) or None,
            direction=_safe_text(payload.get("direction"), limit=128) or None,
            transport=_safe_text(payload.get("transport"), limit=128) or context.transport,
            correlation=_correlation(payload),
            token_usage=_usage(payload),
            cost=_cost(payload),
            attributes=_attributes(
                payload, sanitized, known=known, selected=selected, stability=self.stability
            ),
            raw=cast(Any, payload),
        )


class OpenHandsAdapter(_BaseAdapter):
    name = "openhands"

    def _parse_mapping(self, record: Mapping[str, Any], context: AdapterContext) -> TraceEvent:
        payload, sanitized = _sanitized_mapping(record)
        kind = _safe_text(_first(payload, "kind", "type", "event_type"), limit=256)
        action = payload.get("action")
        observation = payload.get("observation")
        if not kind:
            nested_kind = (
                _first(cast(Mapping[str, Any], action), "kind", "action")
                if isinstance(action, Mapping)
                else _first(cast(Mapping[str, Any], observation), "kind", "observation")
                if isinstance(observation, Mapping)
                else None
            )
            kind = _safe_text(nested_kind, limit=256)
        if not kind:
            return _diagnostic(
                context,
                adapter=self.name,
                version=self.version,
                reason="OpenHands event has no kind or action/observation type",
                record=payload,
                stability=self.stability,
            )
        lowered = kind.casefold()
        if "action" in lowered or "observation" in lowered or "error" in lowered:
            category = "tool"
        elif "llm" in lowered or "message" in lowered:
            category = "model"
        else:
            category = "agent"
        phase = (
            "request"
            if action is not None or "action" in lowered
            else "complete"
            if observation is not None or "observation" in lowered
            else None
        )
        if "action" in lowered:
            phase = "start"
        elif "observation" in lowered or "error" in lowered:
            phase = "complete"
        status_value = payload.get("status")
        if payload.get("error") is not None or "error" in lowered:
            status_value = "error"
        elif "observation" in lowered and status_value is None:
            status_value = "ok"
        known = {
            "source",
            "id",
            "event_id",
            "timestamp",
            "created_at",
            "kind",
            "type",
            "event_type",
            "action",
            "observation",
            "message",
            "content",
            "token_usage",
            "usage",
            "llm_metrics",
            "metrics",
            "status",
            "error",
            "metadata",
            "trace_id",
            "span_id",
            "parent_span_id",
            "request_id",
            "tool_call_id",
            "run_id",
            "llm_response_id",
            "tool_name",
            "action_id",
        }
        selected = {
            "source": payload.get("source"),
            "action": action,
            "observation": observation,
            "message": payload.get("message"),
            "content": payload.get("content"),
            "source_name": context.source_name,
            "llm_response_id": payload.get("llm_response_id"),
            "tool_name": payload.get("tool_name"),
            "action_id": payload.get("action_id"),
        }
        return TraceEvent(
            event_id=_event_id(context, self.name, kind, _first(payload, "event_id", "id")),
            session_id=context.session_id,
            ingest_sequence=context.ingest_sequence,
            source_timestamp=_timestamp(_first(payload, "timestamp", "created_at")),
            observed_timestamp=context.observed_at,
            adapter=self.name,
            adapter_version=self.version,
            category=category,
            kind=kind,
            phase=phase,
            status=_safe_text(status_value, limit=128) or None,
            direction="outbound"
            if phase == "request"
            else "inbound"
            if phase == "complete" and category == "tool"
            else None,
            transport=context.transport,
            correlation=_correlation(payload),
            token_usage=_usage(payload),
            cost=_cost(payload),
            attributes=_attributes(
                payload, sanitized, known=known, selected=selected, stability=self.stability
            ),
            raw=cast(Any, payload),
        )


class UnknownAdapter(_BaseAdapter):
    def __init__(self, requested_name: str = "unknown") -> None:
        self.name = _safe_text(requested_name, limit=128) or "unknown"

    def _parse_mapping(self, record: Mapping[str, Any], context: AdapterContext) -> TraceEvent:
        return _diagnostic(
            context,
            adapter=self.name,
            version=self.version,
            reason="unrecognized captured event shape",
            record=record,
            kind="adapter.unknown",
            stability=self.stability,
        )


class AutoAdapter(_BaseAdapter):
    name = "auto"

    def parse(self, record: object, context: AdapterContext) -> tuple[TraceEvent, ...]:
        decoded, error, original = _decode(record)
        if error is not None:
            return UnknownAdapter("auto").parse(original, context)
        if isinstance(decoded, list):
            events: list[TraceEvent] = []
            sequence = context.ingest_sequence
            truncated = len(decoded) > MAX_EXPANDED_EVENTS
            retained = decoded[: MAX_EXPANDED_EVENTS - 1] if truncated else decoded
            for item in retained:
                parsed = get_adapter(detect_adapter(item)).parse(
                    item, context.at_sequence(sequence)
                )
                events.extend(parsed)
                sequence += len(parsed)
            if truncated:
                events.append(
                    _diagnostic(
                        context.at_sequence(sequence),
                        adapter=self.name,
                        version=self.version,
                        reason=f"record expansion exceeded {MAX_EXPANDED_EVENTS} events",
                        record=None,
                        kind="adapter.event_limit",
                        stability=self.stability,
                    )
                )
            return tuple(events) or UnknownAdapter("auto").parse(decoded, context)
        detected = detect_adapter(decoded)
        return get_adapter(detected).parse(decoded, context)


_NATIVE = NativeAdapter()
_MCP = McpJsonRpcAdapter()
_HERMES = HermesAdapter()
_OPENHANDS = OpenHandsAdapter()
_AUTO = AutoAdapter()


def detect_adapter(record: object) -> str:
    """Return the best adapter name for one decoded or JSON-encoded record."""

    decoded, error, _ = _decode(record)
    if error is not None:
        return "unknown"
    if isinstance(decoded, list):
        return detect_adapter(decoded[0]) if decoded else "unknown"
    if not isinstance(decoded, Mapping):
        return "unknown"
    mapping = cast(Mapping[str, Any], decoded)
    schema = mapping.get("schema")
    if isinstance(schema, str) and schema.startswith("agenttrace.event/"):
        return "native"
    if mapping.get("jsonrpc") is not None:
        return "mcp-jsonrpc"
    provider = str(mapping.get("provider", "")).casefold()
    if provider == "hermes" or "hermes_version" in mapping:
        return "hermes"
    if isinstance(mapping.get("messages"), list) and (
        "started_at" in mapping
        or "input_tokens" in mapping
        or str(mapping.get("source", "")).casefold() not in {"agent", "user", "environment"}
    ):
        return "hermes"
    source = str(mapping.get("source", "")).casefold()
    type_name = str(_first(mapping, "kind", "type", "event_type") or "").casefold()
    if source in {"agent", "user", "environment"} and (
        "action" in mapping or "observation" in mapping or "event" in type_name
    ):
        return "openhands"
    if "openhands" in provider or "openhands" in str(mapping.get("origin", "")).casefold():
        return "openhands"
    return "unknown"


def get_adapter(name: str = "auto") -> EventAdapter:
    """Return an adapter by public name; unknown names return a diagnostic adapter."""

    normalized = name.strip().casefold().replace("_", "-")
    if normalized in {"auto", "detect"}:
        return _AUTO
    if normalized in {"native", "agenttrace", SCHEMA_VERSION}:
        return _NATIVE
    if normalized in {"mcp", "mcp-jsonrpc", "jsonrpc"}:
        return _MCP
    if normalized in {"hermes", "hermes-export"}:
        return _HERMES
    if normalized in {"openhands", "open-hands"}:
        return _OPENHANDS
    if normalized == "unknown":
        return UnknownAdapter()
    return UnknownAdapter(name)


def parse_record(
    record: object, context: AdapterContext, adapter: str = "auto"
) -> tuple[TraceEvent, ...]:
    """Normalize one captured record through the selected adapter."""

    return get_adapter(adapter).parse(record, context)


__all__ = [
    "ADAPTER_VERSION",
    "MAX_RECORD_BYTES",
    "AdapterContext",
    "EventAdapter",
    "detect_adapter",
    "get_adapter",
    "parse_record",
]
