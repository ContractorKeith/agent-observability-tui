"""Application service joining safe inputs, storage, replay, and analysis."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters import AdapterContext, parse_record
from .analysis import PriceCatalog, SessionSummary, compare_sessions, summarize_session
from .export import ExportFormat, export_session
from .model import Cost, TraceEvent
from .redaction import sanitize_payload, sanitize_text
from .sources import (
    ProcessExited,
    ProcessOutput,
    ProcessStarted,
    SourceDiagnostic,
    iter_completed_jsonl,
)
from .storage import ChainVerification, SessionInfo, SessionStore, validate_session_id
from .telemetry import ResourceSample

DEFAULT_REDACTION_POLICY = "agenttrace.redaction/1"


class Observatory:
    """Small, thread-safe-by-storage-boundary application facade."""

    def __init__(self, store: SessionStore, *, catalog: PriceCatalog | None = None) -> None:
        self.store = store
        self.catalog = catalog or PriceCatalog.load()

    @classmethod
    def open(
        cls,
        database: str | Path,
        *,
        price_catalog: str | Path | None = None,
        read_only: bool = False,
    ) -> Observatory:
        configured_catalog = price_catalog or os.environ.get("AGENT_OBSERVABILITY_PRICES")
        return cls(
            SessionStore(database, read_only=read_only),
            catalog=PriceCatalog.load(configured_catalog) if configured_catalog else None,
        )

    def new_session(self, *, adapter: str, session_id: str | None = None) -> str:
        session_id = session_id or f"session-{uuid.uuid4().hex[:12]}"
        session_id = validate_session_id(session_id)
        self.store.start_session(
            session_id,
            adapter=adapter,
            pricing_version=self.catalog.version,
        )
        return session_id

    def ingest_record(
        self,
        record: object,
        *,
        session_id: str,
        adapter: str = "auto",
        source_name: str = "import",
        transport: str | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[TraceEvent, ...]:
        info = self.store.get_session(session_id)
        context = AdapterContext(
            session_id=session_id,
            ingest_sequence=info.event_count + 1,
            observed_at=observed_at or datetime.now(UTC),
            source_name=source_name,
            transport=transport,
        )
        events = parse_record(record, context, adapter=adapter)
        accepted = []
        for event in events:
            event = self._snapshot_local_cost(event)
            if self.store.append_event(event, redaction_policy=DEFAULT_REDACTION_POLICY):
                accepted.append(event)
            else:
                duplicate = self._internal_event(
                    session_id,
                    category="diagnostic",
                    kind="trace.duplicate_event_id",
                    status="warning",
                    observed_at=observed_at or datetime.now(UTC),
                    attributes={
                        "duplicate_event_id": event.event_id,
                        "adapter": event.adapter,
                    },
                )
                self.store.append_event(duplicate, redaction_policy=DEFAULT_REDACTION_POLICY)
                accepted.append(duplicate)
        return tuple(accepted)

    def import_path(
        self,
        path: str | Path,
        *,
        adapter: str = "auto",
        session_id: str | None = None,
        finalize: bool = True,
    ) -> str:
        session_id = self.new_session(adapter=adapter, session_id=session_id)
        for item in iter_completed_jsonl(path, lambda line: line):
            if isinstance(item, SourceDiagnostic):
                self.ingest_source_item(item, session_id=session_id, adapter=adapter)
            else:
                self.ingest_record(
                    item,
                    session_id=session_id,
                    adapter=adapter,
                    source_name="import",
                )
        if finalize:
            self.store.finalize_session(session_id)
        return session_id

    def ingest_source_item(
        self,
        item: object,
        *,
        session_id: str,
        adapter: str = "auto",
    ) -> tuple[TraceEvent, ...]:
        if isinstance(item, ProcessOutput):
            if item.stream == "stdout" and item.text.lstrip().startswith(("{", "[")):
                return self.ingest_record(
                    item.text,
                    session_id=session_id,
                    adapter=adapter,
                    source_name="run",
                    transport="stdout",
                    observed_at=item.observed_at,
                )
            event = self._internal_event(
                session_id,
                category="log",
                kind=f"process.{item.stream}",
                status="warning" if item.truncated else "ok",
                observed_at=item.observed_at,
                attributes={
                    "stream": item.stream,
                    "text": item.text,
                    "byte_count": item.byte_count,
                    "truncated": item.truncated,
                },
            )
        elif isinstance(item, ProcessStarted):
            event = self._internal_event(
                session_id,
                category="session",
                kind="process.started",
                phase="start",
                status="running",
                observed_at=item.observed_at,
                attributes={
                    "pid": item.pid,
                    "executable": Path(item.argv[0]).name,
                    "argument_count": max(0, len(item.argv) - 1),
                    "cwd": item.cwd,
                },
            )
        elif isinstance(item, ProcessExited):
            event = self._internal_event(
                session_id,
                category="session",
                kind="process.exited",
                phase="end",
                status="ok" if item.returncode == 0 else "error",
                observed_at=item.observed_at,
                attributes={
                    "returncode": item.returncode,
                    "exit_status": item.status,
                    "signal_number": item.signal_number,
                    "duration_ms": item.duration_seconds * 1000,
                },
            )
        elif isinstance(item, SourceDiagnostic):
            event = self._internal_event(
                session_id,
                category="diagnostic",
                kind=item.code,
                status="warning",
                observed_at=item.observed_at,
                attributes={
                    "message": item.message,
                    "source": item.source,
                    "path": item.path,
                    "line_number": item.line_number,
                    "byte_offset": item.byte_offset,
                    "details": dict(item.details),
                },
            )
        else:
            return self.ingest_record(
                item,
                session_id=session_id,
                adapter=adapter,
                source_name="source",
            )
        self.store.append_event(event, redaction_policy=DEFAULT_REDACTION_POLICY)
        return (event,)

    def record_resource(self, session_id: str, sample: ResourceSample) -> None:
        sequence = len(self.store.resource_samples(session_id)) + 1
        self.store.append_resource_sample(
            session_id,
            sequence=sequence,
            observed_timestamp=sample.observed_at.isoformat().replace("+00:00", "Z"),
            pid=sample.pid,
            rss_bytes=sample.rss_bytes,
            cpu_percent=sample.cpu_percent,
            status=sample.status,
            detail=",".join(sample.errors) or None,
        )

    def replay(self, session_id: str) -> Iterator[TraceEvent]:
        """Return stored events only. This method has no execution/source side effects."""

        return self.store.iter_events(session_id)

    def sessions(self) -> tuple[SessionInfo, ...]:
        return self.store.list_sessions()

    def summarize(self, session_id: str) -> SessionSummary:
        info = self.store.get_session(session_id)
        return summarize_session(
            session_id,
            self.store.iter_events(session_id),
            status=info.status,
            adapter=info.adapter,
            resources=self.store.resource_samples(session_id),
        )

    def compare(self, session_ids: Iterable[str]) -> tuple[SessionSummary, ...]:
        return compare_sessions(self.store, session_ids)

    def verify(self, session_id: str) -> ChainVerification:
        return self.store.verify_session(session_id)

    def export(
        self,
        session_id: str,
        destination: str | Path,
        *,
        format: ExportFormat = "json",
    ) -> Path:
        return export_session(
            self.store,
            session_id,
            destination,
            format=format,
        )

    def _internal_event(
        self,
        session_id: str,
        *,
        category: str,
        kind: str,
        status: str,
        observed_at: datetime,
        attributes: dict[str, Any],
        phase: str | None = None,
    ) -> TraceEvent:
        info = self.store.get_session(session_id)
        sanitized = sanitize_payload(attributes)
        safe_kind = sanitize_text(kind, max_bytes=256).value
        return TraceEvent(
            event_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{session_id}:{info.event_count + 1}:internal:{safe_kind}",
                )
            ),
            session_id=session_id,
            ingest_sequence=info.event_count + 1,
            source_timestamp=None,
            observed_timestamp=observed_at,
            adapter="internal",
            adapter_version="1",
            category=category,
            kind=str(safe_kind),
            phase=phase,
            status=status,
            attributes={
                **(sanitized.value if isinstance(sanitized.value, dict) else {}),
                "_sanitization": sanitized.metadata(),
            },
            raw=None,
        )

    def _snapshot_local_cost(self, event: TraceEvent) -> TraceEvent:
        """Freeze a local estimate into the immutable event at ingest time."""

        if event.cost is not None or event.token_usage is None:
            return event
        model = event.attributes.get("model") or event.attributes.get("model_name")
        quote = self.catalog.quote(
            str(model) if model else None,
            event.token_usage,
            at=event.source_timestamp or event.observed_timestamp,
        )
        if quote is None:
            return event
        amount = f"{quote.amount_usd:.12f}".rstrip("0").rstrip(".") or "0"
        provenance = sanitize_text(f"locally-estimated: {quote.provenance}", max_bytes=512).value
        return replace(
            event,
            cost=Cost(
                amount_usd=amount,
                provenance=str(provenance),
                price_catalog_version=quote.catalog_version,
                price_effective_from=quote.effective_from,
            ),
        )


def default_database_path() -> Path:
    from platformdirs import user_data_path

    override = os.environ.get("AGENT_OBSERVABILITY_DB")
    if override:
        return Path(override).expanduser()
    return user_data_path("agent-observability-tui", "ContractorKeith") / "sessions.sqlite3"
