"""SQLite persistence for sanitized canonical trace events."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .redaction import sanitize_text

if TYPE_CHECKING:
    from .model import TraceEvent

SCHEMA_VERSION = 2
ZERO_HASH = "0" * 64
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def calculate_event_hash(
    previous_hash: str,
    payload: dict[str, Any],
    redaction_policy: str,
) -> str:
    chain_record = json.dumps(
        {"event": payload, "redaction_policy": redaction_policy},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(f"{previous_hash}\n{chain_record}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_id: str
    created_at: str
    updated_at: str
    status: str
    adapter: str
    model: str | None
    event_count: int
    chain_head: str | None
    finalized: bool
    pricing_version: str | None


@dataclass(frozen=True, slots=True)
class ChainVerification:
    session_id: str
    valid: bool
    finalized: bool
    event_count: int
    chain_head: str | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    event: TraceEvent
    previous_hash: str
    event_hash: str
    redaction_policy: str


class SessionStore:
    """Store immutable event rows and mutable session projections."""

    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path).expanduser().resolve()
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            return
        parent_created = not self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent_created:
            with suppress(OSError):
                os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
        with suppress(OSError):
            os.chmod(self.path, 0o600)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            connection = sqlite3.connect(self.path, timeout=10)
            self._secure_database_files()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _secure_database_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            if candidate.exists():
                with suppress(OSError):
                    os.chmod(candidate, 0o600)

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("session store is read-only")

    def _initialize(self) -> None:
        for attempt in range(5):
            try:
                self._initialize_once()
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold() or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        self._secure_database_files()

    def _initialize_once(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    adapter TEXT NOT NULL,
                    model TEXT,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    chain_head TEXT,
                    finalized INTEGER NOT NULL DEFAULT 0,
                    pricing_version TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    ingest_sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    observed_timestamp TEXT NOT NULL,
                    source_timestamp TEXT,
                    category TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    request_id TEXT,
                    tool_call_id TEXT,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    redaction_policy TEXT NOT NULL,
                    PRIMARY KEY (session_id, ingest_sequence),
                    UNIQUE (session_id, event_id)
                );

                CREATE INDEX IF NOT EXISTS events_correlation_time
                    ON events(session_id, observed_timestamp);
                CREATE INDEX IF NOT EXISTS events_category
                    ON events(session_id, category, kind);

                CREATE TABLE IF NOT EXISTS resource_samples (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    sample_sequence INTEGER NOT NULL,
                    observed_timestamp TEXT NOT NULL,
                    pid INTEGER,
                    rss_bytes INTEGER,
                    cpu_percent REAL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    PRIMARY KEY (session_id, sample_sequence)
                );
                """
            )
            self._ensure_event_columns(connection)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS events_trace_id
                    ON events(session_id, trace_id);
                CREATE INDEX IF NOT EXISTS events_span_id
                    ON events(session_id, span_id);
                CREATE INDEX IF NOT EXISTS events_request_id
                    ON events(session_id, request_id);
                CREATE INDEX IF NOT EXISTS events_tool_call_id
                    ON events(session_id, tool_call_id);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _utc_now()),
            )

    @staticmethod
    def _ensure_event_columns(connection: sqlite3.Connection) -> None:
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        for name in ("trace_id", "span_id", "request_id", "tool_call_id"):
            if name not in existing:
                connection.execute(f"ALTER TABLE events ADD COLUMN {name} TEXT")

    def start_session(
        self,
        session_id: str,
        *,
        adapter: str,
        model: str | None = None,
        pricing_version: str | None = None,
    ) -> None:
        self._require_writable()
        session_id = validate_session_id(session_id)
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sessions(
                    session_id, created_at, updated_at, adapter, model, pricing_version
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, now, now, adapter, model, pricing_version),
            )
        self._secure_database_files()

    def append_event(self, event: TraceEvent, *, redaction_policy: str = "default-v1") -> bool:
        """Append one event. Return False when its ID already exists."""

        self._require_writable()
        validate_session_id(event.session_id)
        payload = event.to_dict()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        model = _event_model(payload)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT finalized, chain_head FROM sessions WHERE session_id = ?",
                (event.session_id,),
            ).fetchone()
            if row is None:
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO sessions(
                        session_id, created_at, updated_at, adapter, model
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (event.session_id, now, now, event.adapter, model),
                )
                previous_hash = ZERO_HASH
            else:
                if row["finalized"]:
                    raise ValueError(f"session {event.session_id!r} is finalized")
                previous_hash = row["chain_head"] or ZERO_HASH

            event_hash = calculate_event_hash(previous_hash, payload, redaction_policy)
            try:
                connection.execute(
                    """
                    INSERT INTO events(
                        session_id, ingest_sequence, event_id, observed_timestamp,
                        source_timestamp, category, kind, status,
                        trace_id, span_id, request_id, tool_call_id, payload_json,
                        previous_hash, event_hash, redaction_policy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.session_id,
                        event.ingest_sequence,
                        event.event_id,
                        payload["observed_timestamp"],
                        payload["source_timestamp"],
                        event.category,
                        event.kind,
                        event.status,
                        event.trace_id,
                        event.span_id,
                        event.request_id,
                        event.tool_call_id,
                        canonical,
                        previous_hash,
                        event_hash,
                        redaction_policy,
                    ),
                )
            except sqlite3.IntegrityError as error:
                duplicate = connection.execute(
                    "SELECT 1 FROM events WHERE session_id = ? AND event_id = ?",
                    (event.session_id, event.event_id),
                ).fetchone()
                if duplicate:
                    connection.rollback()
                    return False
                raise ValueError(
                    f"ingest sequence {event.ingest_sequence} already exists for "
                    f"session {event.session_id}"
                ) from error

            now = _utc_now()
            connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?, event_count = event_count + 1,
                    chain_head = ?, model = COALESCE(model, ?)
                WHERE session_id = ?
                """,
                (now, event_hash, model, event.session_id),
            )
        self._secure_database_files()
        return True

    def append_resource_sample(
        self,
        session_id: str,
        *,
        sequence: int,
        observed_timestamp: str,
        pid: int | None,
        rss_bytes: int | None,
        cpu_percent: float | None,
        status: str,
        detail: str | None = None,
    ) -> None:
        self._require_writable()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO resource_samples(
                    session_id, sample_sequence, observed_timestamp, pid,
                    rss_bytes, cpu_percent, status, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    sequence,
                    observed_timestamp,
                    pid,
                    rss_bytes,
                    cpu_percent,
                    status,
                    detail,
                ),
            )
        self._secure_database_files()

    def finalize_session(self, session_id: str, *, status: str = "complete") -> None:
        self._require_writable()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions SET finalized = 1, status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (status, _utc_now(), session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(session_id)
        self._secure_database_files()

    def get_session(self, session_id: str) -> SessionInfo:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _session_from_row(row)

    def list_sessions(self) -> tuple[SessionInfo, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, session_id"
            ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def iter_stored_events(self, session_id: str) -> Iterator[StoredEvent]:
        from .model import TraceEvent

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, previous_hash, event_hash, redaction_policy
                FROM events WHERE session_id = ? ORDER BY ingest_sequence
                """,
                (session_id,),
            ).fetchall()
        for row in rows:
            yield StoredEvent(
                event=TraceEvent.from_json(row["payload_json"]),
                previous_hash=row["previous_hash"],
                event_hash=row["event_hash"],
                redaction_policy=row["redaction_policy"],
            )

    def iter_events(self, session_id: str) -> Iterator[TraceEvent]:
        for stored in self.iter_stored_events(session_id):
            yield stored.event

    def resource_samples(self, session_id: str) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT sample_sequence, observed_timestamp, pid, rss_bytes,
                       cpu_percent, status, detail
                FROM resource_samples
                WHERE session_id = ? ORDER BY sample_sequence
                """,
                (session_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def verify_session(self, session_id: str) -> ChainVerification:
        info = self.get_session(session_id)
        previous_hash = ZERO_HASH
        expected_sequence = 1
        count = 0
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT ingest_sequence, payload_json, previous_hash, event_hash, redaction_policy
                FROM events WHERE session_id = ? ORDER BY ingest_sequence
                """,
                (session_id,),
            ).fetchall()
        for row in rows:
            sequence = row["ingest_sequence"]
            if sequence != expected_sequence:
                return ChainVerification(
                    session_id,
                    False,
                    info.finalized,
                    count,
                    info.chain_head,
                    f"sequence gap before {sequence}",
                )
            if row["previous_hash"] != previous_hash:
                return ChainVerification(
                    session_id,
                    False,
                    info.finalized,
                    count,
                    info.chain_head,
                    f"previous hash mismatch at sequence {sequence}",
                )
            try:
                payload = json.loads(row["payload_json"])
            except (json.JSONDecodeError, TypeError):
                return ChainVerification(
                    session_id,
                    False,
                    info.finalized,
                    count,
                    info.chain_head,
                    f"invalid payload JSON at sequence {sequence}",
                )
            if not isinstance(payload, dict) or payload.get("ingest_sequence") != sequence:
                return ChainVerification(
                    session_id,
                    False,
                    info.finalized,
                    count,
                    info.chain_head,
                    f"payload sequence mismatch at sequence {sequence}",
                )
            if payload.get("session_id") != session_id:
                return ChainVerification(
                    session_id,
                    False,
                    info.finalized,
                    count,
                    info.chain_head,
                    f"payload session mismatch at sequence {sequence}",
                )
            calculated = calculate_event_hash(previous_hash, payload, row["redaction_policy"])
            if calculated != row["event_hash"]:
                return ChainVerification(
                    session_id,
                    False,
                    info.finalized,
                    count,
                    info.chain_head,
                    f"event hash mismatch at sequence {sequence}",
                )
            previous_hash = calculated
            expected_sequence += 1
            count += 1
        if count != info.event_count or (info.chain_head or ZERO_HASH) != previous_hash:
            return ChainVerification(
                session_id,
                False,
                info.finalized,
                count,
                info.chain_head,
                "session projection does not match immutable events",
            )
        return ChainVerification(session_id, True, info.finalized, count, info.chain_head, None)


def _event_model(payload: dict[str, Any]) -> str | None:
    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        return None
    model = attributes.get("model") or attributes.get("model_name")
    return str(model) if model else None


def validate_session_id(value: str) -> str:
    sanitized = sanitize_text(value, max_bytes=128).value
    if sanitized != value or SESSION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "session ID must be 1-128 safe characters using letters, numbers, '.', '_', or '-'"
        )
    return value


def _session_from_row(row: sqlite3.Row) -> SessionInfo:
    return SessionInfo(
        session_id=row["session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        adapter=row["adapter"],
        model=row["model"],
        event_count=row["event_count"],
        chain_head=row["chain_head"],
        finalized=bool(row["finalized"]),
        pricing_version=row["pricing_version"],
    )
