from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from agent_observability_tui.analysis import PriceCatalog, compare_sessions, summarize_session
from agent_observability_tui.model import TokenUsage, TraceEvent
from agent_observability_tui.storage import SessionStore


def event(
    sequence: int,
    *,
    model: str = "demo/fast",
    duration_ms: float | None = None,
    usage: TokenUsage | None = None,
) -> TraceEvent:
    attributes: dict[str, object] = {"model": model}
    if duration_ms is not None:
        attributes["duration_ms"] = duration_ms
    return TraceEvent(
        event_id=f"event-{sequence}",
        session_id="session-1",
        ingest_sequence=sequence,
        source_timestamp=datetime(2026, 7, 10, 12, tzinfo=UTC) + timedelta(seconds=sequence),
        observed_timestamp=datetime(2026, 7, 10, 12, tzinfo=UTC) + timedelta(seconds=sequence),
        adapter="native",
        adapter_version="1",
        category="model",
        kind="response",
        status="ok",
        token_usage=usage,
        attributes=attributes,
    )


def test_versioned_catalog_prices_known_usage_exactly() -> None:
    catalog = PriceCatalog.load()
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=100_000,
        cache_write_tokens=20_000,
        reasoning_tokens=10_000,
    )

    quote = catalog.quote("demo/fast", usage)

    assert quote is not None
    assert quote.amount_usd == pytest.approx(3.075)
    assert quote.catalog_version == "2026-07-10-demo"
    assert "Fictional" in quote.provenance
    assert catalog.quote("unknown/provider-model", usage) is None


def test_summary_preserves_unknown_usage_and_calculates_percentiles() -> None:
    events = [
        event(1, duration_ms=10, usage=TokenUsage(input_tokens=100, output_tokens=20)),
        event(2, duration_ms=30, usage=None),
        event(3, duration_ms=50, usage=TokenUsage(input_tokens=50, output_tokens=10)),
    ]

    summary = summarize_session(
        "session-1",
        events,
        status="complete",
        adapter="native",
        resources=(
            {"rss_bytes": 10_000, "cpu_percent": 2.5, "status": "observed"},
            {"rss_bytes": 12_000, "cpu_percent": 5.0, "status": "observed"},
        ),
    )

    assert summary.input_tokens == 150
    assert summary.output_tokens == 30
    assert summary.missing_usage_events == 1
    assert summary.estimated_cost_usd is None
    assert summary.p50_duration_ms == 30
    assert summary.p95_duration_ms == pytest.approx(48)
    assert summary.duration_basis == "source"
    assert summary.peak_rss_bytes == 12_000
    assert summary.peak_cpu_percent == 5.0
    assert summary.resource_status == "observed"
    assert summary.resource_sample_count == 2
    assert summary.token_provenance == "source-reported"


def test_unknown_model_stays_unpriced() -> None:
    summary = summarize_session(
        "session-1",
        [event(1, model="provider/unknown", usage=TokenUsage(input_tokens=100))],
    )

    assert summary.estimated_cost_usd is None
    assert summary.pricing_version is None


def test_catalog_rejects_negative_or_non_finite_rates(tmp_path) -> None:
    catalog = tmp_path / "prices.toml"
    catalog.write_text(
        """
[catalog]
version = "bad"
effective_from = "2026-07-10"
currency = "USD"
[[models]]
pattern = "demo/*"
input_per_million = -1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="finite non-negative"):
        PriceCatalog.load(catalog)


def test_catalog_does_not_apply_before_its_effective_date() -> None:
    catalog = PriceCatalog.load()

    assert (
        catalog.quote(
            "demo/fast",
            TokenUsage(input_tokens=1_000),
            at=date(2026, 7, 9),
        )
        is None
    )


@pytest.mark.parametrize("duration", ["NaN", "Infinity", "1e999999", -1])
def test_summary_ignores_non_finite_or_negative_durations(duration) -> None:
    summary = summarize_session(
        "session-1",
        [event(1, duration_ms=duration, usage=None)],
    )

    assert summary.p50_duration_ms is None
    assert summary.p95_duration_ms is None


def test_duration_uses_observed_clock_when_source_coverage_is_partial() -> None:
    first = event(1, usage=None)
    second = event(2, usage=None)
    second = TraceEvent.from_dict({**second.to_dict(), "source_timestamp": None})

    summary = summarize_session("session-1", [first, second])

    assert summary.duration_basis == "observed"
    assert summary.duration_ms == 1000


def test_comparison_adds_per_model_rows_for_mixed_session(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    first = event(1, model="model/a", usage=TokenUsage(input_tokens=10))
    second = event(2, model="model/b", usage=TokenUsage(input_tokens=20))
    store.append_event(first)
    store.append_event(second)
    store.finalize_session("session-1")

    summaries = compare_sessions(store, ["session-1"])

    assert [summary.model for summary in summaries] == ["mixed", "model/a", "model/b"]
    assert [summary.input_tokens for summary in summaries] == [30, 10, 20]
