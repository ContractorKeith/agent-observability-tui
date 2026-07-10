from __future__ import annotations

import json

from agent_observability_tui.redaction import (
    CONTENT_OMITTED,
    REDACTED,
    minimize_sensitive_content,
    sanitize_payload,
    sanitize_text,
)


def test_recursive_redaction_removes_secret_keys_and_token_values() -> None:
    original_token = "sk-proj-exampleSecretValue123456789"
    payload = {
        "Authorization": f"Bearer {original_token}",
        "nested": {
            "password": "correct horse battery staple",
            "message": f"request failed api_key={original_token}",
        },
        "safe": "model output",
    }

    result = sanitize_payload(payload)
    serialized = json.dumps(result.value)

    assert original_token not in serialized
    assert "correct horse battery staple" not in serialized
    assert result.value["Authorization"] == REDACTED
    assert result.value["nested"]["password"] == REDACTED
    assert result.redacted_count == 3
    assert result.value["safe"] == "model output"


def test_hostile_terminal_text_is_inert_and_display_is_bounded() -> None:
    hostile = "\x1b]0;owned\x07\x1b[31m[bold]danger[/bold]\x1b[0m\x00\n" + ("é" * 50)

    result = sanitize_text(hostile, max_bytes=32)

    assert "\x1b" not in result.value
    assert "\x00" not in result.value
    assert "\n" not in result.value
    assert not result.value.startswith("[bold]")
    assert "\\[bold]" in result.value
    assert result.truncated is True
    assert result.original_bytes == len(hostile.encode("utf-8"))
    assert len(result.value.encode("utf-8")) <= 32


def test_payload_cap_is_explicit_and_never_slices_invalid_utf8() -> None:
    payload = {"unknown_vendor_field": "🙂" * 500}

    result = sanitize_payload(payload, max_string_bytes=10_000, max_payload_bytes=180)

    assert result.truncated is True
    assert result.original_bytes > 180
    assert result.value["_truncated"] is True
    assert result.value["_original_bytes"] == result.original_bytes
    assert len(json.dumps(result.value, ensure_ascii=False).encode("utf-8")) <= 180


def test_prompt_and_tool_content_is_omitted_but_structure_remains() -> None:
    sanitized = sanitize_payload(
        {
            "method": "tools/call",
            "arguments": {"command": "cat private.txt"},
            "content": "ordinary private prompt text",
        }
    )

    minimized = minimize_sensitive_content(sanitized.value)

    assert minimized["method"] == "tools/call"
    assert minimized["arguments"] == CONTENT_OMITTED
    assert minimized["content"] == CONTENT_OMITTED


def test_secret_key_is_checked_again_after_terminal_controls_are_removed() -> None:
    result = sanitize_payload({"pass\x1b[31mword": "correct horse battery staple"})

    assert result.value["password"] == REDACTED
