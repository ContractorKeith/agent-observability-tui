"""Atomic, sanitized evidence exports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .analysis import summarize_session
from .storage import ZERO_HASH, calculate_event_hash

if TYPE_CHECKING:
    from .storage import SessionStore

ExportFormat = Literal["json", "markdown"]
MAX_EXPORT_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExportVerification:
    path: str
    session_id: str | None
    valid: bool
    finalized: bool
    event_count: int
    chain_head: str | None
    error: str | None = None


def export_session(
    store: SessionStore,
    session_id: str,
    destination: str | Path,
    *,
    format: ExportFormat = "json",
) -> Path:
    """Export one stored session without executing any trace content."""

    info = store.get_session(session_id)
    verification = store.verify_session(session_id)
    stored_events = tuple(store.iter_stored_events(session_id))
    summary = summarize_session(
        session_id,
        (item.event for item in stored_events),
        status=info.status,
        adapter=info.adapter,
        resources=store.resource_samples(session_id),
    )
    exported_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if format == "json":
        document = {
            "schema": "agenttrace.export/1",
            "exported_at": exported_at,
            "content_capture": "metadata-only-with-sanitized-observed-logs",
            "filters": [],
            "integrity_limit": (
                "SHA-256 chain detects corruption of sanitized records; it is not "
                "proof of fidelity to raw source bytes or protection from a local attacker."
            ),
            "integrity_scope": {
                "chained": ["canonical sanitized event", "redaction policy"],
                "not_chained": [
                    "session projection",
                    "derived summary",
                    "resource samples",
                    "export formatting metadata",
                ],
            },
            "session": asdict(info),
            "verification": asdict(verification),
            "summary": summary.to_dict(),
            "events": [
                {
                    "event": item.event.to_dict(),
                    "integrity": {
                        "redaction_policy": item.redaction_policy,
                        "previous_hash": item.previous_hash,
                        "event_hash": item.event_hash,
                    },
                }
                for item in stored_events
            ],
        }
        content = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
    elif format == "markdown":
        content = _markdown_export(
            info=info,
            verification=verification,
            summary=summary,
            stored_events=stored_events,
            exported_at=exported_at,
        )
    else:
        raise ValueError(f"unsupported export format: {format}")
    return _atomic_write(Path(destination), content)


def _markdown_export(*, info, verification, summary, stored_events, exported_at: str) -> str:
    chain_state = (
        "open-valid-so-far"
        if verification.valid and not verification.finalized
        else "valid"
        if verification.valid
        else "invalid"
    )
    lines = [
        f"# Agent trace evidence: {_inline(info.session_id)}",
        "",
        "Schema: `agenttrace.export/1`",
        "",
        f"Exported: `{_inline(exported_at)}`",
        "",
        "> This export contains sanitized records. Its SHA-256 chain detects accidental",
        "> corruption; it is not notarization or proof of fidelity to the original raw stream.",
        "> The chain covers canonical sanitized events plus redaction policy. Session projections,",
        "> derived summaries, resource samples, and export formatting metadata are outside it.",
        "",
        "Content capture: `metadata-only-with-sanitized-observed-logs`",
        "",
        "## Session summary",
        "",
        f"- Status: `{_inline(info.status)}`",
        f"- Adapter: `{_inline(info.adapter)}`",
        f"- Events: `{summary.event_count}`",
        "- Filters: `none`",
        f"- Tools / errors: `{summary.tool_calls}` / `{summary.errors}`",
        f"- Input / output tokens: `{_known(summary.input_tokens)}` / "
        f"`{_known(summary.output_tokens)}`",
        f"- Token provenance: `{_known(summary.token_provenance)}`",
        f"- Estimated cost (USD): `{_known(summary.estimated_cost_usd)}`",
        f"- Cost provenance: `{_known(summary.cost_provenance)}`",
        f"- Price catalog / effective date: `{_known(summary.pricing_version)}` / "
        f"`{_known(summary.pricing_effective_from)}`",
        f"- Duration / basis: `{_known(summary.duration_ms)}` ms / "
        f"`{_known(summary.duration_basis)}`",
        f"- Peak process RSS: `{_known(summary.peak_rss_bytes)}` bytes",
        f"- Peak process CPU: `{_known(summary.peak_cpu_percent)}` percent",
        f"- Resource state: `{summary.resource_status}` from "
        f"`{summary.resource_sample_count}` samples "
        f"(`{summary.unavailable_resource_samples}` unavailable)",
        f"- Chain: `{chain_state}`; finalized=`{verification.finalized}`",
        "",
        "## Timeline",
        "",
        "| Seq | Time | Category | Kind | Status |",
        "|---:|---|---|---|---|",
    ]
    for stored in stored_events:
        event = stored.event
        timestamp = event.to_dict()["source_timestamp"] or event.to_dict()["observed_timestamp"]
        lines.append(
            f"| {event.ingest_sequence} | {_cell(timestamp)} | {_cell(event.category)} | "
            f"{_cell(event.kind)} | {_cell(event.status or 'unknown')} |"
        )
    lines.extend(["", "## Sanitized event records", ""])
    for stored in stored_events:
        lines.append(f"### Event {stored.event.ingest_sequence}")
        lines.append("")
        record = {
            "event": stored.event.to_dict(),
            "integrity": {
                "redaction_policy": stored.redaction_policy,
                "previous_hash": stored.previous_hash,
                "event_hash": stored.event_hash,
            },
        }
        for line in json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).splitlines():
            lines.append(f"    {line}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def verify_export(path: str | Path) -> ExportVerification:
    """Verify a JSON evidence export without opening its source database."""

    source = Path(path).expanduser().resolve()
    try:
        if not source.is_file():
            raise OSError("export path is not a regular file")
        if source.stat().st_size > MAX_EXPORT_BYTES:
            raise OSError("export exceeds the verification size limit")
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        return ExportVerification(str(source), None, False, False, 0, None, type(error).__name__)
    if not isinstance(document, dict) or document.get("schema") != "agenttrace.export/1":
        return ExportVerification(
            str(source), None, False, False, 0, None, "unsupported export schema"
        )
    session = document.get("session")
    verification = document.get("verification")
    events = document.get("events")
    if (
        not isinstance(session, dict)
        or not isinstance(verification, dict)
        or not isinstance(events, list)
    ):
        return ExportVerification(str(source), None, False, False, 0, None, "invalid export shape")
    session_id = session.get("session_id")
    if not isinstance(session_id, str):
        return ExportVerification(str(source), None, False, False, 0, None, "missing session ID")
    if not isinstance(verification.get("finalized"), bool):
        return ExportVerification(
            str(source), session_id, False, False, 0, None, "invalid finalized state"
        )
    previous_hash = ZERO_HASH
    for expected_sequence, item in enumerate(events, start=1):
        if not isinstance(item, dict):
            return _export_failure(
                source,
                session_id,
                verification,
                expected_sequence - 1,
                previous_hash,
                "invalid event entry",
            )
        event = item.get("event")
        integrity = item.get("integrity")
        if not isinstance(event, dict) or not isinstance(integrity, dict):
            return _export_failure(
                source,
                session_id,
                verification,
                expected_sequence - 1,
                previous_hash,
                "invalid event integrity shape",
            )
        if (
            event.get("ingest_sequence") != expected_sequence
            or event.get("session_id") != session_id
        ):
            return _export_failure(
                source,
                session_id,
                verification,
                expected_sequence - 1,
                previous_hash,
                f"event identity mismatch at sequence {expected_sequence}",
            )
        policy = integrity.get("redaction_policy")
        if not isinstance(policy, str) or integrity.get("previous_hash") != previous_hash:
            return _export_failure(
                source,
                session_id,
                verification,
                expected_sequence - 1,
                previous_hash,
                f"previous hash mismatch at sequence {expected_sequence}",
            )
        try:
            calculated = calculate_event_hash(previous_hash, event, policy)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return _export_failure(
                source,
                session_id,
                verification,
                expected_sequence - 1,
                previous_hash,
                f"invalid canonical event at sequence {expected_sequence}",
            )
        if integrity.get("event_hash") != calculated:
            return _export_failure(
                source,
                session_id,
                verification,
                expected_sequence - 1,
                previous_hash,
                f"event hash mismatch at sequence {expected_sequence}",
            )
        previous_hash = calculated
    count = len(events)
    if verification.get("event_count") != count or verification.get("chain_head") != (
        previous_hash if count else None
    ):
        return _export_failure(
            source,
            session_id,
            verification,
            count,
            previous_hash,
            "export projection does not match events",
        )
    finalized = verification["finalized"]
    return ExportVerification(
        str(source), session_id, True, finalized, count, previous_hash if count else None, None
    )


def _export_failure(
    source: Path,
    session_id: str,
    verification: dict,
    count: int,
    chain_head: str,
    error: str,
) -> ExportVerification:
    return ExportVerification(
        str(source),
        session_id,
        False,
        verification.get("finalized") is True,
        count,
        chain_head if count else None,
        error,
    )


def _known(value: object | None) -> str:
    return "unknown" if value is None else str(value)


def _inline(value: object) -> str:
    return str(value).replace("`", "\\`").replace("\n", " ")


def _cell(value: object) -> str:
    return _inline(value).replace("|", "\\|")


def _atomic_write(path: Path, content: str) -> Path:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
