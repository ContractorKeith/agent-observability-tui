"""Read-only metrics, pricing, and session comparison."""

from __future__ import annotations

import fnmatch
import math
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .redaction import sanitize_text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .model import TraceEvent
    from .storage import SessionStore


@dataclass(frozen=True, slots=True)
class PriceRate:
    pattern: str
    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_write_per_million: float
    reasoning_per_million: float
    provenance: str

    def __post_init__(self) -> None:
        safe_pattern = sanitize_text(self.pattern, max_bytes=256).value
        safe_provenance = sanitize_text(self.provenance, max_bytes=512).value
        if not safe_pattern:
            raise ValueError("price pattern must not be empty")
        object.__setattr__(self, "pattern", str(safe_pattern))
        object.__setattr__(self, "provenance", str(safe_provenance))
        for name in (
            "input_per_million",
            "output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
            "reasoning_per_million",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class PriceQuote:
    amount_usd: float
    catalog_version: str
    effective_from: str
    provenance: str


class PriceCatalog:
    def __init__(
        self,
        *,
        version: str,
        effective_from: str,
        currency: str,
        rates: tuple[PriceRate, ...],
    ) -> None:
        safe_version = sanitize_text(version, max_bytes=128).value
        if not safe_version:
            raise ValueError("price catalog version must not be empty")
        try:
            date.fromisoformat(effective_from)
        except ValueError as error:
            raise ValueError("price catalog effective_from must be YYYY-MM-DD") from error
        if currency != "USD":
            raise ValueError("only USD price catalogs are supported")
        self.version = str(safe_version)
        self.effective_from = effective_from
        self.currency = currency
        self.rates = rates

    @classmethod
    def load(cls, path: str | Path | None = None) -> PriceCatalog:
        if path is None:
            path = Path(__file__).with_name("prices.toml")
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        catalog = data["catalog"]
        rates = tuple(
            PriceRate(
                pattern=item["pattern"],
                input_per_million=float(item.get("input_per_million", 0)),
                output_per_million=float(item.get("output_per_million", 0)),
                cache_read_per_million=float(item.get("cache_read_per_million", 0)),
                cache_write_per_million=float(item.get("cache_write_per_million", 0)),
                reasoning_per_million=float(item.get("reasoning_per_million", 0)),
                provenance=item.get("provenance", "configured local price catalog"),
            )
            for item in data.get("models", [])
        )
        return cls(
            version=catalog["version"],
            effective_from=catalog["effective_from"],
            currency=catalog.get("currency", "USD"),
            rates=rates,
        )

    def quote(
        self,
        model: str | None,
        token_usage: Any,
        *,
        at: date | datetime | None = None,
    ) -> PriceQuote | None:
        if not model or token_usage is None:
            return None
        effective_date = date.fromisoformat(self.effective_from)
        if at is None:
            quote_date = datetime.now(UTC).date()
        elif isinstance(at, datetime):
            quote_date = at.date()
        else:
            quote_date = at
        if quote_date < effective_date:
            return None
        rate = next((item for item in self.rates if fnmatch.fnmatch(model, item.pattern)), None)
        if rate is None:
            return None
        million = 1_000_000
        try:
            amount = (
                _token(token_usage, "input_tokens") * rate.input_per_million
                + _token(token_usage, "output_tokens") * rate.output_per_million
                + _token(token_usage, "cache_read_tokens") * rate.cache_read_per_million
                + _token(token_usage, "cache_write_tokens") * rate.cache_write_per_million
                + _token(token_usage, "reasoning_tokens") * rate.reasoning_per_million
            ) / million
        except OverflowError:
            return None
        if not math.isfinite(amount):
            return None
        return PriceQuote(amount, self.version, self.effective_from, rate.provenance)


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    status: str
    model: str | None
    adapter: str
    event_count: int
    tool_calls: int
    errors: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    token_provenance: str | None
    estimated_cost_usd: float | None
    cost_provenance: str | None
    duration_ms: float | None
    duration_basis: str | None
    p50_duration_ms: float | None
    p95_duration_ms: float | None
    peak_rss_bytes: int | None
    peak_cpu_percent: float | None
    resource_sample_count: int
    unavailable_resource_samples: int
    resource_status: str
    missing_usage_events: int
    pricing_version: str | None
    pricing_effective_from: str | None

    def to_dict(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


def summarize_session(
    session_id: str,
    events: Iterable[TraceEvent],
    *,
    status: str = "unknown",
    adapter: str = "unknown",
    resources: tuple[dict[str, Any], ...] = (),
) -> SessionSummary:
    event_list = list(events)
    model = _find_model(event_list)
    tool_calls = sum(
        event.category == "tool" and event.phase in {None, "start", "instant"}
        for event in event_list
    )
    errors = sum(event.status == "error" or event.category == "error" for event in event_list)
    usage_events = [event.token_usage for event in event_list if event.token_usage is not None]
    token_provenance = _provenance(getattr(usage, "provenance", None) for usage in usage_events)
    missing_usage = sum(
        event.category in {"agent", "model"} and event.token_usage is None for event in event_list
    )
    totals = {
        name: _sum_optional(usage_events, name)
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        )
    }
    explicit_costs = [
        _cost_amount(event.cost) for event in event_list if _cost_amount(event.cost) is not None
    ]
    estimated_cost: float | None
    pricing_version: str | None = None
    pricing_effective_from: str | None = None
    if explicit_costs:
        estimated_cost = sum(explicit_costs)
        versions = {
            version
            for event in event_list
            if event.cost is not None and (version := _cost_version(event.cost)) is not None
        }
        pricing_version = (
            next(iter(versions)) if len(versions) == 1 else "mixed" if versions else None
        )
        effective_dates = {
            event.cost.price_effective_from
            for event in event_list
            if event.cost is not None and event.cost.price_effective_from is not None
        }
        pricing_effective_from = (
            next(iter(effective_dates))
            if len(effective_dates) == 1
            else "mixed"
            if effective_dates
            else None
        )
    else:
        estimated_cost = None
    cost_provenance = _provenance(
        getattr(event.cost, "provenance", None) for event in event_list if event.cost is not None
    )

    if event_list and all(event.source_timestamp is not None for event in event_list):
        timestamps = [event.source_timestamp for event in event_list]
        duration_basis = "source"
    elif event_list:
        timestamps = [event.observed_timestamp for event in event_list]
        duration_basis = "observed"
    else:
        timestamps = []
        duration_basis = None
    duration_ms = None
    if len(timestamps) >= 2:
        duration_ms = (max(timestamps) - min(timestamps)).total_seconds() * 1000
    durations = [value for event in event_list if (value := _event_duration(event)) is not None]
    peak_rss = max(
        (sample["rss_bytes"] for sample in resources if sample.get("rss_bytes") is not None),
        default=None,
    )
    peak_cpu = max(
        (sample["cpu_percent"] for sample in resources if sample.get("cpu_percent") is not None),
        default=None,
    )
    resource_statuses = [str(sample.get("status", "unavailable")) for sample in resources]
    unavailable_resources = sum(status == "unavailable" for status in resource_statuses)
    if "observed" in resource_statuses:
        resource_status = "observed"
    elif "partial" in resource_statuses:
        resource_status = "partial"
    elif resource_statuses:
        resource_status = "unavailable"
    else:
        resource_status = "not-sampled"
    return SessionSummary(
        session_id=session_id,
        status=status,
        model=model,
        adapter=adapter,
        event_count=len(event_list),
        tool_calls=tool_calls,
        errors=errors,
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        cache_write_tokens=totals["cache_write_tokens"],
        reasoning_tokens=totals["reasoning_tokens"],
        token_provenance=token_provenance,
        estimated_cost_usd=estimated_cost,
        cost_provenance=cost_provenance,
        duration_ms=duration_ms,
        duration_basis=duration_basis,
        p50_duration_ms=_percentile(durations, 0.50),
        p95_duration_ms=_percentile(durations, 0.95),
        peak_rss_bytes=peak_rss,
        peak_cpu_percent=peak_cpu,
        resource_sample_count=len(resources),
        unavailable_resource_samples=unavailable_resources,
        resource_status=resource_status,
        missing_usage_events=missing_usage,
        pricing_version=pricing_version,
        pricing_effective_from=pricing_effective_from,
    )


def compare_sessions(
    store: SessionStore,
    session_ids: Iterable[str],
) -> tuple[SessionSummary, ...]:
    summaries = []
    for session_id in session_ids:
        info = store.get_session(session_id)
        events = list(store.iter_events(session_id))
        aggregate = summarize_session(
            session_id,
            events,
            status=info.status,
            adapter=info.adapter,
            resources=store.resource_samples(session_id),
        )
        summaries.append(aggregate)
        models = sorted({model for event in events if (model := _event_model(event))})
        if len(models) > 1:
            for model in models:
                model_summary = summarize_session(
                    session_id,
                    [event for event in events if _event_model(event) == model],
                    status=info.status,
                    adapter=info.adapter,
                )
                summaries.append(replace(model_summary, model=model))
    return tuple(summaries)


def _token(usage: Any, name: str) -> int:
    value = getattr(usage, name, None)
    return int(value) if value is not None else 0


def _sum_optional(items: list[Any], name: str) -> int | None:
    values = [getattr(item, name, None) for item in items]
    known = [int(value) for value in values if value is not None]
    return sum(known) if known else None


def _event_model(event: TraceEvent) -> str | None:
    value = event.attributes.get("model") or event.attributes.get("model_name")
    return str(value) if value else None


def _find_model(events: list[TraceEvent]) -> str | None:
    models = {model for event in events if (model := _event_model(event))}
    if not models:
        return None
    return next(iter(models)) if len(models) == 1 else "mixed"


def _provenance(values: Iterable[object | None]) -> str | None:
    known = {str(value) for value in values if value}
    if not known:
        return None
    return next(iter(known)) if len(known) == 1 else "mixed"


def _cost_amount(cost: Any) -> float | None:
    if cost is None:
        return None
    for name in ("amount_usd", "usd", "amount"):
        value = getattr(cost, name, None)
        if value is not None:
            return float(value)
    return None


def _cost_version(cost: Any) -> str | None:
    for name in ("price_catalog_version", "pricing_version", "catalog_version"):
        value = getattr(cost, name, None)
        if value:
            return str(value)
    return None


def _event_duration(event: TraceEvent) -> float | None:
    value = event.attributes.get("duration_ms")
    try:
        duration = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if duration is None or not math.isfinite(duration) or duration < 0:
        return None
    return duration


def _parse_timestamp(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight
