# Agent Observability TUI — Build Context

## Mission

Build a local-first observability TUI that makes agent and MCP traces understandable,
replayable, comparable, and safe to share. The public hook is: **Debug your local agents
like a pro.**

## Non-negotiable invariants

- Redact and sanitize before persistence, rendering, logging, hashing, or export.
- Replay and comparison are read-only and never invoke a tool or subprocess.
- Child commands use argv directly, never a shell.
- Protocol and framework uncertainty becomes a visible diagnostic, never silent data loss.
- Missing usage/cost/resource data remains unknown.
- Process RSS is not described as Apple GPU or per-process unified memory.
- The SHA-256 record chain is corruption detection for sanitized data, not notarization.

## Main interfaces

- Inbound: native JSONL, captured formats, tailed files, and supervised child output
- Application: `Observatory`
- Durable state: SQLite event store
- Outbound: Textual views, CLI summaries, replay, comparison, and evidence exporters

## Conventions

- Python 3.11+, typed standard-library dataclasses, small explicit functions
- Domain modules do not import Textual, psutil, or CLI parsing
- Tests use sanitized golden fixtures and temporary platform data directories
- Keep user-provided content inert: no markup interpretation or unbounded rendering
- Public docs must state supported modes, adapter maturity, and privacy limitations precisely

## Verification

Run:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
uv run python -m build
```

Then install the built wheel in a clean temporary environment and smoke-test
`agent-observe --help`, `agent-observe demo --headless`, import, replay, and export.
