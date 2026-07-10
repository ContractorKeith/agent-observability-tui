from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_observability_tui.adapters import (
    MAX_RECORD_BYTES,
    AdapterContext,
    detect_adapter,
    get_adapter,
    parse_record,
)
from agent_observability_tui.model import TraceEvent
from agent_observability_tui.redaction import CONTENT_OMITTED, VALUE_OMITTED

FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = datetime(2026, 7, 10, 12, 5, tzinfo=UTC)


def context(sequence: int = 10) -> AdapterContext:
    return AdapterContext(
        session_id="collector-session",
        ingest_sequence=sequence,
        observed_at=OBSERVED_AT,
        source_name="import",
    )


@pytest.mark.parametrize(
    ("fixture", "expected_adapter", "expected_category"),
    [
        ("mcp_request.json", "mcp-jsonrpc", "tool"),
        ("hermes_event.json", "hermes", "tool"),
        ("openhands_event.json", "openhands", "tool"),
    ],
)
def test_auto_detects_experimental_exports_and_preserves_sanitized_unknown_fields(
    fixture: str, expected_adapter: str, expected_category: str
) -> None:
    record = json.loads((FIXTURES / fixture).read_text())

    event = parse_record(record, context())[0]

    assert detect_adapter(record) == expected_adapter
    assert event.adapter == expected_adapter
    assert event.category == expected_category
    assert event.session_id == "collector-session"
    assert event.ingest_sequence == 10
    assert event.observed_timestamp == OBSERVED_AT
    assert event.raw is not None
    assert "unknown" in event.to_json()
    assert "fixture-example-secret-123456" not in event.to_json()
    assert event.attributes["_sanitization"]["policy"] == "agenttrace.redaction/1"


def test_mcp_request_maps_json_rpc_correlation_and_hostile_arguments() -> None:
    record = json.loads((FIXTURES / "mcp_request.json").read_text())
    record["params"]["arguments"]["command"] = "\x1b[31m[link=x]boom[/link]\x1b[0m"

    event = get_adapter("mcp").parse(record, context())[0]

    assert event.kind == "tools/call"
    assert event.phase == "start"
    assert event.direction == "outbound"
    assert event.request_id == "7"
    assert event.tool_call_id == "7"
    assert "\x1b" not in event.to_json()
    assert event.raw["params"]["arguments"] == CONTENT_OMITTED
    assert "boom" not in event.to_json()


def test_native_round_trip_uses_collector_order_and_retains_source_order() -> None:
    native = TraceEvent(
        event_id="native-99",
        session_id="emitter-session",
        ingest_sequence=99,
        source_timestamp=datetime(2026, 7, 10, 11, 0, tzinfo=UTC),
        observed_timestamp=datetime(2026, 7, 10, 11, 1, tzinfo=UTC),
        adapter="native-emitter",
        adapter_version="2.4",
        category="model",
        kind="completion",
        status="ok",
        attributes={"custom": "preserved"},
    ).to_dict()
    native["future_field"] = {"supported_later": True}

    event = get_adapter("native").parse(native, context(sequence=3))[0]

    assert event.adapter == "native"
    assert event.ingest_sequence == 3
    assert event.attributes["source_ingest_sequence"] == 99
    assert event.attributes["source_adapter"] == {"name": "native-emitter", "version": "2.4"}
    assert event.attributes["unknown_fields"]["future_field"] == {"supported_later": VALUE_OMITTED}
    assert event.raw["future_field"] == {"supported_later": VALUE_OMITTED}


@pytest.mark.parametrize(
    "record",
    [
        "{not-json password=hunter2-and-more",
        {"unexpected": {"api_key": "sk-proj-unknownSecret123456"}},
        [42, None, "out-of-order"],
    ],
)
def test_malformed_unknown_and_out_of_order_shapes_become_safe_diagnostics(record: object) -> None:
    events = parse_record(record, context())

    assert events
    assert all(event.category == "diagnostic" for event in events)
    assert all(event.status == "warning" for event in events)
    serialized = "".join(event.to_json() for event in events)
    assert "hunter2-and-more" not in serialized
    assert "unknownSecret123456" not in serialized


def test_hermes_batch_keeps_input_order_but_assigns_collector_sequence() -> None:
    base = json.loads((FIXTURES / "hermes_event.json").read_text())
    batch = {
        "provider": "hermes",
        "events": [{**base, "event_id": "later"}, {**base, "event_id": "earlier"}],
    }

    events = get_adapter("hermes").parse(batch, context(sequence=40))

    assert [event.event_id for event in events] == ["later", "earlier"]
    assert [event.ingest_sequence for event in events] == [40, 41]


def test_hermes_session_preserves_source_pricing_version() -> None:
    record = json.loads((FIXTURES / "hermes_session_export.json").read_text())

    event = get_adapter("hermes").parse(record, context())[0]

    assert event.cost is not None
    assert event.cost.price_catalog_version == "fixture-v1"


def test_hermes_expansion_does_not_copy_session_payload_into_every_message() -> None:
    record = json.loads((FIXTURES / "hermes_session_export.json").read_text())
    record["unknown_large_blob"] = "x" * 10_000
    record["messages"] = record["messages"] * 25

    events = get_adapter("hermes").parse(record, context())
    serialized_size = sum(len(event.to_json().encode()) for event in events)

    assert len(events) == 51
    assert serialized_size < 100_000


def test_generic_batch_expansion_is_bounded_with_terminal_diagnostic() -> None:
    events = get_adapter("native").parse([{}] * 1_010, context())

    assert len(events) == 1_000
    assert events[-1].kind == "adapter.event_limit"


def test_current_hermes_session_export_expands_session_and_messages() -> None:
    export = json.loads((FIXTURES / "hermes_session_export.json").read_text())

    events = parse_record(export, context(sequence=50))

    assert detect_adapter(export) == "hermes"
    assert [event.ingest_sequence for event in events] == [50, 51, 52]
    assert [event.category for event in events] == ["session", "tool", "tool"]
    assert events[0].token_usage is not None
    assert events[0].token_usage.reasoning_tokens == 4
    assert events[0].cost is not None
    assert events[0].cost.provenance == "estimated"
    assert events[1].token_usage is None
    assert events[1].attributes["token_count"] == 7
    assert events[2].tool_call_id == "call-1"


def test_oversized_encoded_record_is_bounded_diagnostic() -> None:
    secret = "sk-proj-oversizedSecret123456789"
    record = '{"message":"' + secret + ("x" * MAX_RECORD_BYTES) + '"}'

    event = parse_record(record, context())[0]

    assert event.category == "diagnostic"
    assert event.kind == "adapter.malformed"
    assert secret not in event.to_json()
    assert event.attributes["_sanitization"]["truncated"] is True
