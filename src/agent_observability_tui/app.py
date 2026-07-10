"""Textual views over sanitized, stored trace sessions."""

from __future__ import annotations

import json
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
)

from .model import TraceEvent
from .observatory import Observatory

MAX_DETAIL_BYTES = 16 * 1024


class ObservabilityApp(App[None]):
    """Sessions, timeline, detail, comparison, and evidence in one local TUI."""

    CSS_PATH = "app.tcss"
    TITLE = "Agent Observability TUI"
    SUB_TITLE = "Debug your local agents like a pro"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("p", "pause", "Pause UI"),
        ("r", "reload", "Refresh"),
        ("slash", "focus_filter", "Filter"),
        ("escape", "clear_filter", "Clear filter"),
    ]

    def __init__(self, observatory: Observatory, *, session_id: str | None = None) -> None:
        super().__init__()
        self.observatory = observatory
        self.session_id = session_id
        self.paused = False
        self.filter_text = ""
        self._session_signature: tuple[tuple[str, int, str], ...] = ()
        self._event_signature: tuple[str | None, int, str] = (None, -1, "")
        self._events_by_sequence: dict[int, TraceEvent] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Loading local sessions…", id="metrics")
        with TabbedContent(initial="trace-tab"):
            with TabPane("Trace", id="trace-tab"), Horizontal(id="trace-layout"):
                with Vertical(id="sessions-panel"):
                    yield Static("Sessions", classes="panel-title")
                    yield DataTable(id="sessions", cursor_type="row")
                with Vertical(id="events-panel"):
                    yield Static("Timeline", classes="panel-title")
                    yield Input(placeholder="Filter kind, status, model, or text", id="filter")
                    yield DataTable(id="events", cursor_type="row")
                with Vertical(id="detail-panel"):
                    yield Static("Selected event", classes="panel-title")
                    yield Static(Text("Select an event to inspect sanitized details."), id="detail")
            with TabPane("Compare", id="compare-tab"):
                yield DataTable(id="comparison", cursor_type="row")
            with TabPane("Evidence", id="evidence-tab"):
                yield Static(Text("Select a session to inspect chain status."), id="evidence")
        yield Footer()

    def on_mount(self) -> None:
        sessions = self.query_one("#sessions", DataTable)
        sessions.add_columns("Session", "Status", "Events", "Adapter")
        events = self.query_one("#events", DataTable)
        events.add_columns("Seq", "Time", "Category", "Kind", "Status")
        comparison = self.query_one("#comparison", DataTable)
        comparison.add_columns(
            "Session",
            "Model",
            "Status",
            "Events",
            "Tools",
            "Errors",
            "Tokens in/out",
            "Est. USD",
            "Peak RSS",
            "Peak CPU",
            "Resource state",
        )
        self.refresh_data(force=True)
        self.set_interval(0.5, self.refresh_data)

    def refresh_data(self, *, force: bool = False) -> None:
        if self.paused and not force:
            return
        sessions = self.observatory.sessions()
        signature = tuple((item.session_id, item.event_count, item.status) for item in sessions)
        if force or signature != self._session_signature:
            self._session_signature = signature
            table = self.query_one("#sessions", DataTable)
            table.clear(columns=False)
            for item in sessions:
                table.add_row(
                    item.session_id,
                    item.status,
                    str(item.event_count),
                    item.adapter,
                    key=item.session_id,
                )
            if self.session_id is None and sessions:
                self.session_id = sessions[0].session_id
            self._refresh_comparison(sessions)

        if self.session_id is None:
            self.query_one("#metrics", Static).update(Text("No stored sessions. Run the demo."))
            return
        info = next((item for item in sessions if item.session_id == self.session_id), None)
        event_count = info.event_count if info else 0
        event_signature = (self.session_id, event_count, self.filter_text)
        if force or event_signature != self._event_signature:
            self._event_signature = event_signature
            self._refresh_events()
        self._refresh_metrics_and_evidence()

    def _refresh_events(self) -> None:
        if self.session_id is None:
            return
        table = self.query_one("#events", DataTable)
        table.clear(columns=False)
        self._events_by_sequence.clear()
        needle = self.filter_text.casefold()
        for event in self.observatory.replay(self.session_id):
            serialized = event.to_json()
            if needle and needle not in serialized.casefold():
                continue
            payload = event.to_dict()
            timestamp = str(payload["source_timestamp"] or payload["observed_timestamp"])
            table.add_row(
                str(event.ingest_sequence),
                timestamp[11:23] if len(timestamp) >= 23 else timestamp,
                event.category,
                event.kind,
                event.status or "unknown",
                key=str(event.ingest_sequence),
            )
            self._events_by_sequence[event.ingest_sequence] = event

    def _refresh_metrics_and_evidence(self) -> None:
        assert self.session_id is not None
        summary = self.observatory.summarize(self.session_id)
        cost = (
            "unpriced"
            if summary.estimated_cost_usd is None
            else f"${summary.estimated_cost_usd:.6f}"
        )
        rss = "unknown" if summary.peak_rss_bytes is None else _human_bytes(summary.peak_rss_bytes)
        cpu = "unknown" if summary.peak_cpu_percent is None else f"{summary.peak_cpu_percent:.1f}%"
        tokens = f"{_known(summary.input_tokens)} in / {_known(summary.output_tokens)} out"
        pause = "  [UI PAUSED]" if self.paused else ""
        self.query_one("#metrics", Static).update(
            Text(
                f"{self.session_id}  •  {summary.status}  •  {summary.event_count} events  •  "
                f"{summary.tool_calls} tools  •  {summary.errors} errors  •  {tokens}  •  "
                f"{cost}  •  peak RSS {rss}  •  peak CPU {cpu}{pause}"
            )
        )
        verification = self.observatory.verify(self.session_id)
        chain_state = (
            "OPEN (valid so far)"
            if verification.valid and not verification.finalized
            else "VALID"
            if verification.valid
            else "INVALID"
        )
        evidence_text = (
            f"Session: {self.session_id}\n\n"
            f"Sanitized record chain: {chain_state}\n"
            f"Finalized: {verification.finalized}\n"
            f"Events verified: {verification.event_count}\n"
            f"Chain head: {verification.chain_head or 'none'}\n\n"
            f"Token provenance: {summary.token_provenance or 'unknown'}\n"
            f"Cost provenance: {summary.cost_provenance or 'unknown'}\n"
            f"Price catalog: {summary.pricing_version or 'unknown'} "
            f"(effective {summary.pricing_effective_from or 'unknown'})\n"
            f"Duration basis: {summary.duration_basis or 'unknown'}\n"
            f"Resource status: {summary.resource_status} "
            f"({summary.resource_sample_count} samples; "
            f"{summary.unavailable_resource_samples} unavailable)\n\n"
            "The SHA-256 chain detects corruption of sanitized stored records. "
            "It does not prove fidelity to original raw bytes and is not secure notarization.\n\n"
            f"Export with: agent-observe export {self.session_id} evidence.json"
        )
        self.query_one("#evidence", Static).update(Text(evidence_text))

    def _refresh_comparison(self, sessions) -> None:
        table = self.query_one("#comparison", DataTable)
        table.clear(columns=False)
        if not sessions:
            return
        for index, summary in enumerate(
            self.observatory.compare(item.session_id for item in sessions)
        ):
            table.add_row(
                summary.session_id,
                summary.model or "unknown",
                summary.status,
                str(summary.event_count),
                str(summary.tool_calls),
                str(summary.errors),
                f"{_known(summary.input_tokens)}/{_known(summary.output_tokens)}",
                "unpriced"
                if summary.estimated_cost_usd is None
                else f"{summary.estimated_cost_usd:.6f}",
                "unknown"
                if summary.peak_rss_bytes is None
                else _human_bytes(summary.peak_rss_bytes),
                "unknown"
                if summary.peak_cpu_percent is None
                else f"{summary.peak_cpu_percent:.1f}%",
                summary.resource_status,
                key=f"{summary.session_id}:{summary.model}:{index}",
            )

    @on(DataTable.RowSelected, "#sessions")
    def select_session(self, event: DataTable.RowSelected) -> None:
        self.session_id = str(event.row_key.value)
        self._event_signature = (None, -1, "")
        self.refresh_data(force=True)

    @on(DataTable.RowSelected, "#events")
    def select_event(self, event: DataTable.RowSelected) -> None:
        try:
            sequence = int(str(event.row_key.value))
        except ValueError:
            return
        trace_event = self._events_by_sequence.get(sequence)
        if trace_event is None:
            return
        rendered = json.dumps(trace_event.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        encoded = rendered.encode()
        if len(encoded) > MAX_DETAIL_BYTES:
            rendered = (
                encoded[:MAX_DETAIL_BYTES].decode(errors="ignore")
                + f"\n… [detail truncated; original bytes={len(encoded)}]"
            )
        self.query_one("#detail", Static).update(Text(rendered, no_wrap=False))

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value
        self._event_signature = (None, -1, "")
        self.refresh_data(force=True)

    def action_pause(self) -> None:
        self.paused = not self.paused
        self._refresh_metrics_and_evidence() if self.session_id else None

    def action_reload(self) -> None:
        self.refresh_data(force=True)

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        filter_input.value = ""
        self.set_focus(self.query_one("#events", DataTable))


def _known(value: object | None) -> str:
    return "unknown" if value is None else str(value)


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"
