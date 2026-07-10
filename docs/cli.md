# CLI workflows

The installed command help is authoritative:

```bash
uv run agent-observe --help
uv run agent-observe COMMAND --help
```

Examples below run from a source checkout. After `uv tool install .` or `pipx install .`, omit
`uv run`.

## Demo

Open the built-in synthetic demo without an external service or API key:

```bash
uv run agent-observe demo
```

Use a headless summary for automation, or select a database where the option is exposed:

```bash
uv run agent-observe demo --headless
uv run agent-observe demo --db ./demo.sqlite3
```

Grammar:

```text
agent-observe demo [--headless] [--db PATH]
```

## Import a completed trace

```bash
uv run agent-observe import ./trace.jsonl --adapter auto --json
uv run agent-observe import ./trace.jsonl --adapter native --session my-session
```

Use `--session` to select the session identifier for this import. `--json` produces structured
command output suitable for capturing the resulting identifier. Explicit adapters are safer for
repeatable automation than `auto`.

Grammar:

```text
agent-observe import PATH [--adapter auto|native|mcp|hermes|openhands] [--session ID] [--json]
```

## Watch an appended trace

```bash
uv run agent-observe watch ./live-trace.jsonl --adapter native
uv run agent-observe watch ./captured-mcp.jsonl --adapter mcp --db ./sessions.sqlite3
```

Watch waits for complete lines. Partial writes, truncation, rotation, malformed input, or source
loss are represented as diagnostics rather than silently ignored.

Grammar:

```text
agent-observe watch PATH [--adapter auto|native|mcp|hermes|openhands] [--db PATH]
```

## Run and observe a child command

```bash
uv run agent-observe run --cwd ./my-agent -- python agent.py --task synthetic-demo
uv run agent-observe run --adapter native --db ./sessions.sqlite3 -- python emit_trace.py
uv run agent-observe run --headless --clean-env --env API_MODE=offline -- python agent.py
```

Everything after `--` is passed directly as an argument vector with no shell. The command is not
sandboxed. Shell operators such as `|`, `&&`, redirects, globbing, and variable expansion are not
interpreted by Agent Observability TUI.

By default the child inherits the current environment. Use `--clean-env`, repeatable
`--drop-env NAME`, and repeatable `--env NAME=VALUE` to narrow or explicitly set it. `--headless`
runs to completion and prints the stored summary without opening Textual.

Grammar:

```text
agent-observe run [--adapter auto|native|mcp|hermes|openhands] [--cwd DIR] [--db PATH]
                  [--session ID] [--clean-env] [--drop-env NAME] [--env NAME=VALUE]
                  [--headless] -- COMMAND [ARGS...]
```

## List sessions

```bash
uv run agent-observe sessions
uv run agent-observe sessions --db ./sessions.sqlite3 --json
```

Grammar:

```text
agent-observe sessions [--db PATH] [--json]
```

## Replay a stored session

```bash
uv run agent-observe replay SESSION_ID
uv run agent-observe replay SESSION_ID --db ./sessions.sqlite3 --headless
```

Replay follows stored ingest sequence. It does not execute tools, launch a child, call a model, or
contact a provider.

Grammar:

```text
agent-observe replay SESSION [--db PATH] [--headless]
```

## Compare captured sessions

```bash
uv run agent-observe compare SESSION_A SESSION_B
uv run agent-observe compare SESSION_A SESSION_B SESSION_C --db ./sessions.sqlite3 --json
```

Comparison is read-only. It summarizes captured duration, status, tool and error counts,
source-reported tokens, estimated cost, peak sampled RSS, and missing-data state; it never reruns
an agent or assigns a quality score. A mixed-model session produces one `mixed` aggregate plus
separate rows for events directly attributed to each model; unscoped tool/resource data remains
only in the aggregate rather than being guessed onto a model.

Grammar:

```text
agent-observe compare SESSION... [--db PATH] [--json]
```

## Export evidence

```bash
uv run agent-observe export SESSION_ID ./evidence.json --format json
uv run agent-observe export SESSION_ID ./evidence.md --format markdown
uv run agent-observe export SESSION_ID ./evidence.json --db ./sessions.sqlite3 --format json
```

Exports are atomic and contain sanitized stored data plus schema, filter, safety-policy,
provenance, and chain-status context. Inspect every export before sharing it.

Grammar:

```text
agent-observe export SESSION DEST [--db PATH] [--format json|markdown]
```

## Verify stored evidence

```bash
uv run agent-observe verify SESSION_ID
uv run agent-observe verify SESSION_ID --db ./sessions.sqlite3 --json
```

Verification can detect accidental edit, insertion, deletion, or reordering in the stored
sanitized record chain. It does not prove origin or defeat a local attacker who can rewrite the
database and recompute the chain.

Grammar:

```text
agent-observe verify SESSION [--db PATH] [--json]
```

Verify a standalone JSON export without its source database:

```bash
uv run agent-observe verify-export ./evidence.json --json
```

Markdown exports carry the `agenttrace.export/1` marker and human-readable chain context, but
standalone machine verification uses the JSON export.

## Database selection

Without an explicit `--db` on commands that expose it, the application uses its owner-local
platform data location. Database and export files request owner-only permissions where supported.
An explicit database is useful for disposable demos and isolated investigations:

```bash
uv run agent-observe demo --headless --db ./scratch.sqlite3
uv run agent-observe sessions --db ./scratch.sqlite3 --json
```

Do not commit databases or evidence exports. They can contain sensitive information even after
automated redaction.

## Price catalog selection

Commands also accept `--prices PATH`, or the `AGENT_OBSERVABILITY_PRICES` environment variable,
to select a reviewed versioned TOML catalog. The bundled `prices.toml` contains only illustrative
demo/local entries. A newly captured session records the selected catalog version; an unmatched
model remains `unpriced`.

```bash
uv run agent-observe import trace.jsonl --prices ./provider-prices-2026-07.toml --json
```

Catalog entries are checked in order with shell-style model patterns:

```toml
[catalog]
version = "reviewed-2026-07"
effective_from = "2026-07-01"
currency = "USD"

[[models]]
pattern = "provider/model-name"
input_per_million = 1.0
output_per_million = 4.0
cache_read_per_million = 0.1
cache_write_per_million = 1.25
reasoning_per_million = 4.0
provenance = "Reviewed provider pricing page, 2026-07-01"
```
