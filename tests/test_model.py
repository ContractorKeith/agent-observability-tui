from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from agent_observability_tui.model import (
    SCHEMA_VERSION,
    CorrelationIds,
    Cost,
    TokenUsage,
    TraceEvent,
)


def test_trace_event_json_round_trip_is_lossless_and_immutable() -> None:
    event = TraceEvent(
        event_id="evt-7",
        session_id="session-1",
        ingest_sequence=7,
        source_timestamp=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        observed_timestamp=datetime(2026, 7, 10, 12, 0, 1, tzinfo=UTC),
        adapter="native",
        adapter_version="1",
        category="tool",
        kind="call",
        phase="complete",
        status="ok",
        direction="outbound",
        transport="stdio",
        correlation=CorrelationIds(
            trace_id="trace-1",
            span_id="span-1",
            parent_span_id="root",
            request_id="request-1",
            tool_call_id="tool-1",
            run_id="run-1",
            extra={"vendor_id": "vendor-1"},
        ),
        token_usage=TokenUsage(
            input_tokens=11,
            output_tokens=4,
            cache_read_tokens=2,
            total_tokens=17,
            provenance="source-reported",
        ),
        cost=Cost(
            amount_usd="0.00042",
            provenance="estimated",
            price_catalog_version="2026-07-10",
        ),
        attributes={"tool": {"name": "search"}, "ok": True},
        raw={"jsonrpc": "2.0", "id": 8},
    )

    restored = TraceEvent.from_json(event.to_json())

    assert restored == event
    assert restored.to_dict()["schema"] == SCHEMA_VERSION
    assert restored.to_dict()["source_timestamp"] == "2026-07-10T12:00:00Z"
    assert restored.trace_id == "trace-1"
    with pytest.raises(FrozenInstanceError):
        restored.status = "error"  # type: ignore[misc]
    with pytest.raises(TypeError):
        restored.attributes["new"] = "value"  # type: ignore[index]


def test_trace_event_rejects_naive_timestamps_and_negative_usage() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        TraceEvent(
            event_id="evt",
            session_id="session",
            ingest_sequence=0,
            source_timestamp=None,
            observed_timestamp=datetime(2026, 7, 10),
            adapter="native",
            adapter_version="1",
            category="log",
            kind="message",
        )

    with pytest.raises(ValueError, match="non-negative"):
        TokenUsage(input_tokens=-1)


def test_trace_event_constructor_enforces_sanitized_attributes_and_raw() -> None:
    secret = "sk-proj-directConstructorSecret123456"

    event = TraceEvent(
        event_id="event-safe",
        session_id="session-safe",
        ingest_sequence=1,
        source_timestamp=None,
        observed_timestamp=datetime(2026, 7, 10, tzinfo=UTC),
        adapter="native",
        adapter_version="1",
        category="log",
        kind="message",
        attributes={"password": secret},
        raw={"message": f"api_key={secret}\x1b[31m[bold]bad[/bold]"},
    )

    serialized = event.to_json()
    assert secret not in serialized
    assert "\x1b" not in serialized
    assert event.attributes["_sanitization"]["policy"] == "agenttrace.redaction/1"
    assert TraceEvent.from_json(serialized) == event
