from __future__ import annotations

from pathlib import Path

from agent_observability_tui.observatory import Observatory

DEMO_TRACE = Path(__file__).parents[1] / "examples" / "demo-trace.jsonl"


def test_demo_import_replay_summary_and_verification_are_coherent(tmp_path: Path) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")

    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="demo")
    first = [event.to_json() for event in observatory.replay(session_id)]
    second = [event.to_json() for event in observatory.replay(session_id)]
    summary = observatory.summarize(session_id)
    verification = observatory.verify(session_id)

    assert first == second
    assert len(first) == 7
    assert summary.event_count == 7
    assert summary.model == "demo/fast"
    assert summary.input_tokens == 1240
    assert summary.output_tokens == 188
    assert summary.tool_calls == 1
    assert summary.estimated_cost_usd is not None
    assert verification.valid is True
    assert verification.finalized is True


def test_imported_native_event_uses_collector_session_and_sequence(tmp_path: Path) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")

    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="collector")
    events = list(observatory.replay(session_id))

    assert {event.session_id for event in events} == {"collector"}
    assert [event.ingest_sequence for event in events] == list(range(1, 8))
    assert events[0].attributes["source_session_id"] == "demo-source"


def test_local_cost_is_snapshotted_at_ingest_not_repriced_on_replay(tmp_path: Path) -> None:
    database = tmp_path / "sessions.sqlite3"
    first_catalog = tmp_path / "first.toml"
    second_catalog = tmp_path / "second.toml"
    template = """
[catalog]
version = "{version}"
effective_from = "2026-07-10"
currency = "USD"

[[models]]
pattern = "demo/*"
input_per_million = {rate}
output_per_million = {rate}
provenance = "synthetic test rate"
"""
    first_catalog.write_text(template.format(version="first", rate=1), encoding="utf-8")
    second_catalog.write_text(template.format(version="second", rate=100), encoding="utf-8")
    initial = Observatory.open(database, price_catalog=first_catalog)
    session_id = initial.import_path(DEMO_TRACE, adapter="native", session_id="priced")
    initial_cost = initial.summarize(session_id).estimated_cost_usd

    reopened = Observatory.open(database, price_catalog=second_catalog)
    replayed = reopened.summarize(session_id)

    assert replayed.estimated_cost_usd == initial_cost
    assert replayed.pricing_version == "first"


def test_duplicate_event_id_becomes_visible_diagnostic(tmp_path: Path) -> None:
    duplicate_trace = tmp_path / "duplicate.jsonl"
    lines = DEMO_TRACE.read_text(encoding="utf-8").splitlines()
    duplicate_trace.write_text(f"{lines[0]}\n{lines[0]}\n", encoding="utf-8")
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")

    session_id = observatory.import_path(duplicate_trace, adapter="native", session_id="duplicates")
    events = list(observatory.replay(session_id))

    assert len(events) == 2
    assert events[1].category == "diagnostic"
    assert events[1].kind == "trace.duplicate_event_id"


def test_mcp_secret_dsn_and_unknown_prose_do_not_reach_storage_or_exports(
    tmp_path: Path,
) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")
    session_id = observatory.new_session(adapter="mcp", session_id="privacy")
    record = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "db_password": "correct horse battery staple",
            "database_url": "postgresql://dbuser:supersecretpassword@localhost/prod",
            "vendor_prompt_blob": "CUSTOMER-CASE-DO-NOT-RETAIN-424242",
        },
    }
    observatory.ingest_record(record, session_id=session_id, adapter="mcp")
    observatory.store.finalize_session(session_id)

    stored = "".join(event.to_json() for event in observatory.replay(session_id))
    json_export = observatory.export(session_id, tmp_path / "evidence.json", format="json")
    markdown_export = observatory.export(session_id, tmp_path / "evidence.md", format="markdown")
    combined = stored + json_export.read_text() + markdown_export.read_text()

    assert "correct horse battery staple" not in combined
    assert "supersecretpassword" not in combined
    assert "CUSTOMER-CASE-DO-NOT-RETAIN-424242" not in combined
