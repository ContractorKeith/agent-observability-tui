# Agent Observability TUI — Stack Decisions

**Date:** 2026-07-10

## Architecture

**Pattern:** local event pipeline with inbound source adapters and outbound views.

```text
source reader / child supervisor
  -> adapter parser
  -> redactor + hostile-text sanitizer
  -> canonical event + integrity chain
  -> single SQLite writer
  -> read-only projections / replay / comparison
  -> CLI, Textual UI, and exporters
```

`Observatory` is the application service. Raw input never flows directly to Textual,
SQLite, metrics, or export code.

## Runtime and language

- Python 3.11+
- `src/` package layout
- Hatchling build backend
- `uv` for reproducible development and CI

## Dependencies

| Package | Purpose |
|---|---|
| Textual | TUI, reactive widgets, and headless Pilot tests |
| psutil | Portable child-process RSS and CPU sampling |
| platformdirs | Owner-local, OS-appropriate session storage |
| pytest / pytest-asyncio | Unit, integration, and async UI tests |
| ruff | Formatting and linting |
| build | Wheel/sdist release gate |

The event model uses standard-library dataclasses and manual validation. SQLite and
TOML reading use the standard library. No application server or ORM is required.

## Storage

SQLite in the platform data directory, with WAL mode, one writer, schema migrations,
and immutable sanitized event rows. Session metadata and projections may be updated.
Database and export files use owner-only permissions where supported.

## Supported source contracts

- Stable: `agenttrace.event/1` native JSONL
- Stable: unstructured child stdout/stderr as sanitized log events
- Experimental: captured MCP JSON-RPC 2.0
- Experimental: Hermes `sessions export` JSONL
- Experimental: OpenHands SDK event dumps

Adapters record their name/version and preserve unknown field names/shape while omitting arbitrary
unknown scalar content by default.

## Metrics and cost

Process RSS/CPU are observed through psutil. Tokens are accepted only when source-reported.
Cost is derived from a versioned local catalog; unknown models remain `unpriced`. All derived
metrics carry provenance. Comparison never executes an agent.

## Deployment

GitHub source repository and installable Python wheel. Initial user install is
`pipx install .` or `uv tool install .`; PyPI publishing is intentionally deferred.

## Explicitly not using

- A web framework or cloud database
- An OTel Collector dependency
- Pydantic or an ORM for the MVP
- `shell=True` for child execution
- Raw Rich/Textual markup from trace content
- GPU/unified-memory attribution claims
