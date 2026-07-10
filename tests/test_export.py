from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from agent_observability_tui.export import verify_export
from agent_observability_tui.observatory import Observatory

DEMO_TRACE = Path(__file__).parents[1] / "examples" / "demo-trace.jsonl"


def test_json_export_contains_sanitized_events_and_integrity_context(tmp_path: Path) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")
    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="demo")
    destination = tmp_path / "evidence" / "demo.json"

    result = observatory.export(session_id, destination, format="json")
    document = json.loads(result.read_text(encoding="utf-8"))

    assert result == destination.resolve()
    assert document["schema"] == "agenttrace.export/1"
    assert document["verification"]["valid"] is True
    assert document["events"][0]["integrity"]["redaction_policy"] == "agenttrace.redaction/1"
    assert len(document["events"]) == 7
    assert result.stat().st_mode & 0o077 == 0
    assert verify_export(result).valid is True


def test_markdown_export_uses_inert_indented_json(tmp_path: Path) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")
    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="demo")

    destination = observatory.export(session_id, tmp_path / "demo.md", format="markdown")
    content = destination.read_text(encoding="utf-8")

    assert "# Agent trace evidence: demo" in content
    assert "Schema: `agenttrace.export/1`" in content
    assert "not notarization" in content
    assert '    "schema": "agenttrace.event/1"' in content


def test_standalone_export_verification_detects_edit_delete_insert_and_reorder(
    tmp_path: Path,
) -> None:
    observatory = Observatory.open(tmp_path / "sessions.sqlite3")
    session_id = observatory.import_path(DEMO_TRACE, adapter="native", session_id="demo")
    original_path = observatory.export(session_id, tmp_path / "original.json", format="json")
    original = json.loads(original_path.read_text())

    mutations = []
    edited = deepcopy(original)
    edited["events"][0]["event"]["kind"] = "edited"
    mutations.append(edited)
    deleted = deepcopy(original)
    deleted["events"].pop(2)
    mutations.append(deleted)
    inserted = deepcopy(original)
    inserted["events"].insert(1, deepcopy(inserted["events"][0]))
    mutations.append(inserted)
    reordered = deepcopy(original)
    reordered["events"][0], reordered["events"][1] = (
        reordered["events"][1],
        reordered["events"][0],
    )
    mutations.append(reordered)

    for index, document in enumerate(mutations):
        path = tmp_path / f"mutation-{index}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        assert verify_export(path).valid is False
