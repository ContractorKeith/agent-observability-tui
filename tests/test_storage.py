from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_observability_tui.model import TokenUsage, TraceEvent
from agent_observability_tui.storage import SessionStore


def make_event(
    sequence: int,
    *,
    session_id: str = "session-1",
    event_id: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        event_id=event_id or f"event-{sequence}",
        session_id=session_id,
        ingest_sequence=sequence,
        source_timestamp=datetime(2026, 7, 10, 12, tzinfo=UTC) + timedelta(seconds=sequence),
        observed_timestamp=datetime(2026, 7, 10, 12, tzinfo=UTC)
        + timedelta(seconds=sequence, milliseconds=10),
        adapter="native",
        adapter_version="1",
        category="model" if sequence == 1 else "tool",
        kind="response" if sequence == 1 else "call",
        phase="instant",
        status="ok",
        token_usage=TokenUsage(input_tokens=10, output_tokens=2) if sequence == 1 else None,
        attributes={"model": "demo/fast", "sequence": sequence},
    )


def test_store_replays_deterministically_and_verifies_finalized_chain(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.start_session("session-1", adapter="native")

    assert store.append_event(make_event(1)) is True
    assert store.append_event(make_event(2)) is True
    assert store.append_event(make_event(2, event_id="event-2")) is False
    store.finalize_session("session-1")

    first = [event.to_json() for event in store.iter_events("session-1")]
    second = [event.to_json() for event in store.iter_events("session-1")]
    verification = store.verify_session("session-1")

    assert first == second
    assert verification.valid is True
    assert verification.finalized is True
    assert verification.event_count == 2
    assert store.get_session("session-1").event_count == 2


def test_verification_detects_event_and_policy_mutation(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path)
    store.append_event(make_event(1), redaction_policy="agenttrace.redaction/1")

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE events SET redaction_policy = 'weakened' WHERE session_id = 'session-1'"
        )

    verification = store.verify_session("session-1")

    assert verification.valid is False
    assert "hash mismatch" in (verification.error or "")


def test_open_session_is_distinct_from_finalized_evidence(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.append_event(make_event(1))

    verification = store.verify_session("session-1")

    assert verification.valid is True
    assert verification.finalized is False


def test_verification_detects_deleted_tail(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path)
    store.append_event(make_event(1))
    store.append_event(make_event(2))

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM events WHERE ingest_sequence = 2")

    verification = store.verify_session("session-1")

    assert verification.valid is False
    assert verification.error == "session projection does not match immutable events"


def test_database_sidecars_are_owner_only_and_existing_parent_mode_is_preserved(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o755)
    path = parent / "sessions.sqlite3"
    store = SessionStore(path)
    store.append_event(make_event(1))

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_read_only_store_does_not_create_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "sessions.sqlite3"

    try:
        SessionStore(missing, read_only=True)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("read-only open should require an existing database")

    assert not missing.exists()
    assert not missing.parent.exists()


def test_verification_detects_row_sequence_edit(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = SessionStore(path)
    store.append_event(make_event(1))
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE events SET ingest_sequence = 99")

    verification = store.verify_session("session-1")

    assert verification.valid is False
    assert "sequence gap" in (verification.error or "")
